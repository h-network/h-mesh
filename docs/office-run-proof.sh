#!/usr/bin/env bash
# Fresh-install proof for `office run` visibility and run-rm (branch switch-agent/office-run).
# Stages: up | prove | down. Run only while no suite run is in progress (the original run
# was serialized with suite runs by holding ~/ci/suite.lock). Only compose project
# h-mesh-runp-proof and image h-mesh-run-proof:<sha7> are touched. No token is printed.
set -Eeuo pipefail
umask 077
ci="$HOME/ci"
SHA="${RUN_PROOF_SHA:?set RUN_PROOF_SHA to the pushed commit}"
BRANCH=switch-agent/office-run
root="$ci/run-proof"
repo="$root/repo"
image="h-mesh-run-proof:${SHA:0:7}"
pod=runp
tenant=proof
api_port=18120
sess_port=18121
stage="${1:?stage}"
mkdir -p "$ci/logs"
exec > >(tee -a "$ci/logs/run-proof-$stage-$(date -u +%Y%m%dT%H%M%SZ).log") 2>&1
ts() { date -u +%H:%M:%SZ; }
ctr="h-mesh-$pod-$tenant-h-mesh-1"
ex()  { docker exec -u ubuntu -e AGENT_NAME="${AS:-architect}" -e TMUX_TMPDIR=/home/ubuntu/.h-mesh/tmux "$ctr" "$@"; }
tm()  { docker exec -u ubuntu "$ctr" sh -c 'TMUX_TMPDIR="$HOME/.h-mesh/tmux" tmux "$@"' sh "$@"; }
compose() {
    H_MESH_TENANT_ENV_FILE="$repo/offices/$pod/$tenant/.env" H_MESH_IMAGE="$image" \
        docker compose -p "h-mesh-$pod-$tenant" -f "$repo/container/compose.yaml" \
        -f "$repo/container/compose.ports.yaml" --env-file "$repo/offices/$pod/$tenant/.env" "$@"
}
windows() { tm list-windows -t "$tenant" -F '#{window_name}:#{pane_dead}' | sort | tr '\n' ' '; }

