#!/usr/bin/env bash
# Conservation under injected switch and port death — ported from h-flock's
# container/scenarios/conservation.sh to h-mesh's bare-host, Redis-backed bus.
#
# h-flock ran this inside a Docker container, shelling in via
# dx() { docker exec -i "$CONTAINER" ...; } and reading custody from
# `docker logs`. h-mesh has no container: every command below runs directly
# against the host this script is executed on (run it ON the acceptance VM,
# or over `ssh host ...`; there is no exec wrapper to route through). Custody
# comes from the switch daemon's own stdout, which setup.sh already redirects
# to a durable per-tenant log file — that file is this script's `docker logs`.
#
# Message shape and bus vocabulary carried over almost unchanged: h-flock's
# `flock.bus` (build/encode/parse, prefix, roster) maps onto h-mesh's
# core.envelope + core.keys + core.registry; `flock.switch` maps onto
# core.service.Switch; a `flock.port cons-N` delivery process maps onto a
# one-shot `python -m modules.tmux.port <agent>` subprocess, spawned per kick
# by the switch itself (core/service.py's transmission()) rather than a
# long-lived per-agent loop — same externally observable shape (a transient
# process per delivery, killable to inject port death) via a different
# internal mechanism.
#
# One deliberate behavioural difference, not just plumbing: h-flock's
# synthetic "cons-N" stations used port_type "api", a receive-only mailbox
# with no window to manage. h-mesh has the same "api" port_type available
# (modules/api/port.py drains ingress straight into a Redis mailbox stream,
# no window needed) and it would be the cheaper, more direct port. This
# script instead registers stations as port_type "tmux" and lets the
# tenant's own tmux_reconciler (already running per setup.sh) create their
# windows as plain `bash -il` panes — no `launch` key is set, so no CLI
# starts. That's a choice, not a workaround: it exercises the real
# production tmux window-creation and delivery path, which is a more direct
# hit on doubt 1 ("window creation is trustworthy") than a synthetic mailbox
# would be, at the cost of needing real tmux windows. Switch STATIONS'
# registry port_type to "api" (and drop the wait_for_windows step) for a
# lighter-weight, mailbox-only run instead.
#
# Scale is down from h-flock's STATIONS=100/ROUNDS=100 (10,000 messages) to
# STATIONS=20/ROUNDS=50 (1,000 messages) by default for a first proof pass on
# a single VM — override via env, same knobs as before. The injection
# schedule (3 switch-kills interleaved with 5 port-kills, evenly spaced) is
# generalised as fractions of the total message count so it scales with
# STATIONS*ROUNDS instead of h-flock's hardcoded 1000..9400 line targets.
#
# NOT ported in this pass: BUILD67 (memory-ceiling stress under a paused
# destination) and BROADCAST69 (fan-out conservation) — both are opt-in modes
# in the original (gated on BUILD67=1 / BROADCAST69=1), not part of its
# default run, and are left for a follow-up once this default flow is
# confirmed to pay off.
set -uo pipefail

POD="${POD:-acceptance}"
TENANT="${TENANT:?set TENANT}"
STATIONS="${STATIONS:-20}"
ROUNDS="${ROUNDS:-50}"
SEND_DELAY="${SEND_DELAY:-0.01}"
REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379/0}"
TMUX_SESSION="${TMUX_SESSION:-$TENANT}"
TMUX_TMPDIR="${TMUX_TMPDIR:-$HOME/.h-mesh/tmux}"
WORK="${WORK:-/tmp/conservation-${TENANT}}"
H_APP="${H_APP:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
RUN_DIR="${H_MESH_RUN_DIR:-$HOME/.h-mesh/run/${TENANT}}"
LOG_FILE="$RUN_DIR/switch.log"
PYTHON="${PYTHON:-python3}"

mkdir -p "$WORK" "$RUN_DIR"
[ -f "$LOG_FILE" ] || : >"$LOG_FILE"

cd "$H_APP"
export POD TENANT REDIS_URL TMUX_SESSION TMUX_TMPDIR PYTHONUNBUFFERED=1

test_switch=""
tmux_switch=""
sampler=""

