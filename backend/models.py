"""Data models for DTN-Manager."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class NodeStatus(str, Enum):
    CREATING = "creating"
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"


class LinkStatus(str, Enum):
    UP = "up"
    DOWN = "down"
    DEGRADED = "degraded"
    ERROR = "error"  # half-built / readiness-gated link (reconcile can repair)


@dataclass
class NodeStats:
    bundles_received: int = 0
    bundles_sent: int = 0
    bundles_forwarded: int = 0


@dataclass
class Node:
    id: int  # IPN node number (1, 2, 3, ...)
    name: str  # Display name (N1, N2, ...)
    container_id: Optional[str] = None
    container_name: str = ""
    status: NodeStatus = NodeStatus.CREATING
    ip_addresses: dict = field(default_factory=dict)  # link_id -> IP
    stats: NodeStats = field(default_factory=NodeStats)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "container_id": self.container_id,
            "container_name": self.container_name,
            "status": self.status.value,
            "ip_addresses": self.ip_addresses,
            "stats": {
                "bundles_received": self.stats.bundles_received,
                "bundles_sent": self.stats.bundles_sent,
                "bundles_forwarded": self.stats.bundles_forwarded,
            },
        }


@dataclass
class NetemConfig:
    loss_percent: float = 0
    delay_ms: int = 0
    jitter_ms: int = 0

    def to_tc_args(self) -> str:
        """Build tc netem argument string."""
        parts = []
        if self.loss_percent > 0:
            parts.append(f"loss {self.loss_percent}%")
        if self.delay_ms > 0:
            if self.jitter_ms > 0:
                parts.append(f"delay {self.delay_ms}ms {self.jitter_ms}ms")
            else:
                parts.append(f"delay {self.delay_ms}ms")
        return " ".join(parts)


@dataclass
class Link:
    id: str  # "1-2"
    node_a: int  # IPN number
    node_b: int  # IPN number
    network_name: str = ""
    network_id: Optional[str] = None
    subnet: str = ""
    ip_a: str = ""
    ip_b: str = ""
    convergence_layer: str = "tcpcl"  # "tcpcl", "udpcl", "ltpcl", "stcpcl", etc.
    status: LinkStatus = LinkStatus.UP
    netem: Optional[NetemConfig] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "node_a": self.node_a,
            "node_b": self.node_b,
            "network_name": self.network_name,
            "subnet": self.subnet,
            "ip_a": self.ip_a,
            "ip_b": self.ip_b,
            "convergence_layer": self.convergence_layer,
            "status": self.status.value,
            "netem": {
                "loss_percent": self.netem.loss_percent,
                "delay_ms": self.netem.delay_ms,
                "jitter_ms": self.netem.jitter_ms,
            } if self.netem else None,
        }
