"""Standalone scenario export — generates a self-contained .tar.gz archive.

Produces a directory structure identical to static_route_periodic_bundles:
  scenario_name/
  ├── Dockerfile
  ├── README.md
  ├── compose.yml
  ├── configs/n{id}/node.ionconfig  +  node.rc
  ├── ion-entrypoint.sh
  ├── start.sh
  ├── stop.sh
  ├── send.sh
  └── recv.sh
"""

from __future__ import annotations

import io
import re
import tarfile
import textwrap
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.services.docker_manager import DockerManager

# Subnet range for exported scenarios (10.x.x.x avoids Docker's 172.x auto-allocation)
_EXPORT_SUBNET_PREFIX = "10.40"


# ── Helpers ──────────────────────────────────────────────────────


def _sorted_links(links: list[dict]) -> list[dict]:
    """Return links sorted by (node_a, node_b) for deterministic ordering."""
    return sorted(links, key=lambda l: (l["node_a"], l["node_b"]))


def _subnet_for_index(idx: int) -> tuple[str, str, str]:
    """Return (subnet_cidr, ip_a, ip_b) for a link index."""
    subnet = f"{_EXPORT_SUBNET_PREFIX}.{idx}.0/24"
    ip_a = f"{_EXPORT_SUBNET_PREFIX}.{idx}.2"
    ip_b = f"{_EXPORT_SUBNET_PREFIX}.{idx}.3"
    return subnet, ip_a, ip_b


# ── File Generators ──────────────────────────────────────────────


def generate_dockerfile() -> str:
    return textwrap.dedent("""\
        FROM ubuntu:22.04

        ENV DEBIAN_FRONTEND=noninteractive

        # Install build dependencies and runtime tools
        RUN apt-get update && apt-get install -y \\
            build-essential git automake autoconf libtool \\
            iproute2 bash iputils-ping procps \\
            && rm -rf /var/lib/apt/lists/*

        # Clone and build ION-DTN from source
        RUN git clone https://github.com/nasa-jpl/ION-DTN.git /opt/ion && \\
            cd /opt/ion && \\
            autoreconf -fi && \\
            ./configure && \\
            make -j$(nproc) && \\
            make install && \\
            ldconfig && \\
            rm -rf /opt/ion

        # Default working directory for ION runtime files (ion.log, SDR, etc.)
        WORKDIR /ion-runtime

        # Keep container alive
        CMD ["tail", "-f", "/dev/null"]
    """)


def generate_entrypoint() -> str:
    return textwrap.dedent("""\
        #!/bin/bash
        # Generic ION entrypoint for exported scenario nodes.
        # Expects:
        #   NODE_NUM   — ION node number (e.g., 1, 2, 3)
        #   /ion-config/node.rc       — combined ION config
        #   /ion-config/node.ionconfig — ION SDR config

        NODE_NUM=${NODE_NUM:-1}
        CONFIG_DIR="/ion-config"

        echo "=== ION-DTN Node ${NODE_NUM} ==="

        cd /ion-runtime

        # Copy ionconfig to runtime directory (ionadmin expects it in cwd)
        if [ -f "${CONFIG_DIR}/node.ionconfig" ]; then
            cp "${CONFIG_DIR}/node.ionconfig" .
        fi

        # Start ION if config exists
        if [ -f "${CONFIG_DIR}/node.rc" ]; then
            echo "Starting ION from ${CONFIG_DIR}/node.rc ..."
            ionstart -I "${CONFIG_DIR}/node.rc"
        else
            echo "No node.rc found — ION not started (awaiting configuration)"
        fi

        echo "OK: Node ${NODE_NUM} ready"

        # Keep container alive
        exec tail -f /dev/null
    """)


def generate_ionconfig() -> str:
    return textwrap.dedent("""\
        ## ION SDR configuration
        ## See man ionconfig for details

        # SDR working memory size (bytes) - 5MB is sufficient for this simple demo
        sdrWmSize 5000000

        # Use default SDR configuration (in-memory)
        configFlags 1
    """)


