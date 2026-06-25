#!/usr/bin/env bash
#
# stop.sh -- Stop the DTN-Manager server.
#
# By default this stops ONLY the web server and LEAVES the Docker topology
# running, so a subsequent ./start.sh re-adopts it via reconcile() (resource
# lifecycle is decoupled from process lifecycle). Pass --teardown to also wipe
# all managed containers and networks.
#
# Usage:
#   ./stop.sh              Stop the server, preserve topology
#   ./stop.sh --teardown   Stop the server AND remove all managed resources
#   ./stop.sh --force      Force-kill the server (still preserves unless
#                          combined with --teardown)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="${SCRIPT_DIR}/.ionmgr.pid"
FORCE_MODE=false
TEARDOWN=false

# shellcheck source=docker/lib_cleanup.sh
source "${SCRIPT_DIR}/docker/lib_cleanup.sh"

# -- Parse arguments -----------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --force|-f)  FORCE_MODE=true; shift ;;
        --teardown)  TEARDOWN=true; shift ;;
        -h|--help)
            echo "Usage: $0 [--force] [--teardown]"
            echo "  --force, -f   Force-kill the server process"
            echo "  --teardown    Also remove all managed containers/networks"
            echo "                (default: server stops, topology is preserved)"
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
            for i in $(seq 1 5); do
                if ! kill -0 "$PID" 2>/dev/null; then
                    break
                fi
                sleep 1
                printf "    Waiting... (%d/5)\r" "$i"
            done
            echo ""
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

# -- Step 2: Resource teardown (opt-in only) ----------------------------------
echo ""
if [[ "$TEARDOWN" == true ]]; then
    echo ">>> Tearing down managed Docker resources..."
    if ! ionmgr_docker_ok; then
        echo "[!] Docker daemon is not reachable -- cleanup could NOT run."
        echo "    Managed resources may still be present. Re-run ./stop.sh"
        echo "    --teardown once Docker is available."
        exit 1
    fi
    ionmgr_reap_managed
    echo "[ok] Managed resources removed"
else
    echo "[i] Topology preserved (run with --teardown to remove all resources)."
    echo "    ./start.sh will re-adopt the running topology via reconcile()."
fi

# -- Done ----------------------------------------------------------------------
echo ""
echo "+==================================================+"
echo "|  [ok] Shutdown complete                          |"
echo "+==================================================+"
