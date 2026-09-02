import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import ANY, call, patch

import pytest
import redis

from core.keys import prefix
from core.registry import port_type
from services.daemons import (
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


def test_start_daemons_then_stop_daemons_cleanly(managed_tmpdir):
    _skip_unless_redis()
    tmpdir = managed_tmpdir("h_mesh_test_daemons_")
    run_dir = Path(tmpdir) / "run"
    pod = f"testpod-{os.urandom(4).hex()}"
    tenant = f"testtenant-{os.urandom(4).hex()}"
    python = Path(sys.executable)
    env = _env(pod, tenant, tmpdir)

    pids = start_daemons(python=python, run_dir=run_dir, env=env)
    assert set(pids) == set(DAEMON_MODULES)
    for name, pid in pids.items():
        assert pid_alive(pid), f"{name} (pid {pid}) not alive right after start"
        assert (run_dir / f"{name}.pid").read_text().strip() == str(pid)
        assert (run_dir / f"{name}.pid.identity").exists()
        assert (run_dir / f"{name}.log").exists()

    stop_daemons(run_dir)
    for name, pid in pids.items():
        assert not pid_alive(pid), f"{name} (pid {pid}) still alive after stop"
        assert not (run_dir / f"{name}.pid").exists()
        assert not (run_dir / f"{name}.pid.identity").exists()

    # Idempotent: stopping again (nothing running, no pidfiles) is a no-op.
    stop_daemons(run_dir)


def test_start_daemons_is_duplicate_safe_against_an_already_running_pair(managed_tmpdir):
    _skip_unless_redis()
    tmpdir = managed_tmpdir("h_mesh_test_daemons_")
    run_dir = Path(tmpdir) / "run"
    pod = f"testpod-{os.urandom(4).hex()}"
    tenant = f"testtenant-{os.urandom(4).hex()}"
    python = Path(sys.executable)
    env = _env(pod, tenant, tmpdir)

    first_pids = start_daemons(python=python, run_dir=run_dir, env=env)
    second_pids = start_daemons(python=python, run_dir=run_dir, env=env)
    assert second_pids == first_pids, "second start_daemons() call spawned new processes"
    stop_daemons(run_dir)


def test_stop_daemons_removes_stale_pidfile_without_error(managed_tmpdir):
    tmpdir = managed_tmpdir("h_mesh_test_daemons_")
    run_dir = Path(tmpdir) / "run"
    run_dir.mkdir(parents=True)
    # A pid that is certainly not running (max pid range, unlikely to collide).
    (run_dir / "switch.pid").write_text("999999999\n")
    stop_daemons(run_dir)
    assert not (run_dir / "switch.pid").exists()


def test_stop_daemons_survives_a_pidfile_vanishing_mid_read(tmp_path, monkeypatch):
    """Reproduces ticket a347bf8d's real TOCTOU, found by watchdog-agent under
    concurrent stress and hit live: a switch.pid deleted by one process's
    reap right as another process's stop_daemons() was reading it raised an
    uncaught FileNotFoundError out of _read_pid, aborting stop_daemons()
    part-way through a teardown (some daemons stopped, some not).

    Constructs the actual interleaving -- deletes the file out from under
    the read the first time anything tries to read *this* path -- rather
    than just calling _read_pid on an already-missing file, which would
    only prove the catch exists, not that the race that produced the
    traceback is actually closed. Patches read_text() rather than exists()
    so this stays a real regression test regardless of which internal
    check the fix ends up using to notice the file is gone.

    A second, corrupt pidfile (tmux_reconciler.pid, the next name
    stop_daemons() visits after switch) pins the secondary consequence
    reviewer asked to strengthen: not just "no crash", but that the
    teardown actually continued past the raced entry rather than
    silently stopping there.
    """
    pidfile = tmp_path / "switch.pid"
    pidfile.write_text("999999999\n")
    later_entry = tmp_path / "tmux_reconciler.pid"
    later_entry.write_text("not-a-pid\n")

    real_read_text = Path.read_text
    vanished = []

    def vanish_then_read(self, *args, **kwargs):
        if self == pidfile and not vanished:
            vanished.append(True)
            pidfile.unlink()
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", vanish_then_read)

    logs: list[str] = []
    stop_daemons(tmp_path, log=logs.append)  # must not raise FileNotFoundError

    assert vanished, "the race was never actually constructed -- test proves nothing"
    assert not pidfile.exists()
    assert not later_entry.exists(), (
        "teardown aborted at the raced entry instead of continuing to "
        "later roster entries"
    )
    assert any("tmux_reconciler" in line and "did not contain a pid" in line for line in logs), logs


def test_stop_daemons_distinguishes_a_corrupt_pidfile_from_an_absent_one(tmp_path):
    """A vanished pidfile and a corrupt one are different facts (ticket
    a347bf8d): one means the daemon is already gone, the other means the
    file is malformed and worth actively removing. Never directly tested
    before -- PidRead.corrupt existed only as an assumption, not a proven
    guard."""
    logs: list[str] = []
    corrupt = tmp_path / "switch.pid"
    corrupt.write_text("not-a-pid\n")
    stop_daemons(tmp_path, log=logs.append)
    assert not corrupt.exists()
    assert any("did not contain a pid, removing" in line for line in logs), logs

    logs.clear()
    absent = tmp_path / "tmux_reconciler.pid"
    assert not absent.exists()
    stop_daemons(tmp_path, log=logs.append)
    assert any("not running (no pidfile)" in line for line in logs), logs
    assert not any("did not contain a pid" in line for line in logs), logs


def test_stop_daemons_does_not_signal_an_unrelated_live_process(tmp_path):
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    pidfile = tmp_path / "switch.pid"
    pidfile.write_text(f"{unrelated.pid}\n")
    (tmp_path / "switch.pid.identity").write_text(json.dumps({
        "v": 1,
        "pid": unrelated.pid,
        "name": "switch",
        "module": "core.service",
        # Deliberately not this live process's start time: the daemon that
        # owned the numeric PID has exited and the kernel reused its number.
        "start_time": "0",
    }))
    try:
        stop_daemons(tmp_path)

        assert unrelated.poll() is None, (
            "an unrelated process named by a stale daemon pidfile was signalled"
        )
        assert not pidfile.exists()
    finally:
        if unrelated.poll() is None:
            unrelated.kill()
        unrelated.wait(timeout=5)


def test_legacy_daemon_is_authenticated_then_stopped_during_first_upgrade(managed_tmpdir):
    _skip_unless_redis()
    tmpdir = managed_tmpdir("h_mesh_test_daemons_legacy_")
    run_dir = Path(tmpdir) / "run"
    env = _env(f"testpod-{os.urandom(4).hex()}", f"testtenant-{os.urandom(4).hex()}", tmpdir)
    pids = start_daemons(
        python=Path(sys.executable), run_dir=run_dir, env=env,
    )
    for name in pids:
        (run_dir / f"{name}.pid.identity").unlink()

    stop_daemons(run_dir, env=env)

    assert all(not pid_alive(pid) for pid in pids.values())


def test_stop_daemons_fails_closed_when_process_identity_cannot_be_read(tmp_path):
    import services.daemons as daemons_mod

    pidfile = tmp_path / "switch.pid"
    pidfile.write_text("1234\n")
    (tmp_path / "switch.pid.identity").write_text(json.dumps({
        "v": 1,
        "pid": 1234,
        "name": "switch",
        "module": "core.service",
        "start_time": "999",
    }))
    logs = []
    read_fd, write_fd = os.pipe()
    with (
        patch.object(daemons_mod.os, "pidfd_open", return_value=read_fd),
        patch.object(daemons_mod, "_process_start_time", return_value=None),
        patch.object(daemons_mod.signal, "pidfd_send_signal") as send_signal,
    ):
        stop_daemons(tmp_path, log=logs.append)
    os.close(write_fd)

    send_signal.assert_not_called()
    assert pidfile.exists()
    assert any("cannot verify" in message and "refusing to signal" in message for message in logs)


def test_stop_daemons_fails_closed_when_pidfds_are_unavailable(monkeypatch, tmp_path):
    import services.daemons as daemons_mod

    pidfile = tmp_path / "switch.pid"
    pidfile.write_text(f"{os.getpid()}\n")
    monkeypatch.delattr(daemons_mod.os, "pidfd_open")
    with patch.object(daemons_mod.os, "kill") as numeric_kill:
        stop_daemons(tmp_path)

    numeric_kill.assert_not_called()
    assert pidfile.exists()


def test_authenticated_daemon_is_signalled_through_pidfd_not_numeric_pid(
    monkeypatch, tmp_path,
):
    """Prove signalling cannot resolve a possibly reused numeric PID."""
    import services.daemons as daemons_mod

    daemon = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    pidfile = tmp_path / "switch.pid"
    pidfile.write_text(f"{daemon.pid}\n")
    (tmp_path / "switch.pid.identity").write_text(json.dumps({
        "v": 1,
        "pid": daemon.pid,
        "name": "switch",
        "module": "core.service",
        "start_time": daemons_mod._process_start_time(daemon.pid),
    }))
    real_kill = os.kill

    def forbid_numeric_signal(pid, sig):
        if sig in (signal.SIGTERM, signal.SIGKILL):
            raise AssertionError(f"numeric signal {sig} attempted for pid {pid}")
        return real_kill(pid, sig)

    monkeypatch.setattr(daemons_mod.os, "kill", forbid_numeric_signal)
    try:
        stop_daemons(tmp_path)

        assert daemon.poll() is not None, "the authenticated daemon was not stopped"
    finally:
        if daemon.poll() is None:
            real_kill(daemon.pid, signal.SIGKILL)
        daemon.wait(timeout=5)


@pytest.mark.parametrize("signal_error", [AttributeError("unavailable"), OSError("denied")])
def test_pidfd_signal_failure_retains_evidence_and_never_falls_back(
    signal_error, tmp_path,
):
    import services.daemons as daemons_mod

    pidfile = tmp_path / "switch.pid"
    identity_file = tmp_path / "switch.pid.identity"
    pidfile.write_text("1234\n")
    identity_file.write_text("identity evidence\n")
    read_fd, write_fd = os.pipe()
    logs = []
    try:
        with (
            patch.object(
                daemons_mod, "_open_owned_pidfd", return_value=(read_fd, "owned")
            ),
            patch.object(
                daemons_mod.signal, "pidfd_send_signal", side_effect=signal_error
            ),
            patch.object(daemons_mod.os, "kill") as numeric_kill,
        ):
            stop_daemons(tmp_path, log=logs.append)
    finally:
        os.close(write_fd)

    numeric_kill.assert_not_called()
    assert pidfile.read_text() == "1234\n"
    assert identity_file.read_text() == "identity evidence\n"
    assert any("cannot safely signal" in message for message in logs)
    assert not any(": stopped" in message for message in logs)


def test_post_sigkill_timeout_retains_evidence_and_reports_unverified_exit(
    monkeypatch, tmp_path,
):
    import services.daemons as daemons_mod

    pidfile = tmp_path / "switch.pid"
    identity_file = tmp_path / "switch.pid.identity"
    pidfile.write_text("1234\n")
    identity_file.write_text("identity evidence\n")
    read_fd, write_fd = os.pipe()
    logs = []
    monkeypatch.setattr(daemons_mod, "STOP_TIMEOUT_SECONDS", 0)
    try:
        with (
            patch.object(
                daemons_mod, "_open_owned_pidfd", return_value=(read_fd, "owned")
            ),
            patch.object(daemons_mod.select, "select", return_value=([], [], [])),
            patch.object(daemons_mod.signal, "pidfd_send_signal") as pidfd_signal,
            patch.object(daemons_mod.os, "kill") as numeric_kill,
        ):
            stop_daemons(tmp_path, log=logs.append)
    finally:
        os.close(write_fd)

    assert [call.args[1] for call in pidfd_signal.call_args_list] == [
        signal.SIGTERM,
        signal.SIGKILL,
    ]
    numeric_kill.assert_not_called()
    assert pidfile.read_text() == "1234\n"
    assert identity_file.read_text() == "identity evidence\n"
    assert any("cannot verify pid 1234 exited after SIGKILL" in message for message in logs)
    assert not any(": stopped" in message for message in logs)


def test_pidfd_processlookup_cleans_up_stale_evidence_without_numeric_signal(tmp_path):
    import services.daemons as daemons_mod

    pidfile = tmp_path / "switch.pid"
    identity_file = tmp_path / "switch.pid.identity"
    pidfile.write_text("1234\n")
    identity_file.write_text("identity evidence\n")
    read_fd, write_fd = os.pipe()
    try:
        with (
            patch.object(
                daemons_mod, "_open_owned_pidfd", return_value=(read_fd, "owned")
            ),
            patch.object(
                daemons_mod.signal,
                "pidfd_send_signal",
                side_effect=ProcessLookupError,
            ),
            patch.object(daemons_mod.os, "kill") as numeric_kill,
        ):
            stop_daemons(tmp_path)
    finally:
        os.close(write_fd)

    numeric_kill.assert_not_called()
    assert not pidfile.exists()
    assert not identity_file.exists()


def test_start_daemons_raises_daemon_error_when_module_is_broken(monkeypatch, managed_tmpdir):
    tmpdir = managed_tmpdir("h_mesh_test_daemons_")
    run_dir = Path(tmpdir) / "run"
    python = Path(sys.executable)
    env = _env("testpod", "testtenant", tmpdir)

    import services.daemons as daemons_mod
    monkeypatch.setitem(daemons_mod.DAEMON_MODULES, "switch", "this.module.does.not.exist")
    with pytest.raises(DaemonError):
        start_daemons(python=python, run_dir=run_dir, env=env)


def test_failed_start_rolls_back_only_daemons_started_by_this_call(monkeypatch, tmp_path):
    import services.daemons as daemons_mod

    monkeypatch.setattr(daemons_mod, "HEALTH_CHECK_SECONDS", 0)
    monkeypatch.setattr(
        daemons_mod,
        "_start_one",
        lambda name, *_args: {"new-a": 101, "new-b": 102}[name],
    )
    (tmp_path / "existing.pid").write_text("99\n")
    alive = {99: True, 101: True, 102: False}
    monkeypatch.setattr(daemons_mod, "pid_alive", lambda pid: alive[pid])
    monkeypatch.setattr(
        daemons_mod,
        "_open_owned_pidfd",
        lambda pid, *_args: ((999, "owned") if pid == 99 else (None, "mismatch")),
    )
    monkeypatch.setattr(daemons_mod.os, "close", lambda _fd: None)

    logs = []
    with patch.object(daemons_mod, "_stop_one") as stop_one:
        with pytest.raises(DaemonError, match="new-b failed to start"):
            start_daemons(
                python=Path(sys.executable),
                run_dir=tmp_path,
                env=_env("testpod", "testtenant", str(tmp_path)),
                daemon_modules={
                    "existing": "example.existing",
                    "new-a": "example.a",
                    "new-b": "example.b",
                },
                log=logs.append,
            )

    assert stop_one.call_args_list == [
        call("new-a", tmp_path / "new-a.pid", log=ANY, env=ANY),
        call("new-b", tmp_path / "new-b.pid", log=ANY, env=ANY),
    ]
    assert not any(": started" in message for message in logs)


def test_delayed_process_failure_rolls_back_live_sibling(monkeypatch, tmp_path):
    import re
    import services.daemons as daemons_mod

    (tmp_path / "healthy_daemon.py").write_text("import time\ntime.sleep(60)\n")
    (tmp_path / "delayed_failure.py").write_text("import time\ntime.sleep(0.2)\n")
    run_dir = tmp_path / "run"
    env = _env("testpod", "testtenant", str(tmp_path))
    env["PYTHONPATH"] = os.pathsep.join((str(tmp_path), env["PYTHONPATH"]))
    monkeypatch.setattr(daemons_mod, "HEALTH_CHECK_SECONDS", 0.6)
    monkeypatch.setattr(daemons_mod, "HEALTH_POLL_SECONDS", 0.02)
    logs = []

    with pytest.raises(DaemonError, match="delayed failed to start"):
        start_daemons(
            python=Path(sys.executable),
            run_dir=run_dir,
            env=env,
            daemon_modules={
                "healthy": "healthy_daemon",
                "delayed": "delayed_failure",
            },
            log=logs.append,
        )

    launched_pids = [
        int(match.group(1))
        for message in logs
        if (match := re.search(r"starting \(pid: (\d+)\)", message))
    ]
    assert len(launched_pids) == 2
    assert all(not pid_alive(pid) for pid in launched_pids)
    assert not (run_dir / "healthy.pid").exists()
    assert not (run_dir / "delayed.pid").exists()
    assert not any(": started" in message for message in logs)


@pytest.mark.parametrize(
    ("entrypoint", "work_patch"),
    (
        ("services.tmux_reconciler.main", "services.tmux_reconciler.TmuxReconciler"),
        ("modules.watchdog.service.main", "modules.watchdog.service.Watchdog"),
        ("services.telegram_bot.main", "services.telegram_bot.MeshClient"),
        ("core.service.main", "core.service.Switch"),
    ),
)
def test_daemon_entrypoints_reject_empty_identity_before_work(
    entrypoint, work_patch, monkeypatch
):
    from importlib import import_module

    monkeypatch.setenv("POD", "")
    monkeypatch.setenv("TENANT", "")
    module_name, function_name = entrypoint.rsplit(".", 1)
    main = getattr(import_module(module_name), function_name)

    with patch(work_patch, side_effect=AssertionError("daemon work began")):
        with pytest.raises(SystemExit, match="POD and TENANT"):
            main()


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


def test_start_daemons_starts_an_optional_daemon_when_requested_in_daemon_modules(managed_tmpdir):
    # api is genuinely startable with no external network calls, given
    # POD/TENANT/API_TOKEN/REDIS_URL -- proves the daemon_modules override
    # actually starts a real, alive extra process end to end, and that the
    # now-broadened stop_daemons() (checking ALL_DAEMON_MODULES) stops it
    # too, not just the always-on pair.
    _skip_unless_redis()
    tmpdir = managed_tmpdir("h_mesh_test_daemons_")
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
    pids = start_daemons(python=python, run_dir=run_dir, env=env, daemon_modules=daemon_modules)
    assert set(pids) == set(DAEMON_MODULES) | {"api"}
    for pid in pids.values():
        assert pid_alive(pid)

    stop_daemons(run_dir)
    for pid in pids.values():
        assert not pid_alive(pid)
    assert not (run_dir / "api.pid").exists()


def test_watchdog_starts_by_default_and_presence_leaves_unknown_after_a_real_hire(managed_tmpdir):
    # The actual bug: DAEMON_MODULES didn't include watchdog, so setup.sh/
    # h-mesh start/h-mesh upgrade never started it, nothing ever sampled
    # presence, and every agent read "unknown" forever with no ticket to
    # explain why. Confirmed through the real path an operator would use to
    # notice -- modules.office.cli's own "status" command -- not just that
    # a watchdog pid happens to exist.
    _skip_unless_redis()
    tmpdir = managed_tmpdir("h_mesh_test_watchdog_")
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


def test_session_daemon_starts_when_telegram_is_configured_not_otherwise(managed_tmpdir):
    # session backs the Telegram bot's "watch" command and hard-requires
    # API_TOKEN (modules.session.app.SessionSettings.from_env) -- confirms
    # it actually starts, alive, once Telegram config makes it eligible via
    # enabled_daemon_modules, and is absent when it doesn't.
    _skip_unless_redis()
    tmpdir = managed_tmpdir("h_mesh_test_session_")
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

    # enabled_daemon_modules() is what setup.sh actually calls -- confirm its
    # shape here -- but only start a subset for real (skip telegram_bot): a
    # fake, non-functional TELEGRAM_BOT_TOKEN would make TelegramBot use the
    # real TelegramClient, not DryRunTelegramClient, risking a real network
    # call to Telegram's servers. This test is about session, not the bot.
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


def test_session_daemon_absent_without_telegram_config():
    assert "session" not in enabled_daemon_modules({})
