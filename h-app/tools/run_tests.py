#!/usr/bin/env python3
"""Run the complete h-mesh suite after verifying its collection invariant."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


RUNNER_PATH = Path(__file__).resolve()


def _find_invoked_tree(start: Path) -> Path | None:
    """Return the Git worktree containing the caller's cwd, if it is h-mesh."""
    resolved = start.resolve()
    git_root = subprocess.run(
        ["git", "-C", str(resolved), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if git_root.returncode != 0:
        return None
    candidate = Path(git_root.stdout.strip()).resolve()
    if not (
        (candidate / "pyproject.toml").is_file()
        and (candidate / "h-app" / "tools" / "run_tests.py").is_file()
    ):
        return None
    return candidate


def _print_provenance(tree: Path | None) -> None:
    rendered_tree = str(tree) if tree is not None else "<unresolved>"
    print(f"test tree: {rendered_tree}", flush=True)
    print(f"runner module: {RUNNER_PATH}", flush=True)
    print(f"pytest cwd: {rendered_tree}", flush=True)


def main() -> int:
    repo_root = _find_invoked_tree(Path.cwd())
    _print_provenance(repo_root)
    if repo_root is None:
        print(
            "error: the invoking cwd is not inside an h-mesh repository; "
            "refusing to guess which tree to test",
            file=sys.stderr,
        )
        return 1

    expected_runner = (repo_root / "h-app" / "tools" / "run_tests.py").resolve()
    if RUNNER_PATH != expected_runner:
        print(
            "error: the imported test runner does not belong to the invoking tree; "
            "refusing to report a suite result\n"
            f"invoking tree runner: {expected_runner}",
            file=sys.stderr,
        )
        return 1

    if sys.argv[1:]:
        print(
            "error: the complete-suite runner accepts no pytest arguments; "
            "forwarding selectors or configuration could narrow the executed suite",
            file=sys.stderr,
        )
        return 1

    return subprocess.call(
        [sys.executable, "-m", "pytest", "-p", "tools.manifest_plugin"],
        cwd=repo_root,
    )


if __name__ == "__main__":
    raise SystemExit(main())
