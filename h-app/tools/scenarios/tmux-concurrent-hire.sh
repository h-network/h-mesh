#!/usr/bin/env bash
# Two StartAgent requests for the same never-before-seen agent name, fired
# concurrently: exactly one window should exist afterward, running whichever
# CLI actually won, with no leftover duplicate or split state. Ported from
# h-flock's tmux-concurrent-hire.sh; see conservation.sh's header for the
# general bare-host environment shift. HTTP delivery maps onto h-mesh's real
# REST API, same as tmux-window-loss.sh.
#
# Real CLI, real race — per the ticket's own instruction to hire real agents
# rather than testing a toy. h-flock's original races claude against codex;
# this races claude against ITSELF (two concurrent StartAgent requests for
# the same new agent name, same cli). Not a simplification of convenience:
# h-agent's own policy is that codex (and agy) REFUSE to start under a
# local provider at all ("codex and agy refuse" rather than silently billing
# a vendor), and this box has no real OpenAI credentials for a genuine
# vendor-backed codex. Racing claude against codex here would really be
# "claude always wins because codex always refuses" — not the concurrent
# race h-flock's version tests. What's actually under test — do two
# concurrent StartAgent calls for the same never-before-seen name produce
# exactly one window, no duplicate/split registry state — is fully exercised
# by two same-cli concurrent requests; which literal CLI symbol wins is not
# the property being checked.
#
# Real hires used to need h-agent's own bin directory manually prepended to
# the RECONCILER's PATH — window_env() constructed the hired pane's PATH
# from scratch and never included wherever h-agent was actually installed
# (a live finding, reported separately). Fixed and merged same day
# (tmux-agent/hired-pane-path-known-locations, 4238f35): window_env() now
# derives the PATH from all known install locations regardless of the
# daemon's own ambient PATH. No workaround needed as of that merge.
set -uo pipefail
. "$(dirname "$0")/_lib.sh"

POD="${POD:-acceptance}"
TENANT="${TENANT:?set TENANT}"
AGENT="${AGENT:-race-hire}"
REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379/0}"
TMUX_SESSION="${TMUX_SESSION:-$TENANT}"
TMUX_TMPDIR="${TMUX_TMPDIR:-$HOME/.h-mesh/tmux}"
H_APP="${H_APP:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
RUN_DIR="${H_MESH_RUN_DIR:-$HOME/.h-mesh/run/${TENANT}}"
PYTHON="${PYTHON:-python3}"
PROVIDER_NAME="${PROVIDER_NAME:-local}"
API_PORT="${API_PORT:-8181}"
API_TOKEN="${API_TOKEN:-$(head -c18 /dev/urandom | base64 | tr -d '=+/')}"

mkdir -p "$RUN_DIR"
cd "$H_APP"
export POD TENANT REDIS_URL TMUX_SESSION TMUX_TMPDIR PYTHONUNBUFFERED=1

reconciler_pid="$(pgrep -f 'services\.tmux_reconciler' | head -1)"
[ -n "$reconciler_pid" ] || incomplete tmux-concurrent-hire reconciler_not_running
upper="$(printf '%s' "$PROVIDER_NAME" | tr '[:lower:]' '[:upper:]' | tr '-' '_')"
tr '\0' '\n' <"/proc/$reconciler_pid/environ" 2>/dev/null | grep -q "^PROVIDER_${upper}_URL=" \
  || incomplete tmux-concurrent-hire "reconciler_missing_PROVIDER_${upper}_URL"

api_pid=""
cleanup() {
  [ -n "$api_pid" ] && kill "$api_pid" 2>/dev/null || true
}
trap cleanup EXIT

nohup env POD="$POD" TENANT="$TENANT" REDIS_URL="$REDIS_URL" API_TOKEN="$API_TOKEN" \
  API_BIND=127.0.0.1 API_PORT="$API_PORT" \
  "$PYTHON" -u -m services.api >>"$RUN_DIR/api.log" 2>&1 &
api_pid=$!
deadline=$((SECONDS + 20))
api_up=0
while [ "$SECONDS" -lt "$deadline" ]; do
  curl -sS -o /dev/null "http://127.0.0.1:${API_PORT}/health" 2>/dev/null && { api_up=1; break; }
  kill -0 "$api_pid" 2>/dev/null || break
  sleep 0.5