def generate_node_rc(
    node_id: int,
    neighbors: list[dict],
    exits: list[dict],
) -> str:
    """Generate a combined node.rc with baked-in exit routes.

    Args:
        node_id: IPN node number
        neighbors: List of dicts with keys peer_id, peer_ip
        exits: List of dicts with keys dest_first, dest_last, gateway_id
    """
    lines = []

    # ionadmin
    lines.append(f"## Combined ION-DTN configuration for Node {node_id} (ipn:{node_id}.*)")
    lines.append(f"## Exported from DTN-Manager")
    lines.append(f"")
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
    for nb in neighbors:
        lines.append(f"a outduct tcp {nb['peer_ip']}:4556 tcpclo")
    lines.append(f"s")
    lines.append(f"## end bpadmin")
    lines.append(f"")

    # ipnadmin — egress plans + exit routes
    lines.append(f"## begin ipnadmin")
    for nb in neighbors:
        lines.append(f"a plan {nb['peer_id']} tcp/{nb['peer_ip']}:4556")
    for ex in exits:
        lines.append(
            f"a exit {ex['dest_first']} {ex['dest_last']} ipn:{ex['gateway_id']}.0"
        )
    lines.append(f"## end ipnadmin")

    return "\n".join(lines) + "\n"


def generate_compose_yml(
    scenario_name: str,
    nodes: list[dict],
    links: list[dict],
    link_map: dict,
) -> str:
    """Generate a docker compose file.

    Args:
        scenario_name: Name of the scenario (used as compose project name)
        nodes: List of node dicts with id
        links: Sorted list of link dicts with node_a, node_b
        link_map: {link_index: (subnet, ip_a, ip_b, network_name)} for each link
    """
    # Use a short prefix for container names
    prefix = _safe_prefix(scenario_name)

    yml_lines = [
        f"name: {scenario_name}",
        "services:",
    ]

    # Build per-node network assignments
    node_networks: dict[int, list[tuple[str, str]]] = {n["id"]: [] for n in nodes}
    for idx, link in enumerate(links):
        subnet, ip_a, ip_b, net_name = link_map[idx]
        node_networks[link["node_a"]].append((net_name, ip_a))
        node_networks[link["node_b"]].append((net_name, ip_b))

    for node in sorted(nodes, key=lambda n: n["id"]):
        nid = node["id"]
        yml_lines.append(f"  n{nid}:")
        yml_lines.append(f"    container_name: {prefix}_n{nid}")
        yml_lines.append(f"    hostname: n{nid}")
        yml_lines.append(f"    build: .")
        yml_lines.append(f"    cap_add:")
        yml_lines.append(f"    - NET_ADMIN")
        yml_lines.append(f"    - IPC_LOCK")
        yml_lines.append(f"    privileged: 'true'")
        yml_lines.append(f"    init: true")
        yml_lines.append(f"    shm_size: '256m'")
        yml_lines.append(f"    volumes:")
        yml_lines.append(f"    - ./compose.yml:/compose.yml:ro")
        yml_lines.append(f"    - ./configs/n{nid}:/ion-config:ro")
        yml_lines.append(f"    - ./ion-entrypoint.sh:/ion-entrypoint.sh:ro")
        yml_lines.append(f"    environment:")
        yml_lines.append(f"    - NODE_ID={nid}")
        yml_lines.append(f"    - NODE_NUM={nid}")
        yml_lines.append(f"    - TYPE=Host")

        nets = node_networks.get(nid, [])
        if nets:
            yml_lines.append(f"    networks:")
            for net_name, ip in nets:
                yml_lines.append(f"      {net_name}:")
                yml_lines.append(f"        ipv4_address: {ip}")

        yml_lines.append(f"    entrypoint: 'bash /ion-entrypoint.sh'")

    # Networks section
    if links:
        yml_lines.append("networks:")
        for idx, link in enumerate(links):
            subnet, ip_a, ip_b, net_name = link_map[idx]
            iface_prefix = f"n{link['node_a']}_n{link['node_b']}_"
            yml_lines.append(f"  {net_name}:")
            yml_lines.append(f"    driver: bridge")
            yml_lines.append(f"    ipam:")
            yml_lines.append(f"      config:")
            yml_lines.append(f"      - subnet: {subnet}")
            yml_lines.append(f"    driver_opts:")
            yml_lines.append(f"      com.docker.network.container_iface_prefix: {iface_prefix}")

    return "\n".join(yml_lines) + "\n"


