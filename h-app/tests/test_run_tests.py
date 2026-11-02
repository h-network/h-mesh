from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tools import manifest_plugin, run_tests


REPO_ROOT = Path(__file__).resolve().parents[2]
H_APP = REPO_ROOT / "h-app"


@pytest.mark.parametrize("passes", [True, False], ids=["success", "failure"])
def test_runner_preserves_ambient_state_bytes_and_isolates_children(tmp_path: Path, passes: bool):
    ambient_home = tmp_path / "ambient-home"
    ambient_state = tmp_path / "ambient-state"
    for directory in (ambient_home / ".h-mesh", ambient_state):
        directory.mkdir(parents=True)
        (directory / "tasks.jsonl").write_bytes(b"existing board bytes\x00\xff\n")
        (directory / "window.log.jsonl").write_bytes(b"existing window bytes\x00\xfe\n")
    custody = ambient_state / "custody.jsonl"
    custody.write_bytes(b"existing custody bytes\n")
    files = [*ambient_state.glob("*.jsonl"), *(ambient_home / ".h-mesh").glob("*.jsonl")]
    before = {path: path.read_bytes() for path in files}
    record = tmp_path / "private-state.json"
    child_code = (
        "from core.logging import record_task_event, log_record; "
        "record_task_event('created', id='child', title='child fixture', agent='fixture', actor='fixture'); "
        "log_record('fixture', 'opened', source='child')"
    )
    source = (
        "import json, os, subprocess, sys\n"
        "from pathlib import Path\n"
        "from core.logging import record_task_event, log_record\n"
        "def test_target_tree_marker():\n"
        "    record_task_event('created', id='parent', title='parent fixture', agent='fixture', actor='fixture')\n"
        "    log_record('fixture', 'opened', source='parent')\n"
        f"    subprocess.run([sys.executable, '-c', {child_code!r}], check=True)\n"
        "    state = Path(os.environ['H_MESH_STATE_DIR'])\n"
        "    assert state.name == '.h-mesh'\n"
        "    assert 'child fixture' in (state / 'tasks.jsonl').read_text()\n"
        "    assert 'child' in (state / 'window.log.jsonl').read_text()\n"
        "    assert (Path.home() / '.h-mesh' / 'tasks.jsonl').resolve() == state / 'tasks.jsonl'\n"
        "    assert 'API_TOKEN' not in os.environ\n"
        "    assert 'VIRTUAL_ENV' not in os.environ\n"
        f"    Path({str(record)!r}).write_text(json.dumps(dict(root=os.environ['H_MESH_TEST_ROOT'])))\n"
        f"    assert {passes!r}\n"
    )
    target = _make_isolated_harness_tree(tmp_path, source)
    core = target / "h-app" / "core"
    core.mkdir()
    for name in ("config.py", "logging.py"):
        shutil.copy(H_APP / "core" / name, core / name)
    environment = os.environ.copy()
    environment.update({
        "PYTHONPATH": str(target / "h-app"), "HOME": str(ambient_home),
        "H_MESH_STATE_DIR": str(ambient_state), "TASK_RECORD": str(ambient_state / "tasks.jsonl"),
        "H_MESH_LOG_FILE": str(ambient_state / "window.log.jsonl"),
        "H_MESH_CUSTODY_FILE": str(custody), "H_MESH_RUN_DIR": str(ambient_state / "run"),
        "H_MESH_WORKDIR": str(ambient_state / "workdir"),
        "VIRTUAL_ENV": str(ambient_state / "venv"), "API_TOKEN": "fixture-secret",
    })
    result = subprocess.run(
        [sys.executable, "-m", "tools.run_tests"], cwd=target, env=environment,
        capture_output=True, text=True, timeout=20,
    )
    assert (result.returncode == 0) is passes, result.stdout + result.stderr
    assert record.exists(), result.stdout + result.stderr
    assert {path: path.read_bytes() for path in files} == before
    assert not Path(json.loads(record.read_text())["root"]).exists()
    assert "fixture-secret" not in result.stdout + result.stderr


