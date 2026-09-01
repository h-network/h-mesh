#!/usr/bin/env bash
# Boundary is intentionally limited to credentials visible to tmux itself and
# to the actual pane processes — this checks what a pane process could see if
# it looked, not what a caller with host access could get some other way.
# Ported from h-flock's tmux-boundary.sh; see conservation.sh's header for
# the general bare-host environment shift. No docker exec here at all: pane
# processes and this script run as the same host user, so /proc/<pid>/environ
# is directly readable without an exec boundary to cross.
set -uo pipefail
. "$(dirname "$0")/_lib.sh"

POD="${POD:-acceptance}"
TENANT="${TENANT:-tmux-lab}"
WRITER="${WRITER:-boundary-writer}"
READER="${READER:-boundary-reader}"
REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379/0}"
TMUX_SESSION="${TMUX_SESSION:-$TENANT}"
TMUX_TMPDIR="${TMUX_TMPDIR:-$HOME/.h-mesh/tmux}"
H_APP="${H_APP:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
PYTHON="${PYTHON:-python3}"
PROHIBITED='^(API_TOKEN|REDIS_PASSWORD|REDISCLI_AUTH|REDIS_URL)='

cd "$H_APP"
export POD TENANT REDIS_URL

env_has_credentials() {
  local label="$1"; shift
  local names
  names="$("$@" 2>/dev/null | grep -E "$PROHIBITED" | cut -d= -f1 || true)"
  if [ -n "$names" ]; then
    echo "  ✗ $label exposed_names=$(printf '%s' "$names" | tr '\n' ',')" >&2
    _FAILED=$((_FAILED+1))
  else
    echo "  ✓ $label has no prohibited credential names"
  fi
}

"$PYTHON" - "$POD" "$TENANT" "$WRITER" "$READER" <<PY
import sys
import redis
from core.keys import prefix
pod, tenant, writer, reader = sys.argv[1:5]
r = redis.Redis.from_url("$REDIS_URL")
r.hset(prefix(pod, tenant, resource="registry"), mapping={writer: "tmux", reader: "tmux"})
PY
deadline=$((SECONDS + 30))
while [ "$SECONDS" -lt "$deadline" ]; do
  have="$(TMUX_TMPDIR="$TMUX_TMPDIR" tmux list-windows -t "$TENANT" -F '#{window_name}' 2>/dev/null)"
  echo "$have" | grep -qx "$WRITER" && echo "$have" | grep -qx "$READER" && break
  sleep 0.5
done

env_has_credentials "tmux-global" env TMUX_TMPDIR="$TMUX_TMPDIR" tmux show-environment -g
for agent in "$WRITER" "$READER"; do
  pane_pid="$(TMUX_TMPDIR="$TMUX_TMPDIR" tmux list-panes -t "${TENANT}:${agent}" -F '#{pane_pid}' 2>/dev/null | head -1)"
  [ -n "$pane_pid" ] || incomplete tmux-boundary "missing_pane_pid_${agent}"
  env_has_credentials "pane-${agent}" sh -c "tr '\0' '\n' </proc/$pane_pid/environ"
done
finish tmux-boundary
