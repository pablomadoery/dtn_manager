"""WebSocket endpoint for real-time topology and stats updates.

Both loops offload every blocking docker call via ``asyncio.to_thread`` and the
topology loop merges in the shared poller snapshot, so a slow/hung docker exec
never freezes the event loop for other clients.
"""

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()

# Connected WebSocket clients
_clients: set[WebSocket] = set()


def _manager(websocket: WebSocket):
    return getattr(websocket.app.state, "manager", None)


async def broadcast(message: dict):
    """Send a message to all connected WebSocket clients."""
    dead = set()
    for ws in _clients:
        try:
            await ws.send_json(message)
        except Exception:
            dead.add(ws)
    _clients.difference_update(dead)


@router.websocket("/ws/topology")
async def ws_topology(websocket: WebSocket):
    """WebSocket that pushes topology + stats updates periodically."""
    await websocket.accept()
    _clients.add(websocket)

    try:
        while True:
            manager = _manager(websocket)
            if manager is None:
                await asyncio.sleep(2)
                continue

            topology = await asyncio.to_thread(manager.get_topology)

            # Merge bundle counts from the shared poller snapshot (no docker
            # load here — the background poller already gathered them).
            snapshot = getattr(websocket.app.state, "stats_snapshot", {})
            for node_data in topology["nodes"]:
                node_stats = snapshot.get(str(node_data["id"]))
                if node_stats and "bpsink_delivered" in node_stats:
                    node_data["stats"]["bundles_received"] = \
                        node_stats["bpsink_delivered"]

            await websocket.send_json({"type": "topology", "data": topology})
            await asyncio.sleep(2)

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _clients.discard(websocket)


@router.websocket("/ws/logs/{node_id}")
async def ws_logs(websocket: WebSocket, node_id: int):
    """Stream bpsink output for a specific node."""
    await websocket.accept()

    last_line_count = 0
    try:
        while True:
            manager = _manager(websocket)
            if manager is None:
                await asyncio.sleep(1)
                continue
            try:
                output = await asyncio.to_thread(
                    manager.get_bpsink_output, node_id, 100)
            except Exception:
                await asyncio.sleep(1)
                continue

            lines = output.strip().split("\n") if output.strip() else []
            if len(lines) > last_line_count:
                new_lines = lines[last_line_count:]
                last_line_count = len(lines)
                await websocket.send_json({
                    "type": "logs",
                    "node_id": node_id,
                    "lines": new_lines,
                })

            await asyncio.sleep(1)

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