done
[ "$api_up" = 1 ] || incomplete tmux-concurrent-hire api_server_did_not_start

TMP="$(mktemp -d)"
cleanup_all() { cleanup; rm -rf "$TMP"; }
trap cleanup_all EXIT

curl -sS -o "$TMP/a" -w '%{http_code}' -H "Authorization: Bearer $API_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"kind\":\"StartAgent\",\"payload\":{\"agent\":\"$AGENT\",\"cli\":\"claude\",\"provider\":\"$PROVIDER_NAME\"}}" \
  "http://127.0.0.1:${API_PORT}/agents/host/envelopes" >"$TMP/a.status" &
curl_a=$!
curl -sS -o "$TMP/b" -w '%{http_code}' -H "Authorization: Bearer $API_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"kind\":\"StartAgent\",\"payload\":{\"agent\":\"$AGENT\",\"cli\":\"claude\",\"provider\":\"$PROVIDER_NAME\"}}" \
  "http://127.0.0.1:${API_PORT}/agents/host/envelopes" >"$TMP/b.status" &
curl_b=$!
# ⚠ Bare `wait` waits for EVERY background job of this shell, including the
# long-running API server (api_pid) started above — which never exits on its
# own. Wait on the two curl PIDs specifically, not everything.
wait "$curl_a" "$curl_b"
status_a="$(cat "$TMP/a.status" 2>/dev/null || echo 000)"
status_b="$(cat "$TMP/b.status" 2>/dev/null || echo 000)"
expect "concurrent hire request a" 202 "$status_a"
expect "concurrent hire request b" 202 "$status_b"

sleep 8
port_type="$("$PYTHON" - "$POD" "$TENANT" "$AGENT" <<PY
import sys
import redis
from core.registry import port_type
pod, tenant, agent = sys.argv[1:4]
r = redis.Redis.from_url("$REDIS_URL")
print(port_type(r, pod=pod, tenant=tenant, agent=agent) or "")
PY
)"
launch="$("$PYTHON" - "$POD" "$TENANT" "$AGENT" <<PY
import sys
import redis
from core.keys import prefix
pod, tenant, agent = sys.argv[1:4]
r = redis.Redis.from_url("$REDIS_URL")
raw = r.get(prefix(pod, tenant, agent=agent, resource="launch"))
print((raw.decode() if isinstance(raw, bytes) else raw) or "")
PY
)"
expect "registry retains tmux port type" tmux "$port_type"
expect "winning CLI is claude" claude "$launch"
windows="$(TMUX_TMPDIR="$TMUX_TMPDIR" tmux list-windows -t "$TENANT" -F '#{window_name}|#{pane_current_command}' 2>/dev/null | awk -F'|' -v a="$AGENT" '$1==a')"
count="$(printf '%s\n' "$windows" | sed '/^$/d' | wc -l | tr -d ' ')"
expect "one window after concurrent hire" 1 "$count"
case "$windows" in *"|$launch"*) echo "  ✓ window command matches winning CLI";; *) echo "  ✗ window command does not match winning CLI" >&2; _FAILED=$((_FAILED+1));; esac

curl -sS -o "$TMP/rehire" -w '%{http_code}' -H "Authorization: Bearer $API_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"kind\":\"StartAgent\",\"payload\":{\"agent\":\"$AGENT\",\"cli\":\"$launch\",\"provider\":\"$PROVIDER_NAME\"}}" \
  "http://127.0.0.1:${API_PORT}/agents/host/envelopes" >"$TMP/rehire.status" || true
expect "unchanged rehire request" 202 "$(cat "$TMP/rehire.status")"
sleep 2
windows_after="$(TMUX_TMPDIR="$TMUX_TMPDIR" tmux list-windows -t "$TENANT" -F '#{window_name}' 2>/dev/null | awk -v a="$AGENT" '$0==a')"
expect "one window after unchanged rehire" 1 "$(printf '%s\n' "$windows_after" | sed '/^$/d' | wc -l | tr -d ' ')"

curl -sS -o /dev/null -H "Authorization: Bearer $API_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"kind\":\"StopAgent\",\"payload\":{\"agent\":\"$AGENT\"}}" \
  "http://127.0.0.1:${API_PORT}/agents/host/envelopes" || true
finish tmux-concurrent-hire
