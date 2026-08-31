#!/usr/bin/env bash
# setup.sh — Host bootstrap script for h-mesh.
# Installs h-mesh in an isolated venv, seeds the fixed lifecycle participants
# (host->office, api->api) in the Redis registry, and starts the required daemons
# (h-mesh-switch and h-mesh-tmux-reconciler).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

POD="${POD:-default}"
TENANT="${TENANT:-default}"
REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379/0}"
TMUX_SESSION="${TMUX_SESSION:-$TENANT}"
TMUX_TMPDIR="${TMUX_TMPDIR:-$HOME/.h-mesh/tmux}"
TMUX_SOCKET="${TMUX_SOCKET:-}"
VENV_PATH="${VENV_PATH:-}"
USE_VENV=1
SKIP_INSTALL=0
NO_DAEMONS=0

usage() {
    cat <<EOF
Usage: ./setup.sh [options]

Options:
  --pod <name>            Pod name (default: \$POD or "default")
  --tenant <name>         Tenant name (default: \$TENANT or "default")
  --redis-url <url>       Redis connection URL (default: \$REDIS_URL or "redis://127.0.0.1:6379/0")
  --session <name>        tmux session name (default: \$TMUX_SESSION or tenant name)
  --tmux-tmpdir <path>    tmux temporary/socket directory (default: \$TMUX_TMPDIR or ~/.h-mesh/tmux)
  --tmux-socket <path>    Explicit tmux socket path (default: \$TMUX_SOCKET or unset)
  --venv <path>           Virtual environment directory (default: \$VIRTUAL_ENV or .venv)
  --no-venv               Do not create/use a virtual environment; use ambient python3
  --skip-install          Skip pip install step
  --no-daemons            Seed registry only, do not start background daemons
  -h, --help              Show this help message
EOF
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --pod)
            POD="$2"; shift 2 ;;
        --tenant)
            TENANT="$2"; shift 2 ;;
        --redis-url)
            REDIS_URL="$2"; shift 2 ;;
        --session)
            TMUX_SESSION="$2"; shift 2 ;;
        --tmux-tmpdir)
            TMUX_TMPDIR="$2"; shift 2 ;;
        --tmux-socket)
            TMUX_SOCKET="$2"; shift 2 ;;
        --venv)
            VENV_PATH="$2"; USE_VENV=1; shift 2 ;;
        --no-venv)
            USE_VENV=0; shift ;;
        --skip-install)
            SKIP_INSTALL=1; shift ;;
        --no-daemons)
            NO_DAEMONS=1; shift ;;
        -h|--help)
            usage ;;
        *)
            echo "Unknown option: $1" >&2
            usage ;;
    esac
done

echo "=== h-mesh :: bootstrap ==="
echo "Pod:          $POD"
echo "Tenant:       $TENANT"
echo "Redis URL:    $REDIS_URL"
echo "tmux Session: $TMUX_SESSION"
echo "tmux Dir:     $TMUX_TMPDIR"

if [ "$USE_VENV" -eq 1 ]; then
    if [ -n "$VENV_PATH" ]; then
        TARGET_VENV="$VENV_PATH"
    elif [ -n "${VIRTUAL_ENV:-}" ]; then
        TARGET_VENV="$VIRTUAL_ENV"
    else
        TARGET_VENV="$SCRIPT_DIR/.venv"
    fi
    if [ ! -d "$TARGET_VENV" ]; then
        echo "Creating virtual environment at $TARGET_VENV..."
        python3 -m venv "$TARGET_VENV"
    fi
    PYTHON="$TARGET_VENV/bin/python"
    echo "Virtualenv:   $TARGET_VENV"
else
    PYTHON="${PYTHON:-python3}"
    echo "Python:       $PYTHON (ambient)"
fi
export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}$SCRIPT_DIR/h-app"
echo