cleanup() {
  [ -n "$sampler" ] && kill "$sampler" 2>/dev/null || true
  if [ -n "$test_switch" ]; then
    kill -9 "$test_switch" >/dev/null 2>&1 || true
  fi
  if [ -n "$tmux_switch" ]; then
    kill -CONT "$tmux_switch" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT
trap 'cleanup; trap - TERM; kill -TERM "$$"' INT TERM

seed_stations() {
  "$PYTHON" - "$POD" "$TENANT" "$STATIONS" <<PY
import sys
import redis
from core.keys import prefix
pod, tenant, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
r = redis.Redis.from_url("$REDIS_URL")
r.hset(prefix(pod, tenant, resource="registry"), mapping={f"cons-{i}": "tmux" for i in range(count)})
PY
}

clear_station_state() {
  "$PYTHON" - "$POD" "$TENANT" <<PY
import sys
import redis
pod, tenant = sys.argv[1:3]
r = redis.Redis.from_url("$REDIS_URL")
keys = list(r.scan_iter(match=f"pod:{pod}:tenant:{tenant}:agent:cons-*"))
if keys:
    r.delete(*keys)
PY
}

wait_for_windows() {
  local deadline=$((SECONDS + ${1:-60})) missing
  while [ "$SECONDS" -lt "$deadline" ]; do
    missing="$("$PYTHON" - "$STATIONS" "$TMUX_SESSION" "$TMUX_TMPDIR" <<PY
import subprocess, sys
count, session, tmpdir = sys.argv[1], sys.argv[2], sys.argv[3]
import os
env = dict(os.environ, TMUX_TMPDIR=tmpdir)
out = subprocess.run(["tmux", "list-windows", "-t", session, "-F", "#{window_name}"],
                      env=env, capture_output=True, text=True)
have = set(out.stdout.split())
want = {f"cons-{i}" for i in range(int(count))}
print(len(want - have))
PY
)"
    [ "$missing" = "0" ] && return 0
    sleep 1
  done
  echo "wait_for_windows timeout: $missing station windows still missing"
  return 1
}

start_test_switch() {
  env REDIS_URL="$REDIS_URL" POD="$POD" TENANT="$TENANT" REGISTRY_POLL_SECONDS=1 \
    "$PYTHON" -u -m core.service >>"$LOG_FILE" 2>&1 &
  echo $!
}

wait_for_queues() {
  local deadline=$((SECONDS + ${1:-600})) egress ingress delivering ports
  local stable_seconds="${STRAND_STABLE_SECONDS:-15}" candidate_since=-1 candidate_ingress=-1
  while [ "$SECONDS" -lt "$deadline" ]; do
    read -r egress ingress < <("$PYTHON" - "$POD" "$TENANT" <<PY
import sys
import redis
from core.keys import prefix
pod, tenant = sys.argv[1:3]
r = redis.Redis.from_url("$REDIS_URL")
def depth(resource):
    return sum(r.llen(key) for key in r.scan_iter(
        match=f"pod:{pod}:tenant:{tenant}:agent:*:{resource}"))
print(depth("egress"), depth("ingress"))
PY
)
    delivering="$("$PYTHON" - "$POD" "$TENANT" <<PY
import sys
import redis
from core.keys import prefix
pod, tenant = sys.argv[1:3]
r = redis.Redis.from_url("$REDIS_URL")
print(r.hlen(prefix(pod, tenant, resource="delivering")))
PY
)"
    [ "$egress" = "0" ] && [ "$ingress" = "0" ] && [ "$delivering" = "0" ] && return 0

    ports="$(ps -eo args= | grep -c '[m]odules.tmux.port cons-' || true)"
    if [ "$egress" = "0" ] && [ "$ingress" -gt 0 ] && [ "$delivering" = "0" ] && [ "$ports" = "0" ]; then
      if [ "$candidate_ingress" != "$ingress" ] || [ "$candidate_since" -lt 0 ]; then
        candidate_since=$SECONDS
        candidate_ingress=$ingress
      elif [ $((SECONDS - candidate_since)) -ge "$stable_seconds" ]; then
        echo "terminal strand candidate stable=${stable_seconds}s ingress=$ingress"
        return 0
      fi
    else
      candidate_since=-1
      candidate_ingress=-1
    fi
    sleep 1
  done
  echo "queue drain timeout egress=$egress ingress=$ingress delivering=$delivering ports=$ports"
  return 1
}

