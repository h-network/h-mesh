"""Host TLS validation and staging must finish before Compose can start."""

import os
import stat
import subprocess
from pathlib import Path

import pytest

from _tls_test_material import certificate_pair
from test_container_bootstrap import BOOTSTRAP, _env_with_fake_docker, _fake_docker


@pytest.fixture
def tls_bootstrap(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    _fake_docker(bin_dir, log)
    office = tmp_path / "office & literal|name"
    office.mkdir()
    env_file = office / ".env"
    cert, key = certificate_pair(tmp_path)
    env = _env_with_fake_docker(bin_dir)
    env.update(API_TLS_CERT=str(cert), API_TLS_KEY=str(key), H_MESH_CA_CERT=str(cert))
    args = ["bash", str(BOOTSTRAP), "--pod", "testpod", "--tenant", "testtenant",
            "--env-file", str(env_file), "--non-interactive", "--skip-build"]

    def run(extra=None, clean=False):
        active = _env_with_fake_docker(bin_dir) if clean else env.copy()
        active.update(extra or {})
        result = subprocess.run(args, env=active, capture_output=True, text=True, timeout=15)
        return result

    return run, env_file, cert, key, log, bin_dir


def test_tls_stages_private_copies_and_readonly_compose_mount(tls_bootstrap):
    run, env_file, cert, key, log, _ = tls_bootstrap
    result = run()
    assert result.returncode == 0, result.stderr
    staged = env_file.parent / "tls"
    assert (staged / "api.crt").read_bytes() == cert.read_bytes()
    assert (staged / "api.key").read_bytes() == key.read_bytes()
    assert (staged / "ca.crt").read_bytes() == cert.read_bytes()
    assert stat.S_IMODE(staged.stat().st_mode) == 0o700
    assert stat.S_IMODE((staged / "api.key").stat().st_mode) == 0o600
    assert (staged / "api.key").stat().st_uid == 1000
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    values = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
    assert values["API_TLS_CERT"] == "/run/h-mesh/tls/api.crt"
    assert values["API_TLS_KEY"] == "/run/h-mesh/tls/api.key"
    assert values["H_MESH_CA_CERT"] == "/run/h-mesh/tls/ca.crt"
    assert values["H_MESH_TLS_DIR"] == str(staged)
    assert "compose.tls.yaml" in log.read_text()
    overlay = (BOOTSTRAP.parent / "compose.tls.yaml").read_text()
    assert "read_only: true" in overlay and "create_host_path: false" in overlay
    assert key.read_text() not in result.stdout + result.stderr


def test_tls_clean_shell_rerun_preserves_bytes_inodes_and_paths(tls_bootstrap):
    run, env_file, _, _, _, _ = tls_bootstrap
    assert run().returncode == 0
    paths = list((env_file.parent / "tls").iterdir())
    original = {p.name: (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns) for p in paths}
    result = run(clean=True)
    assert result.returncode == 0, result.stderr
    assert original == {p.name: (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns) for p in paths}
    assert "H_MESH_TLS_DIR=" + str(env_file.parent / "tls") in env_file.read_text()


def test_tls_new_host_paths_explicitly_replace_only_staged_copies(tls_bootstrap, tmp_path):
    run, env_file, old_cert, old_key, _, _ = tls_bootstrap
    assert run().returncode == 0
    original = old_cert.read_bytes(), old_key.read_bytes()
    cert, key = certificate_pair(tmp_path, "replacement")
    result = run({"API_TLS_CERT": str(cert), "API_TLS_KEY": str(key)})
    assert result.returncode == 0, result.stderr
    assert (env_file.parent / "tls/api.crt").read_bytes() == cert.read_bytes()
    assert (env_file.parent / "tls/api.key").read_bytes() == key.read_bytes()
    assert original == (old_cert.read_bytes(), old_key.read_bytes())


@pytest.mark.parametrize("fault", ["missing-cert", "directory-key", "invalid-cert", "invalid-key", "mismatch", "partial", "invalid-ca"])
def test_tls_bad_material_never_reaches_compose_up(tls_bootstrap, tmp_path, fault):
    run, env_file, cert, key, log, _ = tls_bootstrap
    extra = {}
    if fault == "missing-cert":
        extra["API_TLS_CERT"] = str(tmp_path / "missing")
    elif fault == "directory-key":
        extra["API_TLS_KEY"] = str(tmp_path)
    elif fault == "invalid-cert":
        cert.write_text("not PEM")
    elif fault == "invalid-key":
        key.write_text("private material must never be printed")
    elif fault == "mismatch":
        _, other_key = certificate_pair(tmp_path, "other")
        extra["API_TLS_KEY"] = str(other_key)
    elif fault == "partial":
        extra["API_TLS_KEY"] = ""
    else:
        invalid = tmp_path / "bad-ca"
        invalid.write_text("-----BEGIN CERTIFICATE-----\ninvalid\n-----END CERTIFICATE-----")
        extra["H_MESH_CA_CERT"] = str(invalid)
    result = run(extra)
    assert result.returncode != 0
    assert "TLS" in result.stderr
    assert "up -d" not in log.read_text()
    assert "API_TLS_CERT=" not in env_file.read_text()
    assert not (env_file.parent / "tls").exists()
    assert not list(env_file.parent.glob(".tls.*"))
    assert "private material must never be printed" not in result.stdout + result.stderr


def test_tls_unsupported_uid_fails_before_up(tls_bootstrap):
    run, _, _, _, log, bin_dir = tls_bootstrap
    fake_id = bin_dir / "id"
    fake_id.write_text("#!/bin/sh\nprintf '2001\\n'\n")
    fake_id.chmod(0o755)
    result = run()
    assert result.returncode != 0
    assert "permission: host UID must be 1000 or root" in result.stderr
    assert "up -d" not in log.read_text()


@pytest.mark.parametrize("mode", ["session", "ca-only"])
def test_tls_session_pair_and_ca_only_configuration_are_staged(tls_bootstrap, mode):
    run, env_file, cert, key, _, _ = tls_bootstrap
    result = run({"API_TLS_CERT": "", "API_TLS_KEY": "", "SESSION_TLS_CERT": str(cert) if mode == "session" else "",
                  "SESSION_TLS_KEY": str(key) if mode == "session" else ""})
    assert result.returncode == 0, result.stderr
    text = env_file.read_text()
    assert ("SESSION_TLS_CERT=/run/h-mesh/tls/session.crt" in text) == (mode == "session")
    assert ("SESSION_TLS_KEY=/run/h-mesh/tls/session.key" in text) == (mode == "session")
    assert "H_MESH_CA_CERT=/run/h-mesh/tls/ca.crt" in text


def test_tls_failed_rotation_keeps_existing_working_material(tls_bootstrap, tmp_path):
    run, env_file, _, _, _, _ = tls_bootstrap
    assert run().returncode == 0
    before = {p.name: p.read_bytes() for p in (env_file.parent / "tls").iterdir()}
    result = run({"API_TLS_KEY": str(tmp_path / "missing")})
    assert result.returncode != 0
    assert before == {p.name: p.read_bytes() for p in (env_file.parent / "tls").iterdir()}


def test_tls_staged_secrets_are_excluded_from_image_context():
    assert "offices/" in (BOOTSTRAP.parents[1] / ".dockerignore").read_text().splitlines()