# 1. Install h-mesh package
if [ "$SKIP_INSTALL" -eq 0 ]; then
    echo "Installing h-mesh in editable mode..."
    "$PYTHON" -m pip install -e .
    echo
fi

# 2. Verify Redis connection
echo "Checking Redis connection at $REDIS_URL..."
if ! REDIS_URL="$REDIS_URL" "$PYTHON" -c '
import os, sys, redis
redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
try:
    r = redis.Redis.from_url(redis_url)
    r.ping()
    print("✓ Redis is reachable")
except Exception as e:
    print(f"error: failed to connect to Redis at {redis_url}: {e}", file=sys.stderr)
    sys.exit(1)
'; then
    exit 1
fi
echo

# 3. Ensure isolated tmux directory exists
export TMUX_TMPDIR
mkdir -p "$TMUX_TMPDIR"
chmod 700 "$TMUX_TMPDIR"

# 4. Seed fixed lifecycle participants in the registry (host->office, api->api)
echo "Seeding registry for pod=$POD, tenant=$TENANT..."
POD="$POD" TENANT="$TENANT" REDIS_URL="$REDIS_URL" "$PYTHON" -c '
import os, sys, redis
from core.keys import prefix

pod = os.environ["POD"]
tenant = os.environ["TENANT"]
redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
r = redis.Redis.from_url(redis_url)
registry_key = prefix(pod, tenant, resource="registry")

# Fixed participants needed for routing lifecycle and API envelopes
# "host" routes to modules.office.port; "api" routes to modules.api.port
r.hset(registry_key, mapping={"host": "office", "api": "api"})
print(f"✓ Registry seeded ({registry_key}): host -> office, api -> api")
'
echo

# 5. Start required daemons (h-mesh-switch and h-mesh-tmux-reconciler)
if [ "$NO_DAEMONS" -eq 0 ]; then
    RUN_DIR="${H_MESH_RUN_DIR:-$HOME/.h-mesh/run/${TENANT}}"
    mkdir -p "$RUN_DIR"

    export POD TENANT REDIS_URL TMUX_SESSION TMUX_TMPDIR PYTHONUNBUFFERED=1
    [ -n "$TMUX_SOCKET" ] && export TMUX_SOCKET

    echo "Starting daemons (logs written to $RUN_DIR)..."

    # Start switch
    nohup "$PYTHON" -u -m core.service >> "$RUN_DIR/switch.log" 2>&1 &
    SWITCH_PID=$!
    echo "$SWITCH_PID" > "$RUN_DIR/switch.pid"
    echo "  • h-mesh-switch started (pid: $SWITCH_PID)"

    # Start tmux reconciler
    nohup "$PYTHON" -u -m services.tmux_reconciler >> "$RUN_DIR/tmux_reconciler.log" 2>&1 &
    RECONCILER_PID=$!
    echo "$RECONCILER_PID" > "$RUN_DIR/tmux_reconciler.pid"
    echo "  • h-mesh-tmux-reconciler started (pid: $RECONCILER_PID)"

    sleep 1

    # Verify both processes are still alive
    if ! kill -0 "$SWITCH_PID" 2>/dev/null; then
        echo "error: h-mesh-switch failed to start. Check $RUN_DIR/switch.log" >&2
        exit 1
    fi
    if ! kill -0 "$RECONCILER_PID" 2>/dev/null; then
        echo "error: h-mesh-tmux-reconciler failed to start. Check $RUN_DIR/tmux_reconciler.log" >&2
        exit 1
    fi

    echo
    echo "✓ Daemons are healthy."
    echo
    echo "To hire an initial agent (as host):"
    echo "  export AGENT_NAME=host POD=$POD TENANT=$TENANT"
    echo "  $PYTHON -m modules.office.cli hire <agent-name>"
    echo
    echo "To attach to the tmux session:"
    echo "  TMUX_TMPDIR=$TMUX_TMPDIR tmux attach -t $TMUX_SESSION"
fi
