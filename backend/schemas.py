"""Pydantic schemas for validated scenario import.

Imported scenarios are fully validated against these models BEFORE any
destructive cleanup runs, so a malformed or oversized file can never wipe a
working topology.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, conint, model_validator

from backend import settings


class NetemSpec(BaseModel):
    loss_percent: float = 0
    delay_ms: int = 0
    jitter_ms: int = 0


class NodeSpec(BaseModel):
    id: conint(ge=1, le=settings.MAX_NODES)
    name: Optional[str] = None
    ip_addresses: Optional[dict] = None


class LinkSpec(BaseModel):
    node_a: conint(ge=1, le=settings.MAX_NODES)
    node_b: conint(ge=1, le=settings.MAX_NODES)
    convergence_layer: Optional[str] = None
    subnet: Optional[str] = None
    ip_a: Optional[str] = None
    ip_b: Optional[str] = None
    netem: Optional[NetemSpec] = None


class TopologySpec(BaseModel):
    nodes: list[NodeSpec] = Field(default_factory=list)
    links: list[LinkSpec] = Field(default_factory=list)


class ScenarioSpec(BaseModel):
    version: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = ""
    defaults: Optional[dict] = None
    topology: TopologySpec
    exits: Optional[dict] = None
    positions: Optional[dict] = None

    @model_validator(mode="after")
    def _check(self) -> "ScenarioSpec":
        nodes = self.topology.nodes
        links = self.topology.links

        if not nodes:
            raise ValueError("Scenario has no nodes defined")
        if len(nodes) > settings.MAX_NODES:
            raise ValueError(
                f"Scenario has {len(nodes)} nodes (cap {settings.MAX_NODES})")
        if len(links) > settings.MAX_LINKS:
            raise ValueError(
                f"Scenario has {len(links)} links (cap {settings.MAX_LINKS})")

        # Unique node ids.
        ids = [n.id for n in nodes]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate node ids in scenario")
        id_set = set(ids)

        # Referential integrity: every link endpoint is a declared node.
        for link in links:
            if link.node_a not in id_set or link.node_b not in id_set:
                raise ValueError(
                    f"Link {link.node_a}-{link.node_b} references an "
                    f"undeclared node")
            if link.node_a == link.node_b:
                raise ValueError(f"Self-link on node {link.node_a} not allowed")

        # Version: reject unknown major; accept unknown minor with a warning.
        if self.version:
            major = self.version.split(".")[0]
            supported_majors = {v.split(".")[0]
                                for v in settings.SUPPORTED_SCENARIO_VERSIONS}
            if major not in supported_majors:
                raise ValueError(
                    f"Unsupported scenario version '{self.version}' "
                    f"(supported majors: {sorted(supported_majors)})")
        return self
