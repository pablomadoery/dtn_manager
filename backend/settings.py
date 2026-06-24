"""Runtime settings for DTN-Manager, sourced from environment variables.

All knobs have safe defaults so the app runs with zero configuration. The
deployment scripts (start.sh) export these; operators can override them.
"""

from __future__ import annotations

import os


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# Maximum number of privileged ION containers / bridge networks the manager
# will provision. Bounds resource-exhaustion both interactively and on import.
MAX_NODES: int = _int_env("IONMGR_MAX_NODES", 64)
MAX_LINKS: int = _int_env("IONMGR_MAX_LINKS", 128)

# Largest scenario upload accepted by the import endpoints (bytes).
MAX_IMPORT_BYTES: int = _int_env("IONMGR_MAX_IMPORT_BYTES", 5 * 1024 * 1024)

# When true, the lifespan shutdown wipes all managed Docker resources. Default
# is FALSE: a clean shutdown PRESERVES the topology so a restart can re-adopt it
# (see DockerManager.reconcile). Set IONMGR_CLEANUP_ON_EXIT=1 for ephemeral/dev.
CLEANUP_ON_EXIT: bool = os.environ.get("IONMGR_CLEANUP_ON_EXIT", "0") == "1"

# Scenario versions this build can import. Unknown major versions are rejected;
# unknown minor versions are accepted with a warning.
SUPPORTED_SCENARIO_VERSIONS: tuple[str, ...] = ("1.0", "1.1", "1.2")