def generate_start_script(scenario_name: str, nodes: list[dict]) -> str:
    prefix = _safe_prefix(scenario_name)
    node_count = len(nodes)
    container_list = " ".join(f"{prefix}_n{n['id']}" for n in sorted(nodes, key=lambda n: n["id"]))

    return textwrap.dedent(f"""\
        #!/bin/bash
        # start.sh — Deploy the {scenario_name} scenario

        SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
        cd "$SCRIPT_DIR"

        echo "=== Deploying {scenario_name} Scenario ==="
        echo ""

        # ── Pre-deploy cleanup ──────────────────────────────────────
        # Remove any leftover containers and networks from previous
        # scenario runs that may occupy the 10.40.x.0/24 subnets.
        echo "Cleaning up previous scenario resources..."

        # Stop and remove containers from any old scenario
        OLD_CONTAINERS=$(docker ps -a --filter "name=scenario_" --format '{{{{.Names}}}}' 2>/dev/null)
        if [ -n "$OLD_CONTAINERS" ]; then
            echo "$OLD_CONTAINERS" | xargs -r docker rm -f >/dev/null 2>&1
            echo "  Removed old containers"
        fi

        # Remove networks that use the 10.40.x.0/24 subnet range
        for net in $(docker network ls --filter "driver=bridge" --format '{{{{.Name}}}}' 2>/dev/null); do
            SUBNET=$(docker network inspect "$net" --format '{{{{range .IPAM.Config}}}}{{{{.Subnet}}}}{{{{end}}}}' 2>/dev/null)
            if echo "$SUBNET" | grep -q "^10\\.40\\." 2>/dev/null; then
                docker network rm "$net" >/dev/null 2>&1 && echo "  Removed network $net ($SUBNET)"
            fi
        done

        echo ""

        docker compose up --build -d

        echo ""
        echo "Waiting for ION nodes to initialize..."
        sleep 3

        RUNNING=$(docker compose ps --status running -q | wc -l)
        if [ "$RUNNING" -eq {node_count} ]; then
            echo "✔ All {node_count} nodes are running"
        else
            echo "✘ Expected {node_count} running containers, found $RUNNING"
            docker compose ps
            exit 1
        fi

        for node in {container_list}; do
            if docker exec "$node" bpadmin <<< 'l plan' &>/dev/null; then
                echo "  ✔ $node — ION responding"
            else
                echo "  ✘ $node — ION not responding"
            fi
        done

        # Start bpsink on each node for bundle reception monitoring.
        # Must use 'docker exec' (not entrypoint) so stdout redirects to the
        # log file reliably — the entrypoint pty overrides bash redirects.
        echo ""
        echo "Starting bundle monitors..."
        for node in {container_list}; do
            NODE_NUM=$(docker exec "$node" printenv NODE_NUM 2>/dev/null)
            docker exec "$node" bash -c "bpsink ipn:${{NODE_NUM}}.1 >> /ion-runtime/bpsink.log 2>&1 &"
            echo "  ✔ $node — bpsink on ipn:${{NODE_NUM}}.1"
        done

        echo ""
        echo "=== Scenario Ready ==="
        echo "Send bundles:   ./send.sh <from> <to>         (1/sec, infinite)"
        echo "Send 10 only:   ./send.sh <from> <to> 10"
        echo "Receive:        ./recv.sh <node>"
        echo "Stop scenario:  ./stop.sh"
    """)


def generate_stop_script(scenario_name: str) -> str:
    return textwrap.dedent(f"""\
        #!/bin/bash
        # stop.sh — Tear down the {scenario_name} scenario

        SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
        cd "$SCRIPT_DIR"

        echo "=== Stopping {scenario_name} Scenario ==="

        docker compose down --volumes --remove-orphans

        echo "✔ Scenario stopped (containers, networks, and volumes removed)"
    """)


