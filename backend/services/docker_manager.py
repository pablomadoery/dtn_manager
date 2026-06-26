"""Docker manager for ION-DTN node lifecycle and network management.

Design invariant: the Docker daemon is the single source of truth. The
in-memory ``self.nodes`` / ``self.links`` dicts are a cache that is rebuilt
from live labeled resources by :meth:`reconcile` on every startup, so a crash
that leaves orphaned containers/networks can never desync the manager (the
historical 409 "network already exists" incident).

All container command execution goes through one argv-based primitive
(:meth:`_exec`) — never ``bash -c`` with interpolated user input — and all
mutating methods are serialized by a single re-entrant lock.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import docker
from docker.types import IPAMConfig, IPAMPool

from backend import settings
from backend.errors import (
    BackendTimeout,
    CapacityError,
    ConflictError,
    IonExecError,
    NotFoundError,
    ValidationError,
)
from backend.models import Node, Link, NodeStatus, LinkStatus, NetemConfig
from backend.services.subnet_pool import SubnetPool
from backend.services.ion_config import (
    resolve_cl,
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
LABEL_NODE_ID = "ionmgr.node_id"
LABEL_LINK_ID = "ionmgr.link_id"

# ION admin responses that mean "the desired state already holds" — treated as
# success so a benign duplicate never triggers a rollback or spurious error.
_BENIGN_ION = re.compile(r"duplicate|already exists|already in use", re.IGNORECASE)


class DockerManager:
    """Manages ION-DTN Docker containers and networks."""

    def __init__(self, reconcile: bool = True):
        self.client = docker.from_env()
        self.subnet_pool = SubnetPool(base_second_octet=50)
        self.nodes: dict[int, Node] = {}
        self.links: dict[str, Link] = {}
        self._config_dirs: dict[int, str] = {}  # node_id -> temp config dir
        # Re-entrant: delete_node -> delete_link, cleanup_all -> both.
        self._lock = threading.RLock()
        # Monotonic topology version; bumped on every adopt/create/delete so the
        # frontend can flush stale per-node stats when the set of nodes changes.
        self._epoch = 0
        # Stable per-process identity so a backend restart is detectable client
        # side even if the node-id set happens to repeat.
        self.boot_id = str(os.getpid())
        if reconcile:
            try:
                self.reconcile()
            except Exception as e:  # never let adoption brick startup
                print(f"[reconcile] warning: adoption incomplete: {e}")

    # ── Reconciliation / adoption ────────────────────────────────
    #
    # Make the daemon the source of truth: rebuild nodes/links from live
    # labeled resources, seed id/subnet counters from live state, mark
    # half-built links ERROR, and remove true orphan networks (0 containers).

    def _bump(self) -> None:
        """Advance the topology epoch (caller holds the lock)."""
        self._epoch += 1

    @property
    def epoch(self) -> int:
        with self._lock:
            return self._epoch

    def reconcile(self) -> None:
        """Adopt live labeled Docker resources into in-memory state."""
        with self._lock:
            self._adopt_containers()
            self._adopt_networks()
            self._seed_counters()
            self._bump()

    def _adopt_containers(self) -> None:
        for container in self.client.containers.list(
            all=True, filters={"label": LABEL_KEY}
        ):
            node_id = self._node_id_of(container)
            if node_id is None:
                continue
            status = (NodeStatus.RUNNING if container.status == "running"
                      else NodeStatus.ERROR)
            self.nodes[node_id] = Node(
                id=node_id,
                name=f"N{node_id}",
                container_id=container.id,
                container_name=container.name,
                status=status,
            )

    def _adopt_networks(self) -> None:
        for network in self.client.networks.list(filters={"label": LABEL_KEY}):
            try:
                network.reload()
            except docker.errors.NotFound:
                continue
            pair = self._link_pair_of(network)
            if pair is None:
                continue
            a, b = pair
            attached = network.attrs.get("Containers", {}) or {}

            # A network with no attached containers is a true orphan — reap it.
            if not attached:
                try:
                    network.remove()
                except Exception:
                    pass
                continue

            subnet = self._network_subnet(network)
            ip_a, ip_b = self._ips_from_subnet(subnet) if subnet else ("", "")

            # Recover the real per-container IPs from live attachment data.
            live_ips = self._container_ips(network)
            if a in self.nodes and a in live_ips:
                ip_a = live_ips[a]
            if b in self.nodes and b in live_ips:
                ip_b = live_ips[b]

            link_id = f"{a}-{b}"
            # Half-built: both endpoints must be present and attached.
            healthy = (a in self.nodes and b in self.nodes
                       and a in live_ips and b in live_ips)
            link = Link(
                id=link_id, node_a=a, node_b=b,
                network_name=network.name, network_id=network.id,
                subnet=subnet or "", ip_a=ip_a, ip_b=ip_b,
                status=LinkStatus.UP if healthy else LinkStatus.ERROR,
            )
            self.links[link_id] = link
            if a in self.nodes and ip_a:
                self.nodes[a].ip_addresses[link_id] = ip_a
            if b in self.nodes and ip_b:
                self.nodes[b].ip_addresses[link_id] = ip_b

    def _seed_counters(self) -> None:
        # Seed subnet pool from adopted networks (filter on our 2nd octet).
        indices = []
        for link in self.links.values():
            idx = self._subnet_index(link.subnet)
            if idx is not None:
                indices.append(idx)
        self.subnet_pool.from_live(indices)

    # ── Adoption parsing helpers ─────────────────────────────────

    def _node_id_of(self, container) -> int | None:
        labels = container.labels or {}
        if LABEL_NODE_ID in labels:
            try:
                return int(labels[LABEL_NODE_ID])
            except (TypeError, ValueError):
                pass
        m = re.fullmatch(rf"{re.escape(CONTAINER_PREFIX)}(\d+)", container.name or "")
        return int(m.group(1)) if m else None

    def _link_pair_of(self, network) -> tuple[int, int] | None:
        labels = network.attrs.get("Labels", {}) or {}
        if LABEL_LINK_ID in labels:
            m = re.fullmatch(r"(\d+)-(\d+)", labels[LABEL_LINK_ID])
            if m:
                return int(m.group(1)), int(m.group(2))
        m = re.fullmatch(rf"{re.escape(NETWORK_PREFIX)}(\d+)_(\d+)", network.name or "")
        return (int(m.group(1)), int(m.group(2))) if m else None

    def _network_subnet(self, network) -> str | None:
        config = (network.attrs.get("IPAM", {}) or {}).get("Config") or []
        for entry in config:
            if entry.get("Subnet"):
                return entry["Subnet"]
        return None

    def _container_ips(self, network) -> dict[int, str]:
        """Map adopted node_id -> assigned IPv4 from network attachment data."""
        ips: dict[int, str] = {}
        by_cid = {n.container_id: nid for nid, n in self.nodes.items()
                  if n.container_id}
        by_name = {n.container_name: nid for nid, n in self.nodes.items()}
        for cid, info in (network.attrs.get("Containers", {}) or {}).items():
            nid = by_cid.get(cid) or by_name.get(info.get("Name", ""))
            if nid is None:
                continue
            addr = (info.get("IPv4Address") or "").split("/")[0]
            if addr:
                ips[nid] = addr
        return ips

    def _subnet_index(self, subnet: str) -> int | None:
        """Return the 3rd-octet index of a subnet, only for our 2nd octet."""
        m = re.match(r"172\.(\d+)\.(\d+)\.\d+/\d+", subnet or "")
        if not m or int(m.group(1)) != self.subnet_pool.base:
            return None
        return int(m.group(2))

    def _ips_from_subnet(self, subnet: str) -> tuple[str, str]:
        idx = self._subnet_index(subnet)
        if idx is None:
            return "", ""
        return (f"172.{self.subnet_pool.base}.{idx}.2",
                f"172.{self.subnet_pool.base}.{idx}.3")

    # ── Node Operations ──────────────────────────────────────────

    def create_node(self, node_id: int | None = None) -> Node:
        """Create a new ION-DTN node backed by a Docker container."""
        # Reserve the id and a PENDING placeholder under the lock so a
        # concurrent caller can never compute the same id.
        with self._lock:
            if node_id is None:
                node_id = self._next_node_id()
            if node_id in self.nodes:
                raise ConflictError(f"Node {node_id} already exists")
            if len(self.nodes) >= settings.MAX_NODES:
                raise CapacityError(
                    f"Node cap reached (MAX_NODES={settings.MAX_NODES})")
            placeholder = Node(id=node_id, name=f"N{node_id}",
                               status=NodeStatus.CREATING)
            self.nodes[node_id] = placeholder

        name = f"N{node_id}"
        container_name = f"{CONTAINER_PREFIX}{node_id}"
        config_dir: str | None = None
        container = None
        try:
            # Remove any orphaned container with the same name from a prior run.
            try:
                self.client.containers.get(container_name).remove(force=True)
            except docker.errors.NotFound:
                pass

            # Sweep leftover temp config dirs from a crashed prior session.
            self._sweep_stale_config_dirs(node_id)

            config_dir = tempfile.mkdtemp(prefix=f"ion_n{node_id}_")
            Path(config_dir, "node.ionconfig").write_text(generate_ionconfig())
            Path(config_dir, "node.rc").write_text(
                generate_node_rc(node_id, neighbors=[]))

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
                labels={LABEL_KEY: "true", LABEL_NODE_ID: str(node_id)},
            )

            # Gate on real ION readiness rather than a blind sleep.
            ready = self._wait_for_ion_ready(container_name)

            # Reflect an already-exited container as failed, not RUNNING.
            container.reload()
            if container.status != "running":
                status = NodeStatus.ERROR
            elif not ready:
                status = NodeStatus.ERROR
            else:
                status = NodeStatus.RUNNING
                # Launch bpsink (backgrounded) and verify it actually started.
                self._start_bpsink(container_name, node_id)
                if not self._bpsink_running(container_name):
                    status = NodeStatus.ERROR

            with self._lock:
                self._config_dirs[node_id] = config_dir
                node = Node(
                    id=node_id, name=name,
                    container_id=container.id,
                    container_name=container_name,
                    status=status,
                )
                self.nodes[node_id] = node
                self._bump()
            return node
        except Exception:
            # Roll back: remove container + config dir, drop placeholder.
            try:
                if container is not None:
                    container.remove(force=True)
                else:
                    self.client.containers.get(container_name).remove(force=True)
            except Exception:
                pass
            if config_dir and os.path.exists(config_dir):
                shutil.rmtree(config_dir, ignore_errors=True)
            with self._lock:
                self.nodes.pop(node_id, None)
                self._config_dirs.pop(node_id, None)
            raise

    def delete_node(self, node_id: int) -> None:
        """Delete a node and its container. Also removes all connected links."""
        with self._lock:
            if node_id not in self.nodes:
                raise NotFoundError(f"Node {node_id} does not exist")
            connected_links = [
                lid for lid, link in self.links.items()
                if link.node_a == node_id or link.node_b == node_id
            ]
            node = self.nodes[node_id]

        for link_id in connected_links:
            try:
                self.delete_link(link_id)
            except Exception:
                pass

        try:
            container = self.client.containers.get(node.container_name)
            container.remove(force=True)
        except docker.errors.NotFound:
            pass

        with self._lock:
            config_dir = self._config_dirs.pop(node_id, None)
            self.nodes.pop(node_id, None)
            self._bump()
        if config_dir and os.path.exists(config_dir):
            shutil.rmtree(config_dir, ignore_errors=True)

    def get_node(self, node_id: int) -> Node:
        with self._lock:
            if node_id not in self.nodes:
                raise NotFoundError(f"Node {node_id} does not exist")
            return self.nodes[node_id]

    def list_nodes(self) -> list[Node]:
        with self._lock:
            return list(self.nodes.values())

    # ── Link Operations ──────────────────────────────────────────

    def create_link(self, node_a_id: int, node_b_id: int,
                    convergence_layer: str = "tcpcl") -> Link:
        """Create a link between two nodes (Docker network + ION config).

        Idempotent on the network name and transactional: any partial failure
        rolls back the network, container connections and subnet reservation so
        nothing leaks. A node that is not ION-ready yields a status=ERROR link
        (repairable via reconcile/repair_ion_config) rather than a false UP.
        """
        if node_a_id > node_b_id:
            node_a_id, node_b_id = node_b_id, node_a_id
        link_id = f"{node_a_id}-{node_b_id}"

        # Validate the CL up front so an unsupported value fails loudly.
        cl = resolve_cl(convergence_layer)

        # Reserve id + subnet and insert a PENDING placeholder under the lock.
        with self._lock:
            if link_id in self.links:
                raise ConflictError(f"Link {link_id} already exists")
            if node_a_id not in self.nodes or node_b_id not in self.nodes:
                raise ValidationError(
                    "Both nodes must exist before creating a link")
            if len(self.links) >= settings.MAX_LINKS:
                raise CapacityError(
                    f"Link cap reached (MAX_LINKS={settings.MAX_LINKS})")
            node_a = self.nodes[node_a_id]
            node_b = self.nodes[node_b_id]
            subnet_idx, subnet, ip_a, ip_b = self.subnet_pool.allocate()
            self.links[link_id] = Link(
                id=link_id, node_a=node_a_id, node_b=node_b_id,
                subnet=subnet, ip_a=ip_a, ip_b=ip_b,
                convergence_layer=convergence_layer, status=LinkStatus.DOWN)

        network_name = f"{NETWORK_PREFIX}{node_a_id}_{node_b_id}"
        network = None
        network_created = False
        connected_a = connected_b = False
        try:
            network, network_created = self._get_or_create_network(
                network_name, subnet, link_id)

            # If we adopted an existing network, its real subnet wins.
            real_subnet = self._network_subnet(network) or subnet
            if real_subnet != subnet:
                new_idx = self._subnet_index(real_subnet)
                with self._lock:
                    self.subnet_pool.release(subnet_idx)
                    if new_idx is not None:
                        self.subnet_pool.reserve(new_idx)
                subnet = real_subnet
                ip_a, ip_b = self._ips_from_subnet(real_subnet)
                subnet_idx = new_idx if new_idx is not None else subnet_idx

            container_a = self.client.containers.get(node_a.container_name)
            container_b = self.client.containers.get(node_b.container_name)
            self._connect(network, container_a, ip_a)
            connected_a = True
            self._connect(network, container_b, ip_b)
            connected_b = True

            ready_a = self._wait_for_ion_ready(node_a.container_name)
            ready_b = self._wait_for_ion_ready(node_b.container_name)

            ion_ok = True
            if ready_a:
                ion_ok &= self._apply_link_config(node_a.container_name,
                                                  node_b_id, ip_b, cl.token)
            if ready_b:
                ion_ok &= self._apply_link_config(node_b.container_name,
                                                  node_a_id, ip_a, cl.token)

            status = (LinkStatus.UP if (ready_a and ready_b and ion_ok)
                      else LinkStatus.ERROR)
            with self._lock:
                node_a.ip_addresses[link_id] = ip_a
                node_b.ip_addresses[link_id] = ip_b
                link = self.links[link_id]
                link.network_name = network_name
                link.network_id = network.id
                link.subnet = subnet
                link.ip_a = ip_a
                link.ip_b = ip_b
                link.status = status
                self._bump()
            return link
        except Exception:
            # Compensating rollback — leave no orphan network/subnet/conn.
            try:
                if network is not None:
                    if connected_a:
                        self._safe_disconnect(network, node_a.container_name)
                    if connected_b:
                        self._safe_disconnect(network, node_b.container_name)
                    if network_created:
                        try:
                            network.remove()
                        except Exception:
                            pass
            except Exception:
                pass
            with self._lock:
                self.subnet_pool.release(subnet_idx)
                self.links.pop(link_id, None)
                node_a.ip_addresses.pop(link_id, None)
                node_b.ip_addresses.pop(link_id, None)
            raise

    def _get_or_create_network(self, name: str, subnet: str, link_id: str):
        """Return (network, created_here). Idempotent on the deterministic name.

        Adopts a pre-existing same-named network (the 409 case) instead of
        failing, and tolerates the create() race by falling back to get().
        """
        try:
            existing = self.client.networks.get(name)
            existing.reload()
            return existing, False
        except docker.errors.NotFound:
            pass
        try:
            network = self.client.networks.create(
                name=name,
                driver="bridge",
                ipam=IPAMConfig(pool_configs=[IPAMPool(subnet=subnet)]),
                labels={LABEL_KEY: "true", LABEL_LINK_ID: link_id},
            )
            return network, True
        except docker.errors.APIError as e:
            # 409 race: another writer created it between get and create.
            if getattr(e, "status_code", None) == 409 or "already exists" in str(e):
                existing = self.client.networks.get(name)
                existing.reload()
                return existing, False
            raise

    def _connect(self, network, container, ip: str) -> None:
        """Connect a container at a fixed IP, tolerating an existing attach."""
        try:
            network.connect(container, ipv4_address=ip)
        except docker.errors.APIError as e:
            if "already exists" in str(e) or "already has" in str(e):
                return
            raise

    def _safe_disconnect(self, network, container_name: str) -> None:
        try:
            network.disconnect(container_name, force=True)
        except Exception:
            pass

    def _apply_link_config(self, container_name: str, peer_id: int,
                          peer_ip: str, cl_token: str) -> bool:
        """Add outduct + egress plan for a peer. Returns True on success."""
        ok = self._ion_apply(container_name, "bpadmin",
                             generate_add_outduct_cmd(peer_ip, cl_token))
        ok &= self._ion_apply(container_name, "ipnadmin",
                             generate_add_plan_cmd(peer_id, peer_ip, cl_token))
        return ok

    def delete_link(self, link_id: str) -> None:
        """Remove a link: disconnect network, remove ION config."""
        with self._lock:
            if link_id not in self.links:
                raise NotFoundError(f"Link {link_id} does not exist")
            link = self.links[link_id]
            node_a = self.nodes.get(link.node_a)
            node_b = self.nodes.get(link.node_b)

        # Remove ION config on both nodes (best effort — node may be gone).
        for node, peer_id, peer_ip in [
            (node_a, link.node_b, link.ip_b),
            (node_b, link.node_a, link.ip_a),
        ]:
            if node is not None:
                try:
                    self._ion_exec(node.container_name, "ipnadmin",
                                   generate_delete_plan_cmd(peer_id))
                    self._ion_exec(node.container_name, "bpadmin",
                                   generate_delete_outduct_cmd(peer_ip))
                except Exception:
                    pass

        # Remove the Docker network.
        try:
            network = self.client.networks.get(link.network_name)
            for node in (node_a, node_b):
                if node is not None:
                    self._safe_disconnect(network, node.container_name)
            network.remove()
        except docker.errors.NotFound:
            pass

        with self._lock:
            if node_a is not None:
                node_a.ip_addresses.pop(link_id, None)
            if node_b is not None:
                node_b.ip_addresses.pop(link_id, None)
            idx = self._subnet_index(link.subnet)
            if idx is not None:
                self.subnet_pool.release(idx)
            self.links.pop(link_id, None)
            self._bump()

    def list_links(self) -> list[Link]:
        with self._lock:
            return list(self.links.values())

    # ── Link Disruption ──────────────────────────────────────────

    def disrupt_link(self, link_id: str,
                     loss: float = 100, delay: int = 0, jitter: int = 0) -> Link:
        """Apply tc netem to a link."""
        with self._lock:
            if link_id not in self.links:
                raise NotFoundError(f"Link {link_id} does not exist")
            link = self.links[link_id]
            sides = [(link.node_a, link.ip_a), (link.node_b, link.ip_b)]
            nodes = dict(self.nodes)

        netem = NetemConfig(loss_percent=loss, delay_ms=delay, jitter_ms=jitter)
        tc_args = netem.to_tc_args().split()

        for node_id, ip in sides:
            node = nodes.get(node_id)
            if node:
                iface = self._get_interface(node.container_name, ip)
                if iface:
                    self._exec(
                        node.container_name,
                        ["tc", "qdisc", "replace", "dev", iface,
                         "root", "netem", *tc_args],
                        ignore_errors=True,
                    )

        with self._lock:
            link.netem = netem
            link.status = LinkStatus.DOWN if loss >= 100 else LinkStatus.DEGRADED
        return link

    def restore_link(self, link_id: str) -> Link:
        """Remove tc netem from a link."""
        with self._lock:
            if link_id not in self.links:
                raise NotFoundError(f"Link {link_id} does not exist")
            link = self.links[link_id]
            sides = [(link.node_a, link.ip_a), (link.node_b, link.ip_b)]
            nodes = dict(self.nodes)

        for node_id, ip in sides:
            node = nodes.get(node_id)
            if node:
                iface = self._get_interface(node.container_name, ip)
                if iface:
                    self._exec(
                        node.container_name,
                        ["tc", "qdisc", "del", "dev", iface, "root"],
                        ignore_errors=True,
                    )

        with self._lock:
            link.netem = None
            link.status = LinkStatus.UP
        return link

    # ── Traffic ──────────────────────────────────────────────────

    def send_bundle(self, from_node: int, to_node: int, message: str) -> bool:
        """Send a single bundle using bpsource (argv — no shell parsing)."""
        with self._lock:
            if from_node not in self.nodes:
                raise NotFoundError(f"Node {from_node} does not exist")
            container_name = self.nodes[from_node].container_name
        # message is passed as a distinct argv element; it is never shell-parsed.
        result = self._exec(
            container_name,
            ["bpsource", f"ipn:{int(to_node)}.1", message],
            ignore_errors=True,
        )
        return result.returncode == 0

    # ── Stats ────────────────────────────────────────────────────

    def get_node_logs(self, node_id: int, tail: int = 50) -> str:
        """Get ion.log content from a node."""
        with self._lock:
            if node_id not in self.nodes:
                raise NotFoundError(f"Node {node_id} does not exist")
            container_name = self.nodes[node_id].container_name
        result = self._exec(
            container_name,
            ["tail", "-n", str(int(tail)), "/ion-runtime/ion.log"],
            ignore_errors=True,
        )
        return result.stdout if result.returncode == 0 else ""

    def get_bpsink_output(self, node_id: int, tail: int = 20) -> str:
        """Get recent bpsink output (from bpsink.log file)."""
        with self._lock:
            if node_id not in self.nodes:
                raise NotFoundError(f"Node {node_id} does not exist")
            container_name = self.nodes[node_id].container_name
        result = self._exec(
            container_name,
            ["tail", "-n", str(int(tail)), "/ion-runtime/bpsink.log"],
            ignore_errors=True,
        )
        return result.stdout + result.stderr

    def get_bundle_count(self, node_id: int) -> int:
        """True cumulative count of delivered payloads (whole-file grep -c).

        ``grep -c`` over the entire bpsink.log avoids the windowed-tail bug
        where the count silently caps/decreases once the log exceeds the tail.
        """
        with self._lock:
            if node_id not in self.nodes:
                raise NotFoundError(f"Node {node_id} does not exist")
            container_name = self.nodes[node_id].container_name
        result = self._exec(
            container_name,
            ["grep", "-c", "Payload delivered", "/ion-runtime/bpsink.log"],
            ignore_errors=True,
        )
        # grep exits 1 with no matches and 2 on error (e.g. missing file).
        try:
            return int(result.stdout.strip() or 0)
        except ValueError:
            return 0

    def get_bundle_stats(self, node_id: int) -> dict:
        """Get bundle statistics from a node using bpstats.

        Triggers ``bpstats`` (which appends a fresh ``[x]`` block to ion.log),
        polls for the new block rather than sleeping a fixed interval, then
        parses only the lines after that marker.
        """
        with self._lock:
            if node_id not in self.nodes:
                raise NotFoundError(f"Node {node_id} does not exist")
            container_name = self.nodes[node_id].container_name

        stats = {"src": 0, "fwd": 0, "xmt": 0, "rcv": 0,
                 "dlv": 0, "rfw": 0, "exp": 0}

        # Count [x] blocks before triggering so we can wait for a new one.
        before = self._count_bpstats_markers(container_name)
        try:
            self._exec(container_name, ["bpstats"], ignore_errors=True)
        except Exception:
            pass

        fresh_lines = self._read_after_new_bpstats(container_name, before)
        parsed_any = False
        for line in fresh_lines:
            m = re.search(
                r'\[x\]\s+(src|fwd|xmt|rcv|dlv|rfw|exp)\b.*?\(\+\)\s+(\d+)',
                line,
            )
            if m:
                stats[m.group(1)] = int(m.group(2))
                parsed_any = True
        if not parsed_any and before is not None:
            # Triggered but parsed nothing — surface as a soft warning.
            print(f"[bpstats] node {node_id}: no stats classes parsed")

        try:
            stats["bpsink_delivered"] = self.get_bundle_count(node_id)
        except Exception:
            stats["bpsink_delivered"] = 0
        return stats

    def _count_bpstats_markers(self, container_name: str) -> int:
        result = self._exec(
            container_name,
            ["grep", "-c", "Total bundles", "/ion-runtime/ion.log"],
            ignore_errors=True,
        )
        try:
            return int(result.stdout.strip() or 0)
        except ValueError:
            return 0

    def _read_after_new_bpstats(self, container_name: str,
                                before: int, attempts: int = 5) -> list[str]:
        """Poll ion.log until a new bpstats block appears; return its lines."""
        for _ in range(attempts):
            result = self._exec(
                container_name,
                ["tail", "-n", "60", "/ion-runtime/ion.log"],
                ignore_errors=True,
            )
            if result.returncode == 0:
                lines = result.stdout.splitlines()
                # Find the last bpstats block start ("... Total bundles ...").
                start = None
                for i, line in enumerate(lines):
                    if "Total bundles" in line or "[x]" in line:
                        start = i
                        break
                now = self._count_bpstats_markers(container_name)
                if now > before and start is not None:
                    return lines[start:]
            time.sleep(0.3)
        return []

    def get_all_bundle_stats(self) -> dict:
        """Get bundle stats for all nodes at once (efficient batch call)."""
        with self._lock:
            node_ids = list(self.nodes.keys())
        result = {}
        for node_id in node_ids:
            try:
                result[node_id] = self.get_bundle_stats(node_id)
            except Exception:
                result[node_id] = {
                    "src": 0, "fwd": 0, "xmt": 0, "rcv": 0,
                    "dlv": 0, "rfw": 0, "exp": 0, "bpsink_delivered": 0,
                }
        return result

    # ── Topology ─────────────────────────────────────────────────

    def get_topology(self) -> dict:
        """Return full topology as JSON-serializable dict (snapshot)."""
        with self._lock:
            nodes = list(self.nodes.values())
            links = list(self.links.values())
            epoch = self._epoch
        return {
            "epoch": epoch,
            "boot_id": self.boot_id,
            "nodes": [n.to_dict() for n in nodes],
            "links": [l.to_dict() for l in links],
        }

    # ── Cleanup ──────────────────────────────────────────────────

    def cleanup_all(self) -> None:
        """Remove all managed containers and networks."""
        with self._lock:
            link_ids = list(self.links.keys())
            node_ids = list(self.nodes.keys())

        for link_id in link_ids:
            try:
                self.delete_link(link_id)
            except Exception:
                pass
        for node_id in node_ids:
            try:
                self.delete_node(node_id)
            except Exception:
                pass

        # Belt-and-suspenders: reap any labeled orphan, isolating per-item
        # failures so one bad resource cannot abort the whole sweep.
        for container in self.client.containers.list(
            all=True, filters={"label": LABEL_KEY}
        ):
            try:
                container.remove(force=True)
            except Exception:
                pass
        for network in self.client.networks.list(filters={"label": LABEL_KEY}):
            try:
                network.remove()
            except Exception:
                pass
        # Also remove same-named networks that lost their label (crash leftover).
        for network in self.client.networks.list():
            if (network.name or "").startswith(NETWORK_PREFIX):
                try:
                    network.remove()
                except Exception:
                    pass
        with self._lock:
            self._bump()

    # ── Exit Route Control ────────────────────────────────────────

    def set_all_exits(self) -> list[str]:
        """Compute shortest-path next-hops and inject ION exit routes on all nodes."""
        with self._lock:
            node_ids = list(self.nodes.keys())
        errors: list[str] = []
        for src in node_ids:
            errors.extend(self._reconcile_node_exits(src))
        return errors

    def set_node_exits(self, node_id: int) -> list[str]:
        """Compute shortest-path next-hops and inject ION exit routes on one node."""
        with self._lock:
            if node_id not in self.nodes:
                raise NotFoundError(f"Node {node_id} does not exist")
        return self._reconcile_node_exits(node_id)

    def clear_all_exits(self):
        """Remove all ION exit routes from every node."""
        with self._lock:
            nodes = list(self.nodes.values())
        for node in nodes:
            self._clear_node_exits(node)

    def get_node_config(self, node_id: int) -> dict:
        """Get ION configuration files and live state for a node."""
        with self._lock:
            if node_id not in self.nodes:
                raise NotFoundError(f"Node {node_id} does not exist")
            node = self.nodes[node_id]
            links = list(self.links.values())
            config_dir = self._config_dirs.get(node_id)

        result = {"node_id": node_id, "name": node.name}

        # Build neighbors (with convergence_layer) from current links.
        neighbors = []
        for link in links:
            if link.node_a == node_id:
                neighbors.append({"peer_id": link.node_b, "peer_ip": link.ip_b,
                                  "convergence_layer": link.convergence_layer})
            elif link.node_b == node_id:
                neighbors.append({"peer_id": link.node_a, "peer_ip": link.ip_a,
                                  "convergence_layer": link.convergence_layer})

        # Collect live exit routes via the shared parser.
        exit_entries = []
        try:
            for ex in self.list_exits(node_id):
                parsed = parse_exit_line(ex.get("raw", ""))
                if parsed:
                    exit_entries.append(parsed)
        except Exception:
            pass

        result["node_rc"] = generate_node_rc(node_id, neighbors, exit_entries)

        if config_dir:
            ionconfig_path = Path(config_dir) / "node.ionconfig"
            if ionconfig_path.exists():
                result["node_ionconfig"] = ionconfig_path.read_text()

        plan_lines = []
        for nb in sorted(neighbors, key=lambda n: n["peer_id"]):
            cl = resolve_cl(nb.get("convergence_layer"))
            plan_lines.append(
                f"a plan {nb['peer_id']} {cl.token}/{nb['peer_ip']}:{cl.port}")
        result["plans"] = "\n".join(plan_lines) if plan_lines else ""

        exit_lines = [
            f"a exit {e['dest_first']} {e['dest_last']} ipn:{e['gateway_id']}.0"
            for e in exit_entries
        ]
        result["exits"] = [{"raw": l} for l in exit_lines]
        return result

    def _clear_node_exits(self, node: Node):
        """Remove all exit routes from a single node using the shared parser."""
        result = self._ion_exec(node.container_name, "ipnadmin", "l exit")
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                parsed = parse_exit_line(line)
                if parsed:
                    self._ion_exec(
                        node.container_name, "ipnadmin",
                        f"d exit {parsed['dest_first']} {parsed['dest_last']}")

    def add_exit(self, node_id: int, dest_first: int, dest_last: int,
                 gateway_id: int) -> dict:
        """Add a single exit route on a node."""
        with self._lock:
            if node_id not in self.nodes:
                raise NotFoundError(f"Node {node_id} does not exist")
            node = self.nodes[node_id]
            neighbors = self._neighbors_locked(node_id)
        if gateway_id not in neighbors:
            raise ValidationError(
                f"Gateway {gateway_id} is not a neighbor of node {node_id}. "
                f"Neighbors: {sorted(neighbors)}")
        cmd = generate_add_exit_cmd(dest_first, dest_last, gateway_id)
        self._ion_apply(node.container_name, "ipnadmin", cmd)
        return {"node_id": node_id, "dest_first": dest_first,
                "dest_last": dest_last, "gateway": f"ipn:{gateway_id}.0"}

    def delete_exit(self, node_id: int, dest_first: int,
                    dest_last: int) -> dict:
        """Remove a single exit route from a node."""
        with self._lock:
            if node_id not in self.nodes:
                raise NotFoundError(f"Node {node_id} does not exist")
            node = self.nodes[node_id]
        self._ion_exec(node.container_name, "ipnadmin",
                       f"d exit {int(dest_first)} {int(dest_last)}")
        return {"node_id": node_id, "dest_first": dest_first,
                "dest_last": dest_last}

    def list_exits(self, node_id: int) -> list[dict]:
        """List current exit routes on a node by querying ipnadmin."""
        with self._lock:
            if node_id not in self.nodes:
                raise NotFoundError(f"Node {node_id} does not exist")
            container_name = self.nodes[node_id].container_name
        result = self._ion_exec(container_name, "ipnadmin", "l exit")
        exits = []
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if parse_exit_line(line):
                    exits.append({"raw": line.strip().lstrip(": ").strip()})
        return exits

    def get_neighbors(self, node_id: int) -> set[int]:
        """Return the set of directly connected node IDs."""
        with self._lock:
            return self._neighbors_locked(node_id)

    def _neighbors_locked(self, node_id: int) -> set[int]:
        neighbors = set()
        for link in self.links.values():
            if link.node_a == node_id:
                neighbors.add(link.node_b)
            elif link.node_b == node_id:
                neighbors.add(link.node_a)
        return neighbors

    def apply_raw_exit(self, node_id: int, raw_exit_line: str):
        """Re-apply a raw exit route string captured from ipnadmin."""
        with self._lock:
            if node_id not in self.nodes:
                raise NotFoundError(f"Node {node_id} does not exist")
            container_name = self.nodes[node_id].container_name
        parsed = parse_exit_line(raw_exit_line)
        if parsed:
            cmd = generate_add_exit_cmd(
                parsed["dest_first"], parsed["dest_last"], parsed["gateway_id"])
            self._ion_apply(container_name, "ipnadmin", cmd)

    # ── Route Propagation (internal) ─────────────────────────────

    def _compute_next_hops(self, src: int, adj: dict[int, set[int]]) -> dict[int, int]:
        """BFS next-hop table from src to every reachable node."""
        from collections import deque
        next_hop: dict[int, int] = {}
        visited = {src}
        queue = deque()
        for nb in adj.get(src, set()):
            visited.add(nb)
            next_hop[nb] = nb
            queue.append(nb)
        while queue:
            current = queue.popleft()
            for nb in adj.get(current, set()):
                if nb not in visited:
                    visited.add(nb)
                    next_hop[nb] = next_hop[current]
                    queue.append(nb)
        return next_hop

    def _reconcile_node_exits(self, src: int) -> list[str]:
        """Declaratively converge one node's exits to the BFS-desired set.

        Diffs the live exit set against the desired set and applies only the
        add/delete deltas, then re-lists and verifies. Errors are collected
        and returned rather than silently swallowed.
        """
        errors: list[str] = []
        with self._lock:
            if src not in self.nodes:
                return [f"N{src}: node does not exist"]
            node = self.nodes[src]
            all_ids = list(self.nodes.keys())
            adj: dict[int, set[int]] = {nid: set() for nid in all_ids}
            for link in self.links.values():
                adj[link.node_a].add(link.node_b)
                adj[link.node_b].add(link.node_a)
        if len(all_ids) < 2:
            return errors

        neighbors = adj[src]
        next_hop = self._compute_next_hops(src, adj)

        # Desired exits: every non-neighbor destination -> its first hop.
        desired: dict[tuple[int, int], int] = {}
        for dest, gateway in next_hop.items():
            if dest == src or dest in neighbors:
                continue
            desired[(dest, dest)] = gateway

        # Live exits, parsed via the shared helper.
        live: dict[tuple[int, int], int] = {}
        res = self._ion_exec(node.container_name, "ipnadmin", "l exit")
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                p = parse_exit_line(line)
                if p:
                    live[(p["dest_first"], p["dest_last"])] = p["gateway_id"]

        # Delete stale / wrong-gateway exits.
        for key, gw in live.items():
            if desired.get(key) != gw:
                if not self._ion_apply(node.container_name, "ipnadmin",
                                      f"d exit {key[0]} {key[1]}"):
                    errors.append(f"N{src}: failed to delete exit {key}")
        # Add missing exits.
        for (df, dl), gw in desired.items():
            if live.get((df, dl)) != gw:
                if not self._ion_apply(node.container_name, "ipnadmin",
                                      generate_add_exit_cmd(df, dl, gw)):
                    errors.append(f"N{src}: failed to add exit {df}->{gw}")

        # Verify convergence.
        verify: dict[tuple[int, int], int] = {}
        res2 = self._ion_exec(node.container_name, "ipnadmin", "l exit")
        if res2.returncode == 0:
            for line in res2.stdout.splitlines():
                p = parse_exit_line(line)
                if p:
                    verify[(p["dest_first"], p["dest_last"])] = p["gateway_id"]
        if verify != desired:
            errors.append(
                f"N{src}: exit set did not converge (want {desired}, got {verify})")
        return errors

    def repair_ion_config(self) -> list[str]:
        """Re-apply outducts and plans for all links (import safety net)."""
        errors: list[str] = []
        with self._lock:
            links = list(self.links.values())
            nodes = dict(self.nodes)

        for link in links:
            node_a = nodes.get(link.node_a)
            node_b = nodes.get(link.node_b)
            if not node_a or not node_b:
                continue
            cl = resolve_cl(link.convergence_layer)

            self._wait_for_ion_ready(node_a.container_name)
            self._wait_for_ion_ready(node_b.container_name)

            existing_a = self._list_plan_peers(node_a.container_name)
            existing_b = self._list_plan_peers(node_b.container_name)

            if not self._ion_apply(node_a.container_name, "bpadmin",
                                  generate_add_outduct_cmd(link.ip_b, cl.token)):
                errors.append(f"N{link.node_a}: outduct failed")
            if link.node_b not in existing_a:
                if not self._ion_apply(
                        node_a.container_name, "ipnadmin",
                        generate_add_plan_cmd(link.node_b, link.ip_b, cl.token)):
                    errors.append(f"N{link.node_a}: plan failed")

            if not self._ion_apply(node_b.container_name, "bpadmin",
                                  generate_add_outduct_cmd(link.ip_a, cl.token)):
                errors.append(f"N{link.node_b}: outduct failed")
            if link.node_a not in existing_b:
                if not self._ion_apply(
                        node_b.container_name, "ipnadmin",
                        generate_add_plan_cmd(link.node_a, link.ip_a, cl.token)):
                    errors.append(f"N{link.node_b}: plan failed")
        return errors

    # ── Helpers ───────────────────────────────────────────────────

    def _wait_for_ion_ready(self, container_name: str,
                            max_retries: int = 20, interval: float = 1.0) -> bool:
        """Poll ION until bpadmin responds. Returns True iff ready."""
        for _ in range(max_retries):
            try:
                result = self._exec(container_name, ["bpadmin"], stdin="l\n",
                                    timeout=3, ignore_errors=True)
                stdout = result.stdout.strip()
                if (result.returncode == 0 and stdout
                        and "not initialized" not in stdout):
                    return True
            except BackendTimeout:
                pass  # bpadmin hung — ION not ready yet
            time.sleep(interval)
        return False

    def _start_bpsink(self, container_name: str, node_id: int) -> None:
        """Launch bpsink in the background. node_id is an int (shell-safe)."""
        # A redirect + background fork genuinely needs a shell; node_id is an
        # int we control, so there is no untrusted value in this string.
        self._exec(
            container_name,
            ["bash", "-c",
             f"bpsink 'ipn:{int(node_id)}.1' >> /ion-runtime/bpsink.log 2>&1 &"],
            timeout=5, ignore_errors=True,
        )

    def _bpsink_running(self, container_name: str) -> bool:
        result = self._exec(container_name, ["pgrep", "-x", "bpsink"],
                           timeout=5, ignore_errors=True)
        return result.returncode == 0 and bool(result.stdout.strip())

    def _sweep_stale_config_dirs(self, node_id: int) -> None:
        for path in Path(tempfile.gettempdir()).glob(f"ion_n{node_id}_*"):
            shutil.rmtree(path, ignore_errors=True)

    def _next_node_id(self) -> int:
        """Find the next available node ID (caller holds the lock)."""
        if not self.nodes:
            return 1
        return max(self.nodes.keys()) + 1

    def _list_plan_peers(self, container_name: str) -> set[int]:
        """Query ipnadmin to list existing egress plan peer IDs."""
        peers = set()
        try:
            result = self._ion_exec(container_name, "ipnadmin", "l plan")
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    peer = parse_plan_line(line)
                    if peer is not None:
                        peers.add(peer)
        except Exception:
            pass
        return peers

    # ── Container command execution (single argv-based chokepoint) ──

    def _ion_exec(self, container_name: str, admin_tool: str, command: str):
        """Run an ION admin command by piping it to bpadmin/ipnadmin via stdin.

        The command is delivered on stdin (never assembled into a shell
        string), so quoting/injection is structurally impossible.
        """
        return self._exec(container_name, [admin_tool], stdin=command + "\n",
                          ignore_errors=True)

    def _ion_apply(self, container_name: str, admin_tool: str,
                   command: str) -> bool:
        """Run an ION admin command and classify the result.

        Returns True on success, treating benign 'duplicate/already exists'
        responses as success so a no-op never causes a false failure.
        """
        result = self._ion_exec(container_name, admin_tool, command)
        if result.returncode == 0:
            return True
        combined = (result.stdout or "") + (result.stderr or "")
        return bool(_BENIGN_ION.search(combined))

    def _exec(self, container_name: str, argv: list[str], stdin: str | None = None,
              timeout: int = 15, ignore_errors: bool = False
              ) -> subprocess.CompletedProcess:
        """The one container-exec primitive. Takes an argv list — never a shell
        string with interpolated values. Optional stdin is piped to the process.
        """
        cmd = ["docker", "exec"]
        if stdin is not None:
            cmd.append("-i")
        cmd.append(container_name)
        cmd.extend(argv)
        try:
            result = subprocess.run(
                cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise BackendTimeout(
                f"'{argv[0]}' on {container_name} timed out after {timeout}s") from e
        if result.returncode != 0 and not ignore_errors:
            raise IonExecError(
                f"'{argv[0]}' on {container_name} exited {result.returncode}",
                returncode=result.returncode, stderr=result.stderr)
        return result

    def _get_interface(self, container_name: str, ip: str) -> str | None:
        """Find the network interface name for a given IP inside a container."""
        result = self._exec(
            container_name, ["ip", "-o", "-4", "addr", "show"],
            ignore_errors=True)
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            # "3: eth1    inet 172.50.0.2/24 ..."
            if f" {ip}/" in line or line.rstrip().endswith(f" {ip}"):
                parts = line.split()
                if len(parts) >= 2:
                    return parts[1].rstrip(":")
        return None


# ── Shared ION admin output parsers ──────────────────────────────
#
# One parser per admin output shape, reused everywhere so a single fixture
# test pins the format and a format drift fails loudly in one place.

def parse_exit_line(line: str) -> dict | None:
    """Parse an ipnadmin 'l exit' line.

    Matches: "From 3 through 3, forward via ipn:2.0."
    Returns {dest_first, dest_last, gateway_id} or None.
    """
    m = re.search(
        r'From\s+(\d+)\s+through\s+(\d+),\s*forward\s+via\s+ipn:(\d+)\.0',
        line or "")
    if not m:
        return None
    return {"dest_first": int(m.group(1)), "dest_last": int(m.group(2)),
            "gateway_id": int(m.group(3))}


def parse_plan_line(line: str) -> int | None:
    """Parse an ipnadmin 'l plan' line and return the peer node id.

    ION renders plans like:
      "Egress plan for node number 2: ... via outduct ... tcp/172.50.0.3:4556"
      "To node 2 via tcp/..."
      "2 tcp/172.50.0.3:4556"
    Returns the leading/explicit node number, never an IP octet.
    """
    text = (line or "").strip()
    if not text:
        return None
    m = re.search(r'node\s+number\s+(\d+)', text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r'[Tt]o\s+node\s+(\d+)', text)
    if m:
        return int(m.group(1))
    m = re.match(r'(\d+)\s+(?:tcp|udp|stcp|ltp)/', text)
    if m:
        return int(m.group(1))
    return None
