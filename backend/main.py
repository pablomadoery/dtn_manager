"""DTN-Manager — FastAPI Application."""

import signal
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from backend.api import nodes, links, traffic, websocket, scenarios
from backend.services.docker_manager import DockerManager


# Shared Docker manager instance
docker_manager = DockerManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    print("=== DTN-Manager starting ===")
    print(f"  Docker image: ion-dtn-base")
    print(f"  Subnet pool: 172.50.x.0/24")
    yield
    print("\n=== Shutting down — cleaning up Docker resources ===")
    docker_manager.cleanup_all()
    print("  ✔ Cleanup complete")


app = FastAPI(
    title="DTN-Manager",
    description="Interactive DTN topology management with Docker-backed ION nodes",
    version="1.0.0",
    lifespan=lifespan,
)

# Inject manager into API modules
nodes.manager = docker_manager
links.manager = docker_manager
traffic.manager = docker_manager
websocket.manager = docker_manager
scenarios.manager = docker_manager

# Register API routers
app.include_router(nodes.router)
app.include_router(links.router)
app.include_router(traffic.router)
app.include_router(websocket.router)
app.include_router(scenarios.router)

# Topology endpoint
@app.get("/api/topology")
def get_topology():
    return docker_manager.get_topology()

# Cleanup endpoint
@app.post("/api/cleanup")
def cleanup():
    docker_manager.cleanup_all()
    return {"status": "cleaned up"}

# Exit route control
@app.post("/api/exits/set")
def set_exits():
    docker_manager.set_all_exits()
    return {"status": "exits set"}

@app.post("/api/exits/clear")
def clear_exits():
    docker_manager.clear_all_exits()
    return {"status": "exits cleared"}

@app.get("/api/bundle-stats")
def get_bundle_stats():
    """Get bundle statistics for all nodes."""
    stats = docker_manager.get_all_bundle_stats()
    # Convert int keys to string for JSON
    return {str(k): v for k, v in stats.items()}

# Serve frontend static files
frontend_dir = Path(__file__).parent.parent / "frontend"
app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")


@app.get("/")
def serve_index():
    return FileResponse(str(frontend_dir / "index.html"))
