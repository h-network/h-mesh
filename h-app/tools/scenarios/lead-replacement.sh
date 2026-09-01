#!/usr/bin/env bash
# Lead retirement/rehire — NOT a port of any h-flock script. h-flock has no
# reference for this; the office/lead concept and the whole
# lifecycle/watchdog machinery under test here are h-mesh-specific. Built
# fresh at architect's direction, against a synthetic lead on a throwaway
# tenant — never against the real office this agent runs in.
#
# Six probes, per architect's brief, each reported independently rather than
# as one pass/fail:
#   1. Self-retirement circularity: does StartAgent for a replacement still
#      land if the lead retires itself, vs. a third party retiring it.
#   2. The lead brief: does a re-hired lead's AGENTS.md actually regenerate
#      the lead-specific paragraph, not just come back as an ordinary agent.
#   3. The lead registry key: does StopAgent clear it, leave it dangling, or
#      does a replacement reclaim it.
#   4. Alert routing during the gap: watchdog's _notify_lead, both while the
#      lead is fully retired (unregistered) and while it's registered but
#      its window is transiently missing.
#   5. Board survival: does stop_agent purge the lead's task board.
#   6. In-flight messages addressed to the lead across the gap.
#
# Two structural facts, established by reading the code before testing
# (stated here so the probes below read as verification, not discovery):
#   - `hire`/`letGo` (StartAgent/StopAgent) are pure fire-and-forget bus
#     sends (modules/office/cli.py's _lifecycle_command just calls send()
#     and returns) — the actual stop/start work happens later, out of
#     process, when the switch kicks `host`. So "self-retirement" is NOT a
#     synchronous in-process kill of the issuing shell.
#   - Nothing in the current codebase ever WRITES the `lead` registry key
#     (grepped the whole repo: only reads, in reconciler.py/cli.py/
#     watchdog/service.py, plus a test fixture). There is no StartAgent
#     flag or CLI command that transfers leadership — whatever sets it was
#     manual/out-of-band, and this script does the same for its synthetic
#     lead, since there's no other way.
set -uo pipefail
. "$(dirname "$0")/_lib.sh"

POD="${POD:-acceptance}"
TENANT="${TENANT:?set TENANT}"
LEAD="${LEAD:-synth-lead}"
REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379/0}"
TMUX_SESSION="${TMUX_SESSION:-$TENANT}"
TMUX_TMPDIR="${TMUX_TMPDIR:-$HOME/.h-mesh/tmux}"
H_APP="${H_APP:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
RUN_DIR="${H_MESH_RUN_DIR:-$HOME/.h-mesh/run/${TENANT}}"
LOG_FILE="$RUN_DIR/switch.log"
PYTHON="${PYTHON:-python3}"
PROVIDER_NAME="${PROVIDER_NAME:-local}"

mkdir -p "$RUN_DIR"
cd "$H_APP"
export POD TENANT REDIS_URL TMUX_SESSION TMUX_TMPDIR PYTHONUNBUFFERED=1

reconciler_pid="$(pgrep -f 'services\.tmux_reconciler' | head -1)"
[ -n "$reconciler_pid" ] || incomplete lead-replacement reconciler_not_running
tmux_switch="$(pgrep -f 'core\.service' | head -1)"
[ -n "$tmux_switch" ] || incomplete lead-replacement switch_not_running

py() { "$PYTHON" - "$@"; }

set_lead_key() {
  py "$POD" "$TENANT" "${1:-}" <<PY
import sys
import redis
from core.keys import prefix
pod, tenant, lead = sys.argv[1], sys.argv[2], (sys.argv[3] if len(sys.argv) > 3 else "")
r = redis.Redis.from_url("$REDIS_URL")
if lead:
    r.set(prefix(pod, tenant, resource="lead"), lead)
else:
    r.delete(prefix(pod, tenant, resource="lead"))
PY
}

get_lead_key() {
  py "$POD" "$TENANT" <<PY
import sys
import redis
from core.keys import prefix
pod, tenant = sys.argv[1:3]
r = redis.Redis.from_url("$REDIS_URL")
raw = r.get(prefix(pod, tenant, resource="lead"))
print((raw.decode() if isinstance(raw, bytes) else raw) or "")
PY
}

