#!/bin/bash
# Generic ION entrypoint for dynamically created nodes.
# Expects:
#   NODE_NUM   — ION node number (e.g., 1, 2, 3)
#   /ion-config/node.rc       — combined ION config
#   /ion-config/node.ionconfig — ION SDR config
set -u

NODE_NUM=${NODE_NUM:-1}
CONFIG_DIR="/ion-config"

echo "=== ION-DTN Node ${NODE_NUM} ==="

cd /ion-runtime

# Copy ionconfig to runtime directory (ionadmin expects it in cwd)
if [ -f "${CONFIG_DIR}/node.ionconfig" ]; then
    cp "${CONFIG_DIR}/node.ionconfig" .
fi

# Start ION if config exists. Fail loud: if ionstart fails, exit non-zero so
# Docker marks the container failed instead of presenting a healthy container
# with no BP stack.
if [ -f "${CONFIG_DIR}/node.rc" ]; then
    echo "Starting ION from ${CONFIG_DIR}/node.rc ..."
    if ! ionstart -I "${CONFIG_DIR}/node.rc"; then
        echo "FATAL: ionstart failed for node ${NODE_NUM}" >&2
        exit 1
    fi
    # Confirm the BP stack actually responds before declaring readiness.
    if ! echo 'l' | bpadmin >/dev/null 2>&1; then
        echo "FATAL: bpadmin did not respond after ionstart (node ${NODE_NUM})" >&2
        exit 1
    fi
else
    echo "No node.rc found — ION not started (awaiting configuration)"
fi

echo "OK: Node ${NODE_NUM} ready"

# Keep container alive
exec tail -f /dev/null
