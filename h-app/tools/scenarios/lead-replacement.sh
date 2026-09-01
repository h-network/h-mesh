#!/usr/bin/env bash
# Lead retirement/rehire — NOT a port of any script from the reference
# implementation, which has no equivalent for this; the office/lead concept
# and the whole lifecycle/watchdog machinery under test here are
# h-mesh-specific. Built fresh at architect's direction, against a synthetic
# lead on a throwaway tenant — never against the real office this agent
# runs in.
#
# Six probes, per architect's brief, each reported independently rather than
# as one pass/fail:
#   1. Self-retirement circularity: does StartAgent for a replacement still
#      land if the lead retires itself, vs. a third party retiring it.
#   2. The lead brief: does a re-hired/transferred lead's AGENTS.md actually
#      regenerate the lead-specific paragraph, not just come back as an
#      ordinary agent.
#   3. The lead registry key: does StopAgent clear it, leave it dangling, or
#      does a replacement reclaim it.
#   4. Alert routing during the gap: watchdog's _notify_lead, both while the
#      lead is fully retired (unregistered) and while it's registered but
#      its window is transiently missing.
#   5. Board survival: does stop_agent purge the lead's task board.
#   6. In-flight messages addressed to the lead across the gap.
#
# ⚠ THIS IS THE REGRESSION TEST FOR TWO FIXES, NOT JUST A DISCOVERY SCRIPT.
# The first run of this scenario (2026-09-01) found: the `lead` registry key
# was never written anywhere in the codebase and StopAgent never cleared it
# (dangled at whatever name was last lead), and watchdog's _notify_lead()
# returned silently with zero trace when the lead was unregistered. Both are
# now fixed on main:
#   - lifecycle-agent's `leadership-transfer`: StartAgent accepts a `lead`
#     boolean payload field (`office hire NAME --lead`) that atomically
#     publishes the lead key and registry row together (Lua script);
#     StopAgent does a compare-then-delete — clears the lead key ONLY if it
#     currently equals the agent being stopped.
#   - watchdog-agent's `lead-alert-custody`: _notify_lead now logs a
#     structured `lead_alert_no_lead` record with a reason before returning,
#     instead of returning silently.
# The probe assertions below test the FIXED behavior. If you're reading this
# after another change to lifecycle.py/watchdog/service.py and a probe here
# starts failing, that's this scenario doing its job — update the code or
# update the probe, but don't just widen the assertion to make it pass.
set -uo pipefail
. "$(dirname "$0")/_lib.sh"

POD="${POD:-acceptance}"
TENANT="${TENANT:?set TENANT}"
LEAD="${LEAD:-synth-lead}"
LEAD2="${LEAD2:-synth-lead-2}"
LEAD3="${LEAD3:-synth-lead-3}"
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

# hire_real <agent> [lead: 0|1]
hire_real() {
  local agent="$1" lead="${2:-0}"
  py "$POD" "$TENANT" "$agent" "$PROVIDER_NAME" "$lead" <<PY
import sys
from core.channels import send
import redis
pod, tenant, agent, provider, lead = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5] == "1"
r = redis.Redis.from_url("$REDIS_URL")
payload = {"agent": agent, "cli": "claude", "provider": provider}
if lead:
    payload["lead"] = True
sid = send(r, pod=pod, tenant=tenant, source="host", destination="host", kind="StartAgent", payload=payload)
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
  for agent in "$LEAD" "$LEAD2" "$LEAD3"; do
    retire_real "$agent" >/dev/null 2>&1 || true
    TMUX_TMPDIR="$TMUX_TMPDIR" tmux kill-window -t "${TENANT}:${agent}" >/dev/null 2>&1 || true
    py "$POD" "$TENANT" "$agent" <<PY >/dev/null 2>&1 || true
import sys
import redis
pod, tenant, agent = sys.argv[1:4]
r = redis.Redis.from_url("$REDIS_URL")
for k in r.scan_iter(match=f"pod:{pod}:tenant:{tenant}:agent:{agent}:*"):
    r.delete(k)
r.hdel(f"pod:{pod}:tenant:{tenant}:registry", agent)
PY
  done
  py "$POD" "$TENANT" <<PY >/dev/null 2>&1 || true
import sys
import redis
from core.keys import prefix
pod, tenant = sys.argv[1:3]
r = redis.Redis.from_url("$REDIS_URL")
r.delete(prefix(pod, tenant, resource="lead"))
PY
}
trap cleanup_all EXIT

echo "=== setup: hire $LEAD with --lead (the real transfer mechanism, not a raw redis write) ==="
cleanup_all >/dev/null 2>&1 || true
hire_real "$LEAD" 1 >/dev/null
wait_for_window "$LEAD" 30 || incomplete lead-replacement initial_hire_never_appeared
sleep 2
[ "$(get_lead_key)" = "$LEAD" ] || incomplete lead-replacement lead_key_not_published_on_hire

