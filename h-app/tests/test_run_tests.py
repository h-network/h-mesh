from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tools import manifest_plugin, run_tests


REPO_ROOT = Path(__file__).resolve().parents[2]
H_APP = REPO_ROOT / "h-app"


def test_runner_refuses_to_report_success_for_a_different_tree(tmp_path: Path) -> None:
    target = tmp_path / "other-tree"
    (target / "h-app" / "tools").mkdir(parents=True)
    (target / "h-app" / "tests").mkdir()
    (target / "pyproject.toml").write_text("[tool.pytest.ini_options]\ntestpaths = ['h-app']\n")
    (target / "h-app" / "tools" / "run_tests.py").write_text(
        "raise RuntimeError('the target tree runner was not imported')\n"
    )
    (target / "h-app" / "tests" / "test_target.py").write_text(
        "def test_target_tree_marker():\n    assert True\n"
    )
    (target / "pytest").mkdir()
    (target / "pytest" / "__init__.py").write_text("")
    (target / "pytest" / "__main__.py").write_text(
        "import sys\n"
        "if '--collect-only' in sys.argv:\n"
        "    print('999 tests collected')\n"
        "else:\n"
        "    print('999 passed')\n"
    )
    subprocess.run(["git", "init", "-q", str(target)], check=True)

    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join((str(H_APP), str(target)))
    result = subprocess.run(
        [sys.executable, "-m", "tools.run_tests"],
        cwd=target,
        env=env,
        capture_output=True,
        text=True,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0, (
        "a runner imported from another checkout must not report a passing suite "
        "for that checkout"
    )
    assert "collection invariant satisfied" not in output
    assert "passed" not in output
    assert f"test tree: {target}" in output
    assert f"runner module: {H_APP / 'tools' / 'run_tests.py'}" in output
    assert f"pytest cwd: {target}" in output
    assert "refusing to report a suite result" in output


def test_runner_refuses_to_certify_full_collection_then_run_a_narrow_suite() -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(H_APP)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.run_tests",
            "h-app/tests/test_paths.py",
            "-q",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0, (
        "the complete-suite runner must not certify full collection and then "
        "report success for a narrowed execution"
    )
    assert "collection invariant satisfied" not in output
    assert "9 passed" not in output
    assert "accepts no pytest arguments" in output


def test_invoked_tree_identity_supports_a_linked_git_worktree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    linked = tmp_path / "linked"
    (source / "h-app" / "tools").mkdir(parents=True)
    (source / "pyproject.toml").write_text("[project]\nname = 'h-mesh-fixture'\n")
    (source / "h-app" / "tools" / "run_tests.py").write_text("# fixture\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(source), "worktree", "add", "-q", "-b", "linked", str(linked)],
        check=True,
    )

    assert (linked / ".git").is_file()
    assert run_tests._find_invoked_tree(linked / "h-app") == linked


def test_manifest_difference_distinguishes_missing_from_added(
    tmp_path: Path, monkeypatch
) -> None:
    manifest = tmp_path / "nodeids.txt"
    manifest.write_text("tests/test_example.py::test_retained\ntests/test_example.py::test_missing\n")
    monkeypatch.setattr(manifest_plugin, "NODEID_MANIFEST", manifest)

    missing, added = manifest_plugin.manifest_difference(
        {"tests/test_example.py::test_retained", "tests/test_example.py::test_added"}
    )

    assert missing == ["tests/test_example.py::test_missing"]
    assert added == ["tests/test_example.py::test_added"]
