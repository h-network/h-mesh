#!/usr/bin/env bash
# Proves observable at-most-once loss plus terminal recovery, NOT delivery:
# a message sent while its window is absent is dead-lettered window_missing,
# never opened, and reconciliation restores exactly one fresh pane. Ported
# from h-flock's tmux-window-loss.sh — see conservation.sh's header for the
# general bare-host environment shift.
#
# h-flock's `flock.tmuxhost` daemon (the thing that (re)creates windows to
# match desired state) maps onto h-mesh's `services.tmux_reconciler` — same
# SIGSTOP/SIGCONT mechanic to open a deliberate recreate-gap. HTTP delivery
# maps onto h-mesh's real REST API (`modules/api/server.py`'s
# POST /agents/{agent}/envelopes), started here since setup.sh doesn't run
# it by default (only the switch and reconciler are default daemons).
#
# One real difference, not just plumbing: h-flock required a non-empty
# `launch` key as a precondition (its fixture agents always had a concrete
# CLI). A bare `bash -il` window with no `launch` key at all is a normal,
# valid h-mesh state (see conservation.sh's stations) — so this port only
# requires port_type=tmux to be set, and reports launch (if any) without
# gating on it.
set -uo pipefail
. "$(dirname "$0")/_lib.sh"

POD="${POD:-acceptance}"
TENANT="${TENANT:?set TENANT}"
AGENT="${AGENT:-window-loss-probe}"
REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379/0}"
TMUX_SESSION="${TMUX_SESSION:-$TENANT}"
TMUX_TMPDIR="${TMUX_TMPDIR:-$HOME/.h-mesh/tmux}"
H_APP="${H_APP:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
RUN_DIR="${H_MESH_RUN_DIR:-$HOME/.h-mesh/run/${TENANT}}"
LOG_FILE="$RUN_DIR/switch.log"
PYTHON="${PYTHON:-python3}"
REGISTRY_POLL_SECONDS="${REGISTRY_POLL_SECONDS:-5}"
DEADLINE_SECONDS="${WINDOW_LOSS_DEADLINE_SECONDS:-20}"
[ "$DEADLINE_SECONDS" -ge 15 ] || DEADLINE_SECONDS=15
[ "$DEADLINE_SECONDS" -ge $((REGISTRY_POLL_SECONDS * 2)) ] || DEADLINE_SECONDS=$((REGISTRY_POLL_SECONDS * 2))
API_PORT="${API_PORT:-8180}"
API_TOKEN="${API_TOKEN:-$(head -c18 /dev/urandom | base64 | tr -d '=+/')}"

mkdir -p "$RUN_DIR"
[ -f "$LOG_FILE" ] || : >"$LOG_FILE"
cd "$H_APP"
export POD TENANT REDIS_URL TMUX_SESSION TMUX_TMPDIR PYTHONUNBUFFERED=1

mapfile -t reconciler_pids < <(pgrep -f 'services\.tmux_reconciler' || true)
[ "${#reconciler_pids[@]}" -eq 1 ] || incomplete tmux-window-loss "reconciler_pid_count_${#reconciler_pids[@]}"
reconciler_pid="${reconciler_pids[0]}"

api_pid=""
cleanup() {
  [ -n "$api_pid" ] && kill "$api_pid" 2>/dev/null || true
}
trap cleanup EXIT

# Seed the probe agent and let the (already-running) reconciler build its
# first window, same technique as conservation.sh's stations.
"$PYTHON" - "$POD" "$TENANT" "$AGENT" <<PY
import sys
import redis
from core.keys import prefix
pod, tenant, agent = sys.argv[1:4]
r = redis.Redis.from_url("$REDIS_URL")
r.hset(prefix(pod, tenant, resource="registry"), agent, "tmux")
PY
deadline=$((SECONDS + 30))
while [ "$SECONDS" -lt "$deadline" ]; do
  TMUX_TMPDIR="$TMUX_TMPDIR" tmux list-windows -t "$TENANT" -F '#{window_name}' 2>/dev/null | grep -qx "$AGENT" && break
  sleep 0.5
