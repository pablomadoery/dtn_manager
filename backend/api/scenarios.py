"""Scenario export/import API endpoints."""

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import Response
from pydantic import BaseModel, ValidationError as PydanticValidationError

from backend import settings
from backend.api.deps import get_manager
from backend.schemas import ScenarioSpec
from backend.services.docker_manager import DockerManager
from backend.services.scenario_export import build_export_archive

router = APIRouter(prefix="/api/scenarios", tags=["scenarios"])

SCENARIOS_DIR = Path(__file__).parent.parent.parent / "scenarios"

# Filenames must be a bare basename of this shape — no separators, no traversal.
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+\.json$")


def _safe_scenario_path(filename: str) -> Path:
    """Resolve a user-supplied scenario filename to a path strictly inside
    SCENARIOS_DIR, or raise 400. Single confinement gate for every disk-touching
    scenario endpoint (export write, import-from-disk, delete).
    """
    name = Path(filename).name  # strip any directory component
    if not _SAFE_NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail="Invalid scenario filename (allowed: letters, digits, "
                   "'_', '-', '.', ending in .json)")
    base = SCENARIOS_DIR.resolve()
    candidate = (base / name).resolve()
    # is_relative_to is the load-bearing containment check.
    if not candidate.is_relative_to(base):
        raise HTTPException(status_code=400, detail="Invalid scenario path")
    return candidate


def _sanitize_name(raw: str) -> str:
    """Sanitize an export name into a safe filesystem stem."""
    safe = re.sub(r"[^A-Za-z0-9_\-]", "_", raw or "").strip("_")
    return safe or datetime.now().strftime("scenario_%Y%m%d_%H%M%S")


