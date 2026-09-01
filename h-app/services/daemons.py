"""Start/stop lifecycle for h-mesh's own background daemons.

The switch, tmux-reconciler and watchdog are always-on -- setup.sh starts
and pidfiles them unconditionally (see its "5. Start required daemons"
step). The REST API, Telegram bot, and session door are optional: they only
start when the wizard's persisted config actually asks for them (see
enabled_daemon_modules). This module is the one place that knows which
daemons exist, where their pidfiles live, how to resolve the config
(pod/tenant/venv/tmux dir/env) they run with, and how to bring them up or
down cleanly -- shared by `h-mesh upgrade` (services.upgrade), `h-mesh
start` (this module's own main()), and `h-mesh stop`, so there is one
daemon-lifecycle implementation and one config-resolution path, not one per
caller.

⚠ Every console script in pyproject.toml's [project.scripts] belongs in
exactly one of three buckets, and it's worth checking a new one against
these before assuming it's covered:
  - a background daemon that belongs here (DAEMON_MODULES or
    OPTIONAL_DAEMON_MODULES) -- switch, tmux_reconciler, watchdog, api,
    telegram_bot, session.
  - a short-lived, per-message process the reconciler spawns on demand, not
    a persistent daemon at all -- modules.tmux.port and
    modules.openshell.port (see modules/office/port.py's own
    subprocess.Popen call). Never belongs here; it isn't missing, it's
    never meant to run unprompted.
  - a CLI a human or another script invokes directly, not something
    setup.sh starts in the background -- h-mesh (the dispatcher),
    h-mesh-office, h-mesh-clone-to-all, h-mesh-upgrade, h-mesh-start.
  - services.web_console (the browser console / Mini App gateway) is a
    real, persistent daemon-shaped process, but it isn't in
    pyproject.toml's scripts at all and has its own separate, documented
    deployment path (a Compose service, see clients/web/README.md) rather
    than setup.sh's bare-host bootstrap -- deliberately not added here; a
    future ticket that actually wires that deployment path would decide
    this, not a silent inclusion alongside the tmux-based daemons above.

This inventory is exactly how the watchdog and session gaps were found
(2026-09 -- neither was in DAEMON_MODULES/OPTIONAL_DAEMON_MODULES, so
setup.sh/h-mesh start/h-mesh upgrade never started them, and
h-mesh-watchdog/h-mesh-session existed as console scripts nothing in the
documented install path ever invoked) -- the same audit is the fastest way
to catch a third instance before a user does.
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

from services.tenant_config import read_tenant_env

REPO_ROOT = Path(__file__).resolve().parents[2]

# Add here if setup.sh's own daemon-start step ever grows another one.
# ⚠ watchdog needs no credentials -- just POD/TENANT/REDIS_URL, same as
# switch/tmux_reconciler -- and every multi-agent office wants presence
# sampling and stall/silence alerting from the first hire on, not only once
# someone happens to configure Telegram. That's what makes it always-on
# rather than folded into OPTIONAL_DAEMON_MODULES below.
DAEMON_MODULES = {
    "switch": "core.service",
    "tmux_reconciler": "services.tmux_reconciler",
    "watchdog": "services.watchdog",
}

# Started only when the wizard's collected config asks for them (see
# enabled_daemon_modules below) -- unlike DAEMON_MODULES, not every install
# wants these running.
# ⚠ session (the WebSocket terminal-streaming door behind the Telegram
# bot's "watch" command, and the web console's terminal view) is NOT
# always-on like watchdog: modules.session.app.SessionSettings.from_env()
# hard-raises RuntimeError without API_TOKEN, and today API_TOKEN is only
# ever set when Telegram is configured (see the API_TOKEN auto-generation
# in setup.sh) -- an always-on session would just crash-loop on every
# install that never configured Telegram. Gated on the same condition as
# api/telegram_bot for that reason, not folded into DAEMON_MODULES.
OPTIONAL_DAEMON_MODULES = {
    "api": "services.api",
    "telegram_bot": "services.telegram_bot",
    "session": "services.session",
}

# The full universe of daemons this install could ever have running, past or
# present. stop_daemons() checks against this (not just DAEMON_MODULES) so a
# daemon that was enabled in a previous run and isn't wanted anymore still
# gets stopped, not orphaned.
ALL_DAEMON_MODULES = {**DAEMON_MODULES, **OPTIONAL_DAEMON_MODULES}


def enabled_daemon_modules(env: dict) -> dict[str, str]:
    """The daemons this run should have running: the always-on set, plus the
    optional ones whose required config is actually present in env.

    The Telegram bot needs h-mesh's own REST API to talk to (see
    clients.telegram.bot's README) -- there's no separate "enable the API"
    question in the wizard, so a configured Telegram bot enables both.
    """
    modules = dict(DAEMON_MODULES)
    if env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID"):
        modules["api"] = OPTIONAL_DAEMON_MODULES["api"]
        modules["telegram_bot"] = OPTIONAL_DAEMON_MODULES["telegram_bot"]
        # session backs the bot's "watch" command -- needs API_TOKEN, which
        # only exists once Telegram is configured (see the module docstring
        # above), so it rides the same condition rather than getting one of
        # its own.
        modules["session"] = OPTIONAL_DAEMON_MODULES["session"]
    return modules

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

    Checks ALL_DAEMON_MODULES, not just the always-on set -- an optional
    daemon (api, telegram_bot) enabled in a previous run but not this one
    still gets stopped here, rather than left running unmanaged.

    Idempotent: a daemon that isn't running (no pidfile, or a stale one left
    behind by a crash) is left alone, not an error -- safe to call whether or
    not anything is actually up.
    """
    for name in ALL_DAEMON_MODULES:
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
    *,
    python: Path,
    run_dir: Path,
    env: dict,
    daemon_modules: dict[str, str] | None = None,
    log: Callable[[str], None] = print,
) -> dict[str, int]:
    """Start every daemon in daemon_modules (default: DAEMON_MODULES, the
    always-on set), write pidfiles, and verify they're alive. Pass
    `enabled_daemon_modules(env)` to also start whichever optional daemons
    that env's config asks for.

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
    if daemon_modules is None:
        daemon_modules = DAEMON_MODULES
    run_dir.mkdir(parents=True, exist_ok=True)
    pids: dict[str, int] = {}
    for name, module in daemon_modules.items():
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


def merged_daemon_env(tenant: str, base_env: dict | None = None) -> dict:
    """Persisted tenant config layered beneath a live environment.

    Persisted tenant config (PROVIDER_*/CLAUDE_OAUTH_TOKEN_*, from the
    setup.sh wizard) first, so daemons started fresh -- in a shell that
    never ran the wizard itself, or never exported what it just collected --
    still see them; base_env (the live process env, by default) on top, so
    an explicit export still wins over what's on disk.

    The one place this merge happens, so every daemon-starting caller
    (resolve_config() here, and setup.sh's own daemon-start step, which
    can't go through resolve_config() end-to-end because it must keep
    --no-venv's ambient-python mode working, which resolve_config()'s own
    venv resolution doesn't support) gets it consistently. Skipping this
    merge is exactly what left a wizard-collected OAuth token invisible to
    the reconciler hiring the first agent in the same run -- measured live.
    """
    env = dict(read_tenant_env(tenant))
    env.update(base_env if base_env is not None else os.environ)
    return env


def resolve_config(args: argparse.Namespace) -> DaemonConfig:
    tmux_session = args.tmux_session or args.tenant
    run_dir = Path(os.environ.get("H_MESH_RUN_DIR", str(Path.home() / ".h-mesh" / "run" / args.tenant)))
    venv_dir = _resolve_venv(args.venv)
    python = venv_dir / "bin" / "python"
    tmux_tmpdir = Path(args.tmux_tmpdir)

    existing_pythonpath = os.environ.get("PYTHONPATH")
    h_app_path = str(REPO_ROOT / "h-app")
    pythonpath = f"{existing_pythonpath}{os.pathsep}{h_app_path}" if existing_pythonpath else h_app_path

    env = merged_daemon_env(args.tenant)
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
        description="Start h-mesh's daemons (switch, tmux-reconciler, watchdog) if not already running.",
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
        start_daemons(
            python=config.python, run_dir=config.run_dir, env=config.env,
            daemon_modules=enabled_daemon_modules(config.env),
        )
    except DaemonError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    print()
    print("✓ Daemons are healthy.")


if __name__ == "__main__":
    main()
