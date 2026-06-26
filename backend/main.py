"""DTN-Manager — FastAPI Application."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import docker
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend import settings
from backend.api import nodes, links, traffic, websocket, scenarios
from backend.api.deps import get_manager
from backend.errors import (
    BackendTimeout,
    CapacityError,
    ConflictError,
    DTNManagerError,
    IonExecError,
    NotFoundError,
    ValidationError,
)
from backend.services.docker_manager import DockerManager

log = logging.getLogger("dtn_manager")


# Shared Docker manager instance. Construction runs reconcile(), adopting any
# live labeled resources left by a prior (possibly crashed) session.
docker_manager = DockerManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    print("=== DTN-Manager starting ===")
    print("  Docker image: ion-dtn-base")
    print("  Subnet pool: 172.50.x.0/24")
    print(f"  Adopted {len(docker_manager.nodes)} node(s), "
          f"{len(docker_manager.links)} link(s) from live Docker state")

    app.state.manager = docker_manager

    # One background poller owns per-node stats so docker load is O(1)/interval
    # regardless of how many websocket clients are connected.
    app.state.stats_snapshot = {}
    poller = asyncio.create_task(_stats_poller(app))
    try:
        yield
    finally:
        poller.cancel()
        try:
            await poller
        except asyncio.CancelledError:
            pass
        # Resource lifecycle is decoupled from process lifecycle: a clean
        # shutdown PRESERVES the topology by default so a restart re-adopts it.
        if settings.CLEANUP_ON_EXIT:
            print("\n=== IONMGR_CLEANUP_ON_EXIT set — wiping Docker resources ===")
            docker_manager.cleanup_all()
            print("  ✔ Cleanup complete")
        else:
            print("\n=== Shutting down — topology preserved "
                  "(reconcile will re-adopt on restart) ===")


async def _stats_poller(app: FastAPI, interval: float = 2.0):
    """Periodically refresh the shared bundle-stats snapshot off the event loop."""
    while True:
        try:
            stats = await asyncio.to_thread(docker_manager.get_all_bundle_stats)
            app.state.stats_snapshot = {str(k): v for k, v in stats.items()}
        except Exception as e:  # never let the poller die silently
            log.warning("stats poller error: %s", e)
        await asyncio.sleep(interval)


app = FastAPI(
    title="DTN-Manager",
    description="Interactive DTN topology management with Docker-backed ION nodes",
    version="1.0.0",
    lifespan=lifespan,
)


# ── Central exception mapping ─────────────────────────────────────
# One place maps domain/docker/timeout errors to status codes. Internal detail
# is logged server-side; clients get a clean message, never a raw stack/str(e).

def _install_handlers(app: FastAPI) -> None:
    def _json(status: int, detail: str) -> JSONResponse:
        return JSONResponse(status_code=status, content={"detail": detail})

    @app.exception_handler(NotFoundError)
    async def _not_found(request: Request, exc: NotFoundError):
        return _json(404, str(exc))

    @app.exception_handler(ConflictError)
    async def _conflict(request: Request, exc: ConflictError):
        return _json(409, str(exc))

    @app.exception_handler(ValidationError)
    async def _validation(request: Request, exc: ValidationError):
        return _json(400, str(exc))

    @app.exception_handler(CapacityError)
    async def _capacity(request: Request, exc: CapacityError):
        return _json(400, str(exc))

    @app.exception_handler(BackendTimeout)
    async def _timeout(request: Request, exc: BackendTimeout):
        return _json(504, str(exc))

    @app.exception_handler(IonExecError)
    async def _ion(request: Request, exc: IonExecError):
        log.warning("ION exec error: %s (rc=%s) %s", exc, exc.returncode, exc.stderr)
        return _json(502, str(exc))

    @app.exception_handler(DTNManagerError)
    async def _domain(request: Request, exc: DTNManagerError):
        return _json(400, str(exc))

    @app.exception_handler(docker.errors.NotFound)
    async def _docker_not_found(request: Request, exc):
        return _json(404, "Docker resource not found")

    @app.exception_handler(docker.errors.APIError)
    async def _docker_api(request: Request, exc):
        status = getattr(exc, "status_code", None) or 502
        log.warning("docker API error: %s", exc)
        return _json(status, "Docker API error")

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        # Log the real exception; return a generic message (no internal leak).
        log.exception("unhandled error on %s", request.url.path)
        return _json(500, "Internal server error")


_install_handlers(app)

# Register API routers
app.include_router(nodes.router)
app.include_router(links.router)
app.include_router(traffic.router)
app.include_router(websocket.router)
app.include_router(scenarios.router)


# Topology endpoint
@app.get("/api/topology")
def get_topology(manager: DockerManager = Depends(get_manager)):
    return manager.get_topology()


# Cleanup endpoint
@app.post("/api/cleanup")
def cleanup(manager: DockerManager = Depends(get_manager)):
    manager.cleanup_all()
    return {"status": "cleaned up"}


# Exit route control
@app.post("/api/exits/set")
def set_exits(manager: DockerManager = Depends(get_manager)):
    errors = manager.set_all_exits()
    return {"status": "exits set", "errors": errors}


@app.post("/api/exits/clear")
def clear_exits(manager: DockerManager = Depends(get_manager)):
    manager.clear_all_exits()
    return {"status": "exits cleared"}


@app.get("/api/bundle-stats")
def get_bundle_stats(request: Request):
    """Serve the latest poller snapshot (O(1), no docker load per request)."""
    return getattr(request.app.state, "stats_snapshot", {})


# Serve frontend static files
frontend_dir = Path(__file__).parent.parent / "frontend"
app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")


@app.get("/")
def serve_index():
    return FileResponse(str(frontend_dir / "index.html"))
