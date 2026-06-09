#!/usr/bin/env bash
#
# start.sh -- Deploy and run DTN-Manager
#
# Usage:
#   ./start.sh              Start the server (default port 8080)
#   ./start.sh -p 9090      Start on a custom port
#   ./start.sh --rebuild    Force-rebuild the Docker image
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="${SCRIPT_DIR}/.ionmgr.pid"
LOG_FILE="${SCRIPT_DIR}/.ionmgr.log"
PORT=8080
FORCE_REBUILD=false

# -- Parse arguments -----------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        -p|--port)   PORT="$2"; shift 2 ;;
        --rebuild)   FORCE_REBUILD=true; shift ;;
        -h|--help)
            echo "Usage: $0 [-p PORT] [--rebuild]"
            echo "  -p, --port PORT   Server port (default: 8080)"
            echo "  --rebuild         Force-rebuild the Docker image"
            exit 0
            ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
done

# -- Pre-flight checks --------------------------------------------------------
echo "+==================================================+"
echo "|          DTN-Manager -- Deployment                |"
echo "+==================================================+"
echo ""

# Check if already running
if [[ -f "$PID_FILE" ]]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "[!] Server is already running (PID $OLD_PID)"
        echo "    Run ./stop.sh first, or use a different port."
        exit 1
    else
        echo "[i] Stale PID file found, cleaning up..."
        rm -f "$PID_FILE"
    fi
fi

# Check Docker is available
if ! command -v docker &>/dev/null; then
    echo "[x] Docker is not installed or not in PATH"
    exit 1
fi

if ! docker info &>/dev/null; then
    echo "[x] Docker daemon is not running (or no permissions)"
    echo "    Try: sudo systemctl start docker"
    exit 1
fi
echo "[ok] Docker is available"

# Check Python
if ! command -v python3 &>/dev/null; then
    echo "[x] python3 is not installed"
    exit 1
fi
echo "[ok] Python3 is available"

# -- Step 1: Build Docker image (if needed) ------------------------------------
IMAGE_NAME="ion-dtn-base"
if [[ "$FORCE_REBUILD" == true ]] || ! docker image inspect "$IMAGE_NAME" &>/dev/null; then
    echo ""
    echo ">>> Building Docker image '$IMAGE_NAME' (this may take a few minutes)..."
    docker build -t "$IMAGE_NAME" -f "${SCRIPT_DIR}/docker/Dockerfile.ion" "${SCRIPT_DIR}/docker/"
    echo "[ok] Docker image built"
else
    echo "[ok] Docker image '$IMAGE_NAME' already exists"
fi

# -- Step 2: Install Python dependencies --------------------------------------
echo ""
echo ">>> Installing Python dependencies..."
pip install -q -r "${SCRIPT_DIR}/backend/requirements.txt"
echo "[ok] Dependencies installed"

# -- Step 3: Create scenarios directory ----------------------------------------
SCENARIOS_DIR="${SCRIPT_DIR}/scenarios"
mkdir -p "$SCENARIOS_DIR"
echo "[ok] Scenarios directory ready"

# -- Step 4: Start the server --------------------------------------------------
echo ""
echo ">>> Starting server on port $PORT..."
cd "$SCRIPT_DIR"
nohup python3 -m uvicorn backend.main:app \
    --host 0.0.0.0 \
    --port "$PORT" \
    > "$LOG_FILE" 2>&1 &
SERVER_PID=$!
echo "$SERVER_PID" > "$PID_FILE"

# Wait a moment and verify it started
sleep 2
if kill -0 "$SERVER_PID" 2>/dev/null; then
    echo ""
    echo "+==================================================+"
    echo "|  [ok] Server is running!                         |"
    echo "|                                                  |"
    printf "|  URL:  http://localhost:%-25s|\n" "$PORT"
    printf "|  PID:  %-41s|\n" "$SERVER_PID"
    echo "|  Log:  .ionmgr.log                              |"
    echo "|                                                  |"
    echo "|  Stop: ./stop.sh                                |"
    echo "+==================================================+"
else
    echo "[x] Server failed to start. Check log:"
    cat "$LOG_FILE"
    rm -f "$PID_FILE"
    exit 1
fi