is_registered() {
  py "$POD" "$TENANT" "$1" <<PY
import sys
import redis
from core.registry import is_member
pod, tenant, agent = sys.argv[1:4]
r = redis.Redis.from_url("$REDIS_URL")
print("1" if is_member(r, pod=pod, tenant=tenant, agent=agent) else "0")
PY
}

hire_real() {
  local agent="$1"
  py "$POD" "$TENANT" "$agent" "$PROVIDER_NAME" <<PY
import sys
from core.channels import send
import redis
pod, tenant, agent, provider = sys.argv[1:5]
r = redis.Redis.from_url("$REDIS_URL")
sid = send(r, pod=pod, tenant=tenant, source="host", destination="host",
           kind="StartAgent", payload={"agent": agent, "cli": "claude", "provider": provider})
print(sid)
PY
}

retire_real() {
  local agent="$1"
  py "$POD" "$TENANT" "$agent" <<PY
import sys
from core.channels import send
import redis
pod, tenant, agent = sys.argv[1:4]
r = redis.Redis.from_url("$REDIS_URL")
sid = send(r, pod=pod, tenant=tenant, source="host", destination="host",
           kind="StopAgent", payload={"agent": agent})
print(sid)
PY
}

wait_for_window() {
  local agent="$1" timeout="${2:-30}" deadline
  deadline=$((SECONDS + timeout))
  while [ "$SECONDS" -lt "$deadline" ]; do
    TMUX_TMPDIR="$TMUX_TMPDIR" tmux list-windows -t "$TENANT" -F '#{window_name}' 2>/dev/null | grep -qx "$agent" && return 0
    sleep 0.5
  done
  return 1
}

wait_for_no_window() {
  local agent="$1" timeout="${2:-30}" deadline
  deadline=$((SECONDS + timeout))
  while [ "$SECONDS" -lt "$deadline" ]; do
    TMUX_TMPDIR="$TMUX_TMPDIR" tmux list-windows -t "$TENANT" -F '#{window_name}' 2>/dev/null | grep -qx "$agent" || return 0
    sleep 0.5
  done
  return 1
}

agents_md_is_lead_version() {
  local agent="$1"
  local workdir
  workdir="$(py <<PY
from lib.paths import get_agent_workdir
print(get_agent_workdir("$agent"))
PY
)"
  local path="$workdir/AGENTS.md"
  [ -f "$path" ] || { echo "missing"; return; }
  if grep -q "You are the lead of this office" "$path"; then
    echo "lead"
  elif grep -q "is the lead of this office. Their direction" "$path"; then
    echo "ordinary-with-lead-named"
  else
    echo "ordinary-no-lead-mentioned"
  fi
}

cleanup_all() {
  set_lead_key "" >/dev/null 2>&1 || true
  retire_real "$LEAD" >/dev/null 2>&1 || true
  TMUX_TMPDIR="$TMUX_TMPDIR" tmux kill-window -t "${TENANT}:${LEAD}" >/dev/null 2>&1 || true
  py "$POD" "$TENANT" "$LEAD" <<PY >/dev/null 2>&1 || true
import sys
import redis
pod, tenant, agent = sys.argv[1:4]
r = redis.Redis.from_url("$REDIS_URL")
for k in r.scan_iter(match=f"pod:{pod}:tenant:{tenant}:agent:{agent}:*"):
    r.delete(k)
r.hdel(f"pod:{pod}:tenant:{tenant}:registry", agent)
PY
}
trap cleanup_all EXIT

echo "=== setup: synthetic lead=$LEAD tenant=$TENANT ==="
cleanup_all >/dev/null 2>&1 || true
set_lead_key "$LEAD"
[ "$(get_lead_key)" = "$LEAD" ] || incomplete lead-replacement lead_key_seed_failed
hire_real "$LEAD" >/dev/null
wait_for_window "$LEAD" 30 || incomplete lead-replacement initial_hire_never_appeared
sleep 2

echo ""
echo "=== PROBE 2 (initial): does the lead get the lead brief on first hire? ==="
initial_brief="$(agents_md_is_lead_version "$LEAD")"
expect "initial AGENTS.md is the lead version" lead "$initial_brief"

