"""Disposable PEM material; callers run only under the isolated VM runner."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def tls_host_uid(tmp_path, monkeypatch, request):
    """Use the runner's UID without replacing filesystem staging operations.

    Generate the fixture from production sources, changing only the allowed
    non-root UID. Root still exercises production's chown-to-1000 path.
    """
    source = Path(__file__).resolve().parents[2] / "container"
    target = tmp_path / "bootstrap-fixture" / "container"
    target.mkdir(parents=True)
    for name in ("bootstrap.sh", "compose.yaml", "compose.ports.yaml", "compose.tls.yaml"):
        shutil.copyfile(source / name, target / name)
    uid = os.getuid() or 1000
    script = (source / "tls.sh").read_text()
    allowed_uid = "        0|1000) ;;"
    assert script.count(allowed_uid) == 1, "production TLS UID policy changed; review the fixture"
    (target / "tls.sh").write_text(script.replace(allowed_uid, f"        0|{uid}) ;;", 1))
    monkeypatch.setattr(request.module, "BOOTSTRAP", target / "bootstrap.sh")
    return uid


def certificate_pair(directory, name="server"):
    cert, key = directory / f"{name}.crt", directory / f"{name}.key"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
        "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
        "-keyout", str(key), "-out", str(cert),
    ], check=True, capture_output=True, timeout=15)
    key.chmod(0o600)
    return cert, key
