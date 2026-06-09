# ION-DTN Network Manager — Web Application Plan

## Goal

Build a web application that provides a **visual, interactive control plane** for ION-DTN networks. Users can dynamically create/destroy nodes and links, send traffic, and monitor bundle statistics — all backed by real Docker containers running ION-DTN.

---

## Core Features

| Feature | Description |
|---------|-------------|
| **Node management** | Create / delete ION nodes (each = Docker container) |
| **Link management** | Create / delete links between nodes (each = Docker network) |
| **Link disruption** | Simulate link failure/recovery via `tc netem` |
| **Traffic generation** | Send bundles between any two nodes (`bpsource`) |
| **Live statistics** | Bundle count, throughput, delivery status per node |
| **Topology view** | Interactive graph synchronized with Docker state |
| **Log inspection** | View `ion.log` and `bpsink` output per node |

---

## Architecture

```
┌──────────────────────────────────────────────────┐
│                   Browser (UI)                    │
│  ┌────────────────────────────────────────────┐  │
│  │  Interactive Graph  │  Controls & Stats     │  │
│  │  (Cytoscape.js /    │  - Create node        │  │
│  │   vis-network /     │  - Create link         │  │
│  │   D3.js)            │  - Send traffic        │  │
│  │                     │  - View logs           │  │
│  └────────────────────────────────────────────┘  │
│                    ▲ WebSocket / SSE / REST        │
└────────────────────┼─────────────────────────────┘
                     │
┌────────────────────┼─────────────────────────────┐
│              Backend API Server                    │
│  ┌─────────────┐  ┌──────────────┐               │
│  │ REST API     │  │ WebSocket    │               │
│  │ /nodes       │  │ /ws/stats    │               │
│  │ /links       │  │ /ws/logs     │               │
│  │ /traffic     │  │              │               │
│  └──────┬──────┘  └──────┬───────┘               │
│         │                │                        │
│  ┌──────┴────────────────┴───────┐               │
│  │     Docker Engine API          │               │
│  │  - Container lifecycle         │               │
│  │  - Network management          │               │
│  │  - Exec (ION commands)         │               │
│  │  - Logs streaming              │               │
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

## Technology Alternatives

### Backend

| Option | Pros | Cons | Docker SDK |
|--------|------|------|------------|
| **Python + FastAPI** ⭐ | Fast to develop, async-native, you already use Python for plotting, excellent Docker SDK (`docker-py`) | Less performant than Go for high-concurrency | [docker-py](https://github.com/docker/docker-py) — mature, full API coverage |
| **Node.js + Express** | Rich ecosystem, good WebSocket support, easy frontend integration | Docker SDK (`dockerode`) less mature, callback-heavy | [dockerode](https://github.com/apocas/dockerode) — good but community-maintained |
| **Go + Gin/Fiber** | Docker itself is Go, best Docker SDK, highest performance | Slower development, more boilerplate | [moby/moby client](https://pkg.go.dev/github.com/docker/docker/client) — official, first-party |

> [!TIP]
> **Recommendation: Python + FastAPI** — fastest path to a working prototype. You already have Python scripts for plotting. FastAPI's native async and WebSocket support pair well with Docker log streaming. The `docker-py` SDK is Docker's official Python library.

---

### Frontend Graph Visualization

| Option | Type | Pros | Cons |
|--------|------|------|------|
| **Cytoscape.js** ⭐ | Graph library | Purpose-built for network graphs, automatic layouts (force-directed, grid, circle), pan/zoom, edge styles, touch support, lightweight (~300KB) | Less flexible for non-graph UI |
| **vis-network** | Graph library | Simple API, good for quick prototypes, built-in physics simulation | Less maintained, fewer layout options |
| **D3.js** | General viz | Maximum flexibility, can build anything | High complexity, need to build graph interactions from scratch |
| **React Flow** | React component | Drag-and-drop nodes, modern React patterns | Designed for flowcharts not network graphs, React dependency |

> [!TIP]
> **Recommendation: Cytoscape.js** — purpose-built for network topology visualization. Supports force-directed layouts that auto-arrange nodes, edge styling for link states (up/down/degraded), and runs standalone (no React/Vue dependency). Used by major bioinfo and network tools.

---

### Frontend Framework

| Option | Pros | Cons |
|--------|------|------|
| **Plain HTML/JS + CSS** ⭐ | No build step, simple deployment, direct DOM control | Manual state management |
| **React + Vite** | Component model, large ecosystem | Build tooling, heavier for a control panel |
| **Vue 3 + Vite** | Simpler than React, reactive data binding | Extra dependency |

> [!TIP]
> **Recommendation: Plain HTML/JS** for a first version — the UI is primarily a graph + control panel, not a complex multi-page app. Cytoscape.js works standalone. Can migrate to React later if needed.

---

### Real-Time Communication

| Option | Direction | Pros | Cons |
|--------|-----------|------|------|
| **WebSocket** ⭐ | Bidirectional | Full-duplex, low latency, can send commands back | More complex server-side |
| **SSE (Server-Sent Events)** | Server → Client | Simpler than WebSocket, auto-reconnect, HTTP-native | One-directional only |
| **Polling** | Client → Server | Simplest to implement | Higher latency, wasted requests |

> [!TIP]
> **Recommendation: WebSocket** — we need both directions (client sends commands, server pushes stats/logs). FastAPI has built-in WebSocket support.

---

## Data Model

### Node

```json
{
  "id": 1,
  "name": "N1",
  "ipn_number": 1,
  "container_id": "abc123...",
  "container_name": "ion_n1",
  "status": "running",
  "ip_addresses": {
    "link_1_2": "172.40.0.2"
  },
  "stats": {
    "bundles_received": 42,
    "bundles_sent": 38,
    "bundles_forwarded": 0
  }
}
```

### Link

```json
{
  "id": "1-2",
  "node_a": 1,
  "node_b": 2,
  "network_id": "net_1_2",
  "subnet": "172.40.0.0/24",
  "ip_a": "172.40.0.2",
  "ip_b": "172.40.0.3",
  "status": "up",
  "netem": null
}
```

---

## API Design (REST + WebSocket)

### REST Endpoints

```
# Nodes
POST   /api/nodes              Create a new ION node
GET    /api/nodes              List all nodes
GET    /api/nodes/{id}         Get node details + stats
DELETE /api/nodes/{id}         Remove node (stop + delete container)