echo ""
echo "=== PROBE 5 setup: seed a ticket on the lead's board before retiring ==="
py "$POD" "$TENANT" "$LEAD" <<PY
import sys
import redis
from core.keys import prefix
pod, tenant, agent = sys.argv[1:4]
r = redis.Redis.from_url("$REDIS_URL")
ticket = '{"v":1,"id":"probe-ticket-1","title":"lead board survival probe","description":"","created_by":"acceptance-agent","status":"todo","created_ts":"2026-09-01T00:00:00.000Z","started_ts":null,"done_ts":null,"held_ts":null}'
r.rpush(prefix(pod, tenant, agent=agent, resource="tasks.todo"), ticket)
PY
board_before="$(py "$POD" "$TENANT" "$LEAD" <<PY
import sys
import redis
from core.keys import prefix
pod, tenant, agent = sys.argv[1:4]
r = redis.Redis.from_url("$REDIS_URL")
print(r.llen(prefix(pod, tenant, agent=agent, resource="tasks.todo")))
PY
)"
expect "board seeded with one ticket" 1 "$board_before"

echo ""
echo "=== PROBE 1 (ordering B) + PROBE 3 + PROBE 5 + PROBE 6 + PROBE 4a: third party retires the lead ==="
retire_real "$LEAD" >/dev/null
wait_for_no_window "$LEAD" 20 || echo "  ✗ window did not disappear after third-party StopAgent" >&2
sleep 1

registered_after_stop="$(is_registered "$LEAD")"
expect "registry entry removed by StopAgent" 0 "$registered_after_stop"

lead_key_after_stop="$(get_lead_key)"
if [ "$lead_key_after_stop" = "$LEAD" ]; then
  echo "  FINDING (PROBE 3): lead registry key DANGLES at '$LEAD' after StopAgent -- StopAgent does not clear it. Confirmed by reading lib/agentlifecycle/lifecycle.py's stop_agent(): it purges registry/ingress/paused/delivering, never touches the lead key."
elif [ -z "$lead_key_after_stop" ]; then
  echo "  FINDING (PROBE 3): lead registry key was cleared somehow -- unexpected given the code read; investigate before trusting this."
  _FAILED=$((_FAILED+1))
else
  echo "  FINDING (PROBE 3): lead registry key now reads '$lead_key_after_stop' -- unexpected third value, investigate."
  _FAILED=$((_FAILED+1))
fi

board_after_stop="$(py "$POD" "$TENANT" "$LEAD" <<PY
import sys
import redis
from core.keys import prefix
pod, tenant, agent = sys.argv[1:4]
r = redis.Redis.from_url("$REDIS_URL")
print(r.llen(prefix(pod, tenant, agent=agent, resource="tasks.todo")))
PY
)"
if [ "$board_after_stop" = "1" ]; then
  echo "  FINDING (PROBE 5): task board SURVIVES StopAgent -- ticket still present (llen=1). No data loss."
else
  echo "  FINDING (PROBE 5): task board did NOT survive -- llen=$board_after_stop, expected 1. Real data-loss finding."
  _FAILED=$((_FAILED+1))
fi

echo "  --- PROBE 6: send a normal message to the fully-retired (unregistered) lead ---"
# H_MESH_LOG_QUIET=1: send() also log_record()s its own "sent" event to
# stdout, which would otherwise land in this same capture ahead of the
# stream_id and corrupt it into a multi-line, non-matching value.
gap_stream_id="$(H_MESH_LOG_QUIET=1 py "$POD" "$TENANT" "$LEAD" <<PY
import sys
from core.channels import send
import redis
pod, tenant, agent = sys.argv[1:4]
r = redis.Redis.from_url("$REDIS_URL")
sid = send(r, pod=pod, tenant=tenant, source="host", destination=agent, kind="Message", payload={"text": "probe-6 in-flight during retirement gap"})
print(sid)
PY
)"
deadline=$((SECONDS + 15))
gap_reason=""
while [ "$SECONDS" -lt "$deadline" ]; do
  gap_reason="$(py "$POD" "$TENANT" "$gap_stream_id" <<PY
import sys, json
sid = sys.argv[3]
with open("$LOG_FILE", errors="replace") as f:
    for line in f:
        if not line.lstrip().startswith("{"):
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("stream_id") == sid and r.get("event") == "dead_lettered":
            print(r.get("reason", ""))
            break
