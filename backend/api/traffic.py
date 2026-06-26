"""Traffic management API endpoints."""

import asyncio

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.api.deps import get_manager
from backend.services.docker_manager import DockerManager

router = APIRouter(prefix="/api/traffic", tags=["traffic"])


class SendBundleRequest(BaseModel):
    from_node: int
    to_node: int
    message: str = "hello from web UI"
    count: int = 1


@router.post("/send")
async def send_bundles(req: SendBundleRequest,
                       manager: DockerManager = Depends(get_manager)):
    def _send_all():
        results = []
        for i in range(req.count):
            msg = f"{req.message} #{i+1}" if req.count > 1 else req.message
            results.append(manager.send_bundle(req.from_node, req.to_node, msg))
        return results

    results = await asyncio.to_thread(_send_all)
    return {
        "from_node": req.from_node,
        "to_node": req.to_node,
        "sent": sum(results),
        "failed": len(results) - sum(results),
    }