def generate_send_script(scenario_name: str) -> str:
    prefix = _safe_prefix(scenario_name)
    return textwrap.dedent(f"""\
        #!/bin/bash
        # send.sh — Send bundles periodically (1 per second by default)
        # Usage: ./send.sh <from_node> <to_node> [count] [interval_sec]
        #
        # Examples:
        #   ./send.sh 1 3              # Send indefinitely, 1 bundle/sec
        #   ./send.sh 1 3 10           # Send 10 bundles, 1 per second
        #   ./send.sh 1 3 20 2         # Send 20 bundles, 1 every 2 seconds
        #   ./send.sh 1 3 0 0.5        # Send indefinitely, 2 per second
        #
        # Press Ctrl+C to stop sending.

        FROM_NODE="$1"
        TO_NODE="$2"
        COUNT="${{3:-0}}"          # 0 = infinite
        INTERVAL="${{4:-1}}"       # seconds between bundles

        if [ -z "$FROM_NODE" ] || [ -z "$TO_NODE" ]; then
            echo "Usage: $0 <from_node> <to_node> [count] [interval_sec]"
            echo ""
            echo "  count         Number of bundles (0 = infinite, default: 0)"
            echo "  interval_sec  Seconds between bundles (default: 1)"
            echo ""
            echo "Examples:"
            echo "  $0 1 3              # Infinite, 1/sec"
            echo "  $0 1 3 10           # 10 bundles, 1/sec"
            echo "  $0 1 3 20 2         # 20 bundles, every 2 sec"
            echo "  $0 1 3 0 0.5        # Infinite, 2/sec"
            exit 1
        fi

        SRC_CONTAINER="{prefix}_n${{FROM_NODE}}"

        # Verify container exists
        if ! docker inspect "$SRC_CONTAINER" &>/dev/null; then
            echo "✘ Container $SRC_CONTAINER not found. Is the scenario running?"
            exit 1
        fi

        echo "=== Periodic Bundle Sender ==="
        echo "  From:     ipn:${{FROM_NODE}}.2 ($SRC_CONTAINER)"
        echo "  To:       ipn:${{TO_NODE}}.1"
        echo "  Interval: ${{INTERVAL}}s"
        if [ "$COUNT" -eq 0 ] 2>/dev/null; then
            echo "  Count:    infinite (Ctrl+C to stop)"
        else
            echo "  Count:    $COUNT"
        fi
        echo ""

        SENT=0

        # Trap Ctrl+C to print summary
        trap 'echo ""; echo "=== Stopped: $SENT bundles sent ==="; exit 0' INT

        while true; do
            SENT=$((SENT + 1))
            TIMESTAMP=$(date +%H:%M:%S)
            docker exec "$SRC_CONTAINER" bpsource "ipn:${{TO_NODE}}.1" "bundle #${{SENT}} at ${{TIMESTAMP}}" 2>/dev/null
            echo "  [$TIMESTAMP] Sent bundle #$SENT"

            # Check if we've reached the count
            if [ "$COUNT" -gt 0 ] 2>/dev/null && [ "$SENT" -ge "$COUNT" ]; then
                echo ""
                echo "=== Done: $SENT bundles sent ==="
                break
            fi

            sleep "$INTERVAL"
        done
    """)


def generate_recv_script(scenario_name: str) -> str:
    prefix = _safe_prefix(scenario_name)
    return textwrap.dedent(f"""\
        #!/bin/bash
        # recv.sh — Watch received bundles on a node in real time
        # Usage: ./recv.sh <node>
        #
        # Prints each received bundle payload to stdout in real time.
        # Can run simultaneously with plot.sh (both read the same log).
        # Press Ctrl+C to stop.
        #
        # Examples:
        #   ./recv.sh 3       # Watch bundles arriving on N3
        #   ./recv.sh 2       # Watch bundles arriving on N2

        NODE="$1"

        if [ -z "$NODE" ]; then
            echo "Usage: $0 <node>"
            echo ""
            echo "Examples:"
            echo "  $0 3       # Watch bundles arriving on N3 (ipn:3.1)"
            echo "  $0 2       # Watch bundles arriving on N2 (ipn:2.1)"
            exit 1
        fi

        CONTAINER="{prefix}_n${{NODE}}"

        if ! docker inspect "$CONTAINER" &>/dev/null; then
            echo "✘ Container $CONTAINER not found. Is the scenario running?"
            exit 1
        fi

        echo "=== Watching bundles on ipn:${{NODE}}.1 ($CONTAINER) ==="
        echo "    Press Ctrl+C to stop."
        echo ""

        # Tail the bpsink log file. The background bpsink (started by start.sh)
        # keeps running, so plot.sh continues to work simultaneously.
        docker exec "$CONTAINER" tail -n 0 -f /ion-runtime/bpsink.log
    """)


