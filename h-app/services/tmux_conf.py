"""Install h-mesh's default tmux.conf, without clobbering a user's own.

The container base image bakes a nice-UX tmux.conf in (mouse mode, status
bar, pane borders) -- but that's the image's doing, not any app code's, so a
bare host with no base image gets tmux's plain defaults instead. This
installs h-mesh's own copy (repo root's tmux.conf) as ~/.tmux.conf, the same
idempotent-safe pattern as services.venv_path: setup.sh calls it after
install, h-mesh upgrade calls it too so a pre-existing install picks it up.

Symlinked, not copied, so a later edit to the shipped tmux.conf takes effect
on the next tmux start without needing to reinstall -- and so "is this ours"
is a simple, unambiguous check (does ~/.tmux.conf resolve to this file?)
rather than a content comparison. A real file or a symlink pointing anywhere
else is left alone; this never overwrites a user's own customized config.
"""

import argparse
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED_TMUX_CONF = REPO_ROOT / "tmux.conf"


def install_tmux_conf(
    *,
    source: Path = SHIPPED_TMUX_CONF,
    target: Path | None = None,
    log: Callable[[str], None] = print,
) -> None:
    if target is None:
        target = Path(os.environ.get("HOME", str(Path.home()))) / ".tmux.conf"

    if target.is_symlink():
        if target.resolve() == source.resolve():
            log(f"  • {target}: already installed (symlinked to {source})")
            return
        log(f"  • {target}: left alone (a symlink pointing elsewhere)")
        return
    if target.exists():
        log(f"  • {target}: left alone (already exists, not managed by h-mesh)")
        return

    target.symlink_to(source)
    log(f"  • {target}: installed (symlinked to {source})")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="h-mesh install-tmux-conf",
        description="Install h-mesh's default tmux.conf as ~/.tmux.conf, unless one already exists.",
    )
    parser.parse_args(argv)
    install_tmux_conf()


if __name__ == "__main__":
    main()
