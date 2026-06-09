"""Docker manager for ION-DTN node lifecycle and network management."""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path

import docker
from docker.types import IPAMConfig, IPAMPool

from backend.models import Node, Link, NodeStatus, LinkStatus, NetemConfig
from backend.services.subnet_pool import SubnetPool
from backend.services.ion_config import (
    generate_ionconfig,
    generate_node_rc,
    generate_add_outduct_cmd,
    generate_add_plan_cmd,
    generate_add_exit_cmd,
    generate_delete_outduct_cmd,
    generate_delete_plan_cmd,
)

# Path to entrypoint script (relative to project root)
ENTRYPOINT_PATH = Path(__file__).parent.parent.parent / "docker" / "entrypoint.sh"
ION_IMAGE = "ion-dtn-base"
CONTAINER_PREFIX = "ionmgr_n"
NETWORK_PREFIX = "ionmgr_link_"
LABEL_KEY = "ionmgr.managed"


class DockerManager:
    """Manages ION-DTN Docker containers and networks."""

    def __init__(self):
        self.client = docker.from_env()
        self.subnet_pool = SubnetPool(base_second_octet=50)
        self.nodes: dict[int, Node] = {}
        self.links: dict[str, Link] = {}
        self._config_dirs: dict[int, str] = {}  # node_id -> temp config dir
        self._link_id_counter = 0

    # ── Node Operations ──────────────────────────────────────────

    def create_node(self, node_id: int | None = None) -> Node:
        """Create a new ION-DTN node backed by a Docker container."""
        if node_id is None:
            node_id = self._next_node_id()

        if node_id in self.nodes:
            raise ValueError(f"Node {node_id} already exists")

        name = f"N{node_id}"
        container_name = f"{CONTAINER_PREFIX}{node_id}"

        # Remove any orphaned container with the same name from a prior session
        try:
            stale = self.client.containers.get(container_name)
            stale.remove(force=True)
        except docker.errors.NotFound:
            pass

        # Generate ION configuration files in a temp directory
        config_dir = tempfile.mkdtemp(prefix=f"ion_n{node_id}_")
        self._config_dirs[node_id] = config_dir

        ionconfig_content = generate_ionconfig()
        node_rc_content = generate_node_rc(node_id, neighbors=[])

        Path(config_dir, "node.ionconfig").write_text(ionconfig_content)
        Path(config_dir, "node.rc").write_text(node_rc_content)

        # Create container
        container = self.client.containers.run(
            image=ION_IMAGE,
            name=container_name,
            hostname=f"n{node_id}",
            detach=True,
            init=True,
            privileged=True,
            shm_size="256m",
            entrypoint=["bash", "/ion-entrypoint.sh"],
            environment={"NODE_NUM": str(node_id)},
            volumes={
                config_dir: {"bind": "/ion-config", "mode": "ro"},
                str(ENTRYPOINT_PATH.resolve()): {
                    "bind": "/ion-entrypoint.sh",
                    "mode": "ro",
                },
            },
            labels={LABEL_KEY: "true"},
        )

        # Start bpsink via docker exec (not entrypoint) so file redirects work.
        # The entrypoint's pty overrides bash redirects, but docker exec has no pty.
        import time as _time
        _time.sleep(2)  # Allow ION to initialize endpoints
        subprocess.run(
            ["docker", "exec", container_name, "bash", "-c",
             f"bpsink 'ipn:{node_id}.1' >> /ion-runtime/bpsink.log 2>&1 &"],
            capture_output=True, timeout=5
        )

        node = Node(
            id=node_id,
            name=name,
            container_id=container.id,
            container_name=container_name,
            status=NodeStatus.RUNNING,
        )
        self.nodes[node_id] = node
        return node

    def delete_node(self, node_id: int) -> None:
        """Delete a node and its container. Also removes all connected links."""
        if node_id not in self.nodes:
            raise ValueError(f"Node {node_id} does not exist")

        # Remove all links connected to this node first
        connected_links = [
            lid for lid, link in self.links.items()
            if link.node_a == node_id or link.node_b == node_id
        ]
        for link_id in connected_links:
            self.delete_link(link_id)

        # Stop and remove container
        node = self.nodes[node_id]
        try:
            container = self.client.containers.get(node.container_name)
            container.remove(force=True)
        except docker.errors.NotFound:
            pass

        # Cleanup config dir
        config_dir = self._config_dirs.pop(node_id, None)
        if config_dir and os.path.exists(config_dir):
            import shutil
            shutil.rmtree(config_dir, ignore_errors=True)

        del self.nodes[node_id]

    def get_node(self, node_id: int) -> Node:
        if node_id not in self.nodes:
            raise ValueError(f"Node {node_id} does not exist")
        return self.nodes[node_id]

    def list_nodes(self) -> list[Node]:
        return list(self.nodes.values())

    # ── Link Operations ──────────────────────────────────────────

    def create_link(self, node_a_id: int, node_b_id: int) -> Link:
        """Create a link between two nodes (Docker network + ION config)."""
        # Ensure canonical ordering
        if node_a_id > node_b_id:
            node_a_id, node_b_id = node_b_id, node_a_id

        link_id = f"{node_a_id}-{node_b_id}"
        if link_id in self.links:
            raise ValueError(f"Link {link_id} already exists")

        if node_a_id not in self.nodes or node_b_id not in self.nodes:
            raise ValueError("Both nodes must exist before creating a link")

        node_a = self.nodes[node_a_id]
        node_b = self.nodes[node_b_id]

        # Allocate subnet
        subnet_idx, subnet, ip_a, ip_b = self.subnet_pool.allocate()
        network_name = f"{NETWORK_PREFIX}{node_a_id}_{node_b_id}"

        # Create Docker bridge network
        network = self.client.networks.create(
            name=network_name,
            driver="bridge",
            ipam=IPAMConfig(pool_configs=[IPAMPool(subnet=subnet)]),
            labels={LABEL_KEY: "true"},
        )

        # Connect both containers to the network
        container_a = self.client.containers.get(node_a.container_name)
        container_b = self.client.containers.get(node_b.container_name)
        network.connect(container_a, ipv4_address=ip_a)
        network.connect(container_b, ipv4_address=ip_b)

        # Wait for ION readiness on both nodes before configuring
        self._wait_for_ion_ready(node_a.container_name)
        self._wait_for_ion_ready(node_b.container_name)

        # Hot-reconfigure ION on both nodes: add outduct + egress plan
        self._ion_exec(node_a.container_name, "bpadmin",
                       generate_add_outduct_cmd(ip_b))
        self._ion_exec(node_a.container_name, "ipnadmin",
                       generate_add_plan_cmd(node_b_id, ip_b))

        self._ion_exec(node_b.container_name, "bpadmin",
                       generate_add_outduct_cmd(ip_a))
        self._ion_exec(node_b.container_name, "ipnadmin",
                       generate_add_plan_cmd(node_a_id, ip_a))

        # Store IP mappings on nodes
        node_a.ip_addresses[link_id] = ip_a
        node_b.ip_addresses[link_id] = ip_b

        link = Link(
            id=link_id,
            node_a=node_a_id,
            node_b=node_b_id,
            network_name=network_name,
            network_id=network.id,
            subnet=subnet,
            ip_a=ip_a,
            ip_b=ip_b,
            status=LinkStatus.UP,
        )
        self.links[link_id] = link

        return link

    def delete_link(self, link_id: str) -> None:
        """Remove a link: disconnect network, remove ION config."""
        if link_id not in self.links:
            raise ValueError(f"Link {link_id} does not exist")

        link = self.links[link_id]

        # Remove ION config on both nodes (ignore errors if node is gone)
        for node_id, peer_id, peer_ip in [
            (link.node_a, link.node_b, link.ip_b),
            (link.node_b, link.node_a, link.ip_a),
        ]:
            if node_id in self.nodes:
                node = self.nodes[node_id]
                try:
                    self._ion_exec(node.container_name, "ipnadmin",
                                   generate_delete_plan_cmd(peer_id))
                    self._ion_exec(node.container_name, "bpadmin",
                                   generate_delete_outduct_cmd(peer_ip))
                except Exception:
                    pass
                node.ip_addresses.pop(link_id, None)

        # Remove Docker network
        try:
            network = self.client.networks.get(link.network_name)
            # Disconnect containers first
            for container_name in [
                self.nodes.get(link.node_a, Node(0, "")).container_name,
                self.nodes.get(link.node_b, Node(0, "")).container_name,
            ]:
                if container_name:
                    try:
                        network.disconnect(container_name, force=True)
                    except Exception:
                        pass
            network.remove()
        except docker.errors.NotFound:
            pass

        # Release subnet
        parts = link.subnet.split(".")
        if len(parts) >= 3:
            try:
                subnet_idx = int(parts[2])
                self.subnet_pool.release(subnet_idx)
            except ValueError:
                pass

        del self.links[link_id]

    def list_links(self) -> list[Link]:
        return list(self.links.values())

    # ── Link Disruption ──────────────────────────────────────────

    def disrupt_link(self, link_id: str,
                     loss: float = 100, delay: int = 0, jitter: int = 0) -> Link:
        """Apply tc netem to a link."""
        if link_id not in self.links:
            raise ValueError(f"Link {link_id} does not exist")

        link = self.links[link_id]
        netem = NetemConfig(loss_percent=loss, delay_ms=delay, jitter_ms=jitter)
        tc_args = netem.to_tc_args()

        # Apply on both sides
        for node_id, ip in [(link.node_a, link.ip_a), (link.node_b, link.ip_b)]:
            if node_id in self.nodes:
                iface = self._get_interface(
                    self.nodes[node_id].container_name, ip
                )
                if iface:
                    self._docker_exec(
                        self.nodes[node_id].container_name,
                        f"tc qdisc replace dev {iface} root netem {tc_args}"
                    )

        link.netem = netem
        link.status = LinkStatus.DOWN if loss >= 100 else LinkStatus.DEGRADED
        return link

    def restore_link(self, link_id: str) -> Link:
        """Remove tc netem from a link."""
        if link_id not in self.links:
            raise ValueError(f"Link {link_id} does not exist")

        link = self.links[link_id]

        for node_id, ip in [(link.node_a, link.ip_a), (link.node_b, link.ip_b)]:
            if node_id in self.nodes:
                iface = self._get_interface(
                    self.nodes[node_id].container_name, ip
                )
                if iface:
                    self._docker_exec(
                        self.nodes[node_id].container_name,
                        f"tc qdisc del dev {iface} root",
                        ignore_errors=True,
                    )

        link.netem = None
        link.status = LinkStatus.UP
        return link

    # ── Traffic ──────────────────────────────────────────────────

    def send_bundle(self, from_node: int, to_node: int, message: str) -> bool:
        """Send a single bundle using bpsource."""
        if from_node not in self.nodes:
            raise ValueError(f"Node {from_node} does not exist")
        node = self.nodes[from_node]
        result = self._docker_exec(
            node.container_name,
            f'bpsource ipn:{to_node}.1 "{message}"'
        )
        return result.returncode == 0

    # ── Stats ────────────────────────────────────────────────────

    def get_node_logs(self, node_id: int, tail: int = 50) -> str:
        """Get ion.log content from a node."""
        if node_id not in self.nodes:
            raise ValueError(f"Node {node_id} does not exist")
        result = self._docker_exec(
            self.nodes[node_id].container_name,
            f"tail -{tail} /ion-runtime/ion.log"
        )
        return result.stdout if result.returncode == 0 else ""

    def get_bpsink_output(self, node_id: int, tail: int = 20) -> str:
        """Get recent bpsink output (from bpsink.log file)."""
        if node_id not in self.nodes:
            raise ValueError(f"Node {node_id} does not exist")
        result = subprocess.run(
            ["docker", "exec", self.nodes[node_id].container_name,
             "tail", "-n", str(tail), "/ion-runtime/bpsink.log"],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout + result.stderr

    def get_bundle_count(self, node_id: int) -> int:
        """Parse bpsink output to count received bundles."""
        output = self.get_bpsink_output(node_id, tail=500)
        import re
        matches = re.findall(r"Payload delivered", output)
        return len(matches)

    def get_bundle_stats(self, node_id: int) -> dict:
        """Get comprehensive bundle statistics from a node using bpstats.

        Runs `bpstats` which writes [x] stats lines to ion.log, then parses them.
        Returns dict with keys: src, fwd, xmt, rcv, dlv, rfw, exp (each a count).
        Also returns the raw bpsink delivery count.
        """
        import re

        if node_id not in self.nodes:
            raise ValueError(f"Node {node_id} does not exist")

        node = self.nodes[node_id]
        stats = {"src": 0, "fwd": 0, "xmt": 0, "rcv": 0, "dlv": 0, "rfw": 0, "exp": 0}

        # Trigger bpstats to write fresh stats to ion.log
        try:
            self._docker_exec(node.container_name, "bpstats")
        except Exception:
            pass

        # Parse the latest [x] lines from ion.log
        try:
            result = self._docker_exec(
                node.container_name,
                "tail -20 /ion-runtime/ion.log"
            )
            if result.returncode == 0:
                # [x] classname from TIMESTAMP to TIMESTAMP: ... (+) count bytes
                # classnames: src, fwd, xmt, rcv, dlv, rfw, exp
                for line in result.stdout.splitlines():
                    m = re.search(
                        r'\[x\]\s+(src|fwd|xmt|rcv|dlv|rfw|exp)\s+from\s+.+?\(\+\)\s+(\d+)\s+',
                        line,
                    )
                    if m:
                        key = m.group(1)
                        count = int(m.group(2))
                        stats[key] = count
        except Exception:
            pass

        # Also get bpsink delivery count (actual delivered payloads)
        try:
            stats["bpsink_delivered"] = self.get_bundle_count(node_id)
        except Exception:
            stats["bpsink_delivered"] = 0

        return stats

    def get_all_bundle_stats(self) -> dict:
        """Get bundle stats for all nodes at once (efficient batch call)."""
        result = {}
        for node_id in self.nodes:
            try:
                result[node_id] = self.get_bundle_stats(node_id)
            except Exception:
                result[node_id] = {
                    "src": 0, "fwd": 0, "xmt": 0, "rcv": 0,
                    "dlv": 0, "rfw": 0, "exp": 0, "bpsink_delivered": 0
                }
        return result

    # ── Topology ─────────────────────────────────────────────────

    def get_topology(self) -> dict:
        """Return full topology as JSON-serializable dict."""
        return {
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "links": [l.to_dict() for l in self.links.values()],
        }

    # ── Cleanup ──────────────────────────────────────────────────

    def cleanup_all(self) -> None:
        """Remove all managed containers and networks."""
        # Remove all links first (to disconnect networks cleanly)
        for link_id in list(self.links.keys()):
            try:
                self.delete_link(link_id)
            except Exception:
                pass

        # Remove all nodes
        for node_id in list(self.nodes.keys()):
            try:
                self.delete_node(node_id)
            except Exception:
                pass

        # Fallback: remove any orphaned resources with our label
        for container in self.client.containers.list(
            all=True, filters={"label": LABEL_KEY}
        ):
            container.remove(force=True)

        for network in self.client.networks.list(
            filters={"label": LABEL_KEY}
        ):
            try:
                network.remove()
            except Exception:
                pass

    # ── Exit Route Control ────────────────────────────────────────

    def set_all_exits(self):
        """Compute shortest-path next-hops and inject ION exit routes on all nodes."""
        self._propagate_routes()

    def set_node_exits(self, node_id: int):
        """Compute shortest-path next-hops and inject ION exit routes on a single node."""
        if node_id not in self.nodes:
            raise ValueError(f"Node {node_id} does not exist")
        self._propagate_routes_for_node(node_id)

    def clear_all_exits(self):
        """Remove all ION exit routes from every node."""
        for node in self.nodes.values():
            self._clear_node_exits(node)

    def get_node_config(self, node_id: int) -> dict:
        """Get ION configuration files and live state for a node.

        Regenerates node.rc from the current live topology so plans and
        exits always reflect the actual running state.
        """
        if node_id not in self.nodes:
            raise ValueError(f"Node {node_id} does not exist")

        node = self.nodes[node_id]
        result = {"node_id": node_id, "name": node.name}

        # Build neighbors from current links
        neighbors = []
        for link in self.links.values():
            if link.node_a == node_id:
                neighbors.append({"peer_id": link.node_b, "peer_ip": link.ip_b})
            elif link.node_b == node_id:
                neighbors.append({"peer_id": link.node_a, "peer_ip": link.ip_a})

        # Collect live exit routes
        exit_entries = []
        try:
            raw_exits = self.list_exits(node_id)
            import re as _re
            for ex in raw_exits:
                raw = ex.get("raw", "")
                m = _re.search(
                    r'From\s+(\d+)\s+through\s+(\d+),\s*forward\s+via\s+ipn:(\d+)\.0',
                    raw,
                )
                if m:
                    exit_entries.append({
                        "dest_first": int(m.group(1)),
                        "dest_last": int(m.group(2)),
                        "gateway_id": int(m.group(3)),
                    })
        except Exception:
            pass

        # Generate a live node.rc with current plans and exits
        result["node_rc"] = generate_node_rc(node_id, neighbors, exit_entries)

        # Also include ionconfig from disk
        config_dir = self._config_dirs.get(node_id)
        if config_dir:
            ionconfig_path = Path(config_dir) / "node.ionconfig"
            if ionconfig_path.exists():
                result["node_ionconfig"] = ionconfig_path.read_text()

        # Egress plans in ION config format (for the separate "plans" field)
        plan_lines = [f"a plan {nb['peer_id']} tcp/{nb['peer_ip']}:4556"
                      for nb in sorted(neighbors, key=lambda n: n['peer_id'])]
        result["plans"] = "\n".join(plan_lines) if plan_lines else ""

        # Exit routes in ION config format (for the separate "exits" field)
        exit_lines = [f"a exit {e['dest_first']} {e['dest_last']} ipn:{e['gateway_id']}.0"
                      for e in exit_entries]
        result["exits"] = [{"raw": l} for l in exit_lines]

        return result

    def _clear_node_exits(self, node: Node):
        """Remove all exit routes from a single node."""
        # List exits, then delete each one
        result = self._ion_exec(node.container_name, "ipnadmin", "l exit")
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.strip().splitlines():
                parts = line.strip().split()
                if len(parts) >= 2:
                    try:
                        first_num = parts[0].strip(":")
                        dest = int(first_num)
                        self._ion_exec(
                            node.container_name, "ipnadmin",
                            f"d exit {dest} {dest}"
                        )
                    except (ValueError, IndexError):
                        pass
        # Brute-force cleanup: try deleting exits for all non-neighbor nodes
        neighbors = self.get_neighbors(node.id)
        for other_id in self.nodes:
            if other_id != node.id and other_id not in neighbors:
                try:
                    self._ion_exec(
                        node.container_name, "ipnadmin",
                        f"d exit {other_id} {other_id}"
                    )
                except Exception:
                    pass

    def add_exit(self, node_id: int, dest_first: int, dest_last: int,
                 gateway_id: int) -> dict:
        """Add a single exit route on a node.

        Args:
            node_id: The node to configure.
            dest_first: First destination IPN number in the range.
            dest_last: Last destination IPN number in the range.
            gateway_id: The neighbor node to forward through (ipn:gateway.0).
        """
        if node_id not in self.nodes:
            raise ValueError(f"Node {node_id} does not exist")

        neighbors = self.get_neighbors(node_id)
        if gateway_id not in neighbors:
            raise ValueError(
                f"Gateway {gateway_id} is not a neighbor of node {node_id}. "
                f"Neighbors: {sorted(neighbors)}"
            )

        node = self.nodes[node_id]
        cmd = generate_add_exit_cmd(dest_first, dest_last, gateway_id)
        self._ion_exec(node.container_name, "ipnadmin", cmd)

        return {
            "node_id": node_id,
            "dest_first": dest_first,
            "dest_last": dest_last,
            "gateway": f"ipn:{gateway_id}.0",
        }

    def delete_exit(self, node_id: int, dest_first: int,
                    dest_last: int) -> dict:
        """Remove a single exit route from a node."""
        if node_id not in self.nodes:
            raise ValueError(f"Node {node_id} does not exist")

        node = self.nodes[node_id]
        self._ion_exec(
            node.container_name, "ipnadmin",
            f"d exit {dest_first} {dest_last}"
        )
        return {"node_id": node_id, "dest_first": dest_first,
                "dest_last": dest_last}

    def list_exits(self, node_id: int) -> list[dict]:
        """List current exit routes on a node by querying ipnadmin."""
        if node_id not in self.nodes:
            raise ValueError(f"Node {node_id} does not exist")

        node = self.nodes[node_id]
        result = self._ion_exec(node.container_name, "ipnadmin", "l exit")
        exits = []
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.strip().splitlines():
                line = line.strip().lstrip(": ").strip()
                if not line or "Stopping" in line:
                    continue
                # Only capture actual exit lines like:
                #   "From 3 through 3, forward via ipn:2.0."
                if "forward via" in line:
                    exits.append({"raw": line})
        return exits

    def get_neighbors(self, node_id: int) -> set[int]:
        """Return the set of directly connected node IDs."""
        neighbors = set()
        for link in self.links.values():
            if link.node_a == node_id:
                neighbors.add(link.node_b)
            elif link.node_b == node_id:
                neighbors.add(link.node_a)
        return neighbors

    def apply_raw_exit(self, node_id: int, raw_exit_line: str):
        """Re-apply a raw exit route string captured from ipnadmin.

        Parses the raw 'l exit' output to extract dest and gateway,
        then re-adds the exit route.
        """
        if node_id not in self.nodes:
            raise ValueError(f"Node {node_id} does not exist")

        node = self.nodes[node_id]
        import re
        # Match: "From 3 through 3, forward via ipn:2.0."
        match = re.search(
            r'From\s+(\d+)\s+through\s+(\d+),\s*forward\s+via\s+ipn:(\d+)\.0',
            raw_exit_line
        )
        if match:
            dest_first = int(match.group(1))
            dest_last = int(match.group(2))
            gateway_id = int(match.group(3))
            cmd = generate_add_exit_cmd(dest_first, dest_last, gateway_id)
            self._ion_exec(node.container_name, "ipnadmin", cmd)

    # ── Route Propagation (internal) ─────────────────────────────

    def _propagate_routes(self):
        """Compute shortest-path next-hops and inject ION exit routes.

        For every (source, destination) pair where destination is not a
        direct neighbor, add an ION 'exit' route pointing to the
        next-hop neighbor on the shortest path.

        Uses BFS on the link adjacency graph.
        """
        from collections import deque

        all_ids = list(self.nodes.keys())
        if len(all_ids) < 2:
            return

        # Build adjacency from current links
        adj: dict[int, set[int]] = {nid: set() for nid in all_ids}
        for link in self.links.values():
            adj[link.node_a].add(link.node_b)
            adj[link.node_b].add(link.node_a)

        # For each node, compute next-hop to every other node via BFS
        for src in all_ids:
            node = self.nodes[src]
            neighbors = adj[src]

            # BFS from src
            next_hop: dict[int, int] = {}  # dest -> first hop from src
            visited = {src}
            queue = deque()

            # Seed with direct neighbors
            for nb in neighbors:
                visited.add(nb)
                next_hop[nb] = nb  # direct neighbor: next-hop is itself
                queue.append(nb)

            while queue:
                current = queue.popleft()
                for nb in adj.get(current, []):
                    if nb not in visited:
                        visited.add(nb)
                        next_hop[nb] = next_hop[current]  # inherit first hop
                        queue.append(nb)

            # First, clear all existing exits on this node
            self._ion_exec(
                node.container_name, "ipnadmin",
                "l exit"
            )
            # Delete old exits (we'll re-add them)
            for dest in all_ids:
                if dest != src and dest not in neighbors:
                    try:
                        self._ion_exec(
                            node.container_name, "ipnadmin",
                            f"d exit {dest} {dest}"
                        )
                    except Exception:
                        pass

            # Add exits for non-neighbor destinations
            for dest, gateway in next_hop.items():
                if dest in neighbors:
                    continue  # direct neighbor has a plan already
                cmd = generate_add_exit_cmd(dest, dest, gateway)
                try:
                    self._ion_exec(node.container_name, "ipnadmin", cmd)
                except Exception:
                    pass

    def _propagate_routes_for_node(self, src: int):
        """Compute shortest-path next-hops and inject ION exit routes for a single node."""
        from collections import deque

        all_ids = list(self.nodes.keys())
        if len(all_ids) < 2:
            return

        node = self.nodes[src]

        # Build adjacency from current links
        adj: dict[int, set[int]] = {nid: set() for nid in all_ids}
        for link in self.links.values():
            adj[link.node_a].add(link.node_b)
            adj[link.node_b].add(link.node_a)

        neighbors = adj[src]

        # BFS from src
        next_hop: dict[int, int] = {}
        visited = {src}
        queue = deque()

        for nb in neighbors:
            visited.add(nb)
            next_hop[nb] = nb
            queue.append(nb)

        while queue:
            current = queue.popleft()
            for nb in adj.get(current, []):
                if nb not in visited:
                    visited.add(nb)
                    next_hop[nb] = next_hop[current]
                    queue.append(nb)

        # Clear old exits on this node
        for dest in all_ids:
            if dest != src and dest not in neighbors:
                try:
                    self._ion_exec(
                        node.container_name, "ipnadmin",
                        f"d exit {dest} {dest}"
                    )
                except Exception:
                    pass

        # Add exits for non-neighbor destinations
        for dest, gateway in next_hop.items():
            if dest in neighbors:
                continue
            cmd = generate_add_exit_cmd(dest, dest, gateway)
            try:
                self._ion_exec(node.container_name, "ipnadmin", cmd)
            except Exception:
                pass

    def repair_ion_config(self):
        """Re-apply outducts and plans for all links.

        This is a safety net for import: even if create_link's ION commands
        fail due to timing, this method re-applies them after ION is ready.
        Skips plans that already exist to avoid 'Duplicate egress plan' warnings.
        """
        errors = []

        for link in self.links.values():
            node_a = self.nodes.get(link.node_a)
            node_b = self.nodes.get(link.node_b)
            if not node_a or not node_b:
                continue

            # Ensure ION is ready on both nodes
            self._wait_for_ion_ready(node_a.container_name)
            self._wait_for_ion_ready(node_b.container_name)

            # Check existing plans on each node before adding
            existing_plans_a = self._list_plan_peers(node_a.container_name)
            existing_plans_b = self._list_plan_peers(node_b.container_name)

            # Re-add outducts + plans on node_a (only if plan is missing)
            try:
                self._ion_exec(node_a.container_name, "bpadmin",
                               generate_add_outduct_cmd(link.ip_b))
                if link.node_b not in existing_plans_a:
                    self._ion_exec(node_a.container_name, "ipnadmin",
                                   generate_add_plan_cmd(link.node_b, link.ip_b))
            except Exception as e:
                errors.append(f"N{link.node_a}: {e}")

            # Re-add outducts + plans on node_b (only if plan is missing)
            try:
                self._ion_exec(node_b.container_name, "bpadmin",
                               generate_add_outduct_cmd(link.ip_a))
                if link.node_a not in existing_plans_b:
                    self._ion_exec(node_b.container_name, "ipnadmin",
                                   generate_add_plan_cmd(link.node_a, link.ip_a))
            except Exception as e:
                errors.append(f"N{link.node_b}: {e}")

        return errors

    # ── Helpers ───────────────────────────────────────────────────

    def _wait_for_ion_ready(self, container_name: str,
                            max_retries: int = 20, interval: float = 1.0):
        """Poll ION until bpadmin responds properly."""
        import time
        for i in range(max_retries):
            try:
                result = subprocess.run(
                    ["docker", "exec", container_name, "bash", "-c",
                     "echo 'l' | bpadmin 2>/dev/null"],
                    capture_output=True, text=True, timeout=3
                )
                stdout = result.stdout.strip()
                if (result.returncode == 0 and stdout
                        and "not initialized" not in stdout):
                    return  # ION is truly ready
            except subprocess.TimeoutExpired:
                pass  # bpadmin hung — ION not ready yet
            time.sleep(interval)
        # If we get here, proceed anyway — best effort

    def _next_node_id(self) -> int:
        """Find the next available node ID."""
        if not self.nodes:
            return 1
        return max(self.nodes.keys()) + 1

    def _list_plan_peers(self, container_name: str) -> set[int]:
        """Query ipnadmin to list existing egress plan peer IDs."""
        import re
        peers = set()
        try:
            result = self._ion_exec(container_name, "ipnadmin", "l plan")
            if result.returncode == 0 and result.stdout.strip():
                for line in result.stdout.strip().splitlines():
                    # Match plan lines like "To node 2 via tcp/172.50.0.3:4556"
                    m = re.search(r'[Tt]o\s+node\s+(\d+)', line)
                    if m:
                        peers.add(int(m.group(1)))
                    # Also match plain numeric format "2 tcp/..."
                    m2 = re.match(r'\s*(\d+)\s+tcp/', line)
                    if m2:
                        peers.add(int(m2.group(1)))
        except Exception:
            pass
        return peers

    def _ion_exec(self, container_name: str, admin_tool: str, command: str):
        """Run an ION admin command (bpadmin/ipnadmin) on a container."""
        return self._docker_exec(
            container_name,
            f"echo '{command}' | {admin_tool}"
        )

    def _docker_exec(self, container_name: str, cmd: str,
                     ignore_errors: bool = False) -> subprocess.CompletedProcess:
        """Run a command inside a Docker container."""
        result = subprocess.run(
            ["docker", "exec", container_name, "bash", "-c", cmd],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0 and not ignore_errors:
            pass  # Log but don't crash
        return result

    def _get_interface(self, container_name: str, ip: str) -> str | None:
        """Find the network interface name for a given IP inside a container."""
        result = self._docker_exec(
            container_name,
            f"ip -o -4 addr show | grep '{ip}'"
        )
        if result.returncode == 0 and result.stdout.strip():
            # Format: "3: eth1    inet 172.50.0.2/24 ..."
            parts = result.stdout.strip().split()
            if len(parts) >= 2:
                return parts[1].rstrip(":")
        return None
