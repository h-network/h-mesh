import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
import redis

from core.keys import prefix
from core.registry import port_type
from services.daemons import (
    ALL_DAEMON_MODULES,
    DAEMON_MODULES,
    OPTIONAL_DAEMON_MODULES,
    DaemonError,
    add_common_args,
    enabled_daemon_modules,
    merged_daemon_env,
    pid_alive,
    resolve_config,
    start_daemons,
    stop_daemons,
)
from services.tenant_config import write_tenant_env

REPO_ROOT = Path(__file__).resolve().parents[2]


def _env(pod: str, tenant: str, tmpdir: str) -> dict:
    # ⚠ tmux_reconciler is one of the daemons this starts, and it acts on
    # whatever tmux server/session its env points at. Without an isolated
    # TMUX_TMPDIR/TMUX_SESSION/TMUX_SOCKET here, it inherits the ambient
    # ones -- which, run from inside this office, is the real live office
    # tmux server. reconcile_once() then sees this test's empty roster and
    # kills every window it doesn't recognize as "real" against *that*
    # server: every agent's actual pane. Measured, the hard way -- always
    # isolate all three before calling start_daemons() with a real
    # tmux_reconciler in the mix.
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "h-app")
    env["POD"] = pod
    env["TENANT"] = tenant
    env["REDIS_URL"] = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    env["PYTHONUNBUFFERED"] = "1"
    env["TMUX_SESSION"] = f"sess-{os.urandom(4).hex()}"
    env["TMUX_TMPDIR"] = tmpdir
    env["TMUX_SOCKET"] = os.path.join(tmpdir, "isolated.sock")
    return env


def _skip_unless_redis() -> None:
    redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    try:
        redis.Redis.from_url(redis_url).ping()
    except Exception:
        pytest.skip("Redis server not available at REDIS_URL")


def _kill_and_cleanup(run_dir: Path, tmpdir: str, env: dict) -> None:
    for name in ALL_DAEMON_MODULES:
        pidfile = run_dir / f"{name}.pid"
        if pidfile.exists():
            try:
                pid = int(pidfile.read_text().strip())
                os.kill(pid, 9)
            except (ValueError, OSError):
                pass
    socket_path = env.get("TMUX_SOCKET")
    if socket_path:
        try:
            import subprocess
            subprocess.run(["tmux", "-S", socket_path, "kill-server"],
                            capture_output=True, timeout=5)
        except Exception:
            pass
    shutil.rmtree(tmpdir, ignore_errors=True)


def test_start_daemons_then_stop_daemons_cleanly():
    _skip_unless_redis()
    tmpdir = tempfile.mkdtemp(prefix="h_mesh_test_daemons_")
    run_dir = Path(tmpdir) / "run"
    pod = f"testpod-{os.urandom(4).hex()}"
    tenant = f"testtenant-{os.urandom(4).hex()}"
    python = Path(sys.executable)
    env = _env(pod, tenant, tmpdir)

    try:
        pids = start_daemons(python=python, run_dir=run_dir, env=env)
        assert set(pids) == set(DAEMON_MODULES)
        for name, pid in pids.items():
            assert pid_alive(pid), f"{name} (pid {pid}) not alive right after start"
            assert (run_dir / f"{name}.pid").read_text().strip() == str(pid)
            assert (run_dir / f"{name}.log").exists()

        stop_daemons(run_dir)
        for name, pid in pids.items():
            assert not pid_alive(pid), f"{name} (pid {pid}) still alive after stop"
            assert not (run_dir / f"{name}.pid").exists()

        # Idempotent: stopping again (nothing running, no pidfiles) is a no-op.
        stop_daemons(run_dir)
    finally:
        _kill_and_cleanup(run_dir, tmpdir, env)


def test_start_daemons_is_duplicate_safe_against_an_already_running_pair():
    _skip_unless_redis()
    tmpdir = tempfile.mkdtemp(prefix="h_mesh_test_daemons_")
    run_dir = Path(tmpdir) / "run"
    pod = f"testpod-{os.urandom(4).hex()}"
    tenant = f"testtenant-{os.urandom(4).hex()}"
    python = Path(sys.executable)
    env = _env(pod, tenant, tmpdir)

    try:
        first_pids = start_daemons(python=python, run_dir=run_dir, env=env)
        second_pids = start_daemons(python=python, run_dir=run_dir, env=env)
        assert second_pids == first_pids, "second start_daemons() call spawned new processes"
        stop_daemons(run_dir)
    finally:
        _kill_and_cleanup(run_dir, tmpdir, env)


