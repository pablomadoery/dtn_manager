"""Node management API endpoints."""

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.api.deps import get_manager
from backend.services.docker_manager import DockerManager

router = APIRouter(prefix="/api/nodes", tags=["nodes"])


class CreateNodeRequest(BaseModel):
    node_id: Optional[int] = None


@router.post("")
async def create_node(req: CreateNodeRequest = CreateNodeRequest(),
                      manager: DockerManager = Depends(get_manager)):
    node = await asyncio.to_thread(manager.create_node, req.node_id)
    return node.to_dict()


@router.get("")
def list_nodes(manager: DockerManager = Depends(get_manager)):
    return [n.to_dict() for n in manager.list_nodes()]


@router.get("/{node_id}")
async def get_node(node_id: int, manager: DockerManager = Depends(get_manager)):
    node = manager.get_node(node_id)
    # Read-only: compute the count into the response without mutating shared
    # state, and offload the blocking docker call off the event loop.
    data = node.to_dict()
    count = await asyncio.to_thread(manager.get_bundle_count, node_id)
    data["stats"]["bundles_received"] = count
    return data


@router.delete("/{node_id}")
async def delete_node(node_id: int, manager: DockerManager = Depends(get_manager)):
    await asyncio.to_thread(manager.delete_node, node_id)
    return {"status": "deleted", "node_id": node_id}


@router.get("/{node_id}/logs")
async def get_logs(node_id: int, tail: int = 50,
                   manager: DockerManager = Depends(get_manager)):
    logs = await asyncio.to_thread(manager.get_node_logs, node_id, tail)
    return {"node_id": node_id, "logs": logs}


@router.get("/{node_id}/bpsink")
async def get_bpsink(node_id: int, tail: int = 20,
                     manager: DockerManager = Depends(get_manager)):
    output = await asyncio.to_thread(manager.get_bpsink_output, node_id, tail)
    return {"node_id": node_id, "output": output}


class AddExitRequest(BaseModel):
    dest_first: int
    dest_last: int
    gateway_id: int


@router.post("/{node_id}/exits")
async def add_exit(node_id: int, req: AddExitRequest,
                   manager: DockerManager = Depends(get_manager)):
    return await asyncio.to_thread(
        manager.add_exit, node_id, req.dest_first, req.dest_last, req.gateway_id)


@router.get("/{node_id}/exits")
async def list_exits(node_id: int, manager: DockerManager = Depends(get_manager)):
    exits = await asyncio.to_thread(manager.list_exits, node_id)
    return {"node_id": node_id, "exits": exits}


@router.delete("/{node_id}/exits")
async def delete_exit(node_id: int, dest_first: int, dest_last: int,
                      manager: DockerManager = Depends(get_manager)):
    return await asyncio.to_thread(
        manager.delete_exit, node_id, dest_first, dest_last)


@router.get("/{node_id}/neighbors")
def get_neighbors(node_id: int, manager: DockerManager = Depends(get_manager)):
    # get_neighbors raises NotFoundError via get_node check below.
    manager.get_node(node_id)
    neighbors = sorted(manager.get_neighbors(node_id))
    return {"node_id": node_id, "neighbors": neighbors}


@router.post("/{node_id}/exits/auto")
async def set_node_exits_auto(node_id: int,
                              manager: DockerManager = Depends(get_manager)):
    """Compute shortest-path exits for a single node."""
    errors = await asyncio.to_thread(manager.set_node_exits, node_id)
    return {"status": "exits set", "node_id": node_id, "errors": errors}


@router.get("/{node_id}/config")
async def get_node_config(node_id: int,
                          manager: DockerManager = Depends(get_manager)):
    """Get ION configuration files and live state for a node."""
    return await asyncio.to_thread(manager.get_node_config, node_id)