# Links
POST   /api/links              Create link between two nodes
GET    /api/links              List all links
DELETE /api/links/{id}         Remove link (disconnect network)
POST   /api/links/{id}/disrupt Set tc netem on link (loss, delay, etc.)
POST   /api/links/{id}/restore Remove tc netem rules

# Traffic
POST   /api/traffic/send       Send bundles between nodes
GET    /api/traffic/active      List active traffic flows

# Logs
GET    /api/nodes/{id}/logs    Get ion.log content
GET    /api/nodes/{id}/bpsink  Get bpsink output (docker logs)

# Topology
GET    /api/topology           Full topology (nodes + links + stats)
```

### WebSocket Channels

```
/ws/topology    → Real-time topology updates (node/link state changes)
/ws/stats       → Periodic bundle statistics per node
/ws/logs/{id}   → Stream ion.log or bpsink output for a specific node
```

---

## Key Implementation Details

### 1. Dynamic ION Configuration Generation

When a node is created, the backend generates `node.rc` and `node.ionconfig` dynamically:

- **ionadmin**: `1 <node_num> 'node.ionconfig'`
- **ionsecadmin**: `1`
- **bpadmin**: Register endpoints `.0`, `.1`, `.2`; add protocol `tcp`; add induct on `0.0.0.0:4556`
- **outducts and plans**: Added dynamically when links are created

> [!IMPORTANT]
> **Challenge: Hot-reconfiguration.** When a new link is added to an existing node, we need to add the outduct and egress plan to a running ION instance. This requires running `bpadmin` and `ipnadmin` commands via `docker exec` on a live node:
> ```bash
> docker exec <container> bpadmin <<< 'a outduct tcp <peer_ip>:4556 tcpclo'
> docker exec <container> ipnadmin <<< 'a plan <peer_node> tcp/<peer_ip>:4556'
> ```
> This is a proven capability — ION supports runtime admin commands.

### 2. Docker Network Management

Each link = a dedicated Docker bridge network with a /30 or /24 subnet:

```python
# Create link network
network = docker_client.networks.create(
    name=f"ion_link_{a}_{b}",
    driver="bridge",
    ipam=docker.types.IPAMConfig(
        pool_configs=[docker.types.IPAMPool(subnet=f"172.40.{link_id}.0/24")]
    )
)

