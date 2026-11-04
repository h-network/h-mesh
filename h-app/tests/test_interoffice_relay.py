import io
import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
import redis

from core.envelope import EnvelopeError, build, parse
from core.keys import incarnation_key, office_sent_key, prefix
from core.service import Switch
from lib.agentlifecycle.lifecycle import start_agent
from modules.api.port import deliver_api
from modules.office import cli as office_cli
from modules.interoffice.relay import InterofficeRelay, OfficeWorker, sse_events
from modules.interoffice.store import RelayStateError, Store, cursor_order, parse_office
from services.daemons import pid_alive, start_daemons, stop_daemons

TOKEN = "coop." + "a" * 16 + "." + "b" * 43
REPLY_ID = "c" * 32
OUTBOUND_CORRELATION = "e" * 32
REMOTE_REPLY_CORRELATION = "f" * 32


@pytest.fixture
def store():
    r = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"))
    try:
        r.ping()
    except redis.RedisError:
        pytest.skip("Redis server not available")
    pod, tenant = "relaytest-" + uuid4().hex, "office"
    store = Store(r, pod=pod, tenant=tenant, allow_plaintext=True)
    for agent in ("relay", "alice", "lead"):
        start_agent(
            r, pod=pod, tenant=tenant,
            envelope=build(
                "StartAgent", "host", "host", {"agent": agent, "port_type": "api"},
                pod=pod, tenant=tenant,
            ),
            replace_window=lambda _: pytest.fail("unexpected tmux operation"),
            available_profiles=lambda *_: ["default"],
        )
    r.set(prefix(pod, tenant, resource="lead"), "lead")
    yield store
    keys = list(r.scan_iter(match=prefix(pod, tenant) + ":*"))
    if keys:
        r.delete(*keys)
    r.close()


def add_office(store, url="http://127.0.0.1:1", token=TOKEN, participant="visitor"):
    store.r.hset(store.offices_key, "remote", json.dumps({
        "url": url, "participant": participant, "added_by": "lead", "added_ts": "now",
    }))
    store.r.hset(store.tokens_key, "remote", token)
    return store.snapshot()[0]["remote"]


def sender_record(store, **changes):
    data = {
        "sender": "alice", "office": "remote", "agent": "worker", "ts": "now",
        "sender_incarnation": store.r.get(incarnation_key(store.pod, store.tenant, "alice")).decode(),
        "correlation_id": OUTBOUND_CORRELATION,
    }
    data.update(changes)
    store.r.set(office_sent_key(store.pod, store.tenant, REPLY_ID), json.dumps(data), ex=86400)


def message(cursor="10-0", **changes):
    data = {
        "kind": "Message", "stream_id": uuid5(NAMESPACE_URL, cursor).hex, "cursor": cursor,
        "l2": {"source": "worker", "destination": "visitor"},
        "payload": {"text": "hello from the remote office"}, "in_reply_to": REPLY_ID,
        "correlation_id": REMOTE_REPLY_CORRELATION,
    }
    data.update(changes)
    return data


def frame(data, event="message", event_id=None):
    return f"id: {event_id or data['cursor']}\nevent: {event}\ndata: {json.dumps(data)}\n\n".encode()


def egress(store):
    return [parse(raw) for raw in store.r.lrange(prefix(store.pod, store.tenant, "relay", "egress"), 0, -1)]


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "condition did not become true"


