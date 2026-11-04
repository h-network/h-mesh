#!/usr/bin/env bash
# Container entrypoint. Holds no h-mesh logic of its own: it starts Redis
# (the one dependency setup.sh assumes is already reachable, never something
# it provisions itself beyond a bare-host convenience install), waits for it,
# then hands off entirely to setup.sh's existing non-interactive path -- the
# same wizard-bypass a scripted bare-VM install already uses. The roster,
# hire, and daemon-start steps are exactly setup.sh's own; nothing here
# re-seeds the registry or re-implements hire's bookkeeping.
#
# All configuration is environment variables -- POD, TENANT, AGENTS,
# DEFAULT_CLI, ACCOUNTS, AGENT_CLIS/AGENT_PROFILES/AGENT_PROVIDERS,
# CLAUDE_OAUTH_TOKEN_<PROFILE>, PROVIDER_LOCAL_*, TELEGRAM_*, API_TOKEN --
# see README.md's "Bootstrap script" section for the full set. Extra
# arguments (after the image's own args) pass straight through to setup.sh,
# same as install.sh does on a bare host.
set -euo pipefail

: "${POD:?POD is required (see README.md's non-interactive env vars)}"
: "${TENANT:?TENANT is required (see README.md's non-interactive env vars)}"

log() { printf '[entrypoint] %s\n' "$1"; }

# Bootstrap generates API_TOKEN even without Telegram. Until TLS or an
# explicit plaintext opt-in is configured, keep the API running on container
# loopback instead of rolling back the office's other daemons. App-level
# rejection of plaintext non-loopback listeners remains unchanged.
# This also covers direct container launches that reach setup.sh without a
# token yet: setup generates it before starting the daemons.
if [ "${H_MESH_ALLOW_PLAINTEXT:-0}" != "1" ]; then
    api_tls_ok=0
    [ -n "${API_TLS_CERT:-}" ] && [ -n "${API_TLS_KEY:-}" ] && api_tls_ok=1
    session_tls_ok=$api_tls_ok
    [ -n "${SESSION_TLS_CERT:-}" ] && [ -n "${SESSION_TLS_KEY:-}" ] && session_tls_ok=1
    if [ "$api_tls_ok" -eq 0 ]; then
        export API_BIND=127.0.0.1
        log "API is loopback-only inside the container; published API port is closed. Set API_TLS_CERT and API_TLS_KEY, or H_MESH_ALLOW_PLAINTEXT=1, then recreate the container to open it."
    fi
    [ "$session_tls_ok" -eq 1 ] || export SESSION_BIND=127.0.0.1
fi

# Not operator-configurable inside the container: compose.ports.yaml (or
# `docker run -p`) maps host API_PORT/SESSION_PORT onto the container's
# fixed 8080/8081 doors (see Dockerfile's EXPOSE 8080 8081). Pin them here
# too in case the container is launched with `--env-file` without
# compose.yaml's environment override, so a custom host port in .env never
# moves the internal listen port away from 8080/8081.
export API_PORT=8080
export SESSION_PORT=8081

# Not operator-configurable: Redis lives inside this container, loopback
# only, started a few lines down -- an externally supplied REDIS_URL
# pointing anywhere else would leave setup.sh talking to a server that
# doesn't match the one this script actually starts and waits for.
REDIS_URL="redis://127.0.0.1:6379/0"
REDIS_DATA_DIR="${REDIS_DATA_DIR:-$HOME/.h-mesh/redis}"
mkdir -p "$REDIS_DATA_DIR"

redis_pid=""
tail_pid=""
setup_ran=0

shutdown() {
    local code=$?
    trap - TERM INT EXIT
    log "shutting down (exit=$code)..."

    if [ -n "$tail_pid" ]; then
        kill "$tail_pid" 2>/dev/null || true
    fi

    # setup.sh's own daemon-start step is what actually started
    # switch/tmux_reconciler/watchdog(/api/telegram_bot/session) -- stop
    # them the same way `h-mesh upgrade` does, via the one shared
    # stop_daemons() implementation, rather than a second stop path here.
    if [ "$setup_ran" -eq 1 ]; then
        python -c '
import os, sys
from pathlib import Path

sys.path.insert(0, "h-app")
from services.daemons import merged_daemon_env, stop_daemons

tenant = os.environ["TENANT"]
run_dir = Path(os.environ.get("H_MESH_RUN_DIR") or (Path.home() / ".h-mesh" / "run" / tenant))
stop_daemons(run_dir, env=merged_daemon_env(tenant))
' 2>&1 | while IFS= read -r line; do log "$line"; done || true
    fi

    if [ -n "$redis_pid" ] && kill -0 "$redis_pid" 2>/dev/null; then
        log "stopping redis (pid $redis_pid)..."
        kill -TERM "$redis_pid" 2>/dev/null || true
        wait "$redis_pid" 2>/dev/null || true
    fi

    exit "$code"
}
trap shutdown TERM INT EXIT

log "starting redis-server (loopback only, AOF persistence at $REDIS_DATA_DIR)..."
redis-server --port 6379 --bind 127.0.0.1 --daemonize no \
    --dir "$REDIS_DATA_DIR" --appendonly yes --save '' --logfile '' &
redis_pid=$!

log "waiting for redis..."
redis_ready=0
for _ in $(seq 1 60); do
    if redis-cli -u "$REDIS_URL" ping >/dev/null 2>&1; then
        redis_ready=1
        break
    fi
    kill -0 "$redis_pid" 2>/dev/null || { log "redis exited during startup"; exit 1; }
    sleep 0.5
done
[ "$redis_ready" -eq 1 ] || { log "redis did not become ready in time"; exit 1; }
log "redis is ready"

export H_MESH_RUN_DIR="${H_MESH_RUN_DIR:-$HOME/.h-mesh/run/${TENANT}}"
mkdir -p "$H_MESH_RUN_DIR"

log "running setup.sh --non-interactive..."
/app/setup.sh \
    --pod "$POD" --tenant "$TENANT" --redis-url "$REDIS_URL" \
    --venv "$VIRTUAL_ENV" --skip-deps --skip-install --non-interactive "$@"
setup_ran=1

log "h-mesh is up. Tailing daemon logs from $H_MESH_RUN_DIR..."
shopt -s nullglob
log_files=("$H_MESH_RUN_DIR"/*.log)
if [ "${#log_files[@]}" -eq 0 ]; then
    # --no-daemons was passed through -- nothing to tail, but the container
    # is still the roster/registry's only home; stay up rather than exit 0
    # and look like a crash.
    log "no daemon logs found; idling"
    sleep infinity &
    tail_pid=$!
else
    tail -q -F "${log_files[@]}" &
    tail_pid=$!
fi
wait "$tail_pid"