case "$stage" in
up)
    free_kb=$(df --output=avail -k / | tail -1)
    [ "$free_kb" -ge 2621440 ] || { echo "only $((free_kb/1024)) MiB free on /, need >= 2560"; exit 1; }
    test -z "$(ss -H -ltn "sport = :$api_port or sport = :$sess_port")"
    test ! -e "$root"
    mkdir -p "$root"
    (umask 022; git clone --quiet --single-branch --branch "$BRANCH" \
        https://github.com/h-network/h-mesh.git "$repo")
    cd "$repo"
    test "$(git rev-parse HEAD)" = "$SHA"
    echo "checkout @ $(git rev-parse HEAD)"
    env -i HOME="$HOME" PATH=/usr/local/bin:/usr/bin:/bin H_MESH_IMAGE="$image" \
        AGENTS=architect DEFAULT_CLI=codex \
        ./container/bootstrap.sh --pod "$pod" --tenant "$tenant" \
        --api-port "$api_port" --session-port "$sess_port" --non-interactive >"$root/bootstrap.log" 2>&1
    tail -n 2 "$root/bootstrap.log"
    for i in $(seq 1 90); do
        if ex python -c '
import os
from pathlib import Path
run = Path.home() / ".h-mesh" / "run" / os.environ["TENANT"]
for name in ("tmux_reconciler", "core"):
    pid = run / f"{name}.pid"
    if pid.exists(): os.kill(int(pid.read_text()), 0)
' >/dev/null 2>&1 && tm has-session -t "$tenant" 2>/dev/null; then echo "office up (session $tenant)"; break; fi
        [ "$i" -eq 90 ] && { echo "office not ready"; exit 1; }
        sleep 1
    done
    echo "windows: $(windows)"
    ;;
prove)
    echo "[$(ts)] open: bash (default shell), top, and a one-shot that exits 3"
    ex h-mesh-office run scratch
    ex h-mesh-office run monitor -- top
    ex h-mesh-office run quickie -- sh -c 'echo one-shot-output; exit 3'
    sleep 12     # a few reconciler passes
    echo "windows (name:pane_dead): $(windows)"
    echo "--- office status"
    ex h-mesh-office status
    echo "--- office peers -i"
    ex h-mesh-office peers -i
    echo "--- run.windows in Redis"
    docker exec -u ubuntu "$ctr" redis-cli --raw hgetall "pod:$pod:tenant:$tenant:run.windows"
    echo "--- attach equivalent (no tty here): last lines of each pane BEFORE any send"
    for w in scratch monitor; do
        echo "== __run__$w"; tm capture-pane -p -t "$tenant:__run__$w" | grep -v '^$' | tail -4 | cut -c1-110
        tm capture-pane -p -t "$tenant:__run__$w" > "$root/before-$w.txt"
    done
    echo "[$(ts)] try to send to each run window (every attempt must be refused, exit 1)"
    set +e
    for target in scratch monitor __run__scratch "$tenant:__run__monitor" quickie; do
        ex h-mesh-office send -a "$target" "touch /tmp/PWNED-$target; q q q quit"
        echo "send -a $target -> exit $?"
    done
    ex h-mesh-office broadcast "touch /tmp/PWNED-broadcast; q q q"
    echo "broadcast -> exit $?"
    set -e
    sleep 6
    echo "--- nothing reached the panes"
    for w in scratch monitor; do
        tm capture-pane -p -t "$tenant:__run__$w" > "$root/after-$w.txt"
        if diff <(grep -v '^ *$' "$root/before-$w.txt" | grep -v -E "^top - |^Tasks:|^%Cpu|^MiB|^ *[0-9]+ ") \
                <(grep -v '^ *$' "$root/after-$w.txt" | grep -v -E "^top - |^Tasks:|^%Cpu|^MiB|^ *[0-9]+ ") >/dev/null; then
            echo "__run__$w: pane text unchanged apart from top's live counters"
        else
            echo "__run__$w: PANE CHANGED"; diff "$root/before-$w.txt" "$root/after-$w.txt" | head -10
        fi
        { grep -c -E "PWNED|q q q" "$root/after-$w.txt" || true; } | sed "s/^/__run__$w: lines containing the sent text: /"
    done
    docker exec -u ubuntu "$ctr" sh -c 'ls /tmp/PWNED* 2>/dev/null | wc -l || true' | sed 's/^/marker files created by the bash pane: /'
    echo "windows after the attempts: $(windows)   (top still running = pane_dead 0)"
    echo "--- exited state"
    ex h-mesh-office status | grep "run:quickie"
    echo "--- run-rm"
    set +e
    AS=nobody ex h-mesh-office run-rm scratch --wait 0
    echo "run-rm by a non-lead -> exit $?"
    ex h-mesh-office run-rm ghost --wait 0
    echo "run-rm of an unknown name -> exit $?"
    set -e
    for w in scratch monitor quickie; do ex h-mesh-office run-rm "$w"; done
    echo "windows after run-rm: $(windows)"
    echo "--- status after run-rm"
    ex h-mesh-office status
    ex h-mesh-office peers -i
    docker exec -u ubuntu "$ctr" redis-cli --raw hlen "pod:$pod:tenant:$tenant:run.windows" | sed 's/^/run.windows entries: /'
    docker exec -u ubuntu "$ctr" redis-cli --raw scard "pod:$pod:tenant:$tenant:run.remove" | sed 's/^/pending removal requests: /'
    ;;
down)
    if [ -f "$repo/offices/$pod/$tenant/.env" ]; then compose down -v --remove-orphans || true; fi
    rm -rf -- "$root"
    docker image rm "$image" >/dev/null 2>&1 || true
    echo "cleanup done: project h-mesh-$pod-$tenant, checkout and image $image removed"
    docker ps -a --format '{{.Names}}' | grep -c "^h-mesh-$pod-" || true
    ;;
*) echo "unknown stage"; exit 2 ;;
esac
