"""Link management API endpoints."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix="/api/links", tags=["links"])

# Will be set by main.py
manager = None


class CreateLinkRequest(BaseModel):
    node_a: int
    node_b: int


class DisruptRequest(BaseModel):
    loss_percent: float = 100
    delay_ms: int = 0
    jitter_ms: int = 0


@router.post("")
def create_link(req: CreateLinkRequest):
    try:
        link = manager.create_link(req.node_a, req.node_b)
        return link.to_dict()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("")
def list_links():
    return [l.to_dict() for l in manager.list_links()]


@router.delete("/{link_id}")
def delete_link(link_id: str):
    try:
        manager.delete_link(link_id)
        return {"status": "deleted", "link_id": link_id}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{link_id}/disrupt")
def disrupt_link(link_id: str, req: DisruptRequest = DisruptRequest()):
    try:
        link = manager.disrupt_link(
            link_id,
            loss=req.loss_percent,
            delay=req.delay_ms,
            jitter=req.jitter_ms,
        )
        return link.to_dict()
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{link_id}/restore")
def restore_link(link_id: str):
    try:
        link = manager.restore_link(link_id)
        return link.to_dict()
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
