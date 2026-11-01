from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


RUNNER = Path(__file__).resolve().parents[1] / "tools" / "run_vm_suite.sh"


def _installed_runner(tmp_path: Path) -> Path:
    runner = tmp_path / "run-suite.sh"
    shutil.copy(RUNNER, runner)
    return runner


@pytest.mark.parametrize("arguments", [[], ["../develop"], ["main", "develop"]])
def test_vm_runner_rejects_invalid_invocations_before_setup(tmp_path: Path, arguments):
    runner = _installed_runner(tmp_path)
    result = subprocess.run(
        ["bash", str(runner), *arguments], capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 2
    assert not (tmp_path / "suite.lock").exists()
    assert not (tmp_path / "r").exists()


def test_vm_runner_low_memory_aborts_without_worktree_or_suite(tmp_path: Path):
    runner = _installed_runner(tmp_path)
    environment = os.environ.copy()
    environment["CI_MIN_AVAILABLE_KB"] = "999999999999"
    result = subprocess.run(
        ["bash", str(runner), "origin/main"], env=environment,
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 1
    assert "available RAM" in result.stderr
    assert "protect lab offices" in result.stderr
    summary = json.loads(result.stdout.split("suite summary: ")[1])
    assert summary["ref"] == "origin/main"
    assert summary["collected"] == summary["passed"] == 0
    assert summary["failed_ids"] == []
    assert summary["exit_code"] == 1
    assert not (tmp_path / "r").exists()
    assert not list(tmp_path.glob("w.*"))


def test_vm_runner_lock_wait_times_out_without_setup(tmp_path: Path):
    runner = _installed_runner(tmp_path)
    environment = os.environ.copy()
    environment["CI_LOCK_TIMEOUT"] = "0"
    with (tmp_path / "suite.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = subprocess.run(
            ["bash", str(runner), "main"], env=environment,
            capture_output=True, text=True, timeout=5,
        )
    assert result.returncode == 75
    assert "waiting for suite lock" in result.stdout
    assert "another run owns it" in result.stderr
    assert not (tmp_path / "r").exists()
