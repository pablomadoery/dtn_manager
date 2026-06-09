#!/usr/bin/env bash
#
# stop.sh -- Stop DTN-Manager and clean up all resources
#
# Usage:
#   ./stop.sh              Graceful shutdown
#   ./stop.sh --force      Force-kill and remove all managed containers/networks
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="${SCRIPT_DIR}/.ionmgr.pid"
LOG_FILE="${SCRIPT_DIR}/.ionmgr.log"
FORCE_MODE=false
LABEL_KEY="ionmgr.managed"

# -- Parse arguments -----------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --force|-f) FORCE_MODE=true; shift ;;
        -h|--help)
            echo "Usage: $0 [--force]"
            echo "  --force, -f   Force-kill server and remove all Docker resources"
            exit 0
            ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
done

echo "+==================================================+"
echo "|          DTN-Manager -- Shutdown                  |"
echo "+==================================================+"
echo ""

# -- Step 1: Stop the server process ------------------------------------------
if [[ -f "$PID_FILE" ]]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        if [[ "$FORCE_MODE" == true ]]; then
            echo ">>> Force-killing server (PID $PID)..."
            kill -9 "$PID" 2>/dev/null || true
        else
            echo ">>> Sending graceful shutdown to server (PID $PID)..."
            kill -TERM "$PID" 2>/dev/null || true
            # Wait up to 5 seconds for graceful shutdown
            for i in $(seq 1 5); do
                if ! kill -0 "$PID" 2>/dev/null; then
                    break
                fi
                sleep 1
                printf "    Waiting... (%d/5)\r" "$i"
            done
            echo ""
            # Force-kill if still alive
            if kill -0 "$PID" 2>/dev/null; then
                echo "[!] Graceful shutdown timed out, force-killing..."
                kill -9 "$PID" 2>/dev/null || true
            fi
        fi
        echo "[ok] Server process stopped"
    else
        echo "[i] Server process (PID $PID) was not running"
    fi
    rm -f "$PID_FILE"
else
    echo "[i] No PID file found -- server may not be running"
fi

# -- Step 2: Clean up Docker resources -----------------------------------------
echo ""
echo ">>> Cleaning up Docker resources..."

# Remove managed containers
CONTAINERS=$(docker ps -aq --filter "label=$LABEL_KEY" 2>/dev/null || true)
if [[ -n "$CONTAINERS" ]]; then
    echo "    Removing managed containers..."
    echo "$CONTAINERS" | xargs docker rm -f 2>/dev/null || true
    echo "[ok] Containers removed"
else
    echo "[ok] No managed containers found"
fi

# Remove managed networks
NETWORKS=$(docker network ls -q --filter "label=$LABEL_KEY" 2>/dev/null || true)
if [[ -n "$NETWORKS" ]]; then
    echo "    Removing managed networks..."
    echo "$NETWORKS" | xargs docker network rm 2>/dev/null || true
    echo "[ok] Networks removed"
else
    echo "[ok] No managed networks found"
fi

# -- Done ----------------------------------------------------------------------
echo ""
echo "+==================================================+"
echo "|  [ok] Shutdown complete                          |"
echo "+==================================================+"
