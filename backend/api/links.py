"""Link management API endpoints."""

import asyncio

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.api.deps import get_manager
from backend.services.docker_manager import DockerManager

router = APIRouter(prefix="/api/links", tags=["links"])


class CreateLinkRequest(BaseModel):
    node_a: int
    node_b: int
    convergence_layer: str = "tcpcl"


class DisruptRequest(BaseModel):
    loss_percent: float = 100
    delay_ms: int = 0
    jitter_ms: int = 0


@router.post("")
async def create_link(req: CreateLinkRequest,
                      manager: DockerManager = Depends(get_manager)):
    link = await asyncio.to_thread(
        manager.create_link, req.node_a, req.node_b, req.convergence_layer)
    return link.to_dict()


@router.get("")
def list_links(manager: DockerManager = Depends(get_manager)):
    return [l.to_dict() for l in manager.list_links()]


@router.delete("/{link_id}")
async def delete_link(link_id: str, manager: DockerManager = Depends(get_manager)):
    await asyncio.to_thread(manager.delete_link, link_id)
    return {"status": "deleted", "link_id": link_id}


@router.post("/{link_id}/disrupt")
async def disrupt_link(link_id: str, req: DisruptRequest = DisruptRequest(),
                       manager: DockerManager = Depends(get_manager)):
    link = await asyncio.to_thread(
        manager.disrupt_link, link_id,
        req.loss_percent, req.delay_ms, req.jitter_ms)
    return link.to_dict()


@router.post("/{link_id}/restore")
async def restore_link(link_id: str, manager: DockerManager = Depends(get_manager)):
    link = await asyncio.to_thread(manager.restore_link, link_id)
    return link.to_dict()
