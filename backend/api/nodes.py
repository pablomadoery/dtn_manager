"""Node management API endpoints."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix="/api/nodes", tags=["nodes"])

# Will be set by main.py
manager = None


class CreateNodeRequest(BaseModel):
    node_id: Optional[int] = None


@router.post("")
def create_node(req: CreateNodeRequest = CreateNodeRequest()):
    try:
        node = manager.create_node(req.node_id)
        return node.to_dict()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("")
def list_nodes():
    return [n.to_dict() for n in manager.list_nodes()]


@router.get("/{node_id}")
def get_node(node_id: int):
    try:
        node = manager.get_node(node_id)
        # Update stats
        node.stats.bundles_received = manager.get_bundle_count(node_id)
        return node.to_dict()
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/{node_id}")
def delete_node(node_id: int):
    try:
        manager.delete_node(node_id)
        return {"status": "deleted", "node_id": node_id}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{node_id}/logs")
def get_logs(node_id: int, tail: int = 50):
    try:
        logs = manager.get_node_logs(node_id, tail=tail)
        return {"node_id": node_id, "logs": logs}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{node_id}/bpsink")
def get_bpsink(node_id: int, tail: int = 20):
    try:
        output = manager.get_bpsink_output(node_id, tail=tail)
        return {"node_id": node_id, "output": output}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


class AddExitRequest(BaseModel):
    dest_first: int
    dest_last: int
    gateway_id: int


@router.post("/{node_id}/exits")
def add_exit(node_id: int, req: AddExitRequest):
    try:
        result = manager.add_exit(
            node_id, req.dest_first, req.dest_last, req.gateway_id
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{node_id}/exits")
def list_exits(node_id: int):
    try:
        exits = manager.list_exits(node_id)
        return {"node_id": node_id, "exits": exits}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/{node_id}/exits")
def delete_exit(node_id: int, dest_first: int, dest_last: int):
    try:
        result = manager.delete_exit(node_id, dest_first, dest_last)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{node_id}/neighbors")
def get_neighbors(node_id: int):
    try:
        if node_id not in manager.nodes:
            raise ValueError(f"Node {node_id} does not exist")
        neighbors = sorted(manager.get_neighbors(node_id))
        return {"node_id": node_id, "neighbors": neighbors}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{node_id}/exits/auto")
def set_node_exits_auto(node_id: int):
    """Compute shortest-path exits for a single node."""
    try:
        manager.set_node_exits(node_id)
        return {"status": "exits set", "node_id": node_id}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{node_id}/config")
def get_node_config(node_id: int):
    """Get ION configuration files and live state for a node."""
    try:
        config = manager.get_node_config(node_id)
        return config
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