echo ""
echo "=== PROBE 2 (initial): does hire --lead get the lead brief? ==="
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
echo "=== PROBE 2/3 (differently-named transfer): hire $LEAD2 with --lead while $LEAD is still alive ==="
hire_real "$LEAD2" 1 >/dev/null
wait_for_window "$LEAD2" 30 || incomplete lead-replacement transfer_hire_never_appeared
sleep 2
expect "lead key atomically transfers to the new name" "$LEAD2" "$(get_lead_key)"
transfer_brief="$(agents_md_is_lead_version "$LEAD2")"
expect "differently-named replacement gets the lead brief" lead "$transfer_brief"

echo ""
echo "=== PROBE 1 (ordering B) + PROBE 3 + PROBE 5 + PROBE 6 + PROBE 4a: third party retires the OLD, now-non-lead $LEAD ==="
retire_real "$LEAD" >/dev/null
wait_for_no_window "$LEAD" 20 || echo "  ✗ window did not disappear after third-party StopAgent" >&2
sleep 1

expect "registry entry removed by StopAgent" 0 "$(is_registered "$LEAD")"
expect "lead key SURVIVES retiring a non-lead agent (compare-then-delete didn't match)" "$LEAD2" "$(get_lead_key)"
echo "  FINDING (PROBE 3, transfer survival): confirmed — retiring the OLD lead after leadership already transferred to $LEAD2 does not clobber the current lead key. lib/agentlifecycle/lifecycle.py's stop_agent() now does a Lua compare-then-delete (_REMOVE_MEMBERSHIP_AND_OWN_LEAD_LUA): only clears the lead key if it currently equals the agent being stopped."

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

echo "  --- PROBE 6: send a normal message to the fully-retired (unregistered) $LEAD ---"
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
  echo "  FINDING (PROBE 6): a message sent to the fully-retired $LEAD is dead-lettered by the switch itself (reason='$gap_reason'), never even reaches an ingress queue. Not silently lost -- there IS a custody record -- but not queued for the eventual replacement either. Unchanged behavior, not part of either fix (architect: consistent with the earlier decision not to build dead-letter replay machinery)."
else
  echo "  ✗ PROBE 6: expected dead_lettered reason 'destination is not in tenant registry', got '${gap_reason:-<none found in 15s>}'" >&2
  _FAILED=$((_FAILED+1))
fi

echo "  --- PROBE 4a: watchdog's _notify_lead for a fully-retired (unregistered) agent ---"
# This python subprocess is a standalone _notify_lead() call, not run inside
# the switch process — its log_record() calls print to ITS OWN stdout, not
# to switch.log (H_MESH_LOG_FILE isn't set for it). Capture that stdout
# directly with redirect_stdout rather than suppressing it and then
# searching the wrong file for evidence that was never written there.
alert_reason="$(py "$POD" "$TENANT" "$LEAD" <<PY
import io, json, sys
from contextlib import redirect_stdout
import redis
from modules.watchdog.service import Watchdog
pod, tenant, lead = sys.argv[1:4]
r = redis.Redis.from_url("$REDIS_URL")
w = Watchdog(r, pod=pod, tenant=tenant, session_name=tenant)
captured = io.StringIO()
with redirect_stdout(captured):
    w._notify_lead(lead, "probe-4a synthetic alert during full retirement")
reason = ""
for line in captured.getvalue().splitlines():
    try:
        record = json.loads(line)
    except Exception:
        continue
    if record.get("event") == "lead_alert_no_lead":
        reason = record.get("reason", "")
        break
print(reason)
PY
)"
if [ -n "$alert_reason" ]; then
  echo "  FINDING (PROBE 4a): FIXED — _notify_lead() now logs a structured lead_alert_no_lead record (reason='$alert_reason') before returning, instead of returning silently. watchdog-agent's lead-alert-custody fix confirmed live."
  expect "lead_alert_no_lead record produced for an unregistered lead" "lead '$LEAD' is not a registered agent" "$alert_reason"
else
  echo "  ✗ PROBE 4a: expected a lead_alert_no_lead record, found none -- the silent-drop bug may have regressed" >&2
  _FAILED=$((_FAILED+1))
fi

echo ""
echo "=== PROBE 4b: watchdog's _notify_lead while the CURRENT lead ($LEAD2) is registered but its window is transiently missing ==="
kill -STOP "$reconciler_pid" >/dev/null 2>&1 || incomplete lead-replacement reconciler_stop_failed
stop_deadline=$((SECONDS + 5))
reconciler_state=""
while [ "$SECONDS" -lt "$stop_deadline" ]; do
  reconciler_state="$(awk '/^State:/{print $2}' "/proc/$reconciler_pid/status" 2>/dev/null || true)"
  [ "$reconciler_state" = T ] && break
  sleep 0.1
