"""Traffic management API endpoints."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix="/api/traffic", tags=["traffic"])

# Will be set by main.py
manager = None


class SendBundleRequest(BaseModel):
    from_node: int
    to_node: int
    message: str = "hello from web UI"
    count: int = 1


@router.post("/send")
def send_bundles(req: SendBundleRequest):
    try:
        results = []
        for i in range(req.count):
            msg = f"{req.message} #{i+1}" if req.count > 1 else req.message
            ok = manager.send_bundle(req.from_node, req.to_node, msg)
            results.append(ok)
        return {
            "from_node": req.from_node,
            "to_node": req.to_node,
            "sent": sum(results),
            "failed": len(results) - sum(results),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
