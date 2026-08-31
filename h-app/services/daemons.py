"""Start/stop lifecycle for h-mesh's own background daemons.

The switch and tmux-reconciler are the daemons setup.sh starts and pidfiles
today (see setup.sh's "5. Start required daemons" step). This module is the
one place that knows which daemons exist, where their pidfiles live, how to
resolve the config (pod/tenant/venv/tmux dir/env) they run with, and how to
bring them up or down cleanly -- shared by `h-mesh upgrade` (services.upgrade),
`h-mesh start` (this module's own main()), and `h-mesh stop`, so there is one
daemon-lifecycle implementation and one config-resolution path, not one per
caller.
"""

import argparse
import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[2]

# Add here if setup.sh's own daemon-start step ever grows another one.
DAEMON_MODULES = {
    "switch": "core.service",
    "tmux_reconciler": "services.tmux_reconciler",
}

STOP_TIMEOUT_SECONDS = 10
HEALTH_CHECK_SECONDS = 1


class DaemonError(RuntimeError):
    """A daemon failed to start cleanly."""


def pid_alive(pid: int) -> bool:
    """True if pid refers to a live process.

    ⚠ A zombie still answers `kill(pid, 0)` until its parent reaps it -- a
    daemon this process itself spawned and just sent SIGTERM/SIGKILL to
    would read as "alive" for however long nothing calls wait() on it,
    which is what stop_daemons()'s retry loop was doing: waiting the full
    STOP_TIMEOUT_SECONDS for a process that had already exited. Reap it
    first (non-blocking) if it's ours; a pid we didn't spawn (e.g. a
    previous run's daemon, already reparented to init) raises ECHILD here,
    and falls through to the plain existence check, which is correct for
    that case since init reaps its own children.
    """
    try:
        reaped_pid, _status = os.waitpid(pid, os.WNOHANG)
        if reaped_pid == pid:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_pid(pidfile: Path) -> int | None:
    if not pidfile.exists():
        return None
    try:
        return int(pidfile.read_text().strip())
    except ValueError:
        return None


def _stop_one(name: str, pidfile: Path, *, log: Callable[[str], None]) -> None:
    pid = _read_pid(pidfile)
    if pid is None:
        if pidfile.exists():
            log(f"  • {name}: pidfile {pidfile} did not contain a pid, removing")
            pidfile.unlink(missing_ok=True)
        else:
            log(f"  • {name}: not running (no pidfile)")
        return
    if not pid_alive(pid):
        log(f"  • {name}: pidfile stale (pid {pid} not running), removing")
        pidfile.unlink(missing_ok=True)
        return
    log(f"  • {name}: stopping pid {pid}...")
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            break
        time.sleep(0.2)
    else:
        log(f"  • {name}: pid {pid} did not exit after {STOP_TIMEOUT_SECONDS}s, sending SIGKILL")
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    pidfile.unlink(missing_ok=True)


def stop_daemons(run_dir: Path, *, log: Callable[[str], None] = print) -> None:
    """Stop every known daemon found via a live pidfile under run_dir.

    Idempotent: a daemon that isn't running (no pidfile, or a stale one left
    behind by a crash) is left alone, not an error -- safe to call whether or
    not anything is actually up.
    """
    for name in DAEMON_MODULES:
        _stop_one(name, run_dir / f"{name}.pid", log=log)


def _start_one(name: str, module: str, python: Path, run_dir: Path, env: dict) -> int:
    log_path = run_dir / f"{name}.log"
    pid_path = run_dir / f"{name}.pid"
    with open(log_path, "ab") as log_file:
        proc = subprocess.Popen(
            [str(python), "-u", "-m", module],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(REPO_ROOT),
            start_new_session=True,
        )
    pid_path.write_text(f"{proc.pid}\n")
    return proc.pid