class ScenarioMetadata(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = ""
    positions: Optional[dict] = None  # {"n1": {"x": 100, "y": 200}, ...}


@router.post("/export")
def export_scenario(meta: ScenarioMetadata = ScenarioMetadata(),
                    manager: DockerManager = Depends(get_manager)):
    """Export the current topology as a scenario JSON file."""
    if not manager.nodes:
        raise HTTPException(status_code=400, detail="No topology to export")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = _sanitize_name(meta.name or f"scenario_{timestamp}")

    nodes_data = []
    exits_data = {}
    for node in manager.list_nodes():
        nodes_data.append({
            "id": node.id,
            "name": node.name,
            "ip_addresses": dict(node.ip_addresses),
        })
        try:
            node_exits = manager.list_exits(node.id)
            if node_exits:
                exits_data[str(node.id)] = [e["raw"] for e in node_exits]
        except Exception:
            pass

    links_data = []
    for link in manager.list_links():
        link_entry = {
            "node_a": link.node_a,
            "node_b": link.node_b,
            "convergence_layer": link.convergence_layer,
            "subnet": link.subnet,
            "ip_a": link.ip_a,
            "ip_b": link.ip_b,
        }
        if link.netem and (link.netem.loss_percent > 0 or link.netem.delay_ms > 0):
            link_entry["netem"] = {
                "loss_percent": link.netem.loss_percent,
                "delay_ms": link.netem.delay_ms,
                "jitter_ms": link.netem.jitter_ms,
            }
        links_data.append(link_entry)

    scenario = {
        "version": "1.2",
        "name": name,
        "description": meta.description or "",
        "exported_at": datetime.now().isoformat(),
        "defaults": {"convergence_layer": "tcpcl"},
        "topology": {"nodes": nodes_data, "links": links_data},
        "exits": exits_data,
    }
    if meta.positions:
        scenario["positions"] = meta.positions

    SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
    filepath = _safe_scenario_path(f"{name}.json")
    filepath.write_text(json.dumps(scenario, indent=2))

    return {
        "status": "exported",
        "filename": filepath.name,
        "path": str(filepath),
        "scenario": scenario,
    }


def _parse_scenario(raw: dict) -> ScenarioSpec:
    """Validate a raw scenario dict, mapping failures to 400 (no destruction)."""
    try:
        return ScenarioSpec.model_validate(raw)
    except PydanticValidationError as e:
        raise HTTPException(status_code=422, detail=f"Invalid scenario: {e}")


def _do_import(manager: DockerManager, spec: ScenarioSpec) -> dict:
    """Shared, transactional import. Validation already happened, so the
    cleanup-then-recreate is safe; on a create failure we roll back everything
    created during this import and report a non-2xx so partial success cannot
    masquerade as success.
    """
    manager.cleanup_all()
    errors: list[str] = []

    defaults = spec.defaults or {}
    default_cl = defaults.get("convergence_layer", "tcpcl")

    created_nodes: list[int] = []
    created_links: list[str] = []
    try:
        for node in sorted(spec.topology.nodes, key=lambda n: n.id):
            created = manager.create_node(int(node.id))
            created_nodes.append(created.id)

        for link in spec.topology.links:
            cl = link.convergence_layer or default_cl
            created = manager.create_link(
                int(link.node_a), int(link.node_b), cl)
            created_links.append(created.id)
            if link.netem:
                manager.disrupt_link(
                    created.id,
                    loss=link.netem.loss_percent,
                    delay=link.netem.delay_ms,
                    jitter=link.netem.jitter_ms,
                )
    except Exception as e:
        # Roll back everything created during this import.
        for link_id in created_links:
            try:
                manager.delete_link(link_id)
            except Exception:
                pass
        for node_id in created_nodes:
            try:
                manager.delete_node(node_id)
            except Exception:
                pass
        raise HTTPException(status_code=502, detail=f"Import failed, rolled back: {e}")

    # Best-effort post-create reconciliation (non-fatal).
    errors.extend(manager.repair_ion_config())

    exits_applied = 0
    for node_id_str, exit_lines in (spec.exits or {}).items():
        try:
            node_id = int(node_id_str)
        except (ValueError, TypeError):
            continue
        for raw_line in exit_lines:
            try:
                manager.apply_raw_exit(node_id, raw_line)
                exits_applied += 1
            except Exception as e:
                errors.append(f"Failed to apply exit on node {node_id}: {e}")

    result = {
        "status": "imported",
        "scenario_name": spec.name or "unknown",
        "nodes_created": len(created_nodes),
        "links_created": len(created_links),
        "exits_applied": exits_applied,
    }
    if errors:
        result["errors"] = errors
    return result


@router.post("/import")
async def import_scenario(file: UploadFile = File(...),
                          manager: DockerManager = Depends(get_manager)):
    """Import a scenario from an uploaded JSON file."""
    # Bound the read so an oversized upload cannot exhaust memory (DoS).
    content = await file.read(settings.MAX_IMPORT_BYTES + 1)
    if len(content) > settings.MAX_IMPORT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Scenario exceeds {settings.MAX_IMPORT_BYTES} bytes")
    try:
        raw = json.loads(content)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON file")

    spec = _parse_scenario(raw)  # validates BEFORE any destructive action
    return await asyncio.to_thread(_do_import, manager, spec)


@router.post("/import/file")
async def import_scenario_from_disk(filename: str,
                                    manager: DockerManager = Depends(get_manager)):
    """Import a scenario from a file already on disk in scenarios/."""
    filepath = _safe_scenario_path(filename)
    if not filepath.exists():
        raise HTTPException(status_code=404,
                            detail=f"Scenario file not found: {filepath.name}")
    if filepath.stat().st_size > settings.MAX_IMPORT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Scenario exceeds {settings.MAX_IMPORT_BYTES} bytes")
    try:
        raw = json.loads(filepath.read_text())
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON in scenario file")

    spec = _parse_scenario(raw)
    return await asyncio.to_thread(_do_import, manager, spec)


@router.get("")
def list_scenarios():
    """List all saved scenario files."""
    SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
    scenarios = []
    for f in sorted(SCENARIOS_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            scenarios.append({
                "filename": f.name,
                "name": data.get("name", f.stem),
                "description": data.get("description", ""),
                "exported_at": data.get("exported_at", ""),
                "node_count": len(data.get("topology", {}).get("nodes", [])),
                "link_count": len(data.get("topology", {}).get("links", [])),
            })
        except Exception:
            scenarios.append({"filename": f.name, "name": f.stem,
                              "error": "parse error"})
    return scenarios


@router.delete("/{filename}")
def delete_scenario(filename: str):
    """Delete a saved scenario file."""
    filepath = _safe_scenario_path(filename)
    if not filepath.exists():
        raise HTTPException(status_code=404,
                            detail=f"Scenario not found: {filepath.name}")
    filepath.unlink()
    return {"status": "deleted", "filename": filepath.name}


class ExportBundleRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = ""


@router.post("/export-bundle")
def export_bundle(req: ExportBundleRequest = ExportBundleRequest(),
                  manager: DockerManager = Depends(get_manager)):
    """Export the current topology as a standalone .tar.gz scenario bundle."""
    if not manager.nodes:
        raise HTTPException(status_code=400, detail="No topology to export")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    scenario_name = _sanitize_name(req.name or f"scenario_{timestamp}")

    archive_bytes = build_export_archive(
        manager, scenario_name, description=req.description or "")

    return Response(
        content=archive_bytes,
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{scenario_name}.tar.gz"',
        },
    )