# Connect both containers to the network
network.connect(container_a, ipv4_address=f"172.40.{link_id}.2")
network.connect(container_b, ipv4_address=f"172.40.{link_id}.3")
```

### 3. Subnet Allocation

The backend manages a subnet pool to avoid conflicts:
- Base: `172.40.0.0/16`
- Each link gets `172.40.<link_id>.0/24`
- Link IDs auto-increment
- Released subnets return to the pool

### 4. Bundle Statistics Collection

Two approaches:

**Option A: Parse `bpsink` output** (current approach)
- `bpsink` runs on each node, output captured via `docker logs`
- Backend parses payload text for sequence numbers
- Pro: Already proven. Con: Requires bpsink to run.

**Option B: Use `bpstats2` / `bpacct`** (ION built-in accounting)
- ION has built-in bundle accounting (`bpstats2`, `watch` commands)
- `docker exec <node> bpadmin <<< 'w 1'` → enables periodic watchdog stats
- Pro: Official ION metrics. Con: Requires parsing ion.log.

> [!TIP]
> **Recommendation:** Use both. `bpsink` for real-time payload visibility, `bpadmin watch` for official ION statistics.

### 5. Link Disruption

Same `tc netem` approach proven in the `static_route_disruption` scenario:

```python
# Disrupt
docker_exec(container, f"tc qdisc replace dev {iface} root netem loss 100%")

# Restore
docker_exec(container, f"tc qdisc del dev {iface} root")
```

The API can also support partial degradation:
```json
POST /api/links/1-2/disrupt
{
  "loss_percent": 30,
  "delay_ms": 500,
  "jitter_ms": 100
}
```

---

## Pre-built ION Docker Image

> [!IMPORTANT]
> Building ION from source takes ~5 minutes per container. For dynamic node creation, we should **pre-build a single ION base image** and reuse it for all nodes.

```bash
# Build once
docker build -t ion-dtn-base -f Dockerfile .

