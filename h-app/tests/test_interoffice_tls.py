"""Relay and CLI share strict CA trust; remote diagnostics never reach logs."""

import json
import logging
import ssl
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from _tls_test_material import certificate_pair
from lib.tls_context import TlsConfigError
from modules.interoffice.relay import OfficeWorker
from modules.interoffice.store import RelayStateError, parse_office
from test_interoffice_relay import TOKEN, egress, frame, message, sender_record, store


@pytest.fixture
def tls_server(tmp_path):
    cert, key = certificate_pair(tmp_path)
    requests = []
    body = frame(message())

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_port}", cert, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def office(store, url, ca=None):
    record = {"url": url, "participant": "visitor"}
    if ca is not None:
        record["ca_cert"] = str(ca)
    store.r.hset(store.offices_key, "remote", json.dumps(record))
    store.r.hset(store.tokens_key, "remote", TOKEN)
    return store.snapshot()[0]["remote"]


@pytest.mark.parametrize("selection", ["office", "global", "override", "system", "missing-office"])
def test_tls_relay_uses_shared_ca_precedence_and_real_verified_sse(store, tls_server, tmp_path, monkeypatch, selection):
    url, cert, requests = tls_server
    monkeypatch.delenv("H_MESH_CA_CERT", raising=False)
    chosen = None
    if selection in ("office", "override"):
        chosen = cert
    if selection in ("global", "missing-office"):
        monkeypatch.setenv("H_MESH_CA_CERT", str(cert))
    elif selection == "override":
        monkeypatch.setenv("H_MESH_CA_CERT", str(tmp_path / "bad-global"))
    if selection == "missing-office":
        chosen = tmp_path / "missing-office"
    remote = office(store, url, chosen)
    worker = OfficeWorker(store, remote, mapping_wait=0)
    sender_record(store)
    if selection in ("system", "missing-office"):
        with pytest.raises((urllib.error.URLError, TlsConfigError)):
            worker._opener().open(url, timeout=2)
        assert not requests and not egress(store)
    else:
        with worker._opener().open(url, timeout=2) as response:
            assert worker.receive(response)
        assert len(egress(store)) == 1
        assert store.cursor(remote) == "10-0"


def test_tls_ca_path_changes_are_cooperation_changes_but_not_new_replay_identity(store):
    first = office(store, "https://localhost:1", "/first/ca.crt")
    second = office(store, "https://localhost:1", "/second/ca.crt")
    assert first != second
    assert not store.current(first)
    assert store.ledger_keys(first) == store.ledger_keys(second)
    assert "/first/ca.crt" not in repr(first)


@pytest.mark.parametrize("value", [7, "", False, []], ids=["number", "empty", "boolean", "list"])
def test_tls_invalid_ca_record_is_rejected_without_echoing_path(value):
    with pytest.raises(RelayStateError, match="invalid office configuration"):
        parse_office("remote", json.dumps({"url": "https://localhost", "participant": "visitor", "ca_cert": value}),
                     TOKEN, allow_plaintext=False)


@pytest.mark.parametrize("category", ["config", "verification", "handshake", "connection"])
def test_tls_failures_are_secret_free_and_logged_once_per_state(store, caplog, category):
    worker = OfficeWorker(store, office(store, "https://localhost:1"))
    secret = "Authorization Bearer secret-token https://user:password@remote/body"
    errors = {
        "config": TlsConfigError(secret),
        "verification": ssl.SSLCertVerificationError(1, secret),
        "handshake": ssl.SSLError(1, secret),
        "connection": urllib.error.URLError(secret),
    }
    with caplog.at_level(logging.WARNING):
        worker._connection_failure(errors[category])
        worker._connection_failure(errors[category])
        assert len(caplog.records) == 1
        worker._connection_failure(TlsConfigError(secret))
        assert len(caplog.records) == (1 if category == "config" else 2)
    assert secret not in caplog.text
    assert TOKEN not in caplog.text
    assert "reason=" in caplog.text


def test_tls_http_office_does_not_load_an_irrelevant_ca(store, monkeypatch):
    import modules.interoffice.relay as relay
    worker = OfficeWorker(store, office(store, "http://127.0.0.1:1", "/missing/ca"))
    monkeypatch.setattr(relay, "build_context", lambda *_: pytest.fail("HTTP loaded a CA"))
    assert worker._opener()


def test_tls_run_recovers_when_ca_file_is_repaired_without_repeated_warnings(store, tls_server, tmp_path, monkeypatch, caplog):
    url, cert, _ = tls_server
    ca = tmp_path / "repair-ca.crt"
    ca.write_text("broken private material")
    worker = OfficeWorker(store, office(store, url, ca), mapping_wait=0)
    original_opener, original_receive = worker._opener, worker.receive
    attempts = []

    def opener():
        attempts.append(1)
        if len(attempts) == 3:
            ca.write_bytes(cert.read_bytes())
        return original_opener()

    def receive(response, **kwargs):
        assert original_receive(response, **kwargs)
        worker.stop.set()
        return False

    monkeypatch.setattr(worker, "_opener", opener)
    monkeypatch.setattr(worker, "receive", receive)
    monkeypatch.setattr(worker.stop, "wait", lambda _: worker.stop.is_set())
    with caplog.at_level(logging.INFO):
        worker.run()
    assert len(attempts) == 3
    assert caplog.text.count("event=tls-error reason=invalid-ca-file") == 1
    assert caplog.text.count("event=reconnected") == 1
    assert worker.failure_state is None
    assert len(egress(store)) == 1
    assert str(ca) not in caplog.text and "broken private material" not in caplog.text


@pytest.mark.parametrize("code,reason", [(62, "hostname-mismatch"), (64, "hostname-mismatch"),
                                        (10, "expired-certificate"), (9, "certificate-not-yet-valid"),
                                        (20, "untrusted-certificate")],
                         ids=["hostname", "ip", "expired", "future", "issuer"])
def test_tls_verification_reason_is_closed_and_actionable(store, caplog, code, reason):
    error = ssl.SSLCertVerificationError(1, "untrusted secret-bearing certificate subject")
    error.verify_code = code
    worker = OfficeWorker(store, office(store, "https://localhost:1"))
    with caplog.at_level(logging.WARNING):
        worker._connection_failure(urllib.error.URLError(error))
    assert f"reason={reason}" in caplog.text
    assert "secret-bearing" not in caplog.text
