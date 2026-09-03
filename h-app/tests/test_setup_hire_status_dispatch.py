"""setup.sh's roster-hire summary must never say "requested" (or "hired")
for a status it doesn't recognize, AND the script's own exit code must not
say success when the roster it was asked to produce is incomplete. This
branch has now produced the same falsehood three times, each time one
layer further out:

  round 1  the CLI reported confirmed for a hire it could not attribute
  round 2  the CLI was honest, and setup.sh's dispatch collapsed every
           unexpected status into "requested"
  round 3  the dispatch was honest, and the PROCESS EXIT CODE collapsed it
           into success -- reviewer and architect both FAILED this: an
           unattended installer or CI checking only the process boundary
           saw a clean setup while a requested roster hire was never
           admitted, and the regression at the time actively asserted
           `res.returncode == 0` for that case -- a test that pins the
           wrong behaviour is worse than no test, since it makes the real
           fix look like a regression.

setup.sh now accumulates a HIRE_HAD_ERROR flag across the whole roster loop
(never aborting early -- a later agent's hire is still attempted after an
earlier one fails) and exits nonzero, after every summary/instruction line
has printed, if any agent's hire was a proven rejection (exit 1) OR an
unenumerated/unexpected exit. Architect's explicit call: a proven rejection
counts too, not just an unrecognized exit -- the operator asked for a
roster and did not get it, and that must be detectable without parsing
stderr.

These tests inject real, deterministic exit codes via a python wrapper
standing in for the venv's real interpreter -- not a signal race against a
real subprocess, which proved unreliable to time correctly on a busy shared
sandbox (killing the child of a bash command substitution does not reliably
propagate its exit status to $?, and can even leave the substitution's own
subshell hung -- measured directly, not assumed). The wrapper delegates
every OTHER invocation to the real interpreter unchanged, so the rest of
setup.sh's own run (dependency checks already skipped, but tenant_config/
tmux_conf/statusline/registry-seeding/daemon-start calls) still executes
for real.
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


def _make_fake_venv_with_forced_hire_exits(tmpdir: str, exits: dict) -> str:
    """A --venv directory whose bin/python is a wrapper: for each
    `agent: fake_exit` pair, the specific `-m modules.office.cli hire
    <agent>` invocation gets `fake_exit` immediately, with no real hire
    logic ever running; every other invocation (there are many others in a
    real setup.sh run) delegates to the REAL interpreter unchanged."""
    fake_venv = os.path.join(tmpdir, "fake_venv")
    fake_bin = os.path.join(fake_venv, "bin")
    os.makedirs(fake_bin, exist_ok=True)
    wrapper_path = os.path.join(fake_bin, "python")
    cases = "\n".join(
        f"""    *"modules.office.cli hire {agent} "*)
        echo "SIMULATED: forced hire exit {fake_exit} for {agent}" >&2
        exit {fake_exit}
        ;;"""
        for agent, fake_exit in exits.items()
    )
    script = f"""#!/usr/bin/env bash