@pytest.fixture
def sse_server():
    class State:
        requests = []
        responses = []
        status = 200
        location = None
        posts = []
        post_reply = {"stream_id": REPLY_ID, "correlation_id": OUTBOUND_CORRELATION}

    state = State()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            state.posts.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            body = json.dumps(state.post_reply).encode()
            self.send_response(202)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            number = len(state.requests)
            state.requests.append((self.path, self.headers.get("Authorization")))
            self.send_response(state.status)
            self.send_header("Content-Type", "text/event-stream")
            if state.location:
                self.send_header("Location", state.location)
            self.end_headers()
            if state.status == 200 and state.responses:
                try:
                    self.wfile.write(state.responses[min(number, len(state.responses) - 1)])
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}"
    yield state
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_fake_sse_reconnect_resumes_and_admits_through_the_local_switch(store, sse_server, caplog):
    office = add_office(store, sse_server.url)
    sender_record(store)
    # Intentionally replay an already committed event on connection two.
    sse_server.responses = [frame(message()), frame(message()) + frame(message("20-0"))]
    worker = OfficeWorker(store, office, mapping_wait=0)
    with caplog.at_level(logging.INFO):
        worker.thread.start()
        try:
            wait_until(lambda: store.cursor(office) == "20-0")
        finally:
            worker.stop.set()
            worker.thread.join(timeout=6)
    assert not worker.thread.is_alive()
    assert len(egress(store)) == 2
    assert sse_server.requests[0] == ("/agents/visitor/messages/stream", "Bearer " + TOKEN)
    assert parse_qs(urlsplit(sse_server.requests[1][0]).query) == {"after": ["10-0"]}
    switch = Switch(
        store.r, pod=store.pod, tenant=store.tenant,
        kick=lambda agent, *_: deliver_api(r=store.r, pod=store.pod, tenant=store.tenant, agent=agent),
    )
    assert switch.step(timeout=0.1)
    assert switch.step(timeout=0.1)
    inbox = store.r.xrange(prefix(store.pod, store.tenant, "alice", "inbox"))
    assert len(inbox) == 2
    local = json.loads(inbox[0][1][b"envelope"])
    assert local["l2"] == {"source": "relay", "destination": "alice"}
    assert local["kind"] == "Message"
    assert local["correlation_id"] == OUTBOUND_CORRELATION
    assert local["payload"]["text"] == "[from remote/worker] hello from the remote office"
    assert "in_reply_to" not in local
    assert TOKEN not in caplog.text
    assert "Authorization" not in caplog.text
    assert sse_server.url not in caplog.text
    assert "hello from the remote office" not in caplog.text


def test_early_reply_waits_for_the_post_receipt_mapping(store):
    office = add_office(store)
    writer = threading.Thread(target=lambda: (time.sleep(0.08), sender_record(store)))
    writer.start()
    OfficeWorker(store, office, mapping_wait=0.4).receive(io.BytesIO(frame(message())))
    writer.join()
    assert egress(store)[0]["l2"]["destination"] == "alice"
    assert egress(store)[0]["correlation_id"] == OUTBOUND_CORRELATION
    assert store.cursor(office) == "10-0"


def test_post_receipt_correlation_survives_sse_and_local_switch(store, sse_server, monkeypatch):
    office = add_office(store, sse_server.url)
    monkeypatch.setattr(office_cli, "_context", lambda: (store.r, store.pod, store.tenant, "alice"))
    office_cli.main(["send", "--office", "remote", "-a", "worker", "outbound question"])
    receipt = json.loads(store.r.get(office_sent_key(store.pod, store.tenant, REPLY_ID)))
    assert receipt["correlation_id"] == OUTBOUND_CORRELATION
    assert receipt["sender"] == "alice"
    assert sse_server.posts == [{"text": "outbound question", "as": "visitor"}]

    remote_reply = message()
    sse_server.responses = [frame(remote_reply)]
    worker = OfficeWorker(store, office, mapping_wait=0)
    worker.thread.start()
    try:
        wait_until(lambda: store.cursor(office) == "10-0")
    finally:
        worker.stop.set()
        worker.thread.join(timeout=6)
    assert not worker.thread.is_alive()
    switch = Switch(
        store.r, pod=store.pod, tenant=store.tenant,
        kick=lambda agent, *_: deliver_api(r=store.r, pod=store.pod, tenant=store.tenant, agent=agent),
    )
    assert switch.step(timeout=0.1)
    inbox = store.r.xrange(prefix(store.pod, store.tenant, "alice", "inbox"))
    assert len(inbox) == 1
    local = json.loads(inbox[0][1][b"envelope"])
    assert local["correlation_id"] == receipt["correlation_id"]
    assert local["correlation_id"] != REMOTE_REPLY_CORRELATION
    assert local["stream_id"] not in (REPLY_ID, remote_reply["stream_id"])
    assert local["l2"] == {"source": "relay", "destination": "alice"}
    assert local["payload"]["text"] == "[from remote/worker] hello from the remote office"
    assert "in_reply_to" not in local


