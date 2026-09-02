"""h-mesh upgrade: pull latest, reinstall, and cleanly restart setup.sh's daemons.

Companion to setup.sh, not a replacement for it -- setup.sh bootstraps a
fresh install (creates the venv, seeds the registry, starts daemons for the
first time); this upgrades an existing one in place. It stops whatever
daemons setup.sh already has running (services.daemons.stop_daemons, via the
pidfiles setup.sh writes under $H_MESH_RUN_DIR), pulls and reinstalls, then
starts fresh daemons with the current environment (services.daemons.start_daemons)
-- so it does not double-start daemons against an already-running install,
and a changed env var takes effect on the daemons it restarts. It also
re-persists the venv bin dir on PATH (services.venv_path), re-installs the
default tmux.conf (services.tmux_conf), and re-installs the claude
statusline for the default account (services.claude_statusline), to repair
an install that predates any of those fixes.

⚠ Known, deliberate limit: an agent's tmux pane inherits its environment at
creation time only. This restarts h-mesh's own daemons and reinstalls the
package they run, but it cannot reach into an already-hired agent's live
pane and refresh its env -- that agent keeps whatever it started with until
it's re-hired or its window is otherwise recreated. Solving that would mean
killing live agent sessions on every upgrade, a worse trade than a stale env
var. Not attempted here.
"""

import argparse
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path


from services.claude_statusline import install_statusline
from services.daemons import (
    REPO_ROOT,
    DaemonError,
    add_common_args,
    enabled_daemon_modules,
    resolve_config,
    start_daemons,
    stop_daemons,
)
from services.tmux_conf import install_tmux_conf
from services.venv_path import persist_venv_on_path


def _run(cmd: list[str]) -> None:
    print(f"$ {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="h-mesh upgrade",
        description="Pull latest, reinstall, and cleanly restart h-mesh's own daemons.",
    )
    add_common_args(parser)
    parser.add_argument("--skip-install", action="store_true",
                        help="Skip the pip reinstall step")
    parser.add_argument("--skip-pull", action="store_true",
                        help="Skip `git pull`; only reinstall and restart daemons")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    # ⚠ A self-upgrade runs the code imported before ``main`` started for this
    # entire process. ``git pull`` and an editable reinstall only affect future
    # Python processes. A migration, cleanup, or one-time fix added here cannot
    # run on the first upgrade that installs it; without an explicit re-exec it
    # begins running on the second upgrade. Do not use this function as a
    # first-transition migration hook.
    args = _build_parser().parse_args(argv)
    config = resolve_config(args)

    if not config.python.exists():
        print(f"error: no venv python at {config.python} -- run setup.sh first", file=sys.stderr)
        raise SystemExit(1)

    print("=== h-mesh :: upgrade ===")
    print(f"Repo:         {REPO_ROOT}")
    print(f"Venv:         {config.python.parent.parent}")
    print(f"Pod:          {config.pod}")
    print(f"Tenant:       {config.tenant}")
    print(f"Run dir:      {config.run_dir}")
    print()

    if not args.skip_pull:
        print("Pulling latest...")
        try:
            _run(["git", "-C", str(REPO_ROOT), "pull", "--ff-only"])
        except subprocess.CalledProcessError as exc:
            print(f"error: git pull failed ({exc.returncode}) -- resolve manually, "
                  "then retry with --skip-pull", file=sys.stderr)
            raise SystemExit(1) from exc
        print()

    if not args.skip_install:
        print("Reinstalling h-mesh...")
        try:
            _run([str(config.python), "-m", "pip", "install", "-e", str(REPO_ROOT)])
        except subprocess.CalledProcessError as exc:
            print(f"error: pip install failed ({exc.returncode})", file=sys.stderr)
            raise SystemExit(1) from exc
        print()

    print("Persisting venv bin on PATH (~/.bashrc, ~/.profile)...")
    persist_venv_on_path(config.python.parent.parent, log=print)
    print()

    print("Installing default tmux.conf (unless one already exists)...")
    install_tmux_conf(log=print)
    print()

    print("Installing claude statusline (context-usage progress bar)...")
    install_statusline(Path(os.environ.get("HOME", str(Path.home()))) / ".claude", log=print)
    print()

    print("Stopping existing daemons (if any)...")
    stop_daemons(config.run_dir, env=config.env)
    print()

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
    print("✓ Upgrade complete, daemons are healthy.")
    print()
    print("Note: already-hired agent panes keep the environment they started")
    print("with -- only panes hired after this upgrade pick up the change.")


if __name__ == "__main__":
    main()