case " $* " in
{cases}
esac
exec {sys.executable} "$@"
"""
    with open(wrapper_path, "w") as f:
        f.write(script)
    st = os.stat(wrapper_path)
    os.chmod(wrapper_path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return fake_venv


def _run_setup_with_forced_hire_exits(
    agents: list, exits: dict, *, extra_env: dict | None = None,
) -> subprocess.CompletedProcess:
    """Run setup.sh --non-interactive with roster `agents`, where `exits`
    forces a specific fake hire exit code for named agents (deterministic,
    no real hire logic for those -- see the wrapper docstring above). Any
    agent not in `exits` would go through the real pipeline; every test
    here forces all roster agents so runs stay fast. Cleans up all
    daemons/tmux/redis state before returning, so callers only assert on
    the returned CompletedProcess. `extra_env` layers on top of the
    scrubbed/isolated base env (e.g. a deliberately invalid DEFAULT_CLI, to
    exercise the REAL office hire CLI's own usage-error exit rather than a
    forced one)."""
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

    fake_venv = _make_fake_venv_with_forced_hire_exits(tmpdir, exits)

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
    env["AGENTS"] = ",".join(agents)
    env["TMUX_SESSION"] = f"sess-{os.urandom(4).hex()}"
    env["TMUX_TMPDIR"] = tmpdir
    env["TMUX_SOCKET"] = os.path.join(tmpdir, "isolated.sock")
    if extra_env:
        env.update(extra_env)

    try:
        return subprocess.run(
            [str(SETUP_SH), "--venv", fake_venv, "--skip-install", "--skip-deps", "--non-interactive"],
            env=env, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
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


def test_setup_never_says_requested_for_an_unexpected_hire_exit():
    _skip_unless_redis()
    res = _run_setup_with_forced_hire_exits(["worker1"], {"worker1": 137})

    assert res.returncode != 0, (
        "an unexpected hire exit must make setup.sh's OWN exit code say "
        f"failure, not just the per-agent line:\nstdout:{res.stdout}\nstderr:{res.stderr}"
    )

    # The per-agent error line goes to stderr (setup.sh's own convention
    # for anything that isn't a plain confirmed hire) -- check both, same
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
    assert "One or more roster hires did not succeed" in res.stderr and "worker1" in res.stderr, (
        f"the final failure summary must name the agent that failed:\n{res.stderr}"
    )


def test_setup_continues_roster_and_exits_nonzero_after_an_unexpected_hire_exit():
    """Reviewer/architect: both halves, or fixing one breaks the other --
    an earlier agent's hire failing must not stop the loop (a later roster
    entry is still attempted), but the script's own exit code must still
    say failure overall."""
    _skip_unless_redis()
    res = _run_setup_with_forced_hire_exits(
        ["worker1", "worker2"], {"worker1": 137, "worker2": 2},
    )

    assert res.returncode != 0, (
        "an unexpected hire exit anywhere in the roster must make setup.sh "
        f"exit nonzero overall:\nstdout:{res.stdout}\nstderr:{res.stderr}"
    )

    combined = res.stdout.splitlines() + res.stderr.splitlines()
    worker1_line = next((l for l in combined if "worker1" in l and "•" in l), None)
    worker2_line = next((l for l in combined if "worker2" in l and "•" in l), None)
    assert worker1_line is not None and worker2_line is not None, (
        "both roster entries must produce a per-agent summary line -- a "
        "later entry missing would mean the loop stopped after the "
        f"earlier failure:\nstdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )
    assert "setup error" in worker1_line and "unexpected exit 137" in worker1_line, (
        f"worker1: {worker1_line!r}"
    )
    assert "requested" in worker2_line, (
        "worker2 was never attempted after worker1's failure -- the "
        f"roster loop must not abort early: {worker2_line!r}"
    )

    # worker2's own outcome (exit 2, "requested") is not itself an error --
    # it must not appear anywhere in stderr, including the failure summary.
    assert "worker1" in res.stderr and "worker2" not in res.stderr, (
        f"the failure summary must name only the agent that actually "
        f"failed:\n{res.stderr}"
    )


def test_setup_exits_nonzero_after_a_proven_hire_rejection():
    """Architect's explicit call: a proven rejection (--wait's real
    exit 1, not just an unenumerated exit) must also make setup.sh's own
    exit code say failure -- the operator asked for a roster and did not
    get it, and that must be detectable without parsing stderr. A later
    agent is still attempted (worker2 forced to a plain "requested")."""
    _skip_unless_redis()
    res = _run_setup_with_forced_hire_exits(
        ["worker1", "worker2"], {"worker1": 1, "worker2": 2},
    )

    assert res.returncode != 0, (
        "a proven hire rejection anywhere in the roster must make setup.sh "
        f"exit nonzero overall:\nstdout:{res.stdout}\nstderr:{res.stderr}"
    )

    combined = res.stdout.splitlines() + res.stderr.splitlines()
    worker1_line = next((l for l in combined if "worker1" in l and "•" in l), None)
    worker2_line = next((l for l in combined if "worker2" in l and "•" in l), None)
    assert worker1_line is not None and worker2_line is not None, (
        "both roster entries must produce a per-agent summary line -- a "
        "later entry missing would mean the loop stopped after the "
        f"earlier rejection:\nstdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )
    assert "hire failed" in worker1_line, f"worker1: {worker1_line!r}"
    assert "requested" in worker2_line, (
        "worker2 was never attempted after worker1's rejection -- the "
        f"roster loop must not abort early: {worker2_line!r}"
    )
    assert "worker1" in res.stderr and "worker2" not in res.stderr, (
        f"the failure summary must name only the agent that was actually "
        f"rejected:\n{res.stderr}"
    )


def test_setup_surfaces_a_corrupted_default_cli_instead_of_reporting_requested():
    """Live incident, not a hypothetical: h-mesh-halil's persisted
    DEFAULT_CLI held a Telegram bot token instead of claude/codex/agy (a
    provisioning mistake, not a code bug), so office hire's own argparse
    rejected --cli before ever calling send() -- and that rejection used to
    exit 2, identical to hire's own legitimate "sent, unconfirmed" outcome.
    setup.sh's dispatch trusted that 2 and printed "requested -- no
    rejection seen" for a hire that had never been sent: no entry in
    switch.log, no registry row, no visible error anywhere.

    No forced-exit wrapper here -- DEFAULT_CLI is genuinely invalid and the
    REAL office hire subprocess runs end to end, to prove the fix (hire's
    argparse usage errors now exit 3, distinct from 1/2) actually closes
    the silent-failure path setup.sh's existing "any unenumerated exit is a
    hard, surfaced error" branch was already designed to catch.
    """
    _skip_unless_redis()
    res = _run_setup_with_forced_hire_exits(
        ["worker1"], {}, extra_env={"DEFAULT_CLI": "not-a-real-cli"},
    )

    assert res.returncode != 0, (
        "a hire that was never actually sent (bad --cli) must make "
        f"setup.sh's own exit code say failure:\nstdout:{res.stdout}\nstderr:{res.stderr}"
    )
    combined = res.stdout.splitlines() + res.stderr.splitlines()
    worker1_line = next((l for l in combined if "worker1" in l and "•" in l), None)
    assert worker1_line is not None, (
        f"no per-agent summary line for worker1 in stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )
    assert "requested" not in worker1_line, (
        f"a hire that never called send() was reported as a soft success: {worker1_line!r}"
    )
    assert "setup error" in worker1_line and "unexpected exit 3" in worker1_line, (
        f"a bad --cli must surface as an explicit, named exit code: {worker1_line!r}"
    )
    assert "invalid choice" in res.stderr, (
        f"the real argparse rejection reason must reach the operator:\n{res.stderr}"
    )
