# DTN-Manager — Development Plan

## Goal

Build a web application that provides a **visual, interactive control plane** for ION-DTN networks. Users can dynamically create/destroy nodes and links, send traffic, monitor bundle statistics, and export scenarios as standalone Docker Compose packages — all backed by real Docker containers running ION-DTN.

---

## Architecture (Implemented)

```
┌──────────────────────────────────────────────────┐
│                   Browser (UI)                    │
│  ┌────────────────────────────────────────────┐  │
│  │  Interactive Graph  │  Controls & Stats     │  │
│  │  (Cytoscape.js)     │  - Create node        │  │
│  │                     │  - Create link         │  │
│  │                     │  - Send traffic        │  │
│  │                     │  - Export scenario     │  │
│  └────────────────────────────────────────────┘  │
│                    ▲ WebSocket / REST              │
└────────────────────┼─────────────────────────────┘
                     │
┌────────────────────┼─────────────────────────────┐
│              FastAPI Backend                       │
│  ┌─────────────┐  ┌──────────────┐               │
│  │ REST API     │  │ WebSocket    │               │
│  │ /nodes       │  │ /ws/topology │               │
│  │ /links       │  │ /ws/logs     │               │
│  │ /traffic     │  │              │               │
│  │ /scenarios   │  │              │               │
│  └──────┬──────┘  └──────┬───────┘               │
│         │                │                        │
│  ┌──────┴────────────────┴───────┐               │
│  │     Docker Engine API          │               │
│  │  - Container lifecycle         │               │
│  │  - Network management          │               │
│  │  - Exec (ION admin commands)   │               │
│  │  - bpsink log monitoring       │               │
│  └────────────────────────────────┘               │
│  ┌────────────────────────────────┐               │
│  │     Scenario Export Engine     │               │
│  │  - compose.yml generation      │               │
│  │  - start/stop/send/recv/plot   │               │
│  │  - ION config bundling         │               │
│  └────────────────────────────────┘               │
└───────────────────────────────────────────────────┘
                     │
┌────────────────────┼─────────────────────────────┐
│           Docker Engine                            │
│  ┌─────┐  ┌─────┐  ┌─────┐  ┌─────┐             │
│  │ N1  │  │ N2  │  │ N3  │  │ N4  │  ...         │
│  │ ION │  │ ION │  │ ION │  │ ION │              │
│  └──┬──┘  └──┬──┘  └──┬──┘  └──┬──┘             │
│     └───net───┴───net───┘       │                 │
│              └────────net───────┘                  │
└───────────────────────────────────────────────────┘
```

---

## Technology Stack (Decided)

| Layer | Technology | Status |
|-------|-----------|--------|
| **Backend** | Python + FastAPI | ✅ Implemented |
| **Frontend** | Plain HTML/JS + CSS | ✅ Implemented |
| **Graph** | Cytoscape.js | ✅ Implemented |
| **Real-time** | WebSocket | ✅ Implemented |
| **Docker SDK** | docker-py + subprocess | ✅ Implemented |
| **Plot** | matplotlib | ✅ Implemented |

---

## Project Structure (Actual)

```
dtn_manager/
├── backend/
│   ├── main.py                 # FastAPI app entry point
│   ├── api/
│   │   ├── nodes.py            # Node CRUD endpoints
│   │   ├── links.py            # Link management endpoints
│   │   ├── traffic.py          # Bundle traffic endpoints
│   │   ├── scenarios.py        # Scenario save/load/export
│   │   └── websocket.py        # WebSocket handlers
│   ├── services/
│   │   ├── docker_manager.py   # Docker SDK wrapper
│   │   ├── ion_config.py       # ION config generator
│   │   ├── subnet_pool.py      # Subnet allocator (10.40.x.0/24)
│   │   └── scenario_export.py  # Standalone scenario exporter
│   ├── models.py               # Data models (Node, Link, etc.)
│   └── requirements.txt
├── frontend/
│   ├── index.html              # Main page
│   ├── css/
│   │   └── style.css           # Dark theme
│   └── js/
│       └── app.js              # All frontend logic
├── docker/
│   ├── Dockerfile.ion          # Pre-built ION base image
│   └── entrypoint.sh           # Generic ION entrypoint
├── start.sh                    # Start manager server
├── stop.sh                     # Stop manager + cleanup
└── README.md
```

---

## Key Implementation Details

### 1. Bundle Monitoring (bpsink)

**Problem solved:** Docker allocates a pty for the entrypoint process. Bash `>>` file redirects get overridden by the pty, so `bpsink` output never reaches log files when started from the entrypoint.

**Solution:** Start `bpsink` via `docker exec` (which has no pty) **after** the container is up, from `start.sh` (exported scenarios) or `docker_manager.py` (live manager).

