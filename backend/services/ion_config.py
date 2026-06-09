"""ION-DTN configuration generator.

Generates node.rc and node.ionconfig files dynamically based on
the current topology (which links exist for a given node).
"""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.models import Node, Link


def generate_ionconfig(heap_words: int = 250000, sdr_wm_size: int = 5000000) -> str:
    """Generate node.ionconfig content."""
    return (
        f"sdrWmSize       {sdr_wm_size}\n"
        f"sdrWmKey        -1\n"
        f"configFlags     1\n"
        f"heapWords       {heap_words}\n"
        f"heapKey         -1\n"
        f"logSize         0\n"
        f"logKey          -1\n"
        f"pathName        '/tmp'\n"
    )


def generate_node_rc(node_id: int, neighbors: list[dict],
                     exits: list[dict] | None = None) -> str:
    """Generate combined node.rc file.
    
    Args:
        node_id: IPN node number (e.g., 1, 2, 3)
        neighbors: List of dicts with keys:
            - peer_id: IPN number of the neighbor
            - peer_ip: IP address of the neighbor on the shared link
        exits: Optional list of dicts with keys:
            - dest_first: First destination IPN number
            - dest_last: Last destination IPN number
            - gateway_id: Gateway node IPN number
    """
    lines = []

    # ionadmin
    lines.append(f"## begin ionadmin")
    lines.append(f"1 {node_id} 'node.ionconfig'")
    lines.append(f"s")
    lines.append(f"## end ionadmin")
    lines.append(f"")

    # ionsecadmin
    lines.append(f"## begin ionsecadmin")
    lines.append(f"1")
    lines.append(f"## end ionsecadmin")
    lines.append(f"")

    # bpadmin
    lines.append(f"## begin bpadmin")
    lines.append(f"1")
    lines.append(f"a scheme ipn 'ipnfw' 'ipnadminep'")
    lines.append(f"a endpoint ipn:{node_id}.0 q")
    lines.append(f"a endpoint ipn:{node_id}.1 q")
    lines.append(f"a endpoint ipn:{node_id}.2 q")
    lines.append(f"a protocol tcp 1400 100")
    lines.append(f"a induct tcp 0.0.0.0:4556 tcpcli")
    for neighbor in neighbors:
        lines.append(f"a outduct tcp {neighbor['peer_ip']}:4556 tcpclo")
    lines.append(f"s")
    lines.append(f"## end bpadmin")
    lines.append(f"")

    # ipnadmin — egress plans + exit routes
    lines.append(f"## begin ipnadmin")
    for neighbor in neighbors:
        lines.append(
            f"a plan {neighbor['peer_id']} tcp/{neighbor['peer_ip']}:4556"
        )
    for ex in (exits or []):
        lines.append(
            f"a exit {ex['dest_first']} {ex['dest_last']} ipn:{ex['gateway_id']}.0"
        )
    lines.append(f"## end ipnadmin")

    return "\n".join(lines) + "\n"


def generate_add_outduct_cmd(peer_ip: str) -> str:
    """Generate bpadmin command to add an outduct at runtime."""
    return f"a outduct tcp {peer_ip}:4556 tcpclo"


def generate_add_plan_cmd(peer_id: int, peer_ip: str) -> str:
    """Generate ipnadmin command to add an egress plan at runtime."""
    return f"a plan {peer_id} tcp/{peer_ip}:4556"


def generate_add_exit_cmd(dest_first: int, dest_last: int, gateway_id: int) -> str:
    """Generate ipnadmin command to add a static exit at runtime."""
    return f"a exit {dest_first} {dest_last} ipn:{gateway_id}.0"


def generate_delete_outduct_cmd(peer_ip: str) -> str:
    """Generate bpadmin command to remove an outduct at runtime."""
    return f"d outduct tcp {peer_ip}:4556"


def generate_delete_plan_cmd(peer_id: int) -> str:
    """Generate ipnadmin command to remove an egress plan at runtime."""
    return f"d plan {peer_id}"