done
mapfile -t before < <(TMUX_TMPDIR="$TMUX_TMPDIR" tmux list-windows -t "$TENANT" \
  -F '#{window_name}|#{window_id}|#{pane_pid}' 2>/dev/null | awk -F'|' -v a="$AGENT" '$1==a')
[ "${#before[@]}" -eq 1 ] || incomplete tmux-window-loss "initial_window_count_${#before[@]}"
IFS='|' read -r old_name old_window_id old_pane_pid <<<"${before[0]}"

port_type_before="$("$PYTHON" - "$POD" "$TENANT" "$AGENT" <<PY
import sys
import redis
from core.registry import port_type
pod, tenant, agent = sys.argv[1:4]
r = redis.Redis.from_url("$REDIS_URL")
print(port_type(r, pod=pod, tenant=tenant, agent=agent) or "")
PY
)"
[ "$port_type_before" = "tmux" ] || incomplete tmux-window-loss target_not_tmux

# Start the real API server for the send-while-absent leg.
nohup env POD="$POD" TENANT="$TENANT" REDIS_URL="$REDIS_URL" API_TOKEN="$API_TOKEN" \
  API_BIND=127.0.0.1 API_PORT="$API_PORT" \
  "$PYTHON" -u -m services.api >>"$RUN_DIR/api.log" 2>&1 &
api_pid=$!
deadline=$((SECONDS + 20))
api_up=0
while [ "$SECONDS" -lt "$deadline" ]; do
  curl -sS -o /dev/null -w '' "http://127.0.0.1:${API_PORT}/health" 2>/dev/null && { api_up=1; break; }
  kill -0 "$api_pid" 2>/dev/null || break
  sleep 0.5
done
[ "$api_up" = 1 ] || incomplete tmux-window-loss api_server_did_not_start

resumed=0
resume_reconciler() {
  if [ "$resumed" = 0 ]; then
    resumed=1
    if ! kill -CONT "$reconciler_pid" >/dev/null 2>&1; then
      echo "ERROR: failed to SIGCONT reconciler pid=$reconciler_pid; tenant may remain wedged" >&2
      exit 125
    fi
  fi
}
trap 'resume_reconciler; cleanup' EXIT
kill -STOP "$reconciler_pid" >/dev/null 2>&1 || incomplete tmux-window-loss reconciler_stop_failed
stop_deadline=$((SECONDS + 5))
reconciler_state=""
while [ "$SECONDS" -lt "$stop_deadline" ]; do
  reconciler_state="$(awk '/^State:/{print $2}' "/proc/$reconciler_pid/status" 2>/dev/null || true)"
  [ "$reconciler_state" = T ] && break
  sleep 0.1
done
[ "$reconciler_state" = T ] || incomplete tmux-window-loss reconciler_not_stopped
TMUX_TMPDIR="$TMUX_TMPDIR" tmux kill-window -t "$old_window_id" >/dev/null 2>&1 || incomplete tmux-window-loss window_kill_failed

absent_count="$(TMUX_TMPDIR="$TMUX_TMPDIR" tmux list-windows -t "$TENANT" -F '#{window_name}' 2>/dev/null | awk -v a="$AGENT" '$0==a' | wc -l | tr -d ' ')"
expect "target absent before send" 0 "$absent_count"
if kill -0 "$old_pane_pid" >/dev/null 2>&1; then echo "  ✗ old pane pid survived window kill" >&2; _FAILED=$((_FAILED+1)); else echo "  ✓ old pane pid is gone before send"; fi

response="$(curl -sS -w $'\n%{http_code}' -H "Authorization: Bearer $API_TOKEN" \
  -H 'Content-Type: application/json' -d '{"text":"window-loss-observable-at-most-once"}' \
  "http://127.0.0.1:${API_PORT}/agents/${AGENT}/envelopes" 2>/dev/null || true)"