# Create nodes instantly from pre-built image
docker create --name ion_n1 --image ion-dtn-base ...
```

The existing Dockerfile from `static_route_disruption` is the base.

---

## Project Structure

```
ion_network_manager/
├── backend/
│   ├── main.py                 # FastAPI app entry point
│   ├── api/
│   │   ├── nodes.py            # Node CRUD endpoints
│   │   ├── links.py            # Link management endpoints
│   │   ├── traffic.py          # Bundle traffic endpoints
│   │   └── websocket.py        # WebSocket handlers
│   ├── services/
│   │   ├── docker_manager.py   # Docker SDK wrapper
│   │   ├── ion_config.py       # ION config generator
│   │   ├── subnet_pool.py      # Subnet allocator
│   │   └── stats_collector.py  # Bundle statistics
│   ├── models.py               # Data models
│   └── requirements.txt
├── frontend/
│   ├── index.html              # Main page
│   ├── css/
│   │   └── style.css           # Dark theme, controls
│   └── js/
│       ├── app.js              # Main application logic
│       ├── graph.js            # Cytoscape.js topology view
│       ├── controls.js         # Node/link/traffic controls
│       ├── stats.js            # Statistics panel
│       └── websocket.js        # WebSocket client
├── docker/
│   ├── Dockerfile.ion          # Pre-built ION base image
│   └── entrypoint.sh           # Generic ION entrypoint
└── README.md
```

---

## UI Layout

```
┌─────────────────────────────────────────────────────────────┐
│  ION-DTN Network Manager                          [Status]  │
├──────────────────────────────────┬──────────────────────────┤
│                                  │  Controls               │
│                                  │  ┌────────────────────┐ │
│     Interactive Topology         │  │ + Add Node         │ │
│     (Cytoscape.js)               │  │ + Add Link         │ │
│                                  │  │ ▶ Send Traffic     │ │
│     ┌───┐        ┌───┐          │  └────────────────────┘ │
│     │ 1 │────────│ 2 │          │                          │
│     └───┘        └─┬─┘          │  Node Inspector         │
│                    │             │  ┌────────────────────┐ │
│                  ┌───┐           │  │ Node: N2           │ │
│                  │ 3 │           │  │ Status: Running    │ │
│                  └───┘           │  │ Recv: 42 bundles   │ │
│                                  │  │ Sent: 38 bundles   │ │
│                                  │  │ [View Logs]        │ │
│                                  │  └────────────────────┘ │
├──────────────────────────────────┴──────────────────────────┤
│  Logs / Bundle Feed                                  [N2 ▼]│
│  'bundle #42 at 15:30:07'                                   │
│  'bundle #43 at 15:30:08'                                   │
│  'bundle #44 at 15:30:09'                                   │
└─────────────────────────────────────────────────────────────┘
```

---

## Phased Build Plan

### Phase 1 — Backend Core (2-3 days)
- [ ] Pre-build ION Docker image
- [ ] Docker manager service (create/delete containers, networks)
- [ ] ION config generator (dynamic `node.rc` creation)
- [ ] Subnet pool allocator
- [ ] REST API: nodes CRUD, links CRUD
- [ ] Hot-reconfiguration: add outducts/plans to running nodes

### Phase 2 — Frontend Topology (1-2 days)
- [ ] HTML/CSS scaffold with dark theme
- [ ] Cytoscape.js graph with nodes and edges
- [ ] Control panel (add node, add link, delete)
- [ ] REST API integration (fetch topology, send commands)

### Phase 3 — Traffic & Stats (1-2 days)
- [ ] Traffic send endpoint (`bpsource` via `docker exec`)
- [ ] Statistics collector (parse `bpsink` + `bpadmin watch`)
- [ ] WebSocket for real-time stats push
- [ ] Stats panel in UI

### Phase 4 — Link Disruption (1 day)
- [ ] Disrupt/restore endpoints (`tc netem` via `docker exec`)
- [ ] Visual feedback on graph (edge color/style for link state)
- [ ] Disruption controls in UI

### Phase 5 — Polish (1-2 days)
- [ ] Log viewer panel (streaming `ion.log` via WebSocket)
- [ ] Bundle feed panel (streaming `bpsink` output)
- [ ] Error handling and edge cases
- [ ] Documentation

---

## Open Questions

> [!IMPORTANT]
> **1. Persistence**: Should the topology survive backend restarts? Options:
> - **No persistence** (simplest) — clean slate each time, Docker containers are ephemeral
> - **SQLite** — lightweight, single-file database
> - **JSON file** — minimal persistence of topology state

> [!IMPORTANT]
> **2. Multi-hop routing**: When a link is added between non-adjacent nodes, should the backend automatically compute and install static exits (multi-hop routes), or should the user configure them manually through the UI?

> [!IMPORTANT]
> **3. Contact plans vs static routes**: Should the web app support only static routes (like current scenarios), or also allow defining contact plans (scheduled links with time windows) for CGR-based routing?

> [!IMPORTANT]
> **4. Scale target**: How many nodes should this support? 3-5 (demo), 10-20 (medium), 50+ (large)? This affects subnet management and UI layout strategy.

---

## Verification Plan

### Automated Tests
- Unit tests for ION config generator
- Integration tests: create node → create link → send bundle → verify delivery
- API tests with `pytest` + `httpx`

### Manual Verification
- Create 3 nodes, link them linearly, send bundles end-to-end
- Disrupt a link mid-flow, verify bundles stop, restore, verify resume
- Delete a node, verify cleanup (container + networks removed)
- Verify graph updates reflect Docker state accurately
