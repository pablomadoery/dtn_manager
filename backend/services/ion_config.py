"""ION-DTN configuration generator.

Generates node.rc and node.ionconfig files dynamically based on
the current topology (which links exist for a given node).

Convergence layers are described once in CL_REGISTRY and threaded through every
generator and runtime-reconfiguration command, so a link's convergence_layer is
honored end to end instead of silently defaulting to TCP.
"""

from __future__ import annotations
from typing import TYPE_CHECKING

from backend.errors import ValidationError

if TYPE_CHECKING:
    from backend.models import Node, Link


# ── Convergence-layer registry ────────────────────────────────────
#
# Each entry: protocol token (used in `a protocol`, `a induct`, `a outduct`,
# and `a plan <peer> <token>/<ip>:<port>`), the default port, and the induct
# (cli) / outduct (clo) daemon names.
class _CL:
    def __init__(self, token: str, port: int, cli: str, clo: str,
                 supported: bool = True):
        self.token = token
        self.port = port
        self.cli = cli
        self.clo = clo
        self.supported = supported


CL_REGISTRY: dict[str, _CL] = {
    "tcp": _CL("tcp", 4556, "tcpcli", "tcpclo"),
    "udp": _CL("udp", 4556, "udpcli", "udpclo"),
    "stcp": _CL("stcp", 4556, "stcpcli", "stcpclo"),
    # LTP is not a simple host:port outduct (it needs ltpadmin spans). Declared
    # so it round-trips, but generation raises until proper handling is added.
    "ltp": _CL("ltp", 1113, "ltpcli", "ltpclo", supported=False),
}

# Aliases: the model stores values like "tcpcl"; normalize them to the registry.
_CL_ALIASES = {
    "tcpcl": "tcp", "tcp": "tcp",
    "udpcl": "udp", "udp": "udp",
    "stcpcl": "stcp", "stcp": "stcp",
    "ltpcl": "ltp", "ltp": "ltp",
}


def resolve_cl(convergence_layer: str | None) -> _CL:
    """Map a convergence_layer string to its CL_REGISTRY entry.

    Raises ValidationError for unknown or unsupported CLs so a bad value fails
    loudly instead of being silently treated as TCP.
    """
    key = _CL_ALIASES.get((convergence_layer or "tcp").lower())
    if key is None:
        raise ValidationError(
            f"Unknown convergence layer '{convergence_layer}'. "
            f"Supported: {sorted(set(_CL_ALIASES))}"
        )
    cl = CL_REGISTRY[key]
    if not cl.supported:
        raise ValidationError(
            f"Convergence layer '{convergence_layer}' is recognized but not yet "
            f"supported by the config generator (needs ltpadmin spans)."
        )
    return cl


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
            - convergence_layer (optional): CL for that link (default tcp)
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

    # Determine the distinct CLs present across this node's neighbors so we
    # declare the right protocols/inducts. Always include tcp as a baseline.
    cls = {"tcp": CL_REGISTRY["tcp"]}
    for nb in neighbors:
        cl = resolve_cl(nb.get("convergence_layer"))
        cls[cl.token] = cl

    # bpadmin
    lines.append(f"## begin bpadmin")
    lines.append(f"1")
    lines.append(f"a scheme ipn 'ipnfw' 'ipnadminep'")
    lines.append(f"a endpoint ipn:{node_id}.0 q")
    lines.append(f"a endpoint ipn:{node_id}.1 q")
    lines.append(f"a endpoint ipn:{node_id}.2 q")
    for cl in cls.values():
        lines.append(f"a protocol {cl.token} 1400 100")
        lines.append(f"a induct {cl.token} 0.0.0.0:{cl.port} {cl.cli}")
    for nb in neighbors:
        cl = resolve_cl(nb.get("convergence_layer"))
        lines.append(f"a outduct {cl.token} {nb['peer_ip']}:{cl.port} {cl.clo}")
    lines.append(f"s")
    lines.append(f"## end bpadmin")
    lines.append(f"")

    # ipnadmin — egress plans + exit routes
    lines.append(f"## begin ipnadmin")
    for nb in neighbors:
        cl = resolve_cl(nb.get("convergence_layer"))
        lines.append(
            f"a plan {nb['peer_id']} {cl.token}/{nb['peer_ip']}:{cl.port}"
        )
    for ex in (exits or []):
        lines.append(
            f"a exit {ex['dest_first']} {ex['dest_last']} ipn:{ex['gateway_id']}.0"
        )
    lines.append(f"## end ipnadmin")

    return "\n".join(lines) + "\n"


def generate_add_outduct_cmd(peer_ip: str, convergence_layer: str = "tcp") -> str:
    """Generate bpadmin command to add an outduct at runtime."""
    cl = resolve_cl(convergence_layer)
    return f"a outduct {cl.token} {peer_ip}:{cl.port} {cl.clo}"


def generate_add_plan_cmd(peer_id: int, peer_ip: str,
                          convergence_layer: str = "tcp") -> str:
    """Generate ipnadmin command to add an egress plan at runtime."""
    cl = resolve_cl(convergence_layer)
    return f"a plan {peer_id} {cl.token}/{peer_ip}:{cl.port}"


def generate_add_exit_cmd(dest_first: int, dest_last: int, gateway_id: int) -> str:
    """Generate ipnadmin command to add a static exit at runtime."""
    return f"a exit {dest_first} {dest_last} ipn:{gateway_id}.0"


def generate_delete_outduct_cmd(peer_ip: str, convergence_layer: str = "tcp") -> str:
    """Generate bpadmin command to remove an outduct at runtime."""
    cl = resolve_cl(convergence_layer)
    return f"d outduct {cl.token} {peer_ip}:{cl.port}"


def generate_delete_plan_cmd(peer_id: int) -> str:
    """Generate ipnadmin command to remove an egress plan at runtime."""
    return f"d plan {peer_id}"
