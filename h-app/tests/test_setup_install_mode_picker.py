"""Exercise setup.sh's install-mode routing without bootstrapping either mode."""

import os
import pty
import select
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PICKER = "[container/host] (container): "


@pytest.fixture
def picker_setup(tmp_path):
    setup = tmp_path / "setup.sh"
    shutil.copy2(REPO_ROOT / "setup.sh", setup)
    container = tmp_path / "container"
    container.mkdir()
    bootstrap = container / "bootstrap.sh"
    bootstrap.write_text("#!/usr/bin/env bash\necho container-selected\n")
    bootstrap.chmod(0o755)
    python = tmp_path / "python"
    python.write_text("#!/usr/bin/env bash\necho host-python-invoked >&2\nexit 1\n")
    python.chmod(0o755)
    # No ambient installation mode, credentials, or real Python/Redis work.
    env = {"PATH": os.defpath, "HOME": str(tmp_path), "PYTHON": str(python)}
    return setup, env


def _run_at_tty(setup, env, args, *, answer=None, until):
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        [str(setup), *args], stdin=slave, stdout=slave, stderr=slave, env=env,
    )
    os.close(slave)
    output = ""
    answered = False
    deadline = time.monotonic() + 10
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([master], [], [], 0.1)
            if readable:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                output += chunk.decode(errors="replace")
                if answer is not None and not answered and PICKER in output:
                    os.write(master, (answer + "\n").encode())
                    answered = True
                if until in output:
                    break
        assert until in output, output
        return output
    finally:
        # A host selection is stopped at the pod prompt, before host setup.
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=5)
        os.close(master)


@pytest.mark.parametrize("answer", ["", "h", "Host"])
def test_setup_tty_picker_answer_routing(picker_setup, answer):
    setup, env = picker_setup
    selected = "container-selected" if not answer else "Pod name ["
    output = _run_at_tty(
        setup, env, ["--skip-deps"], answer=answer, until=selected,
    )
    assert PICKER in output
    assert ("container-selected" in output) == (not answer)


@pytest.mark.parametrize("non_interactive", [False, True])
def test_setup_no_tty_still_defaults_to_host(picker_setup, non_interactive):
    setup, env = picker_setup
    args = ["--skip-deps", "--no-venv", "--skip-install", "--no-daemons"]
    if non_interactive:
        args.append("--non-interactive")
    result = subprocess.run(
        [str(setup), *args], stdin=subprocess.DEVNULL, capture_output=True,
        text=True, env=env, timeout=10,
    )
    assert result.returncode == 1  # The stub Python rejects the Redis check.
    assert "host-python-invoked" in result.stderr
    assert "container-selected" not in result.stdout
    assert PICKER not in result.stdout + result.stderr


def test_setup_non_interactive_at_tty_still_defaults_to_host(picker_setup):
    setup, env = picker_setup
    output = _run_at_tty(
        setup, env,
        ["--non-interactive", "--skip-deps", "--no-venv", "--skip-install", "--no-daemons"],
        until="host-python-invoked",
    )
    assert PICKER not in output
    assert "container-selected" not in output