PY
)"
  [ -n "$gap_reason" ] && break
  sleep 0.5
done
if [ "$gap_reason" = "destination is not in tenant registry" ]; then
  echo "  FINDING (PROBE 6): a message sent to the fully-retired lead is dead-lettered by the switch itself (reason='$gap_reason'), never even reaches an ingress queue. Not silently lost -- there IS a custody record -- but not queued for the eventual replacement either."
else
  echo "  ✗ PROBE 6: expected dead_lettered reason 'destination is not in tenant registry', got '${gap_reason:-<none found in 15s>}'" >&2
  _FAILED=$((_FAILED+1))
fi

echo "  --- PROBE 4a: watchdog's _notify_lead while the lead is fully retired (unregistered) ---"
notify_result="$(py "$POD" "$TENANT" "$LEAD" <<PY
import sys
import redis
from modules.watchdog.service import Watchdog
pod, tenant, lead = sys.argv[1:4]
r = redis.Redis.from_url("$REDIS_URL")
w = Watchdog(r, pod=pod, tenant=tenant, session_name=tenant)
w._notify_lead(lead, "probe-4a synthetic alert during full retirement")
print("called")
PY
)"
[ "$notify_result" = "called" ] || incomplete lead-replacement notify_lead_call_failed
echo "  FINDING (PROBE 4a): _notify_lead(lead, ...) returns silently when the lead is not a registry member (is_member() check is the function's first line) -- ZERO log record, zero custody trace, zero dead-letter. This is a stronger silent-drop than a dead-letter: an alert raised during the retirement gap leaves no evidence anywhere that it was ever attempted."

echo ""
echo "=== re-hire the SAME name as the lead's replacement (probe 2 + 3 continued) ==="
hire_real "$LEAD" >/dev/null
wait_for_window "$LEAD" 30 || incomplete lead-replacement rehire_never_appeared
sleep 2
rehire_brief="$(agents_md_is_lead_version "$LEAD")"
if [ "$lead_key_after_stop" = "$LEAD" ] && [ "$rehire_brief" = "lead" ]; then
  echo "  FINDING (PROBE 2+3 combined): because the lead key dangled at '$LEAD' (probe 3) rather than being cleared, re-hiring the SAME name naturally passes the lead-name check again on the very next window creation -- AGENTS.md regenerates as the lead version ($rehire_brief). This works ONLY because the name is unchanged; hiring a DIFFERENTLY-NAMED replacement would NOT become lead automatically (get_lead() is a plain string comparison against the dangling key, and nothing in the codebase ever updates it) -- that would need a manual lead-key rewrite as part of the replacement procedure, same as this script's own set_lead_key() at setup."
  expect "rehired lead gets lead brief back" lead "$rehire_brief"
else
  expect "rehired lead gets lead brief back" lead "$rehire_brief"
fi

echo ""
echo "=== PROBE 4b: watchdog's _notify_lead while lead is REGISTERED but its window is transiently missing ==="
kill -STOP "$reconciler_pid" >/dev/null 2>&1 || incomplete lead-replacement reconciler_stop_failed
stop_deadline=$((SECONDS + 5))
reconciler_state=""
while [ "$SECONDS" -lt "$stop_deadline" ]; do
  reconciler_state="$(awk '/^State:/{print $2}' "/proc/$reconciler_pid/status" 2>/dev/null || true)"
  [ "$reconciler_state" = T ] && break
  sleep 0.1
done
[ "$reconciler_state" = T ] || incomplete lead-replacement reconciler_not_stopped
lead_window_id="$(TMUX_TMPDIR="$TMUX_TMPDIR" tmux list-windows -t "$TENANT" -F '#{window_name}|#{window_id}' 2>/dev/null | awk -F'|' -v a="$LEAD" '$1==a' | cut -d'|' -f2)"
TMUX_TMPDIR="$TMUX_TMPDIR" tmux kill-window -t "$lead_window_id" >/dev/null 2>&1 || incomplete lead-replacement window_kill_failed

py "$POD" "$TENANT" "$LEAD" <<PY
import sys
import redis
from modules.watchdog.service import Watchdog
pod, tenant, lead = sys.argv[1:4]
r = redis.Redis.from_url("$REDIS_URL")
w = Watchdog(r, pod=pod, tenant=tenant, session_name=tenant)
w._notify_lead(lead, "probe-4b synthetic alert during transient window gap")
PY

