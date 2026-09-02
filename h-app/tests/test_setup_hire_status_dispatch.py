"""setup.sh's roster-hire summary must never say "requested" (or "hired")
for a status it doesn't recognize. Reviewer FAILED an earlier version of
this exact block: `if HIRE_STATUS == 1: failed; else: "requested ... no
rejection seen"` folded every OTHER exit code -- a parse failure, a
launcher error, a signal death, anything unexpected -- into the same
reassuring sentence a genuinely-sent, merely-unconfirmed request gets. That
is the same false-positive shape this whole branch exists to remove,
rebuilt one layer out in the shell wrapper that reads the CLI's own honest
exit code.

This test injects a real, deterministic unexpected exit (137, matching a
real OOM-kill/signal-death shape) via a python wrapper standing in for the
venv's real interpreter -- not a signal race against the real subprocess,
which proved unreliable to time correctly on a busy shared sandbox (killing
the child of a bash command substitution does not reliably propagate its
exit status to $?, and can even leave the substitution's own subshell
hung -- measured directly, not assumed). The wrapper delegates every OTHER
invocation to the real interpreter unchanged, so the rest of setup.sh's own
run (dependency checks already skipped, but tenant_config/tmux_conf/
statusline/registry-seeding/daemon-start calls) still executes for real.
"""

import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import redis

REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP_SH = REPO_ROOT / "setup.sh"


def _skip_unless_redis() -> None:
    redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    try:
        redis.Redis.from_url(redis_url).ping()
    except Exception:
        pytest.skip("Redis server not available at REDIS_URL")


def _make_fake_venv_that_injects_an_unexpected_hire_exit(tmpdir: str, agent: str, fake_exit: int) -> str:
    """A --venv directory whose bin/python is a wrapper: the specific
    `-m modules.office.cli hire <agent>` invocation gets `fake_exit`
    immediately, with no real hire logic ever running; every other
    invocation (there are many others in a real setup.sh run) delegates
    to the REAL interpreter unchanged."""
    fake_venv = os.path.join(tmpdir, "fake_venv")
    fake_bin = os.path.join(fake_venv, "bin")
    os.makedirs(fake_bin, exist_ok=True)
    wrapper_path = os.path.join(fake_bin, "python")
    script = f"""#!/usr/bin/env bash
case " $* " in
    *"modules.office.cli hire {agent} "*)
        echo "SIMULATED: process died unexpectedly (e.g. OOM-killed) before completing" >&2
        exit {fake_exit}
        ;;
esac
exec {sys.executable} "$@"
"""
    with open(wrapper_path, "w") as f:
        f.write(script)
    st = os.stat(wrapper_path)
    os.chmod(wrapper_path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return fake_venv


def test_setup_never_says_requested_for_an_unexpected_hire_exit():
    _skip_unless_redis()
    tmpdir = tempfile.mkdtemp(prefix="h_mesh_test_hire_status_dispatch_")
    home_dir = os.path.join(tmpdir, "home")
    os.makedirs(home_dir, exist_ok=True)
    pod = f"testpod-{os.urandom(4).hex()}"
    tenant = f"testtenant-{os.urandom(4).hex()}"

    fake_bin = os.path.join(tmpdir, "bin")
    os.makedirs(fake_bin, exist_ok=True)
    fake_h_agent = os.path.join(fake_bin, "h-agent")
    with open(fake_h_agent, "w") as f:
        f.write("#!/usr/bin/env bash\nexec bash -il\n")
    os.chmod(fake_h_agent, 0o755)

    fake_venv = _make_fake_venv_that_injects_an_unexpected_hire_exit(tmpdir, "worker1", fake_exit=137)

    env = dict(os.environ)
    # ⚠ Scrub credential-shaped vars from the inherited ambient env BEFORE
    # touching setup.sh -- its own env-wins precedence (037fa2fc) means an
    # unscrubbed real CLAUDE_OAUTH_TOKEN_*/TELEGRAM_*/API_TOKEN would get
    # persisted to this throwaway test tenant's config file, not just read.
    # See the never-dump-credentials lesson; a real leak was caught here
    # while first writing this test.
    for k in list(env.keys()):
        if (k.startswith("CLAUDE_OAUTH_TOKEN_") or k == "CLAUDE_CODE_OAUTH_TOKEN"
                or k.startswith("TELEGRAM_") or k in ("API_TOKEN", "H_MESH_API_TOKEN")):
            del env[k]
    env["HOME"] = home_dir
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["PYTHONPATH"] = str(REPO_ROOT / "h-app")
    env["POD"] = pod
    env["TENANT"] = tenant
    env["REDIS_URL"] = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    env["AGENTS"] = "worker1"
    env["TMUX_SESSION"] = f"sess-{os.urandom(4).hex()}"
    env["TMUX_TMPDIR"] = tmpdir
    env["TMUX_SOCKET"] = os.path.join(tmpdir, "isolated.sock")

    try:
        res = subprocess.run(
            [str(SETUP_SH), "--venv", fake_venv, "--skip-install", "--skip-deps", "--non-interactive"],
            env=env, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
        )
        assert res.returncode == 0, f"setup.sh itself should still exit 0:\nstdout:{res.stdout}\nstderr:{res.stderr}"

        # The error line goes to stderr (setup.sh's own convention for
        # anything that isn't a plain confirmed hire) -- check both, same
        # as an operator watching a real terminal would see both streams.
        combined = res.stdout.splitlines() + res.stderr.splitlines()
        worker1_line = next((l for l in combined if "worker1" in l and "•" in l), None)
        assert worker1_line is not None, (
            f"no per-agent summary line for worker1 in stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
        )
        assert "requested" not in worker1_line, (
            f"an unexpected (simulated signal-death) hire exit was reported as a soft success: {worker1_line!r}"
        )
        assert "hired" not in worker1_line, (
            f"an unexpected (simulated signal-death) hire exit was reported as a confirmed hire: {worker1_line!r}"
        )
        assert "setup error" in worker1_line and "unexpected exit 137" in worker1_line, (
            f"an unexpected exit must say so explicitly and name the exit code: {worker1_line!r}"
        )
    finally:
        for name in ("switch", "tmux_reconciler", "watchdog"):
            pidfile = Path(home_dir) / ".h-mesh" / "run" / tenant / f"{name}.pid"
            if pidfile.exists():
                try:
                    os.kill(int(pidfile.read_text().strip()), 9)
                except (ValueError, OSError):
                    pass
        try:
            subprocess.run(["tmux", "-S", env["TMUX_SOCKET"], "kill-server"],
                            capture_output=True, timeout=5)
        except Exception:
            pass
        try:
            r = redis.Redis.from_url(env["REDIS_URL"])
            keys = r.keys(f"pod:{pod}:tenant:{tenant}:*") or []
            if keys:
                r.delete(*keys)
        except Exception:
            pass
        shutil.rmtree(tmpdir, ignore_errors=True)