def test_stop_daemons_removes_stale_pidfile_without_error():
    tmpdir = tempfile.mkdtemp(prefix="h_mesh_test_daemons_")
    run_dir = Path(tmpdir) / "run"
    run_dir.mkdir(parents=True)
    # A pid that is certainly not running (max pid range, unlikely to collide).
    (run_dir / "switch.pid").write_text("999999999\n")
    try:
        stop_daemons(run_dir)
        assert not (run_dir / "switch.pid").exists()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_start_daemons_raises_daemon_error_when_module_is_broken(monkeypatch):
    tmpdir = tempfile.mkdtemp(prefix="h_mesh_test_daemons_")
    run_dir = Path(tmpdir) / "run"
    python = Path(sys.executable)
    env = _env("testpod", "testtenant", tmpdir)

    import services.daemons as daemons_mod
    monkeypatch.setitem(daemons_mod.DAEMON_MODULES, "switch", "this.module.does.not.exist")
    try:
        with pytest.raises(DaemonError):
            start_daemons(python=python, run_dir=run_dir, env=env)
    finally:
        _kill_and_cleanup(run_dir, tmpdir, env)


def test_resolve_config_merges_persisted_tenant_env_beneath_process_env(monkeypatch, tmp_path):
    monkeypatch.setenv("H_MESH_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("PROVIDER_LOCAL_URL", raising=False)
    write_tenant_env("mytenant", {
        "PROVIDER_LOCAL_URL": "http://10.0.0.5:8000",
        "PROVIDER_LOCAL_MODEL": "served-model",
    })

    import argparse
    parser = argparse.ArgumentParser()
    add_common_args(parser)
    args = parser.parse_args(["--tenant", "mytenant"])
    config = resolve_config(args)

    assert config.env["PROVIDER_LOCAL_URL"] == "http://10.0.0.5:8000"
    assert config.env["PROVIDER_LOCAL_MODEL"] == "served-model"


def test_resolve_config_live_env_overrides_persisted_tenant_env(monkeypatch, tmp_path):
    monkeypatch.setenv("H_MESH_STATE_DIR", str(tmp_path))
    write_tenant_env("mytenant", {"PROVIDER_LOCAL_URL": "http://persisted:8000"})
    monkeypatch.setenv("PROVIDER_LOCAL_URL", "http://live-override:8000")

    import argparse
    parser = argparse.ArgumentParser()
    add_common_args(parser)
    args = parser.parse_args(["--tenant", "mytenant"])
    config = resolve_config(args)

    assert config.env["PROVIDER_LOCAL_URL"] == "http://live-override:8000"


def test_merged_daemon_env_includes_persisted_token_not_in_base_env(monkeypatch, tmp_path):
    # The exact shape of the real bug: setup.sh's own shell never exports
    # what the wizard's ask_token() collected -- it only ever lands in the
    # persisted tenant config -- so a caller building env from a bare
    # dict(os.environ) (or any base_env that doesn't have it either) would
    # never see it. merged_daemon_env() must pull it in regardless.
    monkeypatch.setenv("H_MESH_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("CLAUDE_OAUTH_TOKEN_DEFAULT", raising=False)
    write_tenant_env("mytenant", {"CLAUDE_OAUTH_TOKEN_DEFAULT": "sekrit-token"})

    base_env = {"PATH": "/usr/bin"}  # deliberately does not have the token
    env = merged_daemon_env("mytenant", base_env=base_env)

    assert env["CLAUDE_OAUTH_TOKEN_DEFAULT"] == "sekrit-token"
    assert env["PATH"] == "/usr/bin"


def test_merged_daemon_env_base_env_overrides_persisted(monkeypatch, tmp_path):
    monkeypatch.setenv("H_MESH_STATE_DIR", str(tmp_path))
    write_tenant_env("mytenant", {"CLAUDE_OAUTH_TOKEN_DEFAULT": "old-token"})

    env = merged_daemon_env("mytenant", base_env={"CLAUDE_OAUTH_TOKEN_DEFAULT": "new-token"})

    assert env["CLAUDE_OAUTH_TOKEN_DEFAULT"] == "new-token"


def test_enabled_daemon_modules_is_just_the_base_set_without_telegram_config():
    assert set(enabled_daemon_modules({})) == set(DAEMON_MODULES)


def test_enabled_daemon_modules_adds_api_telegram_bot_and_session_when_both_present():
    # session backs the Telegram bot's "watch" command -- rides the same
    # TELEGRAM_BOT_TOKEN+TELEGRAM_CHAT_ID condition as api/telegram_bot,
    # not a separate one, since it needs API_TOKEN and today that's only
    # ever set once Telegram is configured.
    env = {"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_CHAT_ID": "y"}
    modules = enabled_daemon_modules(env)
    assert set(modules) == set(DAEMON_MODULES) | {"api", "telegram_bot", "session"}
    assert modules["api"] == OPTIONAL_DAEMON_MODULES["api"]
    assert modules["telegram_bot"] == OPTIONAL_DAEMON_MODULES["telegram_bot"]
    assert modules["session"] == OPTIONAL_DAEMON_MODULES["session"]


def test_enabled_daemon_modules_requires_both_token_and_chat_id():
    assert set(enabled_daemon_modules({"TELEGRAM_BOT_TOKEN": "x"})) == set(DAEMON_MODULES)
    assert set(enabled_daemon_modules({"TELEGRAM_CHAT_ID": "y"})) == set(DAEMON_MODULES)


def test_start_daemons_starts_an_optional_daemon_when_requested_in_daemon_modules():
    # api is genuinely startable with no external network calls, given
    # POD/TENANT/API_TOKEN/REDIS_URL -- proves the daemon_modules override
    # actually starts a real, alive extra process end to end, and that the
    # now-broadened stop_daemons() (checking ALL_DAEMON_MODULES) stops it
    # too, not just the always-on pair.
    _skip_unless_redis()
    tmpdir = tempfile.mkdtemp(prefix="h_mesh_test_daemons_")
    run_dir = Path(tmpdir) / "run"
    pod = f"testpod-{os.urandom(4).hex()}"
    tenant = f"testtenant-{os.urandom(4).hex()}"
    python = Path(sys.executable)
    env = _env(pod, tenant, tmpdir)
    env["API_TOKEN"] = "test-token-for-api-daemon"
    # ⚠ Explicit, not inherited -- this office's own ambient env sets
    # API_BIND=0.0.0.0 (real infra config), which would make the api
    # daemon demand TLS certs this test doesn't have and refuse to start.
    # Same lesson as TMUX_TMPDIR: never trust an ambient value here.
    env["API_BIND"] = "127.0.0.1"
    # ⚠ Also explicit: this office's own real API server is genuinely
    # listening on the default port 8080 right now (confirmed live) --
    # binding there would either collide or, worse, look like it started
    # against the wrong process. Pick an ephemeral port instead.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        env["API_PORT"] = str(probe.getsockname()[1])

    daemon_modules = {**DAEMON_MODULES, "api": OPTIONAL_DAEMON_MODULES["api"]}
    try:
        pids = start_daemons(python=python, run_dir=run_dir, env=env, daemon_modules=daemon_modules)
        assert set(pids) == set(DAEMON_MODULES) | {"api"}
        for pid in pids.values():
            assert pid_alive(pid)

        stop_daemons(run_dir)
        for pid in pids.values():
            assert not pid_alive(pid)
        assert not (run_dir / "api.pid").exists()
    finally:
        _kill_and_cleanup(run_dir, tmpdir, env)


def test_watchdog_starts_by_default_and_presence_leaves_unknown_after_a_real_hire():
    # The actual bug: DAEMON_MODULES didn't include watchdog, so setup.sh/
    # h-mesh start/h-mesh upgrade never started it, nothing ever sampled
    # presence, and every agent read "unknown" forever with no ticket to
    # explain why. Confirmed through the real path an operator would use to
    # notice -- modules.office.cli's own "status" command -- not just that
    # a watchdog pid happens to exist.
    _skip_unless_redis()
    tmpdir = tempfile.mkdtemp(prefix="h_mesh_test_watchdog_")
    run_dir = Path(tmpdir) / "run"
    pod = f"testpod-{os.urandom(4).hex()}"
    tenant = f"testtenant-{os.urandom(4).hex()}"
    python = Path(sys.executable)
    env = _env(pod, tenant, tmpdir)

    # h-agent is supplied by the runtime base image, not by this repo or CI
    # -- stand in a fake one so the hired window has a long-lived shell,
    # same pattern as test_setup_script.py/test_setup_wizard.py.
    fake_bin = os.path.join(tmpdir, "bin")
    os.makedirs(fake_bin, exist_ok=True)
    fake_h_agent = os.path.join(fake_bin, "h-agent")
    with open(fake_h_agent, "w") as f:
        f.write("#!/usr/bin/env bash\nexec bash -il\n")
    os.chmod(fake_h_agent, 0o755)
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"

    r = redis.Redis.from_url(env["REDIS_URL"])
    try:
        pids = start_daemons(python=python, run_dir=run_dir, env=env)
        assert "watchdog" in pids, "watchdog must be part of the default, always-on set"
        assert pid_alive(pids["watchdog"])

        registry_key = prefix(pod, tenant, resource="registry")
        r.hset(registry_key, mapping={"host": "office"})

        hire_env = dict(env)
        hire_env["AGENT_NAME"] = "host"
        hire_res = subprocess.run(
            [str(python), "-m", "modules.office.cli", "hire", "worker1", "--cli", "claude"],
            env=hire_env, capture_output=True, text=True, timeout=15,
        )
        assert hire_res.returncode == 0, f"hire failed: {hire_res.stderr}\n{hire_res.stdout}"

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if port_type(r, pod=pod, tenant=tenant, agent="worker1") == "tmux":
                break
            time.sleep(0.2)
        assert port_type(r, pod=pod, tenant=tenant, agent="worker1") == "tmux", "worker1 never registered"

        status_out = ""
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            status_res = subprocess.run(
                [str(python), "-m", "modules.office.cli", "status", "worker1"],
                env=hire_env, capture_output=True, text=True, timeout=10,
            )
            status_out = status_res.stdout
            if "unknown" not in status_out:
                break
            time.sleep(0.5)
        assert "unknown" not in status_out, (
            f"presence stayed unknown after a real hire, status output: {status_out!r}\n"
            f"watchdog.log:\n{(run_dir / 'watchdog.log').read_text()}"
        )
    finally:
        _kill_and_cleanup(run_dir, tmpdir, env)


def test_session_daemon_starts_when_telegram_is_configured_not_otherwise():
    # session backs the Telegram bot's "watch" command and hard-requires
    # API_TOKEN (modules.session.app.SessionSettings.from_env) -- confirms
    # it actually starts, alive, once Telegram config makes it eligible via
    # enabled_daemon_modules, and is absent when it doesn't.
    _skip_unless_redis()
    tmpdir = tempfile.mkdtemp(prefix="h_mesh_test_session_")
    run_dir = Path(tmpdir) / "run"
    pod = f"testpod-{os.urandom(4).hex()}"
    tenant = f"testtenant-{os.urandom(4).hex()}"
    python = Path(sys.executable)
    env = _env(pod, tenant, tmpdir)
    env["API_TOKEN"] = "test-token-for-session-daemon"
    env["API_BIND"] = "127.0.0.1"
    env["SESSION_BIND"] = "127.0.0.1"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        env["API_PORT"] = str(probe.getsockname()[1])
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        env["SESSION_PORT"] = str(probe.getsockname()[1])
    env["TELEGRAM_BOT_TOKEN"] = "fake-bot-token"
    env["TELEGRAM_CHAT_ID"] = "12345"

    try:
        # enabled_daemon_modules() is what setup.sh actually calls -- confirm
        # its shape here -- but only start a subset for real (skip
        # telegram_bot): a fake, non-functional TELEGRAM_BOT_TOKEN would make
        # TelegramBot use the real TelegramClient, not DryRunTelegramClient,
        # risking a real network call to Telegram's servers. This test is
        # about session, not the bot.
        daemon_modules = enabled_daemon_modules(env)
        assert "session" in daemon_modules
        start_modules = {**DAEMON_MODULES, "api": daemon_modules["api"], "session": daemon_modules["session"]}
        pids = start_daemons(python=python, run_dir=run_dir, env=env, daemon_modules=start_modules)
        assert "session" in pids
        assert pid_alive(pids["session"]), (
            f"session.log:\n{(run_dir / 'session.log').read_text()}"
        )

        stop_daemons(run_dir)
        assert not pid_alive(pids["session"])
    finally:
        _kill_and_cleanup(run_dir, tmpdir, env)


def test_session_daemon_absent_without_telegram_config():
    assert "session" not in enabled_daemon_modules({})
