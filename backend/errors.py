"""Domain exception hierarchy for DTN-Manager.

These are raised by the service layer (DockerManager and friends) and mapped
to HTTP status codes by a central exception handler in main.py. Keeping them
here (not in the API layer) lets services raise meaningful errors without
importing FastAPI.
"""

from __future__ import annotations


class DTNManagerError(Exception):
    """Base class for all DTN-Manager domain errors."""


class NotFoundError(DTNManagerError):
    """A requested resource (node, link, scenario) does not exist. -> 404."""


class ConflictError(DTNManagerError):
    """The request conflicts with current state (duplicate, name clash). -> 409."""


class ValidationError(DTNManagerError):
    """The request is structurally invalid. -> 400/422."""


class BackendTimeout(DTNManagerError):
    """A backend operation (docker exec, ION admin) timed out. -> 504."""


class IonExecError(DTNManagerError):
    """An ION admin / container command failed (non-zero exit). -> 502."""

    def __init__(self, message: str, returncode: int | None = None,
                 stderr: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


class CapacityError(DTNManagerError):
    """A configured resource cap (MAX_NODES / MAX_LINKS) was exceeded. -> 400."""