status="${response##*$'\n'}"
body="${response%$'\n'*}"
expect "message accepted during reconcile gap" 202 "${status:-000}"
[ "$status" = 202 ] || finish tmux-window-loss
stream_id="$("$PYTHON" -c 'import json,sys; print(json.loads(sys.stdin.read()).get("stream_id", ""))' <<<"$body" 2>/dev/null || true)"
if [ -z "$stream_id" ]; then
  echo "  ✗ HTTP 202 response has no valid stream_id" >&2
  _FAILED=$((_FAILED+1))
  finish tmux-window-loss
fi

custody_counts() {
  "$PYTHON" -c '
import json, sys
sid, target = sys.argv[1:3]
dead = opened = parse_failures = 0
with open(sys.argv[3], errors="replace") as f:
    for line in f:
        if not line.lstrip().startswith("{"):
            continue
        try:
            row = json.loads(line)
        except Exception:
            parse_failures += 1
            continue
        if row.get("stream_id") != sid or row.get("destination") != target:
            continue
        dead += row.get("event") == "dead_lettered" and row.get("reason") == "window_missing"
        opened += row.get("event") == "opened"
print(dead, opened, parse_failures)
' "$stream_id" "$AGENT" "$LOG_FILE"
}

deadline=$((SECONDS + DEADLINE_SECONDS))
dead=0; opened=0; parse_failures=0
while [ "$SECONDS" -lt "$deadline" ]; do
  read -r dead opened parse_failures <<<"$(custody_counts)"
  [ "$parse_failures" = 0 ] || incomplete tmux-window-loss malformed_custody_json
  [ "$dead" -ge 1 ] && break
  sleep 0.2
done
expect "one window_missing dead letter" 1 "$dead"
expect "stream never opened during gap" 0 "$opened"

resume_reconciler
deadline=$((SECONDS + DEADLINE_SECONDS))
recovered=""
recovered_count=0
while [ "$SECONDS" -lt "$deadline" ]; do
  recovered="$(TMUX_TMPDIR="$TMUX_TMPDIR" tmux list-windows -t "$TENANT" -F '#{window_name}|#{pane_pid}' 2>/dev/null | awk -F'|' -v a="$AGENT" '$1==a')"
  recovered_count="$(printf '%s\n' "$recovered" | sed '/^$/d' | wc -l | tr -d ' ')"
  [ "$recovered_count" = 1 ] && break
  sleep 0.2
done
expect "exactly one recovered window" 1 "${recovered_count:-0}"
new_pane_pid="${recovered#*|}"
# window_id is deliberately not compared: tmux may reuse the same id when
# rebuilding a session. A new live pane PID, with the old PID gone, is the
# recovery proof.
[ -n "$new_pane_pid" ] && kill -0 "$new_pane_pid" >/dev/null 2>&1 || { echo "  ✗ recovered pane pid is not live" >&2; _FAILED=$((_FAILED+1)); }
[ "$new_pane_pid" != "$old_pane_pid" ] || { echo "  ✗ recovered pane reused old pane pid" >&2; _FAILED=$((_FAILED+1)); }
if kill -0 "$old_pane_pid" >/dev/null 2>&1; then echo "  ✗ old pane pid is still live" >&2; _FAILED=$((_FAILED+1)); else echo "  ✓ old pane pid is gone"; fi
port_type_after="$("$PYTHON" - "$POD" "$TENANT" "$AGENT" <<PY
import sys
import redis
from core.registry import port_type
pod, tenant, agent = sys.argv[1:4]
r = redis.Redis.from_url("$REDIS_URL")
print(port_type(r, pod=pod, tenant=tenant, agent=agent) or "")
PY
)"
expect "registry desired state unchanged" "$port_type_before" "$port_type_after"
read -r dead opened parse_failures <<<"$(custody_counts)"
[ "$parse_failures" = 0 ] || incomplete tmux-window-loss malformed_custody_json
expect "dead letter remains singular after recovery" 1 "$dead"
expect "recovery did not retry or deliver stream" 0 "$opened"
finish tmux-window-loss