@pytest.mark.parametrize("fields", [
    {}, {"correlation_id": None}, {"correlation_id": "invalid"}, {"correlation_id": ["invalid"]},
], ids=["missing", "null", "invalid", "nonstring"])
def test_legacy_or_invalid_receipt_correlation_keeps_sender_route(store, fields):
    office = add_office(store)
    sender_record(store)
    key = office_sent_key(store.pod, store.tenant, REPLY_ID)
    receipt = json.loads(store.r.get(key))
    del receipt["correlation_id"]
    receipt.update(fields)
    store.r.set(key, json.dumps(receipt), ex=86400)
    OfficeWorker(store, office, mapping_wait=0).receive(io.BytesIO(frame(message())))
    local = egress(store)[0]
    assert local["l2"]["destination"] == "alice"
    assert local["correlation_id"] not in (OUTBOUND_CORRELATION, REMOTE_REPLY_CORRELATION)
    assert store.cursor(office) == "10-0"


def test_daemon_launcher_reads_redis_credentials_without_exporting_them(store, sse_server, managed_tmpdir):
    office = add_office(store, sse_server.url)
    sender_record(store)
    sse_server.responses = [frame(message())]
    tmpdir = Path(managed_tmpdir("h_mesh_test_relay_"))
    run_dir = tmpdir / "run"
    env = {
        "PATH": os.defpath, "HOME": str(tmpdir),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "POD": store.pod, "TENANT": store.tenant,
        "REDIS_URL": os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
        "H_MESH_ALLOW_PLAINTEXT": "1",
        "TMUX_TMPDIR": str(tmpdir), "TMUX_SESSION": "relay-test",
        "TMUX_SOCKET": str(tmpdir / "isolated.sock"),
    }
    try:
        pids = start_daemons(
            python=Path(sys.executable), run_dir=run_dir, env=env,
            daemon_modules={"interoffice_relay": "services.interoffice_relay"},
        )
        pid = pids["interoffice_relay"]
        wait_until(lambda: store.cursor(office) == "10-0")
        assert pid_alive(pid)
        assert TOKEN.encode() not in Path(f"/proc/{pid}/cmdline").read_bytes()
        assert TOKEN.encode() not in Path(f"/proc/{pid}/environ").read_bytes()
        logs = (run_dir / "interoffice_relay.log").read_text()
        assert TOKEN not in logs
        assert "hello from the remote office" not in logs
        assert len(egress(store)) == 1
    finally:
        stop_daemons(run_dir, env=env)
    assert not pid_alive(pid)


@pytest.mark.parametrize("record", [
    None, {"sender_incarnation": None}, {"sender_incarnation": "0" * 32},
    {"office": "different"}, {"agent": "different"}, {"sender": "retired"},
], ids=["missing", "missing-incarnation", "stale-incarnation", "wrong-office", "wrong-agent", "retired-sender"])
def test_unmatched_or_stale_correlation_falls_back_to_the_current_lead(store, record):
    office = add_office(store)
    if record is not None:
        sender_record(store, **record)
    OfficeWorker(store, office, mapping_wait=0).receive(io.BytesIO(frame(message())))
    assert egress(store)[0]["l2"]["destination"] == "lead"
    assert egress(store)[0]["correlation_id"] not in (OUTBOUND_CORRELATION, REMOTE_REPLY_CORRELATION)


def test_unsolicited_message_without_a_lead_remains_uncheckpointed_until_retry(store):
    office = add_office(store)
    store.r.delete(prefix(store.pod, store.tenant, resource="lead"))
    data = message()
    del data["in_reply_to"]
    worker = OfficeWorker(store, office, mapping_wait=0)
    thread = threading.Thread(target=lambda: worker.receive(io.BytesIO(frame(data))))
    thread.start()
    time.sleep(0.05)
    assert store.cursor(office) is None
    assert not egress(store)
    store.r.set(prefix(store.pod, store.tenant, resource="lead"), "lead")
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert egress(store)[0]["l2"]["destination"] == "lead"
    assert store.cursor(office) == "10-0"


