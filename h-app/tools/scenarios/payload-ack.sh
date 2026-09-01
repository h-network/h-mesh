#!/usr/bin/env bash
# Send-and-verify-receipt — ported from h-flock's payload-ack.sh to h-mesh's
# bare-host bus. See conservation.sh's header for the general environment
# shift (no container, no dx() wrapper, custody read from the switch
# daemon's own stdout log instead of `docker logs`).
#
# One real simplification, not just plumbing: h-flock's switch resolves its
# delivery process (`flock.port`) by PATH lookup at kick time, so the
# original script had to shim the actual installed `flock.port` executable
# with a no-op and restore it on exit, to stop the real port racing this
# scenario's own bespoke consumer (payload-ack-port.py) for the same
# ingress. h-mesh's switch (core/service.py's transmission()) instead
# builds its kick target from the registry's port_type as a fixed module
# path — `modules.<port_type>.port` — so this scenario registers its
# stations under a port_type (`payload`) with no real module and installs
# its own tiny no-op stub at that path for the run's duration, removed on
# exit. Same shim-and-restore shape as h-flock, just targeting a module path
# instead of a PATH-resolved executable.
#
# ⚠ Do NOT just let the kick fail on a genuinely missing module instead of
# installing the stub. Measured live: the switch's failed-import child
# shares the switch's own stdout/stderr fd (transmission()'s bare
# subprocess.Popen, no stdout= override), and its interpreter error text can
# land on the same file, in the same instant, as this scenario's own custody
# JSON line, with no newline between them — tearing one legitimate record
# into an unparseable line for any line-based reader (reported separately,
# see project memory / architect). A real installed no-op writes nothing at
# all, so there's nothing to race.
#
# payload-ack-judge.py is copied unmodified: pure custody-log reasoning, no
# flock-specific imports.
set -uo pipefail

POD="${POD:-acceptance}"
TENANT="${TENANT:?set TENANT}"
COUNT="${COUNT:-2}"
ROUNDS="${ROUNDS:-10}"
RUN_ID="${RUN_ID:-$(date +%s)-$$}"
PREFIX="payload-${RUN_ID}-"
REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379/0}"
H_APP="${H_APP:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
RUN_DIR="${H_MESH_RUN_DIR:-$HOME/.h-mesh/run/${TENANT}}"
LOG_FILE="$RUN_DIR/switch.log"
WORK="${WORK:-/tmp/payload-ack-${TENANT}-${RUN_ID}}"
PYTHON="${PYTHON:-python3}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --count) COUNT="$2"; shift 2 ;;
    --rounds) ROUNDS="$2"; shift 2 ;;
    --work) WORK="$2"; shift 2 ;;
    *) echo "INCOMPLETE: unknown option" >&2; exit 100 ;;
  esac
done

mkdir -p "$WORK" "$RUN_DIR"
[ -f "$LOG_FILE" ] || : >"$LOG_FILE"
cd "$H_APP"
export POD TENANT REDIS_URL PYTHONUNBUFFERED=1

names="$(for i in $(seq 1 "$COUNT"); do printf '%s%s ' "$PREFIX" "$i"; done | sed 's/[[:space:]]*$//')"
port_pid=""
STUB_DIR="$H_APP/modules/payload"
stub_installed=0

restore_kick() {
  [ -n "$port_pid" ] && kill "$port_pid" 2>/dev/null || true
  if [ -n "$names" ]; then
    "$PYTHON" - "$POD" "$TENANT" $names <<PY
import sys
import redis
from core.keys import prefix
pod, tenant = sys.argv[1:3]
names = sys.argv[3:]
r = redis.Redis.from_url("$REDIS_URL")
if names:
    r.hdel(prefix(pod, tenant, resource="registry"), *names)
PY
  fi
  [ "$stub_installed" = "1" ] && rm -rf "$STUB_DIR"
}
trap restore_kick EXIT

# Install a real, silent no-op at the module path the switch's kick resolves
# for port_type "payload" — see header comment for why this matters over
# just letting the import fail.
if [ -e "$STUB_DIR" ]; then
  echo "INCOMPLETE: $STUB_DIR already exists, refusing to overwrite" >&2
  exit 100
fi
mkdir -p "$STUB_DIR"
: >"$STUB_DIR/__init__.py"
printf 'def main() -> None:\n    pass\n\n\nif __name__ == "__main__":\n    main()\n' >"$STUB_DIR/port.py"
stub_installed=1

"$PYTHON" - "$POD" "$TENANT" $names <<PY || { echo "INCOMPLETE: roster seed failed" >&2; exit 100; }
import sys
import redis
from core.keys import prefix
pod, tenant = sys.argv[1:3]
names = sys.argv[3:]
r = redis.Redis.from_url("$REDIS_URL")
r.hset(prefix(pod, tenant, resource="registry"), mapping={name: "payload" for name in names})
PY

nohup "$PYTHON" "$H_APP/tools/scenarios/payload-ack-port.py" \
  --pod "$POD" --tenant "$TENANT" --count "$COUNT" --prefix "$PREFIX" --idle-exit 120 \
  >>"$LOG_FILE" 2>&1 &
port_pid=$!

"$PYTHON" -u - >>"$LOG_FILE" 2>&1 <<PY
import hashlib, sys
from core.channels import send
from core.logging import log_record
import redis
names = "$names".split(); rounds = int("$ROUNDS"); pod = "$POD"; tenant = "$TENANT"
r = redis.Redis.from_url("$REDIS_URL")
for rnd in range(rounds):
    for i, source in enumerate(names):
        marker = f"payload-{rnd}-{i}-{source}"
        destination = names[(i + 1) % len(names)]
        sid = send(
            r, pod=pod, tenant=tenant, source=source, destination=destination,
            kind="Message",
            payload={"marker": marker, "checksum": hashlib.sha256(marker.encode()).hexdigest()},
            module="payload-send",
        )
        log_record("payload-send", "payload_sent", stream_id=sid, source=source, destination=destination)
