#!/usr/bin/env bash
# Install as ~/ci/run-suite.sh on the dedicated Linux test VM.
set -Eeuo pipefail
umask 077

refresh=0
if [[ ${1:-} == --refresh ]]; then
    refresh=1
    shift
fi
if [[ $# != 1 ]]; then
    echo "usage: $0 [--refresh] <git-ref>" >&2
    exit 2
fi
ref=${1#origin/}
if [[ $ref == -* ]] || ! git check-ref-format "refs/heads/$ref"; then
    echo "error: supply an origin branch name" >&2
    exit 2
fi
# Name of the util-linux lock utility, not the old project.
lock_program=FLOCK # legacy-name-allow: FLOCK
lock_program=${lock_program,,}

ci_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo=$ci_root/r
venv=$ci_root/v
lock_timeout=${CI_LOCK_TIMEOUT:-900}
minimum_kb=${CI_MIN_AVAILABLE_KB:-1572864}
if [[ ! $lock_timeout =~ ^[0-9]+$ || ! $minimum_kb =~ ^[1-9][0-9]*$ ]] ||
   (( minimum_kb < 1572864 )); then
    echo "error: timeout must be seconds; minimum memory must be at least 1572864 KiB" >&2
    exit 2
fi
mkdir -p "$ci_root/logs" "$ci_root/cache"
exec 9>"$ci_root/suite.lock"

sha=unresolved
run_root=
worktree=
unit=
log=$ci_root/logs/$(date -u +%Y%m%dT%H%M%SZ)-$$.log
touch "$log"

cleanup() {
    status=$?
    trap - EXIT INT TERM
    if [[ -n $unit ]]; then
        # Only our unique transient unit: this also kills double-forked tmux
        # and Redis descendants that escaped the test's process group.
        systemctl --user stop "$unit" >>"$log" 2>&1 || true
        systemctl --user reset-failed "$unit" >>"$log" 2>&1 || true
    fi
    if [[ -n $worktree ]]; then
        git -C "$repo" worktree remove --force "$worktree" >>"$log" 2>&1 || status=1
    fi
    if [[ -n $run_root ]]; then
        rm -rf -- "$run_root" || status=1
    fi
    /usr/bin/python3 - "$ref" "$sha" "$log" "$status" <<'PY'
import json
import re
import sys
from pathlib import Path

ref, sha, log, status = sys.argv[1:]
output = Path(log).read_text(errors="replace")
collected = re.findall(r"collected (\d+) items?", output)
passed = re.findall(r"(?:^|\W)(\d+) passed", output)
summary = dict(
    ref=f"origin/{ref}", sha=sha,
    collected=int(collected[-1]) if collected else 0,
    passed=int(passed[-1]) if passed else 0,
    failed_ids=[line.removeprefix("FAILED ").split(" - ", 1)[0]
                for line in output.splitlines() if line.startswith("FAILED ")],
    exit_code=int(status), log=log,
)
print("suite summary: " + json.dumps(summary), flush=True)
PY
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "waiting for suite lock (timeout ${lock_timeout}s)"
if ! "$lock_program" -w "$lock_timeout" 9; then
    echo "error: suite lock timed out after ${lock_timeout}s; another run owns it" >&2
    exit 75
fi

check_memory() {
    local available
    available=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
    if [[ ! $available =~ ^[0-9]+$ ]] || (( available < minimum_kb )); then
        echo "error: available RAM ${available:-unknown} KiB; need ${minimum_kb} KiB to protect lab offices" >&2
        exit 1
    fi
}
check_memory
for command in git "$lock_program" systemd-run systemctl python3; do
    command -v "$command" >/dev/null || { echo "error: missing $command" >&2; exit 1; }
done
systemctl --user show --property=Version >/dev/null 2>&1 || {
    echo "error: a working user systemd manager is required for descendant cleanup" >&2
    exit 1
}

if [[ ! -d $repo ]]; then
    git clone --quiet --no-checkout https://github.com/h-network/h-mesh.git "$repo"
fi
git -C "$repo" fetch --quiet origin "+refs/heads/$ref:refs/remotes/origin/$ref"
sha=$(git -C "$repo" rev-parse --verify "refs/remotes/origin/$ref^{commit}")
run_root=$(mktemp -d "$ci_root/w.XXXXXX")
worktree=$run_root/t
git -C "$repo" worktree add --quiet --detach "$worktree" "$sha"
mkdir "$run_root/h"
if [[ ! -x $venv/bin/python ]]; then
    python3 -m venv "$venv"
    refresh=1
fi

# Never inherit office configuration, credentials, PYTHONPATH, or user installs.
# The owned bin directory may hold a privately unpacked redis-server wrapper.
clean_env=(/usr/bin/env -i "HOME=$run_root/h" "USER=$(id -un)" "LOGNAME=$(id -un)"
           "PATH=$venv/bin:$ci_root/bin:/usr/bin:/bin" LANG=C.UTF-8 TERM=dumb
           PYTHONNOUSERSITE=1 "PIP_CACHE_DIR=$ci_root/cache")
if (( refresh )); then
    "${clean_env[@]}" "$venv/bin/python" -m pip install 'setuptools>=77' wheel \
        -e "$worktree[test]" >>"$log" 2>&1
else
    # Rebind the editable installation to this exact worktree. Dependencies
    # persist between runs and can be refreshed explicitly with --refresh.
    "${clean_env[@]}" "$venv/bin/python" -m pip install --no-deps \
        --no-build-isolation -e "$worktree" >>"$log" 2>&1
fi
"${clean_env[@]}" /bin/sh -c 'command -v redis-server >/dev/null' || {
    echo "error: install redis-server or put its wrapper in $ci_root/bin" >&2
    exit 1
}
check_memory

unit=hmesh-ci-$(id -u)-$$-$RANDOM.service
echo "suite: origin/$ref @ $sha; nice 10; log: $log"
# RemainAfterExit retains the result until we read it and stop the unit.
# Stopping the unit reaps every surviving descendant before worktree removal.
systemd-run --user --quiet --unit="$unit" --service-type=exec \
    --property=RemainAfterExit=yes --property=KillMode=control-group \
    --property=TimeoutStopSec=10s --property=Nice=10 \
    --property="StandardOutput=append:$log" --property="StandardError=append:$log" \
    --working-directory="$worktree" \
    "${clean_env[@]}" "$venv/bin/python" -m tools.run_tests

while true; do
    state=$(systemctl --user show "$unit" --property=SubState --value)
    case $state in
        exited|failed|dead) break ;;
    esac
    sleep 1
done
status=$(systemctl --user show "$unit" --property=ExecMainStatus --value)
code=$(systemctl --user show "$unit" --property=ExecMainCode --value)
if [[ $code != 1 ]]; then
    status=$((128 + status))
fi
exit "$status"