def generate_readme(
    scenario_name: str,
    nodes: list[dict],
    links: list[dict],
    description: str = "",
) -> str:
    node_ids = sorted(n["id"] for n in nodes)
    node_list = ", ".join(f"N{nid}" for nid in node_ids)

    # Build a simple topology diagram
    topo_lines = []
    for link in _sorted_links(links):
        topo_lines.append(f"  [N{link['node_a']}] ↔ [N{link['node_b']}]")
    topo_str = "\n".join(topo_lines) if topo_lines else "  (no links)"

    return textwrap.dedent(f"""\
        # Scenario: {scenario_name}

        {description or "Exported from DTN-Manager."}

        ## Topology

        **Nodes:** {node_list}

        ```
        {topo_str}
        ```

        ## Quick Start

        ### 1. Deploy

        ```bash
        cd {scenario_name}
        ./start.sh
        ```

        ### 2. Start Receiver (Terminal 1)

        ```bash
        ./recv.sh {node_ids[-1] if node_ids else 1}
        ```

        ### 3. Start Sender (Terminal 2)

        ```bash
        # Send indefinitely, 1 bundle/sec (Ctrl+C to stop)
        ./send.sh {node_ids[0] if node_ids else 1} {node_ids[-1] if node_ids else 1}

        # Send exactly 10 bundles
        ./send.sh {node_ids[0] if node_ids else 1} {node_ids[-1] if node_ids else 1} 10
        ```

        ### 4. Tear Down

        ```bash
        ./stop.sh
        ```

        ## Scripts

        | Script | Usage | Description |
        |--------|-------|-------------|
        | `start.sh` | `./start.sh` | Build and deploy all {len(nodes)} nodes |
        | `send.sh` | `./send.sh <from> <to> [count] [interval]` | Send bundles periodically |
        | `recv.sh` | `./recv.sh <node>` | Print received bundles in real time |
        | `plot.sh` | `./plot.sh [node]` | Real-time bundle arrival plot (matplotlib) |
        | `stop.sh` | `./stop.sh` | Tear down everything |

        ---
        *Exported at {datetime.now().strftime("%Y-%m-%d %H:%M:%S")} by DTN-Manager*
    """)


def generate_plot_script() -> str:
    return textwrap.dedent("""\
        #!/bin/bash
        # plot.sh — Launch the real-time bundle arrival plotter
        # Usage: ./plot.sh [node_number]
        #
        # Examples:
        #   ./plot.sh        # Monitor N3 (default destination)
        #   ./plot.sh 2      # Monitor N2

        SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
        NODE="${1:-3}"

        python3 "$SCRIPT_DIR/plot_bundles.py" --node "$NODE"
    """)


