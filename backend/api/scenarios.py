"""Scenario export/import API endpoints."""

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from typing import Optional

from backend.services.scenario_export import build_export_archive

router = APIRouter(prefix="/api/scenarios", tags=["scenarios"])

# Will be set by main.py
manager = None

SCENARIOS_DIR = Path(__file__).parent.parent.parent / "scenarios"


class ScenarioMetadata(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = ""
    positions: Optional[dict] = None  # {"n1": {"x": 100, "y": 200}, ...}


@router.post("/export")
def export_scenario(meta: ScenarioMetadata = ScenarioMetadata()):
    """Export the current topology as a scenario JSON file.

    Returns the scenario data and also saves it to disk.
    """
    if not manager.nodes:
        raise HTTPException(status_code=400, detail="No topology to export")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = meta.name or f"scenario_{timestamp}"

    # Build scenario data — capture full topology state
    nodes_data = []
    exits_data = {}  # node_id -> list of exit route dicts
    for node in manager.list_nodes():
        node_entry = {
            "id": node.id,
            "name": node.name,
            "ip_addresses": dict(node.ip_addresses),
        }
        nodes_data.append(node_entry)

        # Query live exit routes from the container
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
        # Preserve disruption state if active
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
        "defaults": {
            "convergence_layer": "tcpcl",
        },
        "topology": {
            "nodes": nodes_data,
            "links": links_data,
        },
        "exits": exits_data,
    }

    # Include node positions if provided by the frontend
    if meta.positions:
        scenario["positions"] = meta.positions

    # Save to disk
    SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{name}.json"
    filepath = SCENARIOS_DIR / filename
    filepath.write_text(json.dumps(scenario, indent=2))

    return {
        "status": "exported",
        "filename": filename,
        "path": str(filepath),
        "scenario": scenario,
    }


@router.post("/import")
async def import_scenario(file: UploadFile = File(...)):
    """Import a scenario from an uploaded JSON file.

    Cleans up the current topology first, then recreates nodes and links.
    """
    try:
        content = await file.read()
        scenario = json.loads(content)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON file")

    if "topology" not in scenario:
        raise HTTPException(
            status_code=400,
            detail="Invalid scenario format: missing 'topology' key"
        )

    topology = scenario["topology"]
    nodes_spec = topology.get("nodes", [])
    links_spec = topology.get("links", [])

    if not nodes_spec:
        raise HTTPException(
            status_code=400,
            detail="Scenario has no nodes defined"
        )

    def _do_import(scenario, nodes_spec, links_spec):
        """Blocking import logic — runs in a thread."""
        manager.cleanup_all()
        errors = []

        # Top-level CL default (for v1.1 files that lack per-link CL)
        defaults = scenario.get("defaults", {})
        default_cl = defaults.get("convergence_layer", "tcpcl")

        # Recreate nodes
        created_nodes = []
        for node_def in sorted(nodes_spec, key=lambda n: n["id"]):
            try:
                node = manager.create_node(node_def["id"])
                created_nodes.append(node.id)
            except Exception as e:
                errors.append(f"Failed to create node {node_def['id']}: {e}")

        # Recreate links
        created_links = []
        for link_def in links_spec:
            cl = link_def.get("convergence_layer", default_cl)
            try:
                link = manager.create_link(link_def["node_a"], link_def["node_b"])
                link.convergence_layer = cl
                created_links.append(link.id)

                if "netem" in link_def:
                    netem = link_def["netem"]
                    manager.disrupt_link(
                        link.id,
                        loss=netem.get("loss_percent", 0),
                        delay=netem.get("delay_ms", 0),
                        jitter=netem.get("jitter_ms", 0),
                    )
            except Exception as e:
                errors.append(
                    f"Failed to create link {link_def['node_a']}-{link_def['node_b']}: {e}"
                )

        # Repair ION config: re-apply outducts + plans
        repair_errors = manager.repair_ion_config()
        errors.extend(repair_errors)

        # Re-apply exit routes if captured in the scenario
        exits_spec = scenario.get("exits", {})
        exits_applied = 0
        for node_id_str, exit_lines in exits_spec.items():
            try:
                node_id = int(node_id_str)
                for raw_line in exit_lines:
                    try:
                        manager.apply_raw_exit(node_id, raw_line)
                        exits_applied += 1
                    except Exception as e:
                        errors.append(f"Failed to apply exit on node {node_id}: {e}")
            except (ValueError, TypeError):
                pass

        result = {
            "status": "imported",
            "scenario_name": scenario.get("name", "unknown"),
            "nodes_created": len(created_nodes),
            "links_created": len(created_links),
            "exits_applied": exits_applied,
        }
        if errors:
            result["errors"] = errors
        return result

    return await asyncio.to_thread(
        _do_import, scenario, nodes_spec, links_spec
    )