```
start.sh / docker_manager.py
  └── docker exec <container> bash -c "bpsink 'ipn:N.1' >> /ion-runtime/bpsink.log 2>&1 &"

/ion-runtime/bpsink.log  ← all readers share this file
  ├── recv.sh       → tail -f (terminal)
  ├── plot.sh       → tail -f (matplotlib)
  └── manager API   → tail -n (stats)
```

### 2. Docker Network Management

Each link = a dedicated Docker bridge network with a /24 subnet from the `10.40.x.0/16` pool.

**Subnet conflict resolution:** Exported scenarios use `10.40.0.0/24` and `10.40.1.0/24`. If old scenario networks linger, new deployments fail with "Pool overlaps." The exported `start.sh` now auto-cleans:
1. Removes all `scenario_*` containers
2. Scans Docker networks for `10.40.x.0/24` subnets and removes them

### 3. ION Hot-Reconfiguration

When a link is created between existing running nodes, the backend dynamically adds outducts and egress plans to live ION instances via `docker exec`:

```bash
docker exec <container> bpadmin <<< 'a outduct tcp <peer_ip>:4556 tcpclo'
docker exec <container> ipnadmin <<< 'a plan <peer_node> tcp/<peer_ip>:4556'
```

### 4. Scenario Export

The scenario export engine generates a complete standalone package:

- `compose.yml` with per-node services, networks, and volume mounts
- `ion-entrypoint.sh` — ION initialization (no bpsink, that's in start.sh)
- `start.sh` — pre-deploy cleanup + `docker compose up` + bpsink startup
- `stop.sh` — `docker compose down`
- `send.sh` — `bpsource` via `docker exec`
- `recv.sh` — `tail -f bpsink.log` via `docker exec` (concurrent with plot)
- `plot.sh` / `plot_bundles.py` — real-time matplotlib scatter plot
- `README.md` — scenario-specific documentation

### 5. Bundle Statistics

Stats are collected from ION's `bpstats` command (triggered via `docker exec`). The backend parses `ion.log` for the latest stats line:

| Stat | Meaning |
|------|---------|
| `src` | Bundles sourced (originated) at this node |
| `fwd` | Bundles forwarded (routing decisions made) |
| `xmt` | Bundles transmitted over the convergence layer |
| `rcv` | Bundles received from the convergence layer |
| `dlv` | Bundles delivered to a local endpoint |
| `exp` | Bundles expired (TTL exceeded) |

---

## Build Status

### Phase 1 — Backend Core ✅
- [x] Pre-build ION Docker image
- [x] Docker manager service (create/delete containers, networks)
- [x] ION config generator (dynamic `node.rc` creation)
- [x] Subnet pool allocator (10.40.x.0/24)
- [x] REST API: nodes CRUD, links CRUD
- [x] Hot-reconfiguration: add outducts/plans to running nodes

### Phase 2 — Frontend Topology ✅
- [x] HTML/CSS scaffold with dark theme
- [x] Cytoscape.js graph with nodes and edges
- [x] Control panel (add node, add link, delete)
- [x] REST API integration (fetch topology, send commands)

### Phase 3 — Traffic & Stats ✅
- [x] Traffic send endpoint (`bpsource` via `docker exec`)
- [x] Statistics collector (parse `bpstats` from `ion.log`)
- [x] WebSocket for real-time stats push
- [x] Stats display above each node in the graph

### Phase 4 — Link Disruption ✅
- [x] Disrupt/restore endpoints (`tc netem` via `docker exec`)
- [x] Visual feedback on graph (edge color/style for link state)
- [x] Disruption controls in UI

### Phase 5 — Scenario Export ✅
- [x] Export current topology as standalone Docker Compose package
- [x] Import scenarios from file or upload
- [x] Auto-generated start/stop/send/recv/plot scripts
- [x] Pre-deploy cleanup of conflicting Docker networks
- [x] Reliable bpsink monitoring via log file + docker exec
- [x] Simultaneous recv.sh + plot.sh support

### Phase 6 — Future Work
- [ ] Persistence (topology survives backend restarts)
- [ ] Contact plans / CGR-based routing support
- [ ] Multi-hop route auto-computation
- [ ] Scale testing (10+ nodes)
- [ ] Unit and integration tests

---

## Resolved Issues

### bpsink output not captured by `docker logs`
- **Cause:** Docker's pty allocation for the entrypoint overrides bash `>>` redirects
- **Fix:** Removed bpsink from entrypoint; start via `docker exec` (no pty)

### "Pool overlaps" on scenario start
- **Cause:** Old scenario Docker networks lingering on `10.40.x.0/24` subnets
- **Fix:** `start.sh` auto-cleans old `scenario_*` containers and `10.40.x` networks

### recv.sh and plot.sh can't run simultaneously
- **Cause:** ION allows only one process per endpoint; recv.sh killed the background bpsink
- **Fix:** `recv.sh` now tails `bpsink.log` instead of starting its own bpsink
