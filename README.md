# DTN-Manager

Interactive web application for managing ION-DTN networks backed by Docker containers.

## Features

- **Create/delete nodes** — each node is a Docker container running ION-DTN
- **Create/delete links** — each link is a Docker bridge network with auto-configured ION outducts/plans
- **Send bundles** — inject traffic between any two nodes
- **Link disruption** — simulate failures with `tc netem` (100% loss, configurable)
- **Live stats** — bundle counts updated in real time via WebSocket
- **Log streaming** — view `bpsink` output per node
- **Interactive topology** — Cytoscape.js graph with click-to-inspect

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

## Architecture

```
Browser (Cytoscape.js)  ←→  FastAPI (REST + WebSocket)  ←→  Docker Engine
       UI                        Backend                    ION Containers
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

