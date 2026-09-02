from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


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
