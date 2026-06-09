# DTN-Manager

Interactive web application for managing ION-DTN networks backed by Docker containers.

## Features

- **Create/delete nodes** — each node is a Docker container running ION-DTN
- **Create/delete links** — each link is a Docker bridge network with auto-configured ION outducts/plans
- **Send bundles** — inject traffic between any two nodes
- **Link disruption** — simulate failures with `tc netem` (100% loss, configurable)
- **Live stats** — bundle counts updated in real time via WebSocket (`src`, `fwd`, `xmt`, `rcv`, `dlv`, `exp`)
- **Log streaming** — view `bpsink` output per node
- **Interactive topology** — Cytoscape.js graph with click-to-inspect
- **Scenario export** — export current topology as standalone Docker Compose packages
- **Scenario import** — load saved scenarios from file or upload
- **Bundle arrival plot** — real-time matplotlib plot of bundle arrivals (`plot.sh`)

## Quick Start

```bash
# Start everything (builds Docker image, installs deps, launches server)
./start.sh

# Stop and clean up
./stop.sh

# Options
./start.sh -p 9090       # Custom port
./start.sh --rebuild     # Force Docker image rebuild
./stop.sh --force        # Force-kill everything
```

Then open **http://localhost:8080**

## Exported Scenarios

Export a scenario from the web UI to get a self-contained folder:

```
scenario_<timestamp>/
├── compose.yml              # Docker Compose orchestration
├── Dockerfile               # ION-DTN base image
├── ion-entrypoint.sh        # Container entrypoint (ION init)
├── configs/                 # Per-node ION configuration files
│   ├── n1/node.rc
│   ├── n2/node.rc
│   └── ...
├── start.sh                 # Deploy scenario (with auto-cleanup)
├── stop.sh                  # Tear down containers and networks
├── send.sh                  # Send bundles between nodes
├── recv.sh                  # Watch received bundles in terminal
├── plot.sh                  # Launch real-time bundle arrival plot
├── plot_bundles.py          # Matplotlib plotter
└── README.md                # Scenario-specific instructions
```

### Usage

```bash
cd ~/Downloads/scenario_<timestamp>

# Deploy (auto-cleans conflicting networks from previous runs)
./start.sh

# Send bundles: from N1 to N3, 1 per second (infinite)
./send.sh 1 3

# Watch bundles arriving on N3 (terminal)
./recv.sh 3

# Real-time plot of bundle arrivals on N3
./plot.sh 3

# All three can run simultaneously!
# recv.sh and plot.sh both read from bpsink.log

# Stop everything
./stop.sh
```

## Architecture

```
Browser (Cytoscape.js)  ←→  FastAPI (REST + WebSocket)  ←→  Docker Engine
       UI                        Backend                    ION Containers
```

### Bundle Monitoring Pipeline

```
start.sh
  └── docker exec: bpsink >> /ion-runtime/bpsink.log &

bpsink.log (inside container)
       │
       ├── recv.sh     →  docker exec tail -f  →  terminal output
       ├── plot.sh     →  docker exec tail -f  →  matplotlib plot
       └── manager     →  docker exec tail -n  →  WebSocket stats
```

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/nodes` | Create a node |
| GET | `/api/nodes` | List all nodes |
| DELETE | `/api/nodes/{id}` | Delete a node |
| POST | `/api/links` | Create a link |
| GET | `/api/links` | List all links |
| DELETE | `/api/links/{id}` | Delete a link |
| POST | `/api/links/{id}/disrupt` | Apply tc netem |
| POST | `/api/links/{id}/restore` | Remove tc netem |
| POST | `/api/exits/set` | Set shortest-path exit routes |
| POST | `/api/exits/clear` | Clear all exit routes |
| POST | `/api/traffic/send` | Send bundles |
| GET | `/api/topology` | Full topology |
| POST | `/api/cleanup` | Remove everything |
| GET | `/api/scenarios` | List saved scenarios |
| POST | `/api/scenarios/export` | Export current topology |
| POST | `/api/scenarios/import` | Import from upload |
| POST | `/api/scenarios/import/file` | Import from disk |
| DELETE | `/api/scenarios/{filename}` | Delete saved scenario |
| WS | `/ws/topology` | Real-time updates |
| WS | `/ws/logs/{id}` | Stream node logs |

## ION Bundle Statistics

The dashboard displays per-node stats collected from ION's `bpstats` command:

| Stat | Meaning |
|------|---------|
| `src` | Bundles sourced (originated) at this node |
| `fwd` | Bundles forwarded (routing decisions made) |
| `xmt` | Bundles transmitted over the convergence layer |
| `rcv` | Bundles received from the convergence layer |
| `dlv` | Bundles delivered to a local endpoint |
| `exp` | Bundles expired (TTL exceeded) |

## Requirements

- Docker Engine
- Python 3.10+
- `matplotlib` (for plot scripts in exported scenarios)