def generate_plot_bundles_py(scenario_name: str) -> str:
    prefix = _safe_prefix(scenario_name)
    return textwrap.dedent(f'''\
        #!/usr/bin/env python3
        """
        Real-time bundle arrival plotter for the {scenario_name} scenario.

        Monitors bpsink output on the destination node via `docker logs` and plots
        the bundle sequence number as bundles arrive. Gaps in the plot show when
        the link was disrupted.

        Usage:
            python3 plot_bundles.py                    # Monitor N3 (default)
            python3 plot_bundles.py --node 2           # Monitor N2
            python3 plot_bundles.py --container name   # Specify container directly
        """

        import subprocess
        import re
        import threading
        import time
        import argparse
        import matplotlib.pyplot as plt
        import matplotlib.animation as animation
        from datetime import datetime


        def parse_bundle_number(line: str) -> int | None:
            """Extract bundle sequence number from bpsink output.

            bpsource sends payloads like: bundle #42 at 15:30:07
            bpsink prints them as: 'bundle #42 at 15:30:07'
            """
            match = re.search(r"bundle #(\\d+)", line)
            if match:
                return int(match.group(1))
            return None


        class BundleMonitor:
            """Follows docker logs and records bundle arrivals."""

            def __init__(self, container: str):
                self.container = container
                self.arrivals = []  # list of (timestamp, bundle_number)
                self.lock = threading.Lock()
                self._stop = threading.Event()
                self._thread = None

            def start(self):
                self._thread = threading.Thread(target=self._follow_logs, daemon=True)
                self._thread.start()

            def stop(self):
                self._stop.set()

            def get_data(self):
                with self.lock:
                    return list(self.arrivals)

            def _follow_logs(self):
                """Follow bpsink.log in real-time and parse bundle arrivals."""
                try:
                    proc = subprocess.Popen(
                        ["docker", "exec", self.container,
                         "tail", "-n", "0", "-f", "/ion-runtime/bpsink.log"],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, bufsize=1
                    )
                    while not self._stop.is_set():
                        line = proc.stdout.readline()
                        if not line:
                            if proc.poll() is not None:
                                break
                            continue

                        bundle_num = parse_bundle_number(line)
                        if bundle_num is not None:
                            now = time.time()
                            with self.lock:
                                self.arrivals.append((now, bundle_num))

                except Exception as e:
                    print(f"Monitor error: {{e}}")


        def main():
            parser = argparse.ArgumentParser(
                description="Real-time bundle arrival plotter for {scenario_name}"
            )
            parser.add_argument("--node", type=int, default=3,
                                help="Node number to monitor (default: 3)")
            parser.add_argument("--container", type=str, default=None,
                                help="Container name (overrides --node)")
            parser.add_argument("--window", type=int, default=120,
                                help="Time window in seconds (default: 120)")
            args = parser.parse_args()

            container = args.container or f"{prefix}_n{{args.node}}"

            # Verify container is running
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{{{.State.Running}}}}", container],
                capture_output=True, text=True
            )
            if result.stdout.strip() != "true":
                print(f"✘ Container {{container}} is not running. Start the scenario first.")
                return

            print(f"=== Monitoring bundle arrivals on {{container}} ===")
            print(f"    Time window: {{args.window}}s")
            print(f"    Close the plot window or press Ctrl+C to stop.")
            print()

            # Start monitoring
            monitor = BundleMonitor(container)
            monitor.start()
            start_time = time.time()

            # Setup plot
            plt.style.use("dark_background")
            fig, ax = plt.subplots(figsize=(14, 6))
            fig.patch.set_facecolor("#1a1a2e")
            ax.set_facecolor("#16213e")

            scatter = ax.scatter([], [], c="#4ECDC4", s=20, alpha=0.8, zorder=3)
            line, = ax.plot([], [], color="#4ECDC4", linewidth=1.5, alpha=0.4, zorder=2)

            ax.set_xlabel("Elapsed Time (s)", fontsize=12, color="#e0e0e0")
            ax.set_ylabel("Bundle #", fontsize=12, color="#e0e0e0")
            ax.set_title(f"Bundle Arrivals — {{container}}", fontsize=16,
                         fontweight="bold", color="#ffffff")
            ax.grid(True, alpha=0.15, color="#4ECDC4")
            ax.tick_params(colors="#e0e0e0")

            # Status text
            status_text = ax.text(0.02, 0.95, "", transform=ax.transAxes,
                                  fontsize=10, color="#A8E6CF", verticalalignment="top",
                                  fontfamily="monospace",
                                  bbox=dict(boxstyle="round,pad=0.3",
                                            facecolor="#16213e", edgecolor="#4ECDC4",
                                            alpha=0.8))

            def update(frame):
                data = monitor.get_data()
                if not data:
                    status_text.set_text("Waiting for bundles...")
                    return scatter, line, status_text

                times = [t - start_time for t, _ in data]
                bundles = [b for _, b in data]

                # Update scatter and line
                scatter.set_offsets(list(zip(times, bundles)))
                line.set_data(times, bundles)

                # Adjust axes — always start from 0
                t_max = max(times[-1] + 5, 30)
                ax.set_xlim(0, t_max)
                ax.set_ylim(0, max(bundles) + 5)

                # Calculate stats
                total = len(data)
                latest = bundles[-1]
                elapsed = times[-1]

                # Compute recent rate (last 10 seconds)
                recent_cutoff = times[-1] - 10
                recent = [b for t, b in zip(times, bundles) if t > recent_cutoff]
                rate = len(recent) / min(10, elapsed) if elapsed > 0 else 0

                status_text.set_text(
                    f"Total: {{total}}  |  Latest: #{{latest}}  |  "
                    f"Rate: {{rate:.1f}} bnd/s  |  Elapsed: {{elapsed:.0f}}s"
                )

                return scatter, line, status_text

            ani = animation.FuncAnimation(
                fig, update, interval=500, cache_frame_data=False
            )

            plt.tight_layout()

            try:
                plt.show()
            finally:
                monitor.stop()


        if __name__ == "__main__":
            main()
    ''')