def start_daemons(
    *, python: Path, run_dir: Path, env: dict, log: Callable[[str], None] = print
) -> dict[str, int]:
    """Start every known daemon, write pidfiles, and verify they're alive.

    Duplicate-safe: a daemon already alive (a live pidfile under run_dir) is
    left running as-is rather than started a second time -- callers that want
    a guaranteed-fresh process should stop_daemons() first, as `h-mesh
    upgrade` does; a bare `h-mesh start` against an already-running install
    should not spawn a second switch/reconciler pair.

    ⚠ `env` MUST set TMUX_TMPDIR/TMUX_SESSION/TMUX_SOCKET to values scoped to
    this install -- tmux_reconciler acts on whatever tmux server/session
    those name. An env built from a bare `dict(os.environ)` without
    overriding them inherits the caller's own ambient tmux server; if that's
    a real, populated session (e.g. this office's own), reconcile_once() will
    see this daemon's roster (likely empty, for an unseeded test pod/tenant)
    and kill every window it doesn't recognize as real -- including every
    other agent's live pane. Measured, not hypothetical: see test_daemons.py.

    Returns {name: pid}. Raises DaemonError, naming the daemon and its log
    path, if anything died within the health-check window.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    pids: dict[str, int] = {}
    for name, module in DAEMON_MODULES.items():
        pidfile = run_dir / f"{name}.pid"
        existing_pid = _read_pid(pidfile)
        if existing_pid is not None and pid_alive(existing_pid):
            log(f"  • {name}: already running (pid: {existing_pid}), skipping")
            pids[name] = existing_pid
            continue
        pid = _start_one(name, module, python, run_dir, env)
        pids[name] = pid
        log(f"  • {name} started (pid: {pid})")

    time.sleep(HEALTH_CHECK_SECONDS)
    for name, pid in pids.items():
        if not pid_alive(pid):
            raise DaemonError(f"{name} failed to start. Check {run_dir / f'{name}.log'}")
    return pids


# ---------------------------------------------------------------------------
# Shared config resolution -- one place for the pod/tenant/venv/tmux flags and
# their env fallbacks, so `h-mesh start` and `h-mesh upgrade` resolve the same
# way instead of each parsing its own copy.
# ---------------------------------------------------------------------------


@dataclass
class DaemonConfig:
    pod: str
    tenant: str
    redis_url: str
    tmux_session: str
    tmux_tmpdir: Path
    tmux_socket: str | None
    python: Path
    run_dir: Path
    env: dict[str, str]


def _resolve_venv(venv_arg: str | None) -> Path:
    if venv_arg:
        return Path(venv_arg)
    if os.environ.get("VIRTUAL_ENV"):
        return Path(os.environ["VIRTUAL_ENV"])
    return REPO_ROOT / ".venv"


def add_common_args(parser: argparse.ArgumentParser) -> None:
    """Add the pod/tenant/venv/tmux flags `resolve_config` expects, same defaults as setup.sh."""
    parser.add_argument("--pod", default=os.environ.get("POD", "default"),
                        help="Pod name (default: $POD or \"default\")")
    parser.add_argument("--tenant", default=os.environ.get("TENANT", "default"),
                        help="Tenant name (default: $TENANT or \"default\")")
    parser.add_argument("--redis-url", default=os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
                        help="Redis connection URL")
    parser.add_argument("--session", dest="tmux_session", default=os.environ.get("TMUX_SESSION"),
                        help="tmux session name (default: $TMUX_SESSION or tenant name)")
    parser.add_argument("--tmux-tmpdir", default=os.environ.get("TMUX_TMPDIR", str(Path.home() / ".h-mesh" / "tmux")),
                        help="tmux temporary/socket directory")
    parser.add_argument("--tmux-socket", default=os.environ.get("TMUX_SOCKET"),
                        help="Explicit tmux socket path")
    parser.add_argument("--venv", default=None,
                        help="Virtual environment directory the daemons run under "
                             "(default: $VIRTUAL_ENV or ./.venv)")


def resolve_config(args: argparse.Namespace) -> DaemonConfig:
    tmux_session = args.tmux_session or args.tenant
    run_dir = Path(os.environ.get("H_MESH_RUN_DIR", str(Path.home() / ".h-mesh" / "run" / args.tenant)))
    venv_dir = _resolve_venv(args.venv)
    python = venv_dir / "bin" / "python"
    tmux_tmpdir = Path(args.tmux_tmpdir)

    existing_pythonpath = os.environ.get("PYTHONPATH")
    h_app_path = str(REPO_ROOT / "h-app")
    pythonpath = f"{existing_pythonpath}{os.pathsep}{h_app_path}" if existing_pythonpath else h_app_path

    env = dict(os.environ)
    env.update({
        "POD": args.pod,
        "TENANT": args.tenant,
        "REDIS_URL": args.redis_url,
        "TMUX_SESSION": tmux_session,
        "TMUX_TMPDIR": str(tmux_tmpdir),
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": pythonpath,
    })
    if args.tmux_socket:
        env["TMUX_SOCKET"] = args.tmux_socket

    return DaemonConfig(
        pod=args.pod,
        tenant=args.tenant,
        redis_url=args.redis_url,
        tmux_session=tmux_session,
        tmux_tmpdir=tmux_tmpdir,
        tmux_socket=args.tmux_socket,
        python=python,
        run_dir=run_dir,
        env=env,
    )


def main(argv: Sequence[str] | None = None) -> None:
    """`h-mesh start` -- start h-mesh's own daemons if they aren't already running."""
    parser = argparse.ArgumentParser(
        prog="h-mesh start",
        description="Start h-mesh's daemons (switch, tmux-reconciler) if not already running.",
    )
    add_common_args(parser)
    args = parser.parse_args(argv)
    config = resolve_config(args)

    if not config.python.exists():
        print(f"error: no venv python at {config.python} -- run setup.sh first", file=sys.stderr)
        raise SystemExit(1)

    config.tmux_tmpdir.mkdir(parents=True, exist_ok=True)
    config.tmux_tmpdir.chmod(0o700)

    print(f"Starting daemons (logs written to {config.run_dir})...")
    try:
        start_daemons(python=config.python, run_dir=config.run_dir, env=config.env)
    except DaemonError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    print()
    print("✓ Daemons are healthy.")


if __name__ == "__main__":
    main()
