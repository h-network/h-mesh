import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
import redis

from services.daemons import (
    DaemonError,
    add_common_args,
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
    for name in ("switch", "tmux_reconciler"):
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
        assert set(pids) == {"switch", "tmux_reconciler"}
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