done
[ "$reconciler_state" = T ] || incomplete lead-replacement reconciler_not_stopped
lead2_window_id="$(TMUX_TMPDIR="$TMUX_TMPDIR" tmux list-windows -t "$TENANT" -F '#{window_name}|#{window_id}' 2>/dev/null | awk -F'|' -v a="$LEAD2" '$1==a' | cut -d'|' -f2)"
TMUX_TMPDIR="$TMUX_TMPDIR" tmux kill-window -t "$lead2_window_id" >/dev/null 2>&1 || incomplete lead-replacement window_kill_failed

py "$POD" "$TENANT" "$LEAD2" <<PY
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
  read -r dead_count ingress_count <<<"$(py "$POD" "$TENANT" "$LEAD2" <<PY
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
  echo "  FINDING (PROBE 4b): while registered but window-missing, _notify_lead's alert IS durably admitted to ingress first (bounded, same as a normal forward), then immediately dead-lettered (window_missing) when in-process delivery fails -- it does NOT sit in ingress waiting for the window to come back; it's moved to dead=$dead_count, ingress=$ingress_count. Deliberately unchanged by watchdog-agent's fix (real dead-letter, unit-tested via the real deliver_tmux/DeadLetter path) -- no automatic replay when the window recovers."
else
  echo "  ✗ PROBE 4b: expected dead>=1 ingress=0, got dead=$dead_count ingress=$ingress_count" >&2
  _FAILED=$((_FAILED+1))
fi

kill -CONT "$reconciler_pid" >/dev/null 2>&1
wait_for_window "$LEAD2" 30 || echo "  ✗ window did not recover after resuming reconciler" >&2
sleep 1
read -r dead_after_recovery ingress_after_recovery <<<"$(py "$POD" "$TENANT" "$LEAD2" <<PY
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
echo "=== PROBE 3 (current-lead retirement clears the key): third party retires the CURRENT lead ($LEAD2) ==="
retire_real "$LEAD2" >/dev/null
wait_for_no_window "$LEAD2" 20 || echo "  ✗ window did not disappear after third-party StopAgent" >&2
sleep 1
expect "lead key CLEARS when the current lead is retired" "" "$(get_lead_key)"
echo "  FINDING (PROBE 3, current-lead retirement): confirmed — the same compare-then-delete that let a non-lead's retirement leave the key alone correctly clears it when the retired agent IS the current lead. No dangling key left behind for the next hire to coincidentally match."

echo ""
echo "=== PROBE 1 (ordering A): a lead retires and replaces ITSELF with --lead, from its own pane ==="
hire_real "$LEAD3" 1 >/dev/null
wait_for_window "$LEAD3" 30 || incomplete lead-replacement self_test_hire_never_appeared
sleep 2
self_cmd="h-mesh-office letGo $LEAD3; sleep 0.2; h-mesh-office hire $LEAD3 --provider $PROVIDER_NAME --lead"
TMUX_TMPDIR="$TMUX_TMPDIR" tmux send-keys -t "${TENANT}:${LEAD3}" "$self_cmd" Enter
# The custody log can't cleanly distinguish StopAgent from StartAgent by
# event shape alone (both are just a "sent" envelope to host, no kind field
# surfaced in the log record) -- judge this probe by outcome instead: did the
# self-issued sequence survive past its own pane's death, produce a live
# replacement window, AND actually come back as lead (not just any window --
# the old version of this probe only checked window existence; with the
# --lead flag now real, checking the brief is the stronger, correct test).
sleep 10
self_replacement_alive="$(TMUX_TMPDIR="$TMUX_TMPDIR" tmux list-windows -t "$TENANT" -F '#{window_name}|#{pane_pid}' 2>/dev/null | awk -F'|' -v a="$LEAD3" '$1==a')"
if [ -n "$self_replacement_alive" ]; then
  self_brief="$(agents_md_is_lead_version "$LEAD3")"
  echo "  FINDING (PROBE 1, self-retirement): the self-issued retire+rehire --lead sequence DID complete -- a live '$LEAD3' window exists afterward, brief=$self_brief. This matches the code read: hire/letGo are both fire-and-forget bus sends (send() + return), not synchronous in-process actions, so the issuing shell had already enqueued BOTH envelopes to host's ingress before the actual window-kill (which happens later, out of process, when the switch kicks the office port) could ever interrupt it. The circularity architect was worried about doesn't bite here BECAUSE lifecycle commands are async messages, not direct actions."
  expect "self-issued replacement comes back as lead, not just alive" lead "$self_brief"
else
  echo "  FINDING (PROBE 1, self-retirement): NO live '$LEAD3' window after the self-issued sequence -- the circularity DOES bite in some form. Needs deeper investigation before trusting self-service lead replacement." >&2
  _FAILED=$((_FAILED+1))
fi

finish lead-replacement
