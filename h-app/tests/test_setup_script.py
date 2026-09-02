import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import pytest
import redis

from core.keys import prefix
from core.registry import port_type
from modules.tmux.ops import list_windows, run_tmux

REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP_SH = REPO_ROOT / "setup.sh"


def test_setup_help():
    res = subprocess.run(
        [str(SETUP_SH), "--help"], capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    assert res.returncode == 0
    assert "Usage: ./setup.sh" in res.stdout
    assert "--pod" in res.stdout
    assert "--tenant" in res.stdout
    assert "--redis-url" in res.stdout


def _fake_h_agent_installer(tmpdir: str, *, behavior: str) -> str:
    """Write a fake h-agent installer and return a file:// URL to it (curl
    reads file:// directly, no network needed -- setup.sh always prefers
    curl when both are present, so this never exercises the wget branch).

    behavior:
      "succeed"           -- places a working h-agent and exits 0, always.
      "fail_always"        -- exits 1 without placing h-agent, always.
      "fail_then_succeed"  -- exits 1 with no binary on the first
                              invocation, then succeeds on the second --
                              the exact shape confirmed live on the VM,
                              where re-running the same installer by hand
                              right after a failure succeeded with no
                              other change.
      "fail_differently"   -- exits 1 on the first invocation, 42 on the
                              second, neither placing h-agent -- a
                              deliberately NON-identical pair of failures,
                              so the equal-exit-status reporting only fires
                              when the codes actually match.
      "fail_same_code_different_cause" -- exits 1 BOTH times, neither
                              placing h-agent, but prints different stderr
                              text each time (h-agent's real installer
                              exits 1 for multiple distinct causes --
                              claude/codex/agy each have their own
                              verification-failed path). The counterexample
                              that breaks a message claiming equal exit
                              codes mean an identical, deterministic
                              failure: two different real failures can
                              produce this exact pair of codes.
    """
    script_path = os.path.join(tmpdir, "fake_h_agent_install.sh")
    counter_path = os.path.join(tmpdir, "h_agent_install_attempts")
    place_binary = """
mkdir -p "$bindir"
printf '#!/usr/bin/env bash\\nexec bash -il\\n' > "$bindir/h-agent"
chmod +x "$bindir/h-agent"
exit 0
"""
    if behavior == "succeed":
        body = place_binary
    elif behavior == "fail_always":
        body = "exit 1\n"
    elif behavior == "fail_then_succeed":
        body = f'if [ "$count" -eq 1 ]; then exit 1; fi\n{place_binary}'
    elif behavior == "fail_differently":
        body = 'if [ "$count" -eq 1 ]; then exit 1; fi\nexit 42\n'
    elif behavior == "fail_same_code_different_cause":
        body = (
            'if [ "$count" -eq 1 ]; then '
            'echo "simulated: claude 2.1.251 verification could not be confirmed" >&2; exit 1; '
            'fi\n'
            'echo "simulated: agy 1.1.24 does not match the pinned 1.1.23" >&2\n'
            'exit 1\n'
        )
    else:
        raise ValueError(behavior)

    script = f"""#!/usr/bin/env bash
set -euo pipefail
count_file="{counter_path}"
count=0
[ -f "$count_file" ] && count=$(cat "$count_file")
count=$((count + 1))
echo "$count" > "$count_file"
bindir="${{PREFIX:-$HOME/.local}}/bin"
{body}
"""
    with open(script_path, "w") as f:
        f.write(script)
    os.chmod(script_path, 0o755)
    return f"file://{script_path}", counter_path


def _dep_check_env(tmpdir: str, *, h_agent_url: str) -> dict:
    # No fake h-agent pre-placed on PATH -- deliberately runs the real
    # detect-missing/install/verify path in setup.sh's section 0, not
    # --skip-deps like the other tests here. redis-server, curl, and a
    # usable python3-venv are all real and already present on the test
    # host, so section 0 never touches apt-get; only H_AGENT_INSTALL_URL
    # is faked.
    home_dir = os.path.join(tmpdir, "home")
    os.makedirs(home_dir, exist_ok=True)
    env = dict(os.environ)
    env["HOME"] = home_dir
    env["PYTHONPATH"] = str(REPO_ROOT / "h-app")
    env["H_AGENT_INSTALL_URL"] = h_agent_url
    for k in list(env.keys()):
        if k.startswith("CLAUDE_OAUTH_TOKEN_") or k == "CLAUDE_CODE_OAUTH_TOKEN":
            del env[k]
    return env


def test_setup_fails_loudly_when_h_agent_installer_fails_twice():
    # The bug this replaced: setup.sh never checked curl|bash's exit status
    # at all, so a failed h-agent install was silently swallowed and the
    # script printed success anyway. Confirm the opposite now: a clear,
    # non-zero failure, before anything downstream (venv/daemons) even
    # starts.
    tmpdir = tempfile.mkdtemp(prefix="h_mesh_test_setup_h_agent_")
    try:
        h_agent_url, counter_path = _fake_h_agent_installer(tmpdir, behavior="fail_always")
        run_dir = os.path.join(tmpdir, "run")
        env = _dep_check_env(tmpdir, h_agent_url=h_agent_url)
        env["H_MESH_RUN_DIR"] = run_dir
        res = subprocess.run(
            [str(SETUP_SH), "--pod", "p", "--tenant", "t", "--non-interactive"],
            env=env, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
        )
        assert res.returncode != 0, f"stdout: {res.stdout}\nstderr: {res.stderr}"
        assert "h-agent installer failed twice" in res.stderr, res.stderr
        # Both attempts here exit 1 -- reviewer FAILED an earlier version of
        # this report for calling that "identical" and "non-transient",
        # which the exit status alone can't prove (h-agent's installer
        # exits 1 for multiple distinct causes -- see the same-code/
        # different-cause test below). Report the OBSERVATION and the
        # limit, not a conclusion.
        assert "Both attempts exited with status 1" in res.stderr, res.stderr
        assert "cannot tell from the exit status alone whether both attempts failed for the same reason" in res.stderr, res.stderr
        assert "✓ h-agent installed" not in res.stdout
        assert "✓ Daemons are healthy" not in res.stdout, (
            "setup.sh must not proceed past a failed h-agent install:\n" + res.stdout
        )
        assert os.path.exists(counter_path)
        assert open(counter_path).read().strip() == "2", "expected exactly 2 attempts"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_setup_does_not_report_equal_exit_status_when_attempts_actually_differ():
    # The equal-exit-status line is only accurate when both attempts really
    # did exit the same way -- assert it's absent when the codes differ, so
    # the reporting can't be implemented as "always say this on the second
    # failure" and still pass the matching-codes tests.
    tmpdir = tempfile.mkdtemp(prefix="h_mesh_test_setup_h_agent_")
    try:
        h_agent_url, counter_path = _fake_h_agent_installer(tmpdir, behavior="fail_differently")
        run_dir = os.path.join(tmpdir, "run")
        env = _dep_check_env(tmpdir, h_agent_url=h_agent_url)
        env["H_MESH_RUN_DIR"] = run_dir
        res = subprocess.run(
            [str(SETUP_SH), "--pod", "p", "--tenant", "t", "--non-interactive"],
            env=env, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
        )
        assert res.returncode != 0, f"stdout: {res.stdout}\nstderr: {res.stderr}"
        assert "h-agent installer failed twice (last exit 42)" in res.stderr, res.stderr
        assert "Both attempts exited with status" not in res.stderr, res.stderr
        assert open(counter_path).read().strip() == "2", "expected exactly 2 attempts"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_setup_reports_only_the_observed_status_and_uncertainty_when_exit_codes_match():
    # Reviewer's exact counterexample: h-agent's real installer exits 1 for
    # MULTIPLE distinct causes (claude/codex/agy each have their own
    # verification-failed path), so two attempts returning the same code
    # does not mean they failed for the same reason, that either was
    # deterministic, or that a third attempt or an upstream/host change is
    # what's needed. Two DIFFERENT simulated failures here both exit 1.
    #
    # ⚠ A denylist of forbidden words ("identical", "deterministic", ...)
    # is not proof of anything -- reviewer FAILED an earlier version of this
    # test for exactly that: a future message could add "there is no point
    # retrying" or "another attempt is futile" and every denylisted spelling
    # check would still pass while the same overclaim returned, with the
    # test's own name falsely certifying it can't. Assert the EXACT text of
    # the one line setup.sh itself emits for this case instead of trying to
    # enumerate every dishonest paraphrase -- any future wording change that
    # adds a conclusion this line doesn't already make will change the
    # string and fail here, whatever words it uses. The simulated
    # installer's OWN stderr (a fake dependency's output, not setup.sh's
    # own words) is deliberately left unconstrained -- that content belongs
    # to the dependency, not to what's under test.
    tmpdir = tempfile.mkdtemp(prefix="h_mesh_test_setup_h_agent_")
    try:
        h_agent_url, counter_path = _fake_h_agent_installer(
            tmpdir, behavior="fail_same_code_different_cause"
        )
        run_dir = os.path.join(tmpdir, "run")
        env = _dep_check_env(tmpdir, h_agent_url=h_agent_url)
        env["H_MESH_RUN_DIR"] = run_dir
        res = subprocess.run(
            [str(SETUP_SH), "--pod", "p", "--tenant", "t", "--non-interactive"],
            env=env, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
        )
        assert res.returncode != 0, f"stdout: {res.stdout}\nstderr: {res.stderr}"
        assert "h-agent installer failed twice (last exit 1)" in res.stderr, res.stderr

        observation_lines = [
            line for line in res.stderr.splitlines()
            if line.strip().startswith("Both attempts exited with status")
        ]
        assert len(observation_lines) == 1, (
            f"expected exactly one setup-owned observation line, got "
            f"{observation_lines!r}\nfull stderr:\n{res.stderr}"
        )
        expected = (
            "  Both attempts exited with status 1. setup.sh cannot tell "
            "from the exit status alone whether both attempts failed for "
            "the same reason -- check the installer output above before "
            "deciding whether to retry or to change something on this "
            "host or upstream."
        )
        assert observation_lines[0] == expected, (
            f"setup.sh's own diagnostic line drifted from the approved "
            f"observation-and-uncertainty wording:\n"
            f"  actual:   {observation_lines[0]!r}\n"
            f"  expected: {expected!r}"
        )
        assert open(counter_path).read().strip() == "2", "expected exactly 2 attempts"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_setup_retries_once_after_a_transient_h_agent_install_failure():
    redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    try:
        redis.Redis.from_url(redis_url).ping()
    except Exception:
        pytest.skip("Redis server not available at REDIS_URL")
    tmpdir = tempfile.mkdtemp(prefix="h_mesh_test_setup_h_agent_")
    pod, tenant = f"p-{os.urandom(4).hex()}", f"t-{os.urandom(4).hex()}"
    try:
        h_agent_url, counter_path = _fake_h_agent_installer(tmpdir, behavior="fail_then_succeed")
        run_dir = os.path.join(tmpdir, "run")
        env = _dep_check_env(tmpdir, h_agent_url=h_agent_url)
        env["H_MESH_RUN_DIR"] = run_dir
        env["REDIS_URL"] = redis_url
        res = subprocess.run(
            [str(SETUP_SH), "--pod", pod, "--tenant", tenant,
             "--non-interactive", "--skip-install", "--no-daemons", "--venv", sys.prefix],
            env=env, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
        )
        assert res.returncode == 0, f"stdout: {res.stdout}\nstderr: {res.stderr}"
        assert "attempt 1/2" in res.stdout
        assert "attempt 2/2" in res.stdout
        assert "✓ h-agent installed" in res.stdout
        assert open(counter_path).read().strip() == "2"
        h_agent_bin = os.path.join(env["HOME"], ".local", "bin", "h-agent")
        assert os.path.isfile(h_agent_bin) and os.access(h_agent_bin, os.X_OK)
    finally:
        try:
            keys = redis.Redis.from_url(redis_url).keys(f"pod:{pod}:tenant:{tenant}:*") or []
            if keys:
                redis.Redis.from_url(redis_url).delete(*keys)
        except Exception:
            pass
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_setup_installs_h_agent_on_first_try_when_missing():
    redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    try:
        redis.Redis.from_url(redis_url).ping()
    except Exception:
        pytest.skip("Redis server not available at REDIS_URL")
    tmpdir = tempfile.mkdtemp(prefix="h_mesh_test_setup_h_agent_")
    pod, tenant = f"p-{os.urandom(4).hex()}", f"t-{os.urandom(4).hex()}"
    try:
        h_agent_url, counter_path = _fake_h_agent_installer(tmpdir, behavior="succeed")
        run_dir = os.path.join(tmpdir, "run")
        env = _dep_check_env(tmpdir, h_agent_url=h_agent_url)
        env["H_MESH_RUN_DIR"] = run_dir
        env["REDIS_URL"] = redis_url
        res = subprocess.run(
            [str(SETUP_SH), "--pod", pod, "--tenant", tenant,
             "--non-interactive", "--skip-install", "--no-daemons", "--venv", sys.prefix],
            env=env, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
        )
        assert res.returncode == 0, f"stdout: {res.stdout}\nstderr: {res.stderr}"
        assert "attempt 2/2" not in res.stdout
        assert "✓ h-agent installed" in res.stdout
        assert open(counter_path).read().strip() == "1"
    finally:
        try:
            keys = redis.Redis.from_url(redis_url).keys(f"pod:{pod}:tenant:{tenant}:*") or []
            if keys:
                redis.Redis.from_url(redis_url).delete(*keys)
        except Exception:
            pass
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_setup_seeds_registry_and_runs_e2e_hire_and_message():
    redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    r = redis.Redis.from_url(redis_url)
    try:
        r.ping()
    except Exception:
        pytest.skip("Redis server not available at REDIS_URL")

    tmpdir = tempfile.mkdtemp(prefix="h_mesh_test_setup_")
    socket_path = os.path.join(tmpdir, "isolated.sock")
    run_dir = os.path.join(tmpdir, "run")
    pod = f"testpod-{os.urandom(4).hex()}"
    tenant = f"testtenant-{os.urandom(4).hex()}"
    session_name = f"sess-{os.urandom(4).hex()}"

    # h-agent is supplied by the runtime base image, not by this repo or CI --
    # stand in a fake one so the hired window has a long-lived shell to
    # deliver a message into, same as the base image would give it.
    fake_bin = os.path.join(tmpdir, "bin")
    os.makedirs(fake_bin, exist_ok=True)
    fake_h_agent = os.path.join(fake_bin, "h-agent")
    with open(fake_h_agent, "w") as f:
        f.write("#!/usr/bin/env bash\nexec bash -il\n")
    os.chmod(fake_h_agent, 0o755)

    state_dir = os.path.join(tmpdir, "state")
    os.makedirs(state_dir, exist_ok=True)

    # ⚠ setup.sh now writes to ~/.bashrc and ~/.profile (persisting the venv
    # bin dir on PATH) -- HOME must be isolated here or this test edits the
    # real ones.
    home_dir = os.path.join(tmpdir, "home")
    os.makedirs(home_dir, exist_ok=True)

    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["HOME"] = home_dir
    env["PYTHONPATH"] = str(REPO_ROOT / "h-app")
    env["H_MESH_RUN_DIR"] = run_dir
    env["H_MESH_STATE_DIR"] = state_dir
    env["H_MESH_LOG_FILE"] = os.path.join(state_dir, "window.log.jsonl")
    env["AGENT_NAME"] = "architect"
    env["POD"] = pod
    env["TENANT"] = tenant
    env["REDIS_URL"] = redis_url
    env["TMUX_SESSION"] = session_name
    env["TMUX_SOCKET"] = socket_path

    # Scrub ambient tokens to prevent real credential leakage into test processes
    for k in list(env.keys()):
        if k.startswith("CLAUDE_OAUTH_TOKEN_") or k == "CLAUDE_CODE_OAUTH_TOKEN":
            del env[k]

    switch_pid = None
    reconciler_pid = None

    try:
        # Reuse the venv already running this test process -- it necessarily
        # has h-mesh's dependencies installed, since this file imports them.
        # A path like REPO_ROOT/.venv is not guaranteed to exist or be
        # populated; --skip-install would leave a freshly created one empty.
        venv_dir = sys.prefix
        res = subprocess.run(
            [
                str(SETUP_SH),
                "--pod", pod,
                "--tenant", tenant,
                "--session", session_name,
                "--tmux-socket", socket_path,
                "--redis-url", redis_url,
                "--venv", venv_dir,
                "--skip-install",
                "--skip-deps",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
        if res.returncode != 0:
            sw_log = ""
            tr_log = ""
            try:
                with open(os.path.join(run_dir, "switch.log")) as f:
                    sw_log = f.read()
            except Exception:
                pass
            try:
                with open(os.path.join(run_dir, "tmux_reconciler.log")) as f:
                    tr_log = f.read()
            except Exception:
                pass
            assert res.returncode == 0, f"setup.sh failed: {res.stderr}\nstdout: {res.stdout}\nswitch.log: {sw_log}\ntmux_reconciler.log: {tr_log}"

        # The H-MESH banner is wizard decoration for a human at a terminal --
        # a non-interactive/scripted run like this one must not print it.
        assert "H-MESH" not in res.stdout

        # 1. Verify fixed participants are seeded in Redis
        reg_key = prefix(pod, tenant, resource="registry")
        assert port_type(r, pod=pod, tenant=tenant, agent="host") == "office"
        assert port_type(r, pod=pod, tenant=tenant, agent="api") == "api"

        # setup.sh persists the venv bin dir on PATH so a hired agent's pane
        # or an attaching human can actually run h-mesh-* commands.
        venv_bin = os.path.join(venv_dir, "bin")
        for rc_filename in (".bashrc", ".profile"):
            rc_content = open(os.path.join(home_dir, rc_filename)).read()
            assert venv_bin in rc_content, f"{rc_filename} missing venv bin on PATH"

        # setup.sh also installs the default tmux.conf.
        tmux_conf_path = os.path.join(home_dir, ".tmux.conf")
        assert os.path.islink(tmux_conf_path)
        assert os.path.realpath(tmux_conf_path) == os.path.realpath(str(REPO_ROOT / "tmux.conf"))

        # Read daemon PIDs from run_dir
        with open(os.path.join(run_dir, "switch.pid")) as f:
            switch_pid = int(f.read().strip())
        with open(os.path.join(run_dir, "tmux_reconciler.pid")) as f:
            reconciler_pid = int(f.read().strip())

        assert switch_pid > 0
        assert reconciler_pid > 0

        # 2. Hire an agent: host hires 'worker1'
        venv_python = sys.executable
        env_hire = dict(env)
        env_hire["AGENT_NAME"] = "host"
        hire_res = subprocess.run(
            [venv_python, "-m", "modules.office.cli", "hire", "worker1"],
            env=env_hire,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert hire_res.returncode == 0, f"hire failed: {hire_res.stderr}\nstdout: {hire_res.stdout}"

        # Wait for switch and office port to process StartAgent and update registry
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            pt = port_type(r, pod=pod, tenant=tenant, agent="worker1")
            if pt == "tmux":
                break
            time.sleep(0.2)
        if port_type(r, pod=pod, tenant=tenant, agent="worker1") != "tmux":
            sw_log = open(os.path.join(run_dir, "switch.log")).read()
            tr_log = open(os.path.join(run_dir, "tmux_reconciler.log")).read()
            assert False, f"worker1 not registered. switch.log:\n{sw_log}\ntmux_reconciler.log:\n{tr_log}"

        # Wait for reconciler to create the tmux window for worker1
        deadline = time.monotonic() + 10.0
        windows = set()
        while time.monotonic() < deadline:
            try:
                windows = list_windows(session_name, socket=socket_path)
                if "worker1" in windows:
                    break
            except Exception:
                pass
            time.sleep(0.2)
        assert "worker1" in windows

        # 3. Send a message to worker1
        env_send = dict(env)
        env_send["AGENT_NAME"] = "host"
        send_res = subprocess.run(
            [venv_python, "-m", "modules.office.cli", "send", "-a", "worker1", "welcome to h-mesh worker1"],
            env=env_send,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert send_res.returncode == 0, f"send failed: {send_res.stderr}\nstdout: {send_res.stdout}"

        # Wait for switch and tmux port to deliver the message to worker1 window
        deadline = time.monotonic() + 10.0
        delivered = False
        pane_output = ""
        while time.monotonic() < deadline:
            code, stdout, stderr = run_tmux(
                "capture-pane", "-J", "-p", "-t", f"{session_name}:worker1", socket=socket_path
            )
            if code == 0 and "welcome to h-mesh worker1" in stdout:
                delivered = True
                pane_output = stdout
                break
            time.sleep(0.2)

        assert delivered, f"Message was not delivered to worker1 pane. Last pane text:\n{pane_output}"
        assert "[message from host] welcome to h-mesh worker1" in pane_output

    finally:
        # Stop background daemons
        for pid in (switch_pid, reconciler_pid):
            if pid:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
        # ⚠ Also sweep every pidfile still under run_dir, not just the
        # explicitly-tracked switch/reconciler pids above -- setup.sh
        # starts every daemon in DAEMON_MODULES (now including watchdog),
        # and a fixed pid list here silently orphaned it on every run once
        # that set grew. Measured: it did.
        for pidfile in Path(run_dir).glob("*.pid"):
            try:
                os.kill(int(pidfile.read_text().strip()), signal.SIGTERM)
            except (ValueError, OSError):
                pass

        # Kill test tmux server
        try:
            run_tmux("kill-server", socket=socket_path)
        except Exception:
            pass

        # Clean test Redis keys
        try:
            keys = r.keys(f"{pod}.{tenant}.*") or []
            colon_keys = r.keys(f"pod:{pod}:tenant:{tenant}:*") or []
            all_keys = list(set(keys + colon_keys))
            if all_keys:
                r.delete(*all_keys)
        except Exception:
            pass

        shutil.rmtree(tmpdir, ignore_errors=True)
