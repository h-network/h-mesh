#!/usr/bin/env python3
"""Run the complete h-mesh suite after verifying its collection invariant."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


RUNNER_PATH = Path(__file__).resolve()
FIRST_PARTY_PROBE = "services.daemons"


class RedisSetupError(Exception):
    """A safe Redis endpoint could not be prepared."""


class RunnerInterrupted(BaseException):
    def __init__(self, signum: int):
        self.signum = signum


def _interrupt(signum: int, _frame) -> None:
    raise RunnerInterrupted(signum)


def _redis_endpoint(url: str, *, allow_shared: bool) -> str:
    # Use the client's parser so percent escapes and query-string defaults are
    # checked exactly as they will be used by the tests. Never echo the URL or
    # parser exception: either can contain credentials.
    from redis.connection import parse_url

    try:
        if not url.startswith(("redis://", "rediss://")):
            raise ValueError
        options = parse_url(url)
        host = options.get("host", "localhost")
        port = int(options.get("port", 6379))
        if not host or not 1 <= port <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        raise RedisSetupError(
            "invalid REDIS_URL; use a redis:// or rediss:// TCP URL"
        ) from None
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host.lower() == "localhost"
    endpoint = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    if (not loopback or port == 6379) and not allow_shared:
        raise RedisSetupError(
            f"refusing REDIS_URL endpoint {endpoint}: port 6379 and non-loopback "
            "hosts may be a live office Redis. Unset REDIS_URL for a private "
            "server, or explicitly set H_MESH_ALLOW_SHARED_REDIS=1 to override"
        )
    return endpoint


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


@contextmanager
def _redis_environment(environment: dict[str, str]):
    if "REDIS_URL" in environment:
        endpoint = _redis_endpoint(
            environment["REDIS_URL"],
            allow_shared=environment.get("H_MESH_ALLOW_SHARED_REDIS") == "1",
        )
        print(f"test Redis: {endpoint} (explicit REDIS_URL)", flush=True)
        yield
        return

    binary = shutil.which("redis-server")
    if binary is None:
        raise RedisSetupError(
            "redis-server is not installed; install redis-server and rerun "
            "(for example: apt-get install redis-server), or set REDIS_URL "
            "to a private loopback Redis on a port other than 6379"
        )
    with tempfile.TemporaryDirectory(prefix="h-mesh-test-redis-") as directory:
        # The OS chooses an ephemeral loopback port. If another process wins
        # the bind race, Redis exits and we fail closed before running pytest.
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        if port == 6379:
            raise RedisSetupError("OS selected reserved Redis port 6379; rerun")
        process = None
        try:
            process = subprocess.Popen(
                [binary, "--bind", "127.0.0.1", "--port", str(port),
                 "--save", "", "--appendonly", "no", "--dir", directory,
                 "--unixsocket", str(Path(directory) / "redis.sock"),
                 "--unixsocketperm", "700",
                 "--daemonize", "no"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            deadline = time.monotonic() + 5
            while True:
                if process.poll() is not None:
                    raise RedisSetupError("private redis-server exited during startup")
                try:
                    # Readiness on our unique socket cannot mistake a process
                    # that won the TCP bind race for the server we launched.
                    with socket.socket(socket.AF_UNIX) as client:
                        client.settimeout(0.1)
                        client.connect(str(Path(directory) / "redis.sock"))
                        client.sendall(b"*1\r\n$4\r\nPING\r\n")
                        ready = client.recv(64) == b"+PONG\r\n"
                    if ready and process.poll() is None:
                        break
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise RedisSetupError("private redis-server did not become ready within 5s")
                time.sleep(0.02)
            environment["REDIS_URL"] = f"redis://127.0.0.1:{port}/0"
            print(f"test Redis: 127.0.0.1:{port} (private, ephemeral)", flush=True)
            yield
        except OSError:
            raise RedisSetupError("could not start private redis-server; check its installation") from None
        finally:
            if process is not None:
                _stop_process(process)
            environment.pop("REDIS_URL", None)


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


def _suite_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTEST_ADDOPTS", None)
    environment.pop("PYTEST_PLUGINS", None)
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return environment


def _child_import_path(
    module: str, *, cwd: Path, env: dict[str, str]
) -> tuple[Path | None, str]:
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; "
            f"import {module} as target; "
            "print(Path(target.__file__).resolve())",
        ],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        return None, probe.stderr.strip()
    try:
        return Path(probe.stdout.strip()).resolve(), ""
    except (OSError, RuntimeError) as exc:
        return None, f"invalid child module path: {exc}"


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

    pytest_env = _suite_environment()
    imported_path, import_error = _child_import_path(
        FIRST_PARTY_PROBE, cwd=repo_root, env=pytest_env
    )
    expected_package_root = (repo_root / "h-app").resolve()
    if imported_path is None or not imported_path.is_relative_to(expected_package_root):
        rendered_import = str(imported_path) if imported_path is not None else "<not importable>"
        isolated_venv = repo_root / ".venv"
        isolated_python = isolated_venv / "bin" / "python"
        print(
            "error: the test interpreter cannot import first-party children from "
            "the tree being certified\n"
            f"interpreter: {sys.executable}\n"
            f"module: {FIRST_PARTY_PROBE}\n"
            f"resolved module: {rendered_import}\n"
            f"child import error: {import_error or '<none>'}\n"
            f"expected under: {expected_package_root}\n"
            "create a tree-local environment and install this tree before running "
            "the suite; do not install into the invoking interpreter unless you "
            "own it:\n"
            f"  {shlex.join(['python3', '-m', 'venv', str(isolated_venv)])}\n"
            f"  {shlex.join([str(isolated_python), '-m', 'pip', 'install', '-e', str(repo_root)])}\n"
            f"  {shlex.join([str(isolated_python), '-m', 'tools.run_tests'])}",
            file=sys.stderr,
        )
        return 1

    previous_handlers = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, _interrupt)
        with _redis_environment(pytest_env):
            return _run_suite(repo_root, pytest_env)
    except RedisSetupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except RunnerInterrupted as exc:
        print(f"test runner interrupted by signal {exc.signum}", file=sys.stderr)
        return 128 + exc.signum
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def _run_suite(repo_root: Path, pytest_env: dict[str, str]) -> int:
    manifest = repo_root / "h-app" / "tools" / "test_nodeids.txt"
    manifest_bytes = manifest.read_bytes()
    expected_count = len([line for line in manifest_bytes.splitlines() if line.strip()])
    nonce = secrets.token_hex(32)
    with tempfile.TemporaryDirectory(prefix="h-mesh-test-attestation-") as temp_dir:
        attestation_path = Path(temp_dir) / "complete.json"
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
