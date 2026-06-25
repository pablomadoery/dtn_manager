"""Shared FastAPI dependencies.

Replaces the old per-module ``manager = None`` globals with dependency
injection off ``app.state.manager``, so reaching a route before the manager is
initialized yields a clean 503 instead of an opaque AttributeError 500.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import HTTPException, Request

if TYPE_CHECKING:
    from backend.services.docker_manager import DockerManager


def get_manager(request: Request) -> "DockerManager":
    manager = getattr(request.app.state, "manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="Manager not initialized")
    return manager