def test_local_policy_rejection_does_not_checkpoint(store):
    office = add_office(store)
    store.r.hset(prefix(store.pod, store.tenant, "relay", "tags"), "export", '["one"]')
    store.r.hset(prefix(store.pod, store.tenant, "lead", "tags"), "import", '["two"]')
    with pytest.raises(EnvelopeError):
        OfficeWorker(store, office, mapping_wait=0).receive(io.BytesIO(frame(message())))
    assert store.cursor(office) is None
    assert not egress(store)


def test_unenrolled_relay_does_not_create_an_unmanaged_egress_queue(store):
    office = add_office(store)
    store.r.hdel(prefix(store.pod, store.tenant, resource="registry"), "relay")
    with pytest.raises(RelayStateError, match="not enrolled"):
        OfficeWorker(store, office, mapping_wait=0).receive(io.BytesIO(frame(message())))
    assert store.cursor(office) is None
    assert not egress(store)


def test_post_admission_checkpoint_failure_replays_without_duplicate_admission(store, monkeypatch):
    office = add_office(store)
    sender_record(store)
    worker = OfficeWorker(store, office, mapping_wait=0)
    checkpoint = store.checkpoint
    monkeypatch.setattr(store, "checkpoint", lambda *_: (_ for _ in ()).throw(redis.ConnectionError()))
    with pytest.raises(redis.ConnectionError):
        worker.receive(io.BytesIO(frame(message())))
    assert len(egress(store)) == 1
    assert store.cursor(office) is None
    monkeypatch.setattr(store, "checkpoint", checkpoint)
    worker.receive(io.BytesIO(frame(message())))
    assert len(egress(store)) == 1
    assert {item["correlation_id"] for item in egress(store)} == {OUTBOUND_CORRELATION}
    assert len({item["stream_id"] for item in egress(store)}) == 1
    assert store.cursor(office) == "10-0"


@pytest.mark.parametrize("kind", ["Command", "StartAgent", "StopAgent", "Pause", "Resume", "Attachment"])
def test_remote_control_kinds_are_rejected_without_local_admission(store, kind):
    office = add_office(store)
    OfficeWorker(store, office, mapping_wait=0).receive(io.BytesIO(frame(message(kind=kind))))
    assert not egress(store)
    assert store.cursor(office) == "10-0"


@pytest.mark.parametrize("raw", [
    b'id: 10-0\nevent: error\ndata: {"error":"private"}\n\n',
    b'id: 10-0\ndata: invalid-json\n\n',
    frame(message(cursor="11-0"), event_id="10-0"),
    frame(message(), event_id="not-a-cursor"),
    frame(message(l2={"source": "worker\nforged", "destination": "visitor"})),
], ids=["stream-error", "invalid-json", "mismatched-cursor", "invalid-cursor", "invalid-source"])
def test_invalid_stream_data_cannot_advance_the_cursor(store, raw):
    office = add_office(store)
    with pytest.raises(RelayStateError):
        OfficeWorker(store, office, mapping_wait=0).receive(io.BytesIO(raw))
    assert not egress(store)
    assert store.cursor(office) is None


def test_sse_parser_handles_comments_multiline_and_incomplete_frames():
    raw = b': keepalive\n\nid: 9-1\nevent: message\ndata: {\ndata: "x":1}\n\nid: 10-0\ndata: incomplete'
    assert list(sse_events(io.BytesIO(raw))) == [("message", "9-1", '{\n"x":1}')]


def test_sse_parser_enforces_frame_bounds_and_stops_on_idle_heartbeats(monkeypatch):
    import modules.interoffice.relay as relay
    monkeypatch.setattr(relay, "MAX_FRAME_BYTES", 32)
    with pytest.raises(RelayStateError, match="limit"):
        list(sse_events(io.BytesIO(b"data: " + b"x" * 40 + b"\n\n")))
    stop = threading.Event()

    class Heartbeats:
        def readline(self, *_):
            stop.set()
            return b": keepalive\n"

    assert list(sse_events(Heartbeats(), stop)) == []