snapshot() {
  local elapsed="$1" pid="$2"
  "$PYTHON" - "$POD" "$TENANT" "$elapsed" "$pid" <<PY
import sys
import redis
pod, tenant, elapsed, pid = sys.argv[1:5]
r = redis.Redis.from_url("$REDIS_URL")
info = r.info("memory")
q = 0
for suffix in ("egress", "ingress", "dead"):
    for key in r.scan_iter(match=f"pod:{pod}:tenant:{tenant}:agent:*:{suffix}"):
        q += r.llen(key)
rss = 0
try:
    with open(f"/proc/{pid}/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1])
except (OSError, ValueError):
    pass
print(f"{elapsed}\t{info['used_memory']}\t{q}\t{rss}")
PY
}

reconcile() {
  local ledger="$1" label="$2"
  cp "$LOG_FILE" "$WORK/${label}.log"
  "$PYTHON" - "$POD" "$TENANT" >"$WORK/${label}.dead.jsonl" <<PY
import json, sys
import redis
from core.envelope import parse
pod, tenant = sys.argv[1:3]
r = redis.Redis.from_url("$REDIS_URL")
for key in r.scan_iter(match=f"pod:{pod}:tenant:{tenant}:agent:*:dead"):
    for raw in r.lrange(key, 0, -1):
        try:
            print(json.dumps(parse(raw)))
        except Exception:
            print("__CONSERVATION_DEAD_JSON_PARSE_FAILURE__")
PY
  "$PYTHON" - "$POD" "$TENANT" >"$WORK/${label}.ingress.jsonl" <<PY
import json, sys
import redis
from core.envelope import parse
pod, tenant = sys.argv[1:3]
r = redis.Redis.from_url("$REDIS_URL")
for key in r.scan_iter(match=f"pod:{pod}:tenant:{tenant}:agent:*:ingress"):
    for raw in r.lrange(key, 0, -1):
        try:
            print(json.dumps(parse(raw)))
        except Exception:
            print("__CONSERVATION_INGRESS_JSON_PARSE_FAILURE__")
PY
  "$PYTHON" tools/scenarios/reconcile-unicast.py \
    "$ledger" "$WORK/${label}.log" "$WORK/${label}.dead.jsonl" \
    "$WORK/${label}.ingress.jsonl" "$WORK/injections.tsv"
}

echo "conservation pod=$POD tenant=$TENANT stations=$STATIONS rounds=$ROUNDS work=$WORK log=$LOG_FILE"

: >"$WORK/injections.tsv"
: >"$WORK/samples.tsv"

# One-time reset before the first station wave, not repeated inside the run:
# a killed port process never releases its delivering lock (core/dispatch.py's
# delivery_lock is a bare HSETNX with no lease/TTL) — confirmed live 2026-09-01,
# reported separately. That's a real finding when THIS run's own port-kill
# injections cause it and the reconcile below should show it as a stranded
# message. It is noise, not signal, if it's left over from a PRIOR invocation
# of this script — clear it once, up front, rather than mid-run where it would
# mask exactly the behaviour this scenario exists to measure.
pkill -9 -f 'modules\.tmux\.port cons-' 2>/dev/null || true
"$PYTHON" - "$POD" "$TENANT" <<PY
import sys
import redis
from core.keys import prefix
pod, tenant = sys.argv[1:3]
r = redis.Redis.from_url("$REDIS_URL")
r.delete(prefix(pod, tenant, resource="delivering"))
PY

seed_stations
clear_station_state
seed_stations
wait_for_windows 120 || { echo "HARNESS DEFECT: station windows never appeared"; exit 3; }

tmux_switch="$(pgrep -f 'core\.service' | head -1)"
[ -n "$tmux_switch" ] || { echo "HARNESS DEFECT: no running core.service switch found for this tenant"; exit 3; }

echo "== negative control: terminal strand is classified promptly =="
"$PYTHON" - "$POD" "$TENANT" <<PY
import sys
import redis
from core.envelope import build, encode
from core.keys import prefix
pod, tenant = sys.argv[1:3]
r = redis.Redis.from_url("$REDIS_URL")
frame = build("Message", "cons-0", "cons-1", {"sequence": "negative-strand"}, pod=pod, tenant=tenant)
r.rpush(prefix(pod, tenant, "cons-1", "ingress"), encode(frame))
PY
strand_started=$SECONDS
strand_control_stable="${STRAND_CONTROL_STABLE_SECONDS:-3}"
strand_control_timeout="${STRAND_CONTROL_TIMEOUT:-12}"
STRAND_STABLE_SECONDS="$strand_control_stable" wait_for_queues "$strand_control_timeout" \
  || { echo "HARNESS DEFECT: terminal strand was not classified promptly"; exit 3; }
strand_elapsed=$((SECONDS - strand_started))
strand_depth="$("$PYTHON" - "$POD" "$TENANT" <<PY
import sys
import redis
from core.keys import prefix
pod, tenant = sys.argv[1:3]
r = redis.Redis.from_url("$REDIS_URL")
print(r.llen(prefix(pod, tenant, "cons-1", "ingress")))
PY
)"
[ "$strand_depth" = "1" ] && [ "$strand_elapsed" -ge "$strand_control_stable" ] \
    && [ "$strand_elapsed" -lt "$strand_control_timeout" ] \
  || { echo "HARNESS DEFECT: strand gate elapsed=${strand_elapsed}s depth=$strand_depth"; exit 3; }
