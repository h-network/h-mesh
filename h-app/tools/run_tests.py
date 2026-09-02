#!/usr/bin/env python3
"""Run the complete h-mesh suite after verifying its collection invariant."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
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

    manifest = repo_root / "h-app" / "tools" / "test_nodeids.txt"
    manifest_bytes = manifest.read_bytes()
    expected_count = len([line for line in manifest_bytes.splitlines() if line.strip()])
    nonce = secrets.token_hex(32)
    with tempfile.TemporaryDirectory(prefix="h-mesh-test-attestation-") as temp_dir:
        attestation_path = Path(temp_dir) / "complete.json"
        pytest_env = os.environ.copy()
        pytest_env.pop("PYTEST_ADDOPTS", None)
        pytest_env.pop("PYTEST_PLUGINS", None)
        pytest_env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        pytest_env["H_MESH_TEST_ATTESTATION_PATH"] = str(attestation_path)
        pytest_env["H_MESH_TEST_ATTESTATION_NONCE"] = nonce
        child_status = subprocess.call(
            [
                sys.executable,
                "-m",
                "pytest",
                "-o",
                "addopts=",
                "-p",
                "tools.manifest_plugin",
            ],
            cwd=repo_root,
            env=pytest_env,
        )
        if child_status != 0:
            return child_status
        if not attestation_path.is_file():
            print(
                "error: pytest exited 0 without a suite-completion attestation; "
                "the test process did not prove completion",
                file=sys.stderr,
            )
            return 1

        expected_attestation = {
            "version": 1,
            "nonce": nonce,
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "terminal_count": expected_count,
        }
        try:
            actual_attestation = json.loads(attestation_path.read_text())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            print(
                f"error: pytest produced an invalid suite-completion attestation: {exc}",
                file=sys.stderr,
            )
            return 1
        if actual_attestation != expected_attestation:
            print(
                "error: pytest suite-completion attestation does not match this "
                "runner invocation, manifest, or tree",
                file=sys.stderr,
            )
            return 1

    print(
        f"suite execution invariant satisfied: {expected_count} terminal outcomes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