@pytest.mark.parametrize("status", [302, 401], ids=["redirect", "unauthorized"])
def test_http_errors_and_redirects_never_expose_or_forward_credentials(store, sse_server, caplog, status):
    office = add_office(store, sse_server.url)
    sse_server.status = status
    sse_server.location = "http://127.0.0.1:1/" + TOKEN
    worker = OfficeWorker(store, office)
    with caplog.at_level(logging.WARNING):
        worker.thread.start()
        try:
            wait_until(lambda: f"status={status}" in caplog.text)
        finally:
            worker.stop.set()
            worker.thread.join(timeout=6)
    assert len(sse_server.requests) == 1
    assert TOKEN not in caplog.text
    assert "http://" not in caplog.text
    assert "Authorization" not in caplog.text
    assert store.cursor(office) is None


def test_reconnect_backoff_is_capped_and_exception_content_is_never_logged(store, caplog, monkeypatch):
    import modules.interoffice.relay as relay
    office = add_office(store)

    class Stop:
        delays = []
        def is_set(self):
            return len(self.delays) >= 7
        def wait(self, delay):
            self.delays.append(delay)
            return self.is_set()

    class BrokenConnection:
        def open(self, *_args, **_kwargs):
            raise RuntimeError(TOKEN)

    stop = Stop()
    worker = OfficeWorker(store, office, stop=stop)
    worker.opener = BrokenConnection()
    monkeypatch.setattr(relay.random, "uniform", lambda *_: 0)
    with caplog.at_level(logging.WARNING):
        worker.run()
    assert stop.delays == [1, 2, 4, 8, 16, 30, 30]
    assert TOKEN not in caplog.text
    assert "disconnected" in caplog.text


def test_new_worker_resumes_committed_cursors_without_readmitting(store):
    office = add_office(store)
    OfficeWorker(store, office, mapping_wait=0).receive(io.BytesIO(frame(message())))
    OfficeWorker(store, office, mapping_wait=0).receive(io.BytesIO(frame(message())))
    assert len(egress(store)) == 1
    assert store.cursor(office) == "10-0"


@pytest.mark.parametrize("value", ["9-10", "10-0"])
def test_cursor_order_is_numeric(value):
    assert cursor_order("9-9") < cursor_order(value)


@pytest.mark.parametrize("url", ["http://remote", "https://user:secret@remote", "https://remote?token=secret"],
                         ids=["plaintext", "userinfo", "query"])
def test_secret_bearing_urls_and_plaintext_without_opt_in_are_refused(url):
    with pytest.raises(RelayStateError):
        parse_office("remote", json.dumps({"url": url, "participant": "visitor"}), TOKEN, allow_plaintext=False)


@pytest.mark.parametrize("change", ["url", "token", "participant"])
def test_supervisor_starts_on_first_record_and_resets_cursor_on_cooperation_changes(store, change):
    class Thread:
        alive = False
        def start(self):
            self.alive = True
        def is_alive(self):
            return self.alive
        def join(self, **_):
            self.alive = False

    class Worker:
        def __init__(self, _, office):
            self.office, self.stop, self.thread = office, threading.Event(), Thread()

    supervisor = InterofficeRelay(store, worker_factory=Worker)
    supervisor.reconcile()
    assert not supervisor.workers
    first = add_office(store)
    supervisor.reconcile()
    original = supervisor.workers["remote"]
    assert store.checkpoint(first, "99-0")
    changes = {change: {"url": "http://127.0.0.1:2", "token": TOKEN[:-1] + "c", "participant": "another"}[change]}
    replacement = add_office(store, **changes)
    supervisor.reconcile()
    assert original.stop.is_set()
    assert supervisor.workers["remote"].office == replacement
    assert store.cursor(replacement) is None
    assert store.checkpoint(first, "100-0") is False
    store.r.hdel(store.offices_key, "remote")
    supervisor.reconcile()
    assert not supervisor.workers