echo "TERMINAL STRAND DETECTED elapsed=${strand_elapsed}s depth=$strand_depth"
clear_station_state; seed_stations; wait_for_windows 120 || exit 3
[ "${STRAND_CONTROL_ONLY:-0}" = "1" ] && exit 0

echo "== negative control: duplicate =="
: >"$WORK/negative-duplicate.tsv"
"$PYTHON" - "$POD" "$TENANT" >"$WORK/negative-duplicate.tsv" <<PY
import sys, time
import redis
from core.envelope import build, encode
from core.keys import prefix
pod, tenant = sys.argv[1:3]
r = redis.Redis.from_url("$REDIS_URL")
frame = build("Message", "cons-0", "cons-1", {"sequence": "negative-duplicate"}, pod=pod, tenant=tenant)
raw = encode(frame)
print(f"negative-duplicate\t{frame['stream_id']}\tcons-1\t{time.time()}")
r.rpush(prefix(pod, tenant, "cons-0", "egress"), raw, raw)
PY
wait_for_queues
if reconcile "$WORK/negative-duplicate.tsv" negative-duplicate >"$WORK/negative-duplicate.result"; then
  cat "$WORK/negative-duplicate.result"
  echo "HARNESS DEFECT: intentional duplicate passed silently"
  exit 3
else
  rc=$?; cat "$WORK/negative-duplicate.result"
  [ "$rc" = "2" ] || { echo "HARNESS DEFECT: duplicate control failed for wrong reason rc=$rc"; exit 3; }
fi

echo "== negative control: loss =="
clear_station_state; seed_stations; wait_for_windows 120 || exit 3
kill -STOP "$tmux_switch"
: >"$WORK/negative-loss.tsv"
"$PYTHON" - "$POD" "$TENANT" >"$WORK/negative-loss.tsv" <<PY
import sys, time
import redis
from core.envelope import build, encode
from core.keys import prefix
pod, tenant = sys.argv[1:3]
r = redis.Redis.from_url("$REDIS_URL")
frame = build("Message", "cons-0", "cons-1", {"sequence": "negative-loss"}, pod=pod, tenant=tenant)
print(f"negative-loss\t{frame['stream_id']}\tcons-1\t{time.time()}")
r.rpush(prefix(pod, tenant, "cons-0", "egress"), encode(frame))
r.lpop(prefix(pod, tenant, "cons-0", "egress"))
PY
if reconcile "$WORK/negative-loss.tsv" negative-loss >"$WORK/negative-loss.result"; then
  cat "$WORK/negative-loss.result"
  echo "HARNESS DEFECT: intentional loss passed silently"
  exit 3