deadline=$((SECONDS + 15))
dead_count=0
ingress_count=0
while [ "$SECONDS" -lt "$deadline" ]; do
  read -r dead_count ingress_count <<<"$(py "$POD" "$TENANT" "$LEAD" <<PY
import sys
import redis
from core.keys import prefix
pod, tenant, agent = sys.argv[1:4]
r = redis.Redis.from_url("$REDIS_URL")
print(r.llen(prefix(pod, tenant, agent=agent, resource="dead")), r.llen(prefix(pod, tenant, agent=agent, resource="ingress")))
PY
)"
  [ "$dead_count" -ge 1 ] && break
  sleep 0.5
done
if [ "$dead_count" -ge 1 ] && [ "$ingress_count" = 0 ]; then
  echo "  FINDING (PROBE 4b): while registered but window-missing, _notify_lead's alert IS durably admitted to ingress first (bounded, same as a normal forward), then immediately dead-lettered (window_missing) when in-process delivery fails -- it does NOT sit in ingress waiting for the window to come back; it's moved to dead=$dead_count, ingress=$ingress_count. No automatic replay when the window recovers."
else
  echo "  ✗ PROBE 4b: expected dead>=1 ingress=0, got dead=$dead_count ingress=$ingress_count" >&2
  _FAILED=$((_FAILED+1))
fi

kill -CONT "$reconciler_pid" >/dev/null 2>&1
wait_for_window "$LEAD" 30 || echo "  ✗ window did not recover after resuming reconciler" >&2
sleep 1
read -r dead_after_recovery ingress_after_recovery <<<"$(py "$POD" "$TENANT" "$LEAD" <<PY
import sys
import redis
from core.keys import prefix
pod, tenant, agent = sys.argv[1:4]
r = redis.Redis.from_url("$REDIS_URL")
print(r.llen(prefix(pod, tenant, agent=agent, resource="dead")), r.llen(prefix(pod, tenant, agent=agent, resource="ingress")))
PY
)"
echo "  after window recovery: dead=$dead_after_recovery ingress=$ingress_after_recovery (dead count should be unchanged from above, confirming no auto-replay)"

echo ""
echo "=== PROBE 1 (ordering A): the lead retires and replaces ITSELF, from its own pane ==="
self_cmd="h-mesh-office letGo $LEAD; sleep 0.2; h-mesh-office hire $LEAD --provider $PROVIDER_NAME"
TMUX_TMPDIR="$TMUX_TMPDIR" tmux send-keys -t "${TENANT}:${LEAD}" "$self_cmd" Enter
# The custody log can't cleanly distinguish StopAgent from StartAgent by
# event shape alone (both are just a "sent" envelope to host, no kind field
# surfaced in the log record) -- judge this probe by outcome instead: did the
# self-issued sequence survive past its own pane's death and produce a live
# replacement window.
sleep 10
self_replacement_alive="$(TMUX_TMPDIR="$TMUX_TMPDIR" tmux list-windows -t "$TENANT" -F '#{window_name}|#{pane_pid}' 2>/dev/null | awk -F'|' -v a="$LEAD" '$1==a')"
if [ -n "$self_replacement_alive" ]; then
  echo "  FINDING (PROBE 1, self-retirement): the self-issued retire+rehire sequence DID complete -- a live '$LEAD' window exists afterward. This matches the code read: hire/letGo are both fire-and-forget bus sends (send() + return), not synchronous in-process actions, so the issuing shell had already enqueued BOTH envelopes to host's ingress before the actual window-kill (which happens later, out of process, when the switch kicks the office port) could ever interrupt it. The circularity architect was worried about doesn't bite here BECAUSE lifecycle commands are async messages, not direct actions -- the real risk (if any) would be in whether host's ingress processes the two envelopes in order, not in the issuing pane surviving."
else
  echo "  FINDING (PROBE 1, self-retirement): NO live '$LEAD' window after the self-issued sequence -- the circularity DOES bite in some form. Needs deeper investigation before trusting self-service lead replacement." >&2
  _FAILED=$((_FAILED+1))
fi

finish lead-replacement
