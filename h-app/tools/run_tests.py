#!/usr/bin/env python3
"""Run the complete h-mesh suite after verifying its collection invariant."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


RUNNER_PATH = Path(__file__).resolve()
EXPECTED_MINIMUM_TEST_COUNT = 759


def _find_invoked_tree(start: Path) -> Path | None:
    """Return the repository containing the caller's cwd, if it is h-mesh."""
    resolved = start.resolve()
    for candidate in (resolved, *resolved.parents):
        if (
            (candidate / "pyproject.toml").is_file()
            and (candidate / "h-app" / "tools" / "run_tests.py").is_file()
        ):
            return candidate
    return None


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

    collect = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if collect.returncode != 0:
        sys.stdout.write(collect.stdout)
        sys.stderr.write(collect.stderr)
        return collect.returncode

    summary = re.search(r"(\d+) tests? collected", collect.stdout)
    if summary is None:
        sys.stdout.write(collect.stdout)
        print("error: pytest collection output did not contain a test count", file=sys.stderr)
        return 1

    collected = int(summary.group(1))
    if collected < EXPECTED_MINIMUM_TEST_COUNT:
        print(
            f"error: expected at least {EXPECTED_MINIMUM_TEST_COUNT} tests, "
            f"but pytest collected {collected}.\n"
            "If tests were intentionally removed, update "
            "EXPECTED_MINIMUM_TEST_COUNT in h-app/tools/run_tests.py.",
            file=sys.stderr,
        )
        return 1

    print(f"collection invariant satisfied: {collected} tests")
    return subprocess.call(
        [sys.executable, "-m", "pytest", *sys.argv[1:]],
        cwd=repo_root,
    )


if __name__ == "__main__":
    raise SystemExit(main())