else
  rc=$?; cat "$WORK/negative-loss.result"
  [ "$rc" = "1" ] || { echo "HARNESS DEFECT: loss control failed for wrong reason rc=$rc"; exit 3; }
fi

echo "== clean stressed run =="
clear_station_state; seed_stations; wait_for_windows 120 || exit 3
: >"$WORK/ledger.tsv"; : >"$WORK/injections.tsv"; : >"$WORK/samples.tsv"
test_switch="$(start_test_switch)"
run_start="$(date +%s)"
(
  while true; do
    now="$(date +%s)"
    snapshot "$((now-run_start))" "$test_switch" >>"$WORK/samples.tsv" 2>/dev/null || true
    sleep 15
  done
) & sampler=$!

"$PYTHON" -u - "$POD" "$TENANT" "$STATIONS" "$ROUNDS" "$SEND_DELAY" >"$WORK/ledger.tsv" <<PY &
import sys, time
import redis
from core.envelope import build, encode
from core.keys import prefix
pod, tenant, stations, rounds, delay = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), float(sys.argv[5])
r = redis.Redis.from_url("$REDIS_URL")
for rnd in range(rounds):
    for i in range(stations):
        seq = rnd * stations + i
        dst = f"cons-{(i + 1) % stations}"
        frame = build("Message", f"cons-{i}", dst, {"sequence": seq}, pod=pod, tenant=tenant)
        print(f"{seq}\t{frame['stream_id']}\tcons-{i}\t{dst}\t{time.time()}", flush=True)
        r.rpush(prefix(pod, tenant, f"cons-{i}", "egress"), encode(frame))
        if delay:
            time.sleep(delay)
PY
producer=$!

total=$((STATIONS * ROUNDS))
targets=()
for i in 0 1 2 3 4 5 6 7; do
  targets+=("$(( total * (10 + 12 * i) / 100 ))")
done
idx=0
for target in "${targets[@]}"; do
  while [ "$(wc -l <"$WORK/ledger.tsv")" -lt "$target" ] && kill -0 "$producer" 2>/dev/null; do sleep 0.1; done
  start="$(date +%s.%N)"
  if [ "$idx" = 1 ] || [ "$idx" = 4 ] || [ "$idx" = 7 ]; then
    old="$test_switch"
    kill -9 "$old" 2>/dev/null || true
    sleep 0.2
    test_switch="$(start_test_switch)"
    end="$(date +%s.%N)"
    printf '%s\t%s\tswitch-kill\told=%s,new=%s,target=%s\n' "$start" "$end" "$old" "$test_switch" "$target" | tee -a "$WORK/injections.tsv"
  else
    killed=""
    until [ -n "$killed" ]; do
      killed="$(ps -eo pid=,args= | awk '/modules.tmux.port cons-/ && !/awk/ {print $1; exit}')"
      [ -n "$killed" ] || sleep 0.02
      kill -0 "$producer" 2>/dev/null || break
    done
    [ -n "$killed" ] && kill -9 "$killed" 2>/dev/null || true
    end="$(date +%s.%N)"
    printf '%s\t%s\tport-kill\tpid=%s,target=%s\n' "$start" "$end" "$killed" "$target" | tee -a "$WORK/injections.tsv"
  fi
  idx=$((idx + 1))
done
wait "$producer"
wait_for_queues
kill "$sampler" 2>/dev/null || true; sampler=""
snapshot "$(( $(date +%s) - run_start ))" "$test_switch" >>"$WORK/samples.tsv" 2>/dev/null || true
kill -9 "$test_switch" 2>/dev/null || true; test_switch=""
kill -CONT "$tmux_switch"; tmux_switch=""

echo "== growth samples: elapsed_s used_memory_bytes queue_depth switch_rss_kib =="
cat "$WORK/samples.tsv"
echo "== reconciliation =="
reconcile "$WORK/ledger.tsv" clean