@router.post("/import/file")
async def import_scenario_from_disk(filename: str):
    """Import a scenario from a file already on disk in the scenarios/ directory."""
    filepath = SCENARIOS_DIR / filename
    if not filepath.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Scenario file not found: {filename}"
        )

    try:
        scenario = json.loads(filepath.read_text())
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON in scenario file")

    if "topology" not in scenario:
        raise HTTPException(
            status_code=400,
            detail="Invalid scenario format: missing 'topology' key"
        )

    topology = scenario["topology"]
    nodes_spec = topology.get("nodes", [])
    links_spec = topology.get("links", [])

    def _do_import_file():
        """Blocking import logic — runs in a thread."""
        manager.cleanup_all()
        errors = []

        # Top-level CL default (for v1.1 files that lack per-link CL)
        defaults = scenario.get("defaults", {})
        default_cl = defaults.get("convergence_layer", "tcpcl")

        created_nodes = []
        for node_def in sorted(nodes_spec, key=lambda n: n["id"]):
            try:
                node = manager.create_node(node_def["id"])
                created_nodes.append(node.id)
            except Exception as e:
                errors.append(f"Failed to create node {node_def['id']}: {e}")

        created_links = []
        for link_def in links_spec:
            cl = link_def.get("convergence_layer", default_cl)
            try:
                link = manager.create_link(link_def["node_a"], link_def["node_b"])
                link.convergence_layer = cl
                created_links.append(link.id)

                if "netem" in link_def:
                    netem = link_def["netem"]
                    manager.disrupt_link(
                        link.id,
                        loss=netem.get("loss_percent", 0),
                        delay=netem.get("delay_ms", 0),
                        jitter=netem.get("jitter_ms", 0),
                    )
            except Exception as e:
                errors.append(
                    f"Failed to create link {link_def['node_a']}-{link_def['node_b']}: {e}"
                )

        repair_errors = manager.repair_ion_config()
        errors.extend(repair_errors)

        exits_spec = scenario.get("exits", {})
        exits_applied = 0
        for node_id_str, exit_lines in exits_spec.items():
            try:
                node_id = int(node_id_str)
                for raw_line in exit_lines:
                    try:
                        manager.apply_raw_exit(node_id, raw_line)
                        exits_applied += 1
                    except Exception as e:
                        errors.append(f"Failed to apply exit on node {node_id}: {e}")
            except (ValueError, TypeError):
                pass

        result = {
            "status": "imported",
            "scenario_name": scenario.get("name", "unknown"),
            "nodes_created": len(created_nodes),
            "links_created": len(created_links),
            "exits_applied": exits_applied,
        }
        if errors:
            result["errors"] = errors
        return result

    return await asyncio.to_thread(_do_import_file)


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
            scenarios.append({"filename": f.name, "name": f.stem, "error": "parse error"})
    return scenarios


@router.delete("/{filename}")
def delete_scenario(filename: str):
    """Delete a saved scenario file."""
    filepath = SCENARIOS_DIR / filename
    if not filepath.exists():
        raise HTTPException(status_code=404, detail=f"Scenario not found: {filename}")
    filepath.unlink()
    return {"status": "deleted", "filename": filename}


class ExportBundleRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = ""


@router.post("/export-bundle")
def export_bundle(req: ExportBundleRequest = ExportBundleRequest()):
    """Export the current topology as a standalone .tar.gz scenario bundle.

    The archive contains everything needed to run the scenario manually:
    Dockerfile, compose.yml, ION configs, start/stop/send/recv scripts,
    and a README.
    """
    if not manager.nodes:
        raise HTTPException(status_code=400, detail="No topology to export")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # Sanitize name for filesystem use
    raw_name = req.name or f"scenario_{timestamp}"
    scenario_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', raw_name).strip('_')
    if not scenario_name:
        scenario_name = f"scenario_{timestamp}"

    archive_bytes = build_export_archive(
        manager,
        scenario_name,
        description=req.description or "",
    )

    return Response(
        content=archive_bytes,
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{scenario_name}.tar.gz"',
        },
    )
