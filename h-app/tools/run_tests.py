#!/usr/bin/env python3
"""Run the complete h-mesh suite after verifying its collection invariant."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_MINIMUM_TEST_COUNT = 660


def main() -> int:
    collect = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=REPO_ROOT,
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
        cwd=REPO_ROOT,
    )


if __name__ == "__main__":
    raise SystemExit(main())