PY

capture_diagnostics() {
  local rc="$1"
  [ "$rc" -eq 0 ] && return 0
  echo "PAYLOAD_DIAGNOSTICS retaining work=$WORK"
  cp "$LOG_FILE" "$WORK/diagnostic-container.log" 2>/dev/null || true
  echo "NO_DOCKER_INSPECT: bare-host run, no container to inspect" >"$WORK/diagnostic-inspect.json"
  ps -ef >"$WORK/diagnostic-processes.txt" 2>&1 || true
  "$PYTHON" - "$POD" "$TENANT" >"$WORK/diagnostic-keyspace.jsonl" 2>&1 <<PY || true
import json, sys
import redis
pod, tenant = sys.argv[1:3]
r = redis.Redis.from_url("$REDIS_URL")
pattern = f"pod:{pod}:tenant:{tenant}:*"
for key in sorted(r.scan_iter(match=pattern)):
    k = key.decode() if isinstance(key, bytes) else key
    print(json.dumps({"key": k, "type": r.type(key).decode()}))
PY
  "$PYTHON" - "$POD" "$TENANT" >"$WORK/diagnostic-queues.tsv" 2>&1 <<PY || true
import sys
import redis
pod, tenant = sys.argv[1:3]
r = redis.Redis.from_url("$REDIS_URL")
pattern = f"pod:{pod}:tenant:{tenant}:agent:*"
for key in sorted(r.scan_iter(match=pattern)):
    k = key.decode() if isinstance(key, bytes) else key
    if k.endswith((":ingress", ":egress")):
        print(f"{k}\t{r.llen(key)}")
PY
  [ -s "$WORK/diagnostic-queues.tsv" ] || printf '%s\n' 'NO_NONEMPTY_QUEUES: empty lists are deleted after drain' >"$WORK/diagnostic-queues.tsv"
  printf '%s\n' 'NO_WINDOW_LOG_FILE: h-mesh has no per-tenant window-log file analog; custody rides the switch stdout log captured above' >"$WORK/diagnostic-window.log.jsonl"
  local ok=1 f
  for f in diagnostic-container.log diagnostic-inspect.json diagnostic-processes.txt diagnostic-keyspace.jsonl diagnostic-queues.tsv diagnostic-window.log.jsonl; do
    [ -s "$WORK/$f" ] || ok=0
    grep -Eq 'Traceback \(most recent call last\):|No module named' "$WORK/$f" 2>/dev/null && ok=0
  done
  sha256sum "$WORK"/diagnostic-* >"$WORK/diagnostic-sha256.txt" 2>&1 || ok=0
  [ "$ok" = 1 ] && echo "PAYLOAD_DIAGNOSTICS status=complete" || echo "PAYLOAD_DIAGNOSTICS status=incomplete" >&2
}

drained=0
zero_polls=0
for _ in $(seq 1 120); do
  depth="$("$PYTHON" - "$POD" "$TENANT" "$PREFIX" <<PY
import sys
import redis
pod, tenant, prefix_val = sys.argv[1:4]
r = redis.Redis.from_url("$REDIS_URL")
total = 0
for suffix in ("ingress", "egress"):
    for key in r.scan_iter(match=f"pod:{pod}:tenant:{tenant}:agent:{prefix_val}*:{suffix}"):
        total += r.llen(key)
print(total)
PY
)"
  if [ "${depth:-1}" = "0" ]; then
    zero_polls=$((zero_polls + 1))
    [ "$zero_polls" -ge 2 ] && { drained=1; break; }
  else
    zero_polls=0
  fi
  sleep 1
done
cp "$LOG_FILE" "$WORK/custody.log" 2>/dev/null || exit 100
[ -s "$WORK/custody.log" ] || exit 100
[ "$drained" = "1" ] || { echo "PAYLOAD_RESULT rc=100 reason=queues_not_drained"; capture_diagnostics 100; exit 100; }

expected=$((COUNT * ROUNDS))
ack_deadline=120
ack_ready=0
got=0
for _ in $(seq 1 "$ack_deadline"); do
  cp "$LOG_FILE" "$WORK/custody.poll.log" 2>/dev/null || true
  got="$("$PYTHON" "$H_APP/tools/scenarios/payload-ack-judge.py" "$WORK/custody.poll.log" --ack-count "$PREFIX" 2>/dev/null || printf 0)"
  [ "$got" -ge "$expected" ] && { ack_ready=1; break; }
  sleep 1
done
if [ "$ack_ready" -ne 1 ]; then
  echo "PAYLOAD_WAIT reason=ack_leg_unknown_timeout expected=$expected observed=$got" >&2
  capture_diagnostics 100
  echo "RESULT payload-ack incomplete reason=ack_leg_unknown_timeout" >&2
  exit 100
fi
cp "$LOG_FILE" "$WORK/custody.log" 2>/dev/null || exit 100
"$PYTHON" "$H_APP/tools/scenarios/payload-ack-judge.py" "$WORK/custody.log" "$expected" "$PREFIX"
rc=$?
[ "$rc" -eq 0 ] || capture_diagnostics "$rc"
if [ "$rc" -eq 0 ]; then
  echo "RESULT payload-ack pass"
else
  echo "RESULT payload-ack fail failed=$rc" >&2
fi
exit "$rc"