def build_export_archive(
    manager: DockerManager,
    scenario_name: str,
    description: str = "",
) -> bytes:
    """Build a .tar.gz archive containing the full standalone scenario.

    Returns the archive as bytes ready to stream to the client.
    """
    nodes_data = []
    for node in manager.list_nodes():
        nodes_data.append({
            "id": node.id,
            "name": node.name,
        })

    links_raw = []
    for link in manager.list_links():
        links_raw.append({
            "node_a": link.node_a,
            "node_b": link.node_b,
        })
    links_data = _sorted_links(links_raw)

    # Assign deterministic subnets and build link map
    # link_map: {idx: (subnet, ip_a, ip_b, network_name)}
    link_map: dict[int, tuple[str, str, str, str]] = {}
    for idx, link in enumerate(links_data):
        subnet, ip_a, ip_b = _subnet_for_index(idx)
        net_name = f"n{link['node_a']}_n{link['node_b']}"
        link_map[idx] = (subnet, ip_a, ip_b, net_name)

    # Build per-node neighbor and IP data for the exported subnet assignments
    node_neighbors: dict[int, list[dict]] = {n["id"]: [] for n in nodes_data}
    for idx, link in enumerate(links_data):
        subnet, ip_a, ip_b, net_name = link_map[idx]
        node_neighbors[link["node_a"]].append({
            "peer_id": link["node_b"],
            "peer_ip": ip_b,
        })
        node_neighbors[link["node_b"]].append({
            "peer_id": link["node_a"],
            "peer_ip": ip_a,
        })

    # Collect live exit routes from each node
    node_exits: dict[int, list[dict]] = {}
    for node in nodes_data:
        nid = node["id"]
        exits = []
        try:
            raw_exits = manager.list_exits(nid)
            neighbors_set = manager.get_neighbors(nid)
            for ex in raw_exits:
                # Parse: "From 3 through 3, forward via ipn:2.0."
                m = re.search(
                    r'From\s+(\d+)\s+through\s+(\d+),\s*forward\s+via\s+ipn:(\d+)\.0',
                    ex.get("raw", ""),
                )
                if m:
                    exits.append({
                        "dest_first": int(m.group(1)),
                        "dest_last": int(m.group(2)),
                        "gateway_id": int(m.group(3)),
                    })
        except Exception:
            pass
        node_exits[nid] = exits

    # Build all files
    files: dict[str, str] = {}

    files["Dockerfile"] = generate_dockerfile()
    files["ion-entrypoint.sh"] = generate_entrypoint()
    files["compose.yml"] = generate_compose_yml(
        scenario_name, nodes_data, links_data, link_map
    )
    files["start.sh"] = generate_start_script(scenario_name, nodes_data)
    files["stop.sh"] = generate_stop_script(scenario_name)
    files["send.sh"] = generate_send_script(scenario_name)
    files["recv.sh"] = generate_recv_script(scenario_name)
    files["plot.sh"] = generate_plot_script()
    files["plot_bundles.py"] = generate_plot_bundles_py(scenario_name)
    files["README.md"] = generate_readme(
        scenario_name, nodes_data, links_data, description
    )

    # Per-node configs
    for node in nodes_data:
        nid = node["id"]
        files[f"configs/n{nid}/node.ionconfig"] = generate_ionconfig()
        files[f"configs/n{nid}/node.rc"] = generate_node_rc(
            nid,
            neighbors=node_neighbors.get(nid, []),
            exits=node_exits.get(nid, []),
        )

    # Pack into tar.gz
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for filepath, content in sorted(files.items()):
            full_path = f"{scenario_name}/{filepath}"
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=full_path)
            info.size = len(data)
            # Make shell and python scripts executable (0o755), others readable (0o644)
            if filepath.endswith(".sh") or filepath.endswith(".py"):
                info.mtime = int(datetime.now().timestamp())
                info.mode = 0o755
            else:
                info.mtime = int(datetime.now().timestamp())
                info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))

    return buf.getvalue()


def _safe_prefix(name: str) -> str:
    """Convert a scenario name to a safe container prefix."""
    # Replace non-alphanumeric chars with underscores, collapse multiples
    safe = re.sub(r'[^a-zA-Z0-9]', '_', name)
    safe = re.sub(r'_+', '_', safe).strip('_').lower()
    return safe or "scenario"
