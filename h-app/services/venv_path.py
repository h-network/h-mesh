"""Persist a venv's bin dir on PATH durably, for interactive shells.

setup.sh only affects the process that runs it -- exporting PATH there does
nothing for anyone else. While hired agent panes get PATH injected directly into
their window environment via modules.tmux.ops.window_env, a human attaching
to the session gets a fresh login shell; they do not inherit PATH from
whatever shell happened to run setup.sh. The way h-mesh-* commands
(including the unified `h-mesh` dispatcher) are on PATH for attaching users is
a PATH export written into shell startup files (~/.bashrc and ~/.profile).

`h-mesh upgrade` calls this too: to repair an install that predates this
fix, and in case the venv path itself ever changes.
"""

import argparse
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Callable

_MARKER_BEGIN = "# >>> h-mesh venv PATH >>>"
_MARKER_END = "# <<< h-mesh venv PATH <<<"

# bash reads ~/.bash_profile / ~/.bash_login / ~/.profile (first found) as a
# login shell, and ~/.bashrc as an interactive non-login shell. `bash -il`
# (both login and interactive) is what agent panes and attaching humans
# actually get -- writing to both covers whichever startup-file chain the
# base image's shell follows, without assuming one sources the other.
_TARGET_FILES = (".bashrc", ".profile")


def _block(venv_bin: Path) -> str:
    return f'{_MARKER_BEGIN}\nexport PATH="{venv_bin}:$PATH"\n{_MARKER_END}\n'


def _upsert(path: Path, block: str) -> bool:
    """Insert or replace the marked PATH block in path. Returns True if changed."""
    existing = path.read_text() if path.exists() else ""
    if _MARKER_BEGIN in existing and _MARKER_END in existing:
        start = existing.index(_MARKER_BEGIN)
        end = existing.index(_MARKER_END) + len(_MARKER_END)
        # Consume one trailing newline right after the end marker, if
        # present, so repeated upserts don't accumulate blank lines.
        tail_start = end + 1 if existing[end:end + 1] == "\n" else end
        new_content = existing[:start] + block + existing[tail_start:]
        if new_content == existing:
            return False
        path.write_text(new_content)
        return True
    separator = "" if not existing or existing.endswith("\n") else "\n"
    path.write_text(existing + separator + block)
    return True


def persist_venv_on_path(venv_dir: Path, *, log: Callable[[str], None] = print) -> None:
    """Idempotently add venv_dir/bin to PATH via ~/.bashrc and ~/.profile.

    Safe to call on every setup.sh/upgrade run: re-adds nothing if the
    marked block already has the right path, and updates it in place
    (without disturbing the rest of the file) if the venv moved.
    """
    venv_bin = venv_dir / "bin"
    block = _block(venv_bin)
    home = Path(os.environ.get("HOME", str(Path.home())))
    for filename in _TARGET_FILES:
        target = home / filename
        if _upsert(target, block):
            log(f"  • {target}: added {venv_bin} to PATH")
        else:
            log(f"  • {target}: already up to date")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="h-mesh persist-path",
        description="Persist a venv's bin dir on PATH via ~/.bashrc and ~/.profile.",
    )
    parser.add_argument("venv", help="Path to the virtualenv directory")
    args = parser.parse_args(argv)
    persist_venv_on_path(Path(args.venv))


if __name__ == "__main__":
    main()
