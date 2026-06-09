"""WebSocket endpoint for real-time topology and stats updates."""

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()

# Will be set by main.py
manager = None

# Connected WebSocket clients
_clients: set[WebSocket] = set()


async def broadcast(message: dict):
    """Send a message to all connected WebSocket clients."""
    dead = set()
    for ws in _clients:
        try:
            await ws.send_json(message)
        except Exception:
            dead.add(ws)
    _clients -= dead


@router.websocket("/ws/topology")
async def ws_topology(websocket: WebSocket):
    """WebSocket that pushes topology + stats updates periodically."""
    await websocket.accept()
    _clients.add(websocket)

    try:
        while True:
            # Push current topology with stats
            topology = manager.get_topology()

            # Update bundle counts for each node
            for node_data in topology["nodes"]:
                try:
                    count = manager.get_bundle_count(node_data["id"])
                    node_data["stats"]["bundles_received"] = count
                except Exception:
                    pass

            await websocket.send_json({
                "type": "topology",
                "data": topology,
            })

            await asyncio.sleep(2)  # Update every 2 seconds

    except WebSocketDisconnect:
        _clients.discard(websocket)
    except Exception:
        _clients.discard(websocket)


@router.websocket("/ws/logs/{node_id}")
async def ws_logs(websocket: WebSocket, node_id: int):
    """Stream bpsink output for a specific node."""
    await websocket.accept()

    last_line_count = 0
    try:
        while True:
            output = manager.get_bpsink_output(node_id, tail=100)
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