@pytest.mark.parametrize("name", ["AGENT_NAME", "TENANT"])
def test_runner_refuses_office_identity_before_children(tmp_path: Path, name: str):
    target = _make_isolated_harness_tree(
        tmp_path, "raise AssertionError('must not collect office test')\n"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(target / "h-app")
    environment[name] = "fixture-office"
    result = subprocess.run(
        [sys.executable, "-m", "tools.run_tests"], cwd=target, env=environment,
        capture_output=True, text=True, timeout=10,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "refusing an office identity" in output
    assert name in output
    assert "collected" not in output
    assert "test state:" not in output


@pytest.mark.parametrize("url", [None, "redis://127.0.0.1:6379/0", "redis://remote.example:16379/0"],
                         ids=["unset", "default-port", "remote"])
def test_pytest_guard_rejects_unsafe_redis_even_with_override(url):
    environment = {"H_MESH_ALLOW_SHARED_REDIS": "1"}
    if url is not None:
        environment["REDIS_URL"] = url
    with run_tests._state_environment(environment):
        with pytest.raises(run_tests.UnsafeTestEnvironment, match="REDIS_URL"):
            run_tests._require_safe_test_environment(environment)


@pytest.mark.parametrize("name", [
    "HOME", "H_MESH_STATE_DIR", "H_MESH_WORKDIR", "H_MESH_RUN_DIR", "H_MESH_LOG_FILE",
    "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "TASK_RECORD", "H_MESH_CUSTODY_FILE",
])
def test_pytest_guard_rejects_ambient_path_overrides(tmp_path: Path, name: str):
    environment = {"REDIS_URL": "redis://127.0.0.1:16379/0"}
    with run_tests._state_environment(environment):
        environment[name] = str(tmp_path / "ambient-state")
        with pytest.raises(run_tests.UnsafeTestEnvironment, match="non-private"):
            run_tests._require_safe_test_environment(environment)


@pytest.mark.parametrize("root", [None, "/"], ids=["unset", "filesystem-root"])
def test_pytest_guard_rejects_missing_or_broad_state_root(root):
    environment = {"REDIS_URL": "redis://127.0.0.1:16379/0"}
    with run_tests._state_environment(environment):
        if root is None:
            environment.pop("H_MESH_TEST_ROOT")
        else:
            environment["H_MESH_TEST_ROOT"] = root
        with pytest.raises(run_tests.UnsafeTestEnvironment, match="non-private"):
            run_tests._require_safe_test_environment(environment)


def test_pytest_guard_rejects_state_symlink_escape(tmp_path: Path):
    environment = {"REDIS_URL": "redis://127.0.0.1:16379/0"}
    outside = tmp_path / "ambient-state"
    outside.mkdir()
    with run_tests._state_environment(environment):
        state = Path(environment["H_MESH_STATE_DIR"])
        (state / "run").rmdir()
        state.rmdir()
        state.symlink_to(outside, target_is_directory=True)
        with pytest.raises(run_tests.UnsafeTestEnvironment, match="non-private"):
            run_tests._require_safe_test_environment(environment)


def _install_session_guard(target: Path, reaper_source: str) -> None:
    tests = target / "h-app" / "tests"
    shutil.copy(H_APP / "tests" / "conftest.py", tests / "conftest.py")
    (tests / "_leak_manifest.py").write_text(reaper_source)


@pytest.mark.parametrize("unsafe", ["redis-unset", "redis-default-port", "redis-remote", "ambient-state"])
def test_direct_pytest_refuses_before_collection_or_reaping(tmp_path: Path, unsafe: str):
    target = _make_isolated_harness_tree(
        tmp_path, "raise AssertionError('test module must not be imported')\n"
    )
    _install_session_guard(target, "def reap_all_orphans(log=print):\n    raise AssertionError('reaper must not run')\n")
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(target / "h-app")
    with run_tests._state_environment(environment):
        if unsafe == "redis-unset":
            environment.pop("REDIS_URL", None)
        elif unsafe == "redis-default-port":
            environment["REDIS_URL"] = "redis://127.0.0.1:6379/0"
        elif unsafe == "redis-remote":
            environment["REDIS_URL"] = "redis://remote.example:16379/0"
        else:
            environment["H_MESH_STATE_DIR"] = str(tmp_path / "ambient-state")
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-o", "addopts="], cwd=target, env=environment,
            capture_output=True, text=True, timeout=10,
        )
    output = result.stdout + result.stderr
    assert result.returncode != 0
    expected = "non-private" if unsafe == "ambient-state" else "REDIS_URL"
    assert expected in output
    assert "test module must not be imported" not in output
    assert "reaper must not run" not in output
    assert "collected" not in output


def test_direct_pytest_accepts_private_runner_environment(tmp_path: Path):
    target = _make_isolated_harness_tree(
        tmp_path, "def test_target_tree_marker():\n    assert True\n"
    )
    record = tmp_path / "reaper-called"
    _install_session_guard(
        target, "from pathlib import Path\ndef reap_all_orphans(log=print):\n"
        f"    Path({str(record)!r}).write_text('private environment validated')\n"
        "    return 0, 0\n",
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(target / "h-app")
    with run_tests._state_environment(environment):
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-o", "addopts="], cwd=target, env=environment,
            capture_output=True, text=True, timeout=10,
        )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert record.exists()


def test_suite_environment_keeps_import_path_and_scrubs_office_settings(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(H_APP))
    for name in ("VIRTUAL_ENV", "VENV_PATH", "H_MESH_STATE_DIR", "H_MESH_LOG_FILE", "TASK_RECORD",
                 "H_MESH_CUSTODY_FILE", "TMUX", "TMUX_SOCKET", "OFFICE_TOOLS", "AGENTS", "API_TOKEN"):
        monkeypatch.setenv(name, "fixture-value")
    environment = run_tests._suite_environment()
    assert environment["PYTHONPATH"] == str(H_APP)
    assert environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert "fixture-value" not in environment.values()


@pytest.mark.parametrize("url,endpoint", [
    ("redis://127.0.0.1:16379/0", "127.0.0.1:16379"),
    ("redis://localhost:16379/1", "localhost:16379"),
    ("rediss://user:password@[::1]:16379/0", "[::1]:16379"),
    ("redis://127.1.2.3:16379/0", "127.1.2.3:16379"),
    ("redis://%31%32%37.0.0.1:16379/0", "127.0.0.1:16379"),
    ("redis://127.0.0.1/0?port=16379", "127.0.0.1:16379"),
    ("redis://127.0.0.1:16379/0?port=6379", "127.0.0.1:16379"),
])
def test_runner_accepts_explicit_private_redis(url: str, endpoint: str, monkeypatch, capsys):
    monkeypatch.setattr(run_tests.subprocess, "Popen", lambda *a, **k: pytest.fail("must reuse explicit Redis"))
    environment = {"REDIS_URL": url}
    with run_tests._redis_environment(environment):
        assert environment["REDIS_URL"] == url
    assert f"test Redis: {endpoint} (explicit REDIS_URL)" in capsys.readouterr().out


@pytest.mark.parametrize("url", [
    "redis://127.0.0.1:6379/0",
    "redis://localhost/0",
    "redis://[::1]:6379/0",
    "rediss://user:secret@remote.example:16379/0",
    "redis://0.0.0.0:16379/0",
    "redis://[::]:16379/0",
    "redis://%72emote.example:16379/0",
    "redis://127.0.0.1/0?port=6379",
    "redis:///0?host=remote.example&port=16379",
])
def test_runner_refuses_shared_redis_before_pytest(tmp_path: Path, url: str):
    target = _make_isolated_harness_tree(
        tmp_path, "raise AssertionError('pytest must not collect this module')\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(target / "h-app")
    env["REDIS_URL"] = url
    env.pop("H_MESH_ALLOW_SHARED_REDIS", None)
    result = subprocess.run(
        [sys.executable, "-m", "tools.run_tests"], cwd=target, env=env,
        capture_output=True, text=True, timeout=15,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "refusing REDIS_URL endpoint" in output
    assert "H_MESH_ALLOW_SHARED_REDIS=1" in output
    assert "secret" not in output
    assert "collected" not in output
    assert "pytest must not collect" not in output


@pytest.mark.parametrize("url", [
    "", "unix:///tmp/redis.sock", "not a URL", "redis://localhost:bad/0",
    "redis://localhost:99999/0", "redis://localhost/0?port=bad",
])
def test_runner_rejects_invalid_redis_url_without_echoing_credentials(url: str):
    with pytest.raises(run_tests.RedisSetupError, match="invalid REDIS_URL"):
        run_tests._redis_endpoint(url, allow_shared=True)


@pytest.mark.parametrize("override", ["", "true", "0", "1"])
def test_runner_shared_redis_requires_exact_explicit_override(override: str, monkeypatch, capsys):
    monkeypatch.setattr(run_tests.subprocess, "Popen", lambda *a, **k: pytest.fail("must not launch or connect"))
    environment = {"REDIS_URL": "redis://user:secret@remote.example:6379/0",
                   "H_MESH_ALLOW_SHARED_REDIS": override}
    if override == "1":
        with run_tests._redis_environment(environment):
            pass
        assert "remote.example:6379" in capsys.readouterr().out
    else:
        with pytest.raises(run_tests.RedisSetupError) as exc:
            with run_tests._redis_environment(environment):
                pytest.fail("shared endpoint was accepted")
        assert "secret" not in str(exc.value)


def test_runner_missing_redis_server_has_actionable_failure(monkeypatch):
    monkeypatch.setattr(run_tests.shutil, "which", lambda name: None)
    with pytest.raises(run_tests.RedisSetupError, match="install redis-server"):
        with run_tests._redis_environment({}):
            pytest.fail("must not run without Redis")


def test_runner_redis_readiness_timeout_cleans_process_and_directory(tmp_path: Path, monkeypatch):
    binary = tmp_path / "redis-server"
    binary.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
    binary.chmod(0o755)
    monkeypatch.setattr(run_tests.shutil, "which", lambda name: str(binary))
    launched = []
    popen = subprocess.Popen

    def capture_launch(args, **kwargs):
        process = popen(args, **kwargs)
        launched.append((process, Path(args[args.index("--dir") + 1])))
        return process

    monkeypatch.setattr(run_tests.subprocess, "Popen", capture_launch)
    clock = iter([0, 10])
    monkeypatch.setattr(run_tests.time, "monotonic", lambda: next(clock))
    environment = {}
    with pytest.raises(run_tests.RedisSetupError, match="did not become ready"):
        with run_tests._redis_environment(environment):
            pytest.fail("must not run before Redis readiness")
    assert len(launched) == 1
    process, directory = launched[0]
    assert process.poll() is not None
    assert not directory.exists()
    assert "REDIS_URL" not in environment


def _private_harness_environment(target: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(target / "h-app")
    environment.pop("REDIS_URL", None)
    environment.pop("H_MESH_ALLOW_SHARED_REDIS", None)
    return environment


def _redis_record_source(record: Path) -> str:
    return (
        "import json, os, time\n"
        "from pathlib import Path\n"
        "import redis\n"
        "def test_target_tree_marker():\n"
        "    url = os.environ['REDIS_URL']\n"
        "    client = redis.Redis.from_url(url)\n"
        "    server = client.info('server')\n"
        "    config = client.config_get()\n"
        f"    Path({str(record)!r}).write_text(json.dumps(dict(\n"
        "        url=url, pid=server['process_id'], child_pid=os.getpid(),\n"
        "        directory=config['dir'], save=config['save'],\n"
        "        appendonly=config['appendonly'], bind=config['bind'])))\n"
    )


def _assert_private_redis_cleaned(record: Path) -> None:
    from urllib.parse import urlsplit

    data = json.loads(record.read_text())
    endpoint = urlsplit(data["url"])
    assert endpoint.hostname == "127.0.0.1"
    assert endpoint.port != 6379
    assert data["save"] == ""
    assert data["appendonly"] == "no"
    assert data["bind"] == "127.0.0.1"
    assert not Path(data["directory"]).exists()
    with pytest.raises(ProcessLookupError):
        os.kill(data["pid"], 0)
    with pytest.raises(OSError):
        socket.create_connection((endpoint.hostname, endpoint.port), timeout=0.1)


@pytest.mark.parametrize("passes", [True, False])
def test_runner_private_redis_reaches_child_and_is_cleaned_after_exit(tmp_path: Path, passes: bool):
    record = tmp_path / "redis.json"
    target = _make_isolated_harness_tree(
        tmp_path, _redis_record_source(record) + f"    assert {passes!r}\n"
    )
    result = subprocess.run(
        [sys.executable, "-m", "tools.run_tests"], cwd=target,
        env=_private_harness_environment(target), capture_output=True, text=True, timeout=20,
    )
    assert (result.returncode == 0) is passes, result.stdout + result.stderr
    assert "test Redis: 127.0.0.1:" in result.stdout
    assert "(private, ephemeral)" in result.stdout
    _assert_private_redis_cleaned(record)


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_runner_interrupt_cleans_private_redis_and_pytest(tmp_path: Path, signum: int):
    record = tmp_path / "redis.json"
    target = _make_isolated_harness_tree(
        tmp_path, _redis_record_source(record) + "    time.sleep(30)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "tools.run_tests"], cwd=target,
        env=_private_harness_environment(target), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not record.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert record.exists(), "pytest did not reach the private Redis"
        process.send_signal(signum)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 128 + signum, stdout + stderr
        _assert_private_redis_cleaned(record)
        with pytest.raises(ProcessLookupError):
            os.kill(json.loads(record.read_text())["child_pid"], 0)
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=10)


def test_runner_failed_redis_start_cleans_directory_and_refuses_pytest(tmp_path: Path):
    target = _make_isolated_harness_tree(
        tmp_path, "raise AssertionError('pytest must not collect this module')\n"
    )
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    record = tmp_path / "redis-args.json"
    binary = binary_dir / "redis-server"
    binary.write_text(
        f"#!{sys.executable}\nimport json, sys\nfrom pathlib import Path\n"
        f"Path({str(record)!r}).write_text(json.dumps(sys.argv))\nraise SystemExit(1)\n"
    )
    binary.chmod(0o755)
    env = _private_harness_environment(target)
    env["PATH"] = str(binary_dir) + os.pathsep + env["PATH"]
    result = subprocess.run(
        [sys.executable, "-m", "tools.run_tests"], cwd=target, env=env,
        capture_output=True, text=True, timeout=15,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "private redis-server exited during startup" in output
    assert "collected" not in output
    args = json.loads(record.read_text())
    assert not Path(args[args.index("--dir") + 1]).exists()


def _make_isolated_harness_tree(tmp_path: Path, test_source: str) -> Path:
    target = tmp_path / "isolated-tree"
    tools = target / "h-app" / "tools"
    tests = target / "h-app" / "tests"
    services = target / "h-app" / "services"
    tools.mkdir(parents=True)
    tests.mkdir()
    services.mkdir()
    shutil.copy(H_APP / "tools" / "run_tests.py", tools / "run_tests.py")
    shutil.copy(H_APP / "tools" / "manifest_plugin.py", tools / "manifest_plugin.py")
    (tools / "test_nodeids.txt").write_text(
        "h-app/tests/test_target.py::test_target_tree_marker\n"
    )
    (tests / "test_target.py").write_text(test_source)
    (services / "daemons.py").write_text("# first-party child import probe\n")
    (target / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        "testpaths = ['h-app']\n"
        "addopts = '--collect-only'\n"
    )
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    return target


def test_runner_environment_reaches_first_party_child_processes() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import services.daemons as m; "
            "print(Path(m.__file__).resolve())",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, (
        "a suite environment must let spawned children import first-party modules; "
        f"stderr was {result.stderr!r}"
    )
    assert Path(result.stdout.strip()).is_relative_to(H_APP), (
        "the child must import services.daemons from the tree being certified; "
        f"resolved {result.stdout.strip()!r}"
    )


def test_runner_refuses_before_pytest_when_child_import_path_is_missing(
    tmp_path: Path,
) -> None:
    target = _make_isolated_harness_tree(
        tmp_path, "def test_target_tree_marker():\n    assert True\n"
    )
    (target / "h-app" / "services" / "daemons.py").unlink()
    isolated_venv = tmp_path / "isolated-python"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(isolated_venv)],
        check=True,
        capture_output=True,
        text=True,
    )
    isolated_python = isolated_venv / "bin" / "python"
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [str(isolated_python), str(target / "h-app" / "tools" / "run_tests.py")],
        cwd=target,
        env=env,
        capture_output=True,
        text=True,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert f"interpreter: {isolated_python}" in output
    assert "module: services.daemons" in output
    assert "resolved module: <not importable>" in output
    assert "ModuleNotFoundError" in output
    assert f"expected under: {(target / 'h-app').resolve()}" in output
    expected_venv = target / ".venv"
    assert f"python3 -m venv {expected_venv}" in output
    assert f"{expected_venv / 'bin' / 'python'} -m pip install -e {target}" in output
    assert f"{sys.executable} -m pip install" not in output
    assert "collected" not in output
    assert "suite execution invariant satisfied" not in output


def test_runner_refuses_before_pytest_when_child_imports_another_tree(
    tmp_path: Path,
) -> None:
    target = _make_isolated_harness_tree(
        tmp_path, "def test_target_tree_marker():\n    assert True\n"
    )
    foreign = tmp_path / "foreign" / "services"
    foreign.mkdir(parents=True)
    foreign_daemons = foreign / "daemons.py"
    foreign_daemons.write_text("# wrong-tree module\n")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(foreign.parent)
    result = subprocess.run(
        [sys.executable, str(target / "h-app" / "tools" / "run_tests.py")],
        cwd=target,
        env=env,
        capture_output=True,
        text=True,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert f"interpreter: {sys.executable}" in output
    assert "module: services.daemons" in output
    assert f"resolved module: {foreign_daemons.resolve()}" in output
    assert f"expected under: {(target / 'h-app').resolve()}" in output
    expected_venv = target / ".venv"
    assert f"python3 -m venv {expected_venv}" in output
    assert f"{expected_venv / 'bin' / 'python'} -m pip install -e {target}" in output
    assert f"{sys.executable} -m pip install" not in output
    assert "collected" not in output
    assert "suite execution invariant satisfied" not in output


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


def test_runner_cannot_certify_zero_execution_as_a_passing_suite(tmp_path: Path) -> None:
    target = _make_isolated_harness_tree(
        tmp_path, "def test_target_tree_marker():\n    assert True\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(target / "h-app")
    env["PYTEST_ADDOPTS"] = "--collect-only"
    env["PYTEST_PLUGINS"] = "plugin_that_must_not_be_loaded"
    result = subprocess.run(
        [sys.executable, "-m", "tools.run_tests"],
        cwd=target,
        env=env,
        capture_output=True,
        text=True,
    )

    output = result.stdout + result.stderr
    false_success = (
        result.returncode == 0
        and "collection invariant satisfied" in output
        and "passed" not in output
    )
    assert not false_success, (
        "the suite certificate must not describe collection when zero tests executed"
    )
    assert result.returncode == 0
    assert "1 passed" in output
    assert "suite execution invariant satisfied: 1 terminal outcomes" in output


def test_setup_phase_skip_cannot_satisfy_execution_invariant(tmp_path: Path) -> None:
    target = _make_isolated_harness_tree(
        tmp_path,
        "import pytest\n"
        "@pytest.mark.skip(reason='setup-only terminal state')\n"
        "def test_target_tree_marker():\n"
        "    raise AssertionError('call phase must not run')\n",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(target / "h-app")
    result = subprocess.run(
        [sys.executable, "-m", "tools.run_tests"],
        cwd=target,
        env=env,
        capture_output=True,
        text=True,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0, (
        "a setup-phase skip must not be certified as an executed test call"
    )
    assert (
        "not executed: no call-phase report: "
        "h-app/tests/test_target.py::test_target_tree_marker"
    ) in output
    assert "suite execution invariant satisfied" not in output


def test_call_phase_skip_cannot_satisfy_execution_invariant(tmp_path: Path) -> None:
    target = _make_isolated_harness_tree(
        tmp_path,
        "import pytest\n"
        "def test_target_tree_marker():\n"
        "    pytest.skip('Redis server is unavailable')\n"
        "    raise AssertionError('reviewed assertion did not run')\n",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(target / "h-app")
    result = subprocess.run(
        [sys.executable, "-m", "tools.run_tests"],
        cwd=target,
        env=env,
        capture_output=True,
        text=True,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0, (
        "a call-phase skip must not certify when the reviewed assertion never ran"
    )
    assert "not executed: skipped" in output.lower()
    assert "Redis server is unavailable" in output
    assert "suite execution invariant satisfied" not in output


def test_later_finish_failure_cannot_leave_a_public_suite_certificate(tmp_path: Path) -> None:
    target = _make_isolated_harness_tree(
        tmp_path, "def test_target_tree_marker():\n    assert True\n"
    )
    (target / "h-app" / "conftest.py").write_text(
        "import pytest\n"
        "@pytest.hookimpl(hookwrapper=True, tryfirst=True)\n"
        "def pytest_sessionfinish():\n"
        "    yield\n"
        "    raise RuntimeError('failure after manifest sessionfinish')\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(target / "h-app")
    result = subprocess.run(
        [sys.executable, "-m", "tools.run_tests"],
        cwd=target,
        env=env,
        capture_output=True,
        text=True,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "failure after manifest sessionfinish" in output
    assert "suite execution invariant satisfied" not in output, (
        "a certificate must not precede a later failure in pytest process completion"
    )


def test_clean_child_exit_before_finish_cannot_make_runner_succeed(tmp_path: Path) -> None:
    target = _make_isolated_harness_tree(
        tmp_path,
        "import os\n"
        "def test_target_tree_marker():\n"
        "    os._exit(0)\n",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(target / "h-app")
    result = subprocess.run(
        [sys.executable, "-m", "tools.run_tests"],
        cwd=target,
        env=env,
        capture_output=True,
        text=True,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0, (
        "child exit 0 without completed finish attestation must not make the runner pass"
    )
    assert "exited 0 without a suite-completion attestation" in output
    assert "suite execution invariant satisfied" not in output


def test_mismatched_attestation_is_distinct_from_absent_attestation(tmp_path: Path) -> None:
    target = _make_isolated_harness_tree(
        tmp_path, "def test_target_tree_marker():\n    assert True\n"
    )
    (target / "h-app" / "conftest.py").write_text(
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "import pytest\n"
        "@pytest.hookimpl(hookwrapper=True, tryfirst=True)\n"
        "def pytest_sessionfinish():\n"
        "    yield\n"
        "    path = Path(os.environ['H_MESH_TEST_ATTESTATION_PATH'])\n"
        "    attestation = json.loads(path.read_text())\n"
        "    attestation['nonce'] = 'stale-or-foreign-invocation'\n"
        "    path.write_text(json.dumps(attestation))\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(target / "h-app")
    result = subprocess.run(
        [sys.executable, "-m", "tools.run_tests"],
        cwd=target,
        env=env,
        capture_output=True,
        text=True,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "attestation does not match this runner invocation" in output
    assert "without a suite-completion attestation" not in output
    assert "suite execution invariant satisfied" not in output
