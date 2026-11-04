"""Disposable PEM material; callers run only under the isolated VM runner."""

import subprocess


def certificate_pair(directory, name="server"):
    cert, key = directory / f"{name}.crt", directory / f"{name}.key"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
        "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
        "-keyout", str(key), "-out", str(cert),
    ], check=True, capture_output=True, timeout=15)
    key.chmod(0o600)
    return cert, key
