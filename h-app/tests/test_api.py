import asyncio
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import redis

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from core.channels import send
from core.envelope import build, encode, parse
from clients.telegram.bot import TelegramBot
from core.keys import incarnation_key, prefix
from lib.chat_memory import ChatMemory
from modules.api import server as server_module
from modules.api.port import deliver_api
from modules.api.server import ApiSettings, create_app


class FakeRedis:
    def __init__(self):
        self.registry = {"api": "api", "alice": "tmux", "telegram": "api"}
        self.hashes = {}
        self.lists = {}
        self.streams = {}
        self.kv = {}

    def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value

    def hkeys(self, key):
        if key.endswith(":registry"):
            return [name.encode() for name in self.registry]
        return list(self.hashes.get(key, {}))

    def hexists(self, key, field):
        return field in self.registry if key.endswith(":registry") else field in self.hashes.get(key, {})

    def hget(self, key, field):
        if key.endswith(":registry"):
            return self.registry.get(field)
        return self.hashes.get(key, {}).get(field)

    def hgetall(self, key):
        return self.hashes.get(key, {})

    def hdel(self, key, *fields):
        count = 0
        for field in fields:
            if self.hashes.get(key, {}).pop(field, None) is not None:
                count += 1
        return count

    def get(self, key):
        return self.kv.get(key)

    def set(self, key, value, ex=None):
        self.kv[key] = value
        return True

    def llen(self, key):
        return len(self.lists.get(key, []))

    def lrange(self, key, start, end):
        values = self.lists.get(key, [])
        return values[start:] if end == -1 else values[start : end + 1]

    def lpop(self, key):
        values = self.lists.get(key, [])
        return values.pop(0) if values else None

    def rpush(self, key, *values):
        self.lists.setdefault(key, []).extend(values)
        return len(self.lists[key])

    def xadd(self, key, fields, maxlen=None, approximate=True):
        entries = self.streams.setdefault(key, [])
        entry_id = f"{len(entries) + 1}-0"
        entries.append((entry_id, fields))
        if maxlen and len(entries) > maxlen:
            del entries[:-maxlen]
        return entry_id

    def xrange(self, key, min="-", max="+", count=None):
        return self.streams.get(key, [])[:count]

    def eval(self, script, numkeys, *args):
        keys = args[:numkeys]
        argv = args[numkeys:]
        if "LRANGE" in script and "DEL" in script:
            return self.lists.pop(keys[0], [])
        if "reply_correlation verify delivery" in script:
            incarnation_key, claim_key = keys
            expected = argv[0]
            current = self.get(incarnation_key)
            if current is None or current != expected:
                return None
            return self.get(claim_key)
        return 1

    def pipeline(self, transaction=False):
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.calls = []

    def lrange(self, key, start, end):
        self.calls.append((key, start, end))
        return self

    def execute(self):
        return [self.redis.lrange(*call) for call in self.calls]


def request(app, method, path, *, token=None, body=None):
    sent = []
    received = False
    encoded = json.dumps(body).encode() if body is not None else b""
    headers = [(b"content-type", b"application/json")]
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": encoded, "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 8080),
        "root_path": "",
    }
    asyncio.run(app(scope, receive, send))
    start = next(item for item in sent if item["type"] == "http.response.start")
    raw_body = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
    return start["status"], json.loads(raw_body)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        self.app = create_app(
            settings=ApiSettings(pod="test", tenant="office", api_token="secret"),
            redis_client=self.redis,
        )

    def test_auth_and_health(self):
        self.assertEqual(request(self.app, "GET", "/health")[0], 401)
        self.assertEqual(request(self.app, "GET", "/health", token="secret"), (200, {"status": "ok"}))

    def test_external_route_contract(self):
        routes = {(method, route.path) for route in self.app.routes for method in getattr(route, "methods", set())}
        expected = {
            ("GET", "/health"),
            ("GET", "/agents"),
            ("GET", "/agents/{agent}"),
            ("POST", "/agents/{agent}/envelopes"),
            ("GET", "/agents/{agent}/messages"),
            ("GET", "/agents/{agent}/messages/stream"),
            ("GET", "/agents/{agent}/activity"),
            ("GET", "/agents/{agent}/activity/stream"),
            ("GET", "/agents/{agent}/board"),
            ("GET", "/agents/{agent}/contexts"),
            ("GET", "/agents/{agent}/live"),
            ("GET", "/agents/{agent}/live/events"),
            ("GET", "/agents/{agent}/live/stream"),
            ("GET", "/board"),
            ("GET", "/alerts"),
            ("GET", "/alerts/stream"),
            ("GET", "/restdoc"),
            ("GET", "/docs"),
            ("GET", "/redoc"),
            ("GET", "/openapi.json"),
        }
        self.assertTrue(expected.issubset(routes))

    def test_unverified_delivery_does_not_override_active_presence(self):
        """A stale observation must not make Telegram refuse the fresh probe
        that could resolve it; actual inability belongs to that send result."""
        presence = prefix("test", "office", "alice", "presence")
        blocked = prefix("test", "office", "alice", "blocked")
        self.redis.hashes[presence] = {
            "state": "idle",
            "since": "2026-09-02T10:00:00Z",
            "last_activity": "2026-09-02T10:00:08Z",
        }
        self.redis.hashes[blocked] = {
            "since": "2026-09-02T06:00:00Z",
            "stream_id": "a" * 32,
        }

        class ApiBackedMesh:
            def get_presence(inner_self, agent):
                return request(self.app, "GET", f"/agents/{agent}", token="secret")

            def send_message(inner_self, destination, text):
                return request(
                    self.app, "POST", f"/agents/{destination}/envelopes",
                    token="secret", body={"text": text, "as": "telegram"},
                )

        class TelegramSink:
            def __init__(inner_self):
                inner_self.messages = []

            def send_chat_action(inner_self, chat_id):
                return None

            def send_message(inner_self, chat_id, text, **kwargs):
                inner_self.messages.append(text)

        telegram = TelegramSink()
        bot = TelegramBot(
            ApiBackedMesh(), telegram, target_agent="alice", no_activity_push=True,
        )

        reply = bot.handle_user_prompt("operator", "new evidence")

        self.assertEqual(
            reply,
            "✅ Sent to alice. A prior delivery remains unverified; this send is fresh evidence.",
        )
        self.assertEqual(len(self.redis.lists[prefix("test", "office", "telegram", "egress")]), 1)
        status, body = request(self.app, "GET", "/agents/alice", token="secret")
        self.assertEqual(status, 200)
        self.assertEqual(body["presence"]["state"], "idle")
        self.assertEqual(body["delivery_unverified"], {
            "since": "2026-09-02T06:00:00Z",
            "stream_id": "a" * 32,
        })

    def test_absent_presence_stays_unknown_with_unverified_delivery(self):
        """Delivery uncertainty cannot be promoted into either availability or blockage."""
        blocked = prefix("test", "office", "alice", "blocked")
        self.redis.hashes[blocked] = {"since": "", "stream_id": ""}

        status, body = request(self.app, "GET", "/agents/alice", token="secret")

        self.assertEqual(status, 200)
        self.assertEqual(body["presence"]["state"], "unknown")
        self.assertEqual(body["delivery_unverified"], {"since": "", "stream_id": ""})

    def test_qualified_destination_routes_through_core_channel(self):
        status, body = request(
            self.app,
            "POST",
            "/agents/test:office:alice/envelopes",
            token="secret",
            body={"text": "hello"},
        )
        self.assertEqual(status, 202)
        self.assertIn("stream_id", body)
        queued = parse(self.redis.lists[prefix("test", "office", "api", "egress")][0])
        self.assertEqual(queued["l2"]["destination"], "alice")
        self.assertEqual(queued["l3"]["destination"], "test:office:alice")

    def test_post_envelope_does_not_report_a_write_failure_as_a_rejection(self):
        """CLASS 2, architect's provenance audit (ticket 51caad5f): the
        only except clause converting a post_envelope failure into an
        explicit HTTP 422 rejection catches EnvelopeError specifically --
        and core.channels.send()'s own contract (its comment above the
        rpush call: "Only RPUSH belongs inside the outcome-unknown
        window") proves EnvelopeError is raised only by validation that
        completes BEFORE the egress write. A failure from the write step
        itself must never be classified as a proven rejection: the caller
        cannot tell from a 422 whether their message was actually queued,
        so reporting a write failure that way would be a confident, wrong
        claim -- the exact harm this ticket's Class 2 predicate names.
        Simulates the write step itself failing with a plain exception
        (never EnvelopeError -- send() cannot raise that type from within
        the rpush try) and confirms it does NOT come back as this
        endpoint's 422 rejection status."""
        def failing_rpush(*args, **kwargs):
            raise ConnectionError("redis unreachable")

        with patch.object(self.redis, "rpush", side_effect=failing_rpush):
            with self.assertRaises(ConnectionError):
                request(
                    self.app, "POST", "/agents/test:office:alice/envelopes",
                    token="secret", body={"text": "hello"},
                )

    def test_nonlocal_and_malformed_destination_statuses(self):
        status, _ = request(
            self.app, "POST", "/agents/other:office:alice/envelopes",
            token="secret", body={"text": "hello"},
        )
        self.assertEqual(status, 422)
        status, _ = request(
            self.app, "POST", "/agents/test:office:alice:extra/envelopes",
            token="secret", body={"text": "hello"},
        )
        self.assertEqual(status, 404)

    def test_api_port_writes_mailbox_and_dead_letters_corrupt_input(self):
        ingress = prefix("test", "office", "telegram", "ingress")
        valid = encode(build("Message", "alice", "telegram", {"text": "reply"}, pod="test", tenant="office"))
        self.redis.lists[ingress] = [valid, "not an envelope"]

        deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")

        inbox = prefix("test", "office", "telegram", "inbox")
        dead = prefix("test", "office", "telegram", "dead")
        self.assertEqual(len(self.redis.streams[inbox]), 1)
        self.assertEqual(json.loads(self.redis.streams[inbox][0][1]["envelope"])["payload"], {"text": "reply"})
        self.assertEqual(self.redis.lists[dead], ["not an envelope"])

    def test_deliver_api_does_not_dead_letter_an_envelope_after_a_failed_inbox_write(self):
        """CLASS 2, architect's provenance audit (ticket 51caad5f), second
        candidate site: deliver_api's only except clause that classifies a
        rejection wraps parse(raw) alone, never the r.xadd inbox write that
        follows it -- parse() takes no Redis handle at all, so it cannot
        have touched storage, which is a stronger guarantee than an
        in-function comment (core.channels.send()'s case) because it holds
        structurally, by parse()'s own signature. The fragile part is the
        SHAPE of the try/except, not parse()'s purity: if a later change
        widened that except to also cover the xadd call, a write failure
        would be misclassified as a proven rejection (dead-lettered) when
        the caller cannot actually tell whether inbox storage received it.
        Simulates the write step failing and confirms it propagates
        uncaught -- never silently classified into the dead-letter queue."""
        ingress = prefix("test", "office", "telegram", "ingress")
        valid = encode(build("Message", "alice", "telegram", {"text": "reply"}, pod="test", tenant="office"))
        self.redis.lists[ingress] = [valid]

        def failing_xadd(*args, **kwargs):
            raise ConnectionError("redis unreachable")

        with patch.object(self.redis, "xadd", side_effect=failing_xadd):
            with self.assertRaises(ConnectionError):
                deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")

        dead = prefix("test", "office", "telegram", "dead")
        self.assertEqual(self.redis.lists.get(dead, []), [])

    def test_deliver_api_never_logs_the_raw_content_of_a_rejected_envelope(self):
        """CLASS 1, architect's provenance audit (ticket 51caad5f):
        parse()'s EnvelopeError messages are constructed in core/envelope.py
        from whatever the wire said -- _address/_segment interpolate the
        remote value itself (`{value!r}`) into several of them -- so
        str(exc) is remote-influenced by construction, exactly like the
        telegram client and watchdog leaks this ticket cites. Craft a wire
        frame with a VALID L2 header (parse_for_switch succeeds) but an L3
        source that fails segment validation, carrying a marker that must
        never reach the durable custody log deliver_api writes."""
        valid = encode(build("Message", "alice", "telegram", {"text": "hi"}, pod="test", tenant="office"))
        header, body = valid[:256], valid[256:]
        body_dict = json.loads(body)
        marker = "UNTRUSTED_REMOTE_MARKER_should_never_reach_logs"
        body_dict["l3"]["source"] = f"test:office:{marker}"
        tampered = header + json.dumps(body_dict, separators=(",", ":"))
        ingress = prefix("test", "office", "telegram", "ingress")
        self.redis.lists[ingress] = [tampered]

        out = io.StringIO()
        with redirect_stdout(out):
            deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")

        self.assertNotIn(marker, out.getvalue())
        dead = prefix("test", "office", "telegram", "dead")
        self.assertEqual(self.redis.lists[dead], [tampered])

    def test_deliver_api_never_logs_the_in_reply_to_value_or_reply_source_it_drops(self):
        """Reviewer's exact finding against 301ae87 (ticket 51caad5f):
        is_valid_reply_id restricts SHAPE (32 lowercase hex characters), not
        provenance -- a remote sender chooses the bytes freely within that
        shape, so a syntactically valid in_reply_to is still remote data by
        origin, same predicate as a malformed one. The prior fix closed
        EnvelopeError's str(exc) but left _drop_untrustworthy_reply_correlation
        interpolating in_reply_to, reply_source and agent directly into the
        free-text `reason` -- redundant with the dedicated source/destination
        fields _record already populates, and a second instance of the same
        leak class. Covers both branches that interpolated a value: verdict
        False ("was never delivered") and verdict None ("provenance
        unavailable")."""
        marker = "deadbeefdeadbeefdeadbeefdeadbeef"
        ingress = prefix("test", "office", "telegram", "ingress")
        envelope = build(
            "Message", "alice", "telegram", {"text": "hi"},
            pod="test", tenant="office", in_reply_to=marker,
        )
        self.redis.lists[ingress] = [encode(envelope)]

        out = io.StringIO()
        with redirect_stdout(out):
            deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")
        self.assertNotIn(marker, out.getvalue())

        self.redis.lists[ingress] = [encode(envelope)]

        def broken_get(key):
            raise ConnectionError("redis unavailable")

        out2 = io.StringIO()
        with patch.object(self.redis, "get", side_effect=broken_get), redirect_stdout(out2):
            deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")
        self.assertNotIn(marker, out2.getvalue())

    def test_known_reply_correlation_and_dead_letter_branches_use_closed_literal_reasons(self):
        """EXAMPLE-LEVEL coverage, not a module-wide guarantee -- reviewer's
        exact correction, still the accurate description after the source
        checker's retreat (ticket 0e6cdc0f), and now reflected in this
        test's own name too (reviewer's second, scoped correction on
        d4431bd: the old name's "always" claimed the module-wide guarantee
        this retreat withdrew, even after the docstring was fixed -- a
        node ID is what the manifest and a reviewer see first). This drives
        every branch that exists TODAY (the ones enumerated below) with
        adversarial values and confirms none of them currently leak. It has
        no mechanism to discover a NEW caller added elsewhere in the file,
        so it cannot and does not prove "any third site would fail". A
        prior AST-based source checker attempted that stronger, module-wide
        claim and was retired after five review rounds each found a real
        way past it, the last being a `global` declaration moving binding
        ownership without changing nesting depth -- a genuinely new
        category its own documented stopping condition correctly refused
        to patch a sixth time. No automated module-wide guarantee remains
        in this file -- a future or new call site requires manual review.
        This test only pins the branches enumerated here today -- and the
        name's own claim was re-checked against the body for exactly this
        risk: `closed_reasons` originally listed "in_reply_to present but
        reply has no l2 source" without ever producing it, since parse()
        guarantees a non-empty l2.source for anything that reaches
        deliver_api at all -- that branch is dead code on the wire path,
        reachable only by calling _drop_untrustworthy_reply_correlation
        directly. Added that direct call so every literal this test
        allows is also a literal this test actually observed at least
        once, not merely a member of a superset nothing produces."""
        closed_reasons = {
            None,
            "malformed in_reply_to",
            "in_reply_to present but reply has no l2 source",
            "in_reply_to provenance unavailable (storage unreachable)",
            "in_reply_to was never delivered to the claimed source",
            "malformed envelope",
        }

        def reasons_from(out: str) -> set:
            return {json.loads(line).get("reason") for line in out.splitlines()}

        ingress = prefix("test", "office", "telegram", "ingress")
        adversarial_markers = [
            "deadbeefdeadbeefdeadbeefdeadbeef",
            "cafebabecafebabecafebabecafebabe",
            "0" * 32,
            "not-a-valid-hex-id-at-all-nope!!",
            "SECRET_LEAK_ATTEMPT_UPPER_CASE_XX",
        ]

        # Malformed and never-delivered branches: build(in_reply_to=...)
        # rejects anything not already 32 lowercase hex, so tamper the
        # wire form directly to reach deliver_api with whatever the
        # marker actually is, valid-shaped or not.
        for marker in adversarial_markers:
            envelope = build("Message", "alice", "telegram", {"text": "hi"}, pod="test", tenant="office")
            raw = self._tamper_in_reply_to(envelope, marker)
            self.redis.lists[ingress] = [raw]
            out = io.StringIO()
            with redirect_stdout(out):
                deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")
            self.assertNotIn(marker, out.getvalue())
            self.assertLessEqual(reasons_from(out.getvalue()), closed_reasons)

        # Provenance-unavailable branch: valid-shaped id, storage unreachable.
        for marker in ("deadbeefdeadbeefdeadbeefdeadbeef", "cafebabecafebabecafebabecafebabe"):
            envelope = build(
                "Message", "alice", "telegram", {"text": "hi"},
                pod="test", tenant="office", in_reply_to=marker,
            )
            self.redis.lists[ingress] = [encode(envelope)]

            def broken_get(key):
                raise ConnectionError("redis unavailable")

            out = io.StringIO()
            with patch.object(self.redis, "get", side_effect=broken_get), redirect_stdout(out):
                deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")
            self.assertNotIn(marker, out.getvalue())
            self.assertLessEqual(reasons_from(out.getvalue()), closed_reasons)

        # Dead-letter path: malformed frames hitting different EnvelopeError
        # raise sites in core/envelope.py -- a bad L2 header name, a bad L3
        # body address, and non-JSON body -- must all reduce to the single
        # "malformed envelope" literal, regardless of what remote text
        # triggered the rejection or which field carried it.
        valid = encode(build("Message", "alice", "telegram", {"text": "hi"}, pod="test", tenant="office"))
        header = valid[:256]
        marker = "LEAK_MARKER_FOR_DEAD_LETTER_PATH"
        malformed_raws = [
            "short",
            header + "not json",
            valid[:65] + marker.ljust(63) + valid[128:256] + valid[256:],
            header + json.dumps({
                "kind": "Message", "ts": "x",
                "l3": {"source": f"test:office:{marker}", "destination": "test:office:telegram"},
                "payload": {},
            }, separators=(",", ":")),
        ]
        for raw in malformed_raws:
            self.redis.lists[ingress] = [raw]
            out = io.StringIO()
            with redirect_stdout(out):
                deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")
            self.assertNotIn(marker, out.getvalue())
            self.assertLessEqual(reasons_from(out.getvalue()), closed_reasons)

        # "no l2 source" branch: unreachable through the wire/parse() path
        # above -- parse_for_switch's _segment validates L2 source as a
        # non-empty identifier for every envelope that reaches deliver_api
        # at all, so an empty l2.source can never survive a real parse().
        # Called directly, bypassing parse(), the way a differently-shaped
        # future caller of this function might; closed_reasons above would
        # otherwise list a literal this test never actually produces.
        from modules.api.port import _drop_untrustworthy_reply_correlation

        out = io.StringIO()
        with redirect_stdout(out):
            _drop_untrustworthy_reply_correlation(
                self.redis, pod="test", tenant="office", agent="telegram",
                envelope={
                    "stream_id": "a" * 32, "correlation_id": "b" * 32,
                    "l2": {"source": ""}, "in_reply_to": "c" * 32,
                },
            )
        self.assertLessEqual(reasons_from(out.getvalue()), closed_reasons)
        self.assertIn(
            "in_reply_to present but reply has no l2 source",
            reasons_from(out.getvalue()),
        )

    def _tamper_in_reply_to(self, envelope, value):
        """Bypass build()/encode()'s strict validation to simulate an
        already-parsed, permissive frame carrying whatever the wire said --
        the shape deliver_api actually has to defend against."""
        raw = encode(envelope)
        header, body = raw[:256], raw[256:]
        body_dict = json.loads(body)
        body_dict["in_reply_to"] = value
        return header + json.dumps(body_dict, separators=(",", ":"))

    def test_deliver_api_keeps_in_reply_to_when_delivered_by_the_claimed_client(self):
        from lib.reply_correlation import record_delivered

        target = "a" * 32
        self.redis.set(incarnation_key("test", "office", "alice"), "test-incarnation")
        # alice was really sent `target` by telegram, and is now replying
        # to telegram -- the claimed source matches deliver_api's own agent.
        record_delivered(self.redis, pod="test", tenant="office", agent="alice", stream_id=target, source="telegram")
        ingress = prefix("test", "office", "telegram", "ingress")
        envelope = build(
            "Message", "alice", "telegram", {"text": "reply"},
            pod="test", tenant="office", in_reply_to=target,
        )
        self.redis.lists[ingress] = [encode(envelope)]

        deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")

        inbox = prefix("test", "office", "telegram", "inbox")
        stored = json.loads(self.redis.streams[inbox][0][1]["envelope"])
        self.assertEqual(stored["in_reply_to"], target)

    def test_deliver_api_drops_in_reply_to_that_was_never_delivered(self):
        # Well-formed, but this agent never received it -- the confident-lie
        # case, not the format-error case. Must be dropped just like a
        # malformed id, and must not be surfaced to the client at all.
        target = "b" * 32
        ingress = prefix("test", "office", "telegram", "ingress")
        envelope = build(
            "Message", "alice", "telegram", {"text": "reply"},
            pod="test", tenant="office", in_reply_to=target,
        )
        self.redis.lists[ingress] = [encode(envelope)]

        deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")

        inbox = prefix("test", "office", "telegram", "inbox")
        stored = json.loads(self.redis.streams[inbox][0][1]["envelope"])
        self.assertNotIn("in_reply_to", stored)

    def test_deliver_api_drops_in_reply_to_delivered_by_a_different_client(self):
        # The cross-client case: telegram really sent `target` to alice.
        # alice now replies to webconsole (a different API client) naming
        # it. was_delivered must check WHO delivered it, not just whether
        # it was delivered to alice from anywhere -- otherwise webconsole
        # would receive a confident correlation to a turn it never
        # originated.
        from lib.reply_correlation import record_delivered

        target = "c" * 32
        self.redis.set(incarnation_key("test", "office", "alice"), "test-incarnation")
        record_delivered(self.redis, pod="test", tenant="office", agent="alice", stream_id=target, source="telegram")
        ingress = prefix("test", "office", "webconsole", "ingress")
        envelope = build(
            "Message", "alice", "webconsole", {"text": "reply"},
            pod="test", tenant="office", in_reply_to=target,
        )
        self.redis.lists[ingress] = [encode(envelope)]

        deliver_api(r=self.redis, pod="test", tenant="office", agent="webconsole")

        inbox = prefix("test", "office", "webconsole", "inbox")
        stored = json.loads(self.redis.streams[inbox][0][1]["envelope"])
        self.assertNotIn("in_reply_to", stored)

    def test_deliver_api_drops_a_peer_tmux_originated_id_toward_any_api_client(self):
        # The second direction of the cross-client fix, distinct from the
        # test above: `target` was never sent by ANY api client -- alice
        # sent it to bob, tmux-to-tmux, entirely off the api door's radar.
        # bob must not be able to launder that peer message into a
        # validated correlation by naming it in a reply to telegram (or any
        # other api client). This is not "wrong client", it's "no client at
        # all originated it" -- a distinct case from the one above, and the
        # one this fix is most likely to have missed if the binding were
        # only checked against a specific known-wrong client rather than
        # against the true recorded source in general.
        from lib.reply_correlation import record_delivered

        target = "e" * 32
        self.redis.set(incarnation_key("test", "office", "bob"), "test-incarnation")
        record_delivered(self.redis, pod="test", tenant="office", agent="bob", stream_id=target, source="alice")
        ingress = prefix("test", "office", "telegram", "ingress")
        envelope = build(
            "Message", "bob", "telegram", {"text": "reply"},
            pod="test", tenant="office", in_reply_to=target,
        )
        self.redis.lists[ingress] = [encode(envelope)]

        deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")

        inbox = prefix("test", "office", "telegram", "inbox")
        stored = json.loads(self.redis.streams[inbox][0][1]["envelope"])
        self.assertNotIn("in_reply_to", stored)

    def test_real_tmux_delivery_then_deliver_api_rejects_peer_originated_correlation(self):
        # Same case as above, but through the actual delivery path rather
        # than calling record_delivered directly -- removes any doubt that
        # this only holds because the unit test constructed the provenance
        # record by hand rather than the way message_opener really would.
        from unittest.mock import MagicMock
        from modules.tmux.port import message_opener

        self.redis.set(incarnation_key("test", "office", "bob"), "test-incarnation")
        target = send(
            self.redis, pod="test", tenant="office", source="alice",
            destination="bob", payload={"text": "peer message"},
        )
        raw = self.redis.lpop(prefix("test", "office", "alice", "egress"))
        envelope = parse(raw)
        with patch("modules.tmux.port.list_windows", return_value={"bob"}), \
             patch("modules.tmux.port.submit_text"):
            message_opener(self.redis, "test", "office", "bob", envelope, "sess", socket=None)

        reply_ingress = prefix("test", "office", "telegram", "ingress")
        reply_envelope = build(
            "Message", "bob", "telegram", {"text": "claiming the peer message"},
            pod="test", tenant="office", in_reply_to=target,
        )
        self.redis.lists[reply_ingress] = [encode(reply_envelope)]

        deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")

        inbox = prefix("test", "office", "telegram", "inbox")
        stored = json.loads(self.redis.streams[inbox][0][1]["envelope"])
        self.assertNotIn("in_reply_to", stored)

    def test_overlapping_prompts_answered_out_of_order_correlate_independently(self):
        # The scenario that actually motivated this feature: two prompts
        # delivered to the same agent before either is answered, answered
        # in reverse order. The harm this checks for is not "does
        # correlation exist" but "can one concurrently-delivered id's
        # provenance contaminate another's" -- the confident-lie failure
        # this whole feature exists to prevent, specifically under the
        # concurrency that motivated it, not just sequentially.
        from modules.tmux.port import message_opener

        self.redis.set(incarnation_key("test", "office", "bob"), "test-incarnation")
        target_a = send(
            self.redis, pod="test", tenant="office", source="telegram",
            destination="bob", payload={"text": "question A"},
        )
        raw_a = self.redis.lpop(prefix("test", "office", "telegram", "egress"))
        target_b = send(
            self.redis, pod="test", tenant="office", source="telegram",
            destination="bob", payload={"text": "question B"},
        )
        raw_b = self.redis.lpop(prefix("test", "office", "telegram", "egress"))

        with patch("modules.tmux.port.list_windows", return_value={"bob"}), \
             patch("modules.tmux.port.submit_text"):
            # Both delivered before either is answered.
            message_opener(self.redis, "test", "office", "bob", parse(raw_a), "sess", socket=None)
            message_opener(self.redis, "test", "office", "bob", parse(raw_b), "sess", socket=None)

        # Answered in reverse order: B first, then A.
        ingress = prefix("test", "office", "telegram", "ingress")
        reply_b = build("Message", "bob", "telegram", {"text": "answer B"}, pod="test", tenant="office", in_reply_to=target_b)
        self.redis.lists[ingress] = [encode(reply_b)]
        deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")

        reply_a = build("Message", "bob", "telegram", {"text": "answer A"}, pod="test", tenant="office", in_reply_to=target_a)
        self.redis.lists[ingress] = [encode(reply_a)]
        deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")

        inbox = prefix("test", "office", "telegram", "inbox")
        by_text = {
            json.loads(fields["envelope"])["payload"]["text"]: json.loads(fields["envelope"]).get("in_reply_to")
            for _, fields in self.redis.streams[inbox]
        }
        self.assertEqual(by_text["answer B"], target_b)
        self.assertEqual(by_text["answer A"], target_a)
        self.assertNotEqual(by_text["answer B"], target_a)
        self.assertNotEqual(by_text["answer A"], target_b)

    def test_deliver_api_drops_malformed_in_reply_to(self):
        ingress = prefix("test", "office", "telegram", "ingress")
        envelope = build("Message", "alice", "telegram", {"text": "reply"}, pod="test", tenant="office")
        self.redis.lists[ingress] = [self._tamper_in_reply_to(envelope, "not-a-valid-id")]

        deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")

        inbox = prefix("test", "office", "telegram", "inbox")
        stored = json.loads(self.redis.streams[inbox][0][1]["envelope"])
        self.assertNotIn("in_reply_to", stored)

    def test_deliver_api_drops_present_null_in_reply_to(self):
        # A key PRESENT with value null is not the same as absent -- must
        # be caught and dropped, not passed through as a stored null.
        ingress = prefix("test", "office", "telegram", "ingress")
        envelope = build("Message", "alice", "telegram", {"text": "reply"}, pod="test", tenant="office")
        self.redis.lists[ingress] = [self._tamper_in_reply_to(envelope, None)]

        deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")

        inbox = prefix("test", "office", "telegram", "inbox")
        stored = json.loads(self.redis.streams[inbox][0][1]["envelope"])
        self.assertNotIn("in_reply_to", stored)

    def test_deliver_api_drops_present_empty_string_in_reply_to(self):
        ingress = prefix("test", "office", "telegram", "ingress")
        envelope = build("Message", "alice", "telegram", {"text": "reply"}, pod="test", tenant="office")
        self.redis.lists[ingress] = [self._tamper_in_reply_to(envelope, "")]

        deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")

        inbox = prefix("test", "office", "telegram", "inbox")
        stored = json.loads(self.redis.streams[inbox][0][1]["envelope"])
        self.assertNotIn("in_reply_to", stored)

    def test_deliver_api_leaves_absent_in_reply_to_untouched(self):
        ingress = prefix("test", "office", "telegram", "ingress")
        envelope = build("Message", "alice", "telegram", {"text": "reply"}, pod="test", tenant="office")
        self.redis.lists[ingress] = [encode(envelope)]

        deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")

        inbox = prefix("test", "office", "telegram", "inbox")
        stored = json.loads(self.redis.streams[inbox][0][1]["envelope"])
        self.assertNotIn("in_reply_to", stored)

    def test_deliver_api_drops_and_logs_distinct_reason_when_provenance_unavailable(self):
        # A storage outage must not be recorded as "was never delivered" --
        # that would be a false claim about what actually happened.
        target = "d" * 32
        ingress = prefix("test", "office", "telegram", "ingress")
        envelope = build(
            "Message", "alice", "telegram", {"text": "reply"},
            pod="test", tenant="office", in_reply_to=target,
        )
        self.redis.lists[ingress] = [encode(envelope)]

        def broken_get(key):
            raise ConnectionError("redis unavailable")

        captured = io.StringIO()
        with patch.object(self.redis, "get", side_effect=broken_get), \
             redirect_stdout(captured):
            deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")

        inbox = prefix("test", "office", "telegram", "inbox")
        stored = json.loads(self.redis.streams[inbox][0][1]["envelope"])
        self.assertNotIn("in_reply_to", stored)
        dropped_lines = [
            line for line in captured.getvalue().splitlines()
            if json.loads(line).get("event") == "reply_correlation_dropped"
        ]
        self.assertEqual(len(dropped_lines), 1)
        self.assertIn("provenance unavailable", dropped_lines[0])
        self.assertNotIn("was never delivered", dropped_lines[0])

    def test_idle_sse_stream_emits_keepalive_without_new_entries(self):
        """No existing test opened an SSE stream and left it idle -- every
        prior test either never connects to /alerts/stream or /agents/{a}/
        activity/stream at all (test_external_route_contract just checks the
        route is registered), or the client-side bot.py tests feed entries
        into a mocked stream_fn immediately. So the silent-idle path -- an
        open connection with nothing ever written to the underlying Redis
        stream -- was never exercised by anything. This drives the real
        ASGI app with a receive() that never signals disconnect, shrinks the
        keepalive interval so the test doesn't wait multiple real seconds,
        and asserts a comment line actually reaches the wire while idle.
        """
        first = True

        async def receive():
            nonlocal first
            if first:
                first = False
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.sleep(3600)
            return {"type": "http.disconnect"}

        body_chunks = []

        async def send(message):
            if message["type"] == "http.response.body":
                body_chunks.append(message.get("body", b""))

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/alerts/stream",
            "raw_path": b"/alerts/stream",
            "query_string": b"",
            "headers": [(b"authorization", b"Bearer secret")],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 8080),
            "root_path": "",
        }

        async def run():
            with patch.object(server_module, "SSE_KEEPALIVE_INTERVAL_S", 0.05):
                task = asyncio.ensure_future(self.app(scope, receive, send))
                await asyncio.sleep(0.3)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        asyncio.run(run())

        combined = b"".join(body_chunks)
        self.assertIn(b": keepalive\n\n", combined)


class RealApiPortSubprocessTests(unittest.TestCase):
    """Runs `python -m modules.api.port` as a real subprocess, the way the
    switch actually invokes it. A bare unittest of deliver_api() -- or a mock
    asserting Popen was called with the right argv -- would both stay green
    even with no main()/__main__ guard at all, since neither ever imports the
    module as __main__ and lets it exit. This is the class of test that
    catches that: it fails loudly (nonzero exit, or ingress left undrained)
    if the module has nothing runnable behind `python -m`.
    """

    def setUp(self):
        self.redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
        self.r = redis.Redis.from_url(self.redis_url)
        self.r.ping()
        self.pod = "real-api-test"
        self.tenant = f"tenant-{os.urandom(4).hex()}"
        self.registry = prefix(self.pod, self.tenant, resource="registry")
        self.r.hset(self.registry, mapping={"harry": "tmux", "ivy": "api"})

    def tearDown(self):
        keys = self.r.keys(f"{self.pod}.{self.tenant}.*")
        if keys:
            self.r.delete(*keys)

    def test_real_api_module_subprocess_invocation(self):
        send(
            self.r,
            pod=self.pod,
            tenant=self.tenant,
            source="harry",
            destination="ivy",
            payload={"text": "invoked via python -m modules.api.port"},
        )
        raw = self.r.lpop(prefix(self.pod, self.tenant, "harry", "egress"))
        self.r.rpush(prefix(self.pod, self.tenant, "ivy", "ingress"), raw)

        env = dict(os.environ)
        env["PYTHONPATH"] = str(H_APP)
        env["POD"] = self.pod
        env["TENANT"] = self.tenant
        env["REDIS_URL"] = self.redis_url

        res = subprocess.run(
            [sys.executable, "-m", "modules.api.port", "ivy"],
            cwd=str(H_APP),
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"port main failed: {res.stderr}")

        self.assertIsNone(self.r.lpop(prefix(self.pod, self.tenant, "ivy", "ingress")))

        inbox_key = prefix(self.pod, self.tenant, "ivy", "inbox")
        entries = self.r.xrange(inbox_key, min="-", max="+")
        self.assertEqual(len(entries), 1)
        delivered = json.loads(entries[0][1][b"envelope"])
        self.assertEqual(delivered["payload"], {"text": "invoked via python -m modules.api.port"})

        delivering_key = prefix(self.pod, self.tenant, agent="ivy", resource="delivering")
        self.assertIsNone(self.r.get(delivering_key))


class AgentContextsRealRedisTests(unittest.TestCase):
    """GET /agents/{agent}/contexts reads real ChatMemory index keys (ZSETs
    via SCAN) -- this file's own FakeRedis/FakePipeline (used by ApiTests
    above) only implements the narrow set of commands core.channels' own
    Lua scripts need, same reason test_claude_sdk_port.py's port-level tests
    moved to real Redis."""

    def setUp(self):
        self.redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
        self.r = redis.Redis.from_url(self.redis_url)
        try:
            self.r.ping()
        except Exception:
            self.skipTest("real Redis server not available at REDIS_URL")
        self.pod = "test"
        self.tenant = f"contexts-{os.urandom(4).hex()}"
        self.registry = prefix(self.pod, self.tenant, resource="registry")
        self.r.hset(self.registry, "bob", "claude_sdk")
        self.r.hset(self.registry, "alice", "tmux")
        self.app = create_app(
            settings=ApiSettings(pod=self.pod, tenant=self.tenant, api_token="secret"),
            redis_client=self.r,
        )

    def test_unknown_agent_is_404(self):
        status, body = request(self.app, "GET", "/agents/nobody/contexts", token="secret")
        self.assertEqual(status, 404)

    def test_non_claude_sdk_agent_is_404(self):
        status, body = request(self.app, "GET", "/agents/alice/contexts", token="secret")
        self.assertEqual(status, 404)

    def test_claude_sdk_agent_with_no_contexts_yet(self):
        status, body = request(self.app, "GET", "/agents/bob/contexts", token="secret")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"agent": "bob", "contexts": []})

    def test_lists_live_contexts_for_the_agent(self):
        memory = ChatMemory(self.r, self.pod, self.tenant, "bob", ttl_seconds_max=3600)
        memory.write_turn("bgp-65001", "user", "hello", 3600)
        memory.write_turn("ospf-area0", "user", "hi", 3600)

        status, body = request(self.app, "GET", "/agents/bob/contexts", token="secret")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"agent": "bob", "contexts": ["bgp-65001", "ospf-area0"]})

    def test_requires_auth(self):
        status, _ = request(self.app, "GET", "/agents/bob/contexts")
        self.assertEqual(status, 401)


class WebuiRoutesRealRedisTests(unittest.TestCase):
    """The webui-facing routes (modules/webui/routes.py), mounted onto this
    same api app -- see modules/webui/port.py's own docstring for why there
    is no separate daemon/app to construct here. Real Redis for the same
    reason AgentContextsRealRedisTests is: XADD/XRANGE aren't in this file's
    own FakeRedis."""

    def setUp(self):
        self.redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
        self.r = redis.Redis.from_url(self.redis_url)
        try:
            self.r.ping()
        except Exception:
            self.skipTest("real Redis server not available at REDIS_URL")
        self.pod = "test"
        self.tenant = f"webuiroutes-{os.urandom(4).hex()}"
        self.registry = prefix(self.pod, self.tenant, resource="registry")
        self.r.hset(self.registry, "webui1", "webui")
        self.r.hset(self.registry, "alice", "tmux")
        self.app = create_app(
            settings=ApiSettings(pod=self.pod, tenant=self.tenant, api_token="secret"),
            redis_client=self.r,
        )

    def test_unknown_agent_live_page_is_404(self):
        status, _ = request(self.app, "GET", "/agents/nobody/live/events", token="secret")
        self.assertEqual(status, 404)

    def test_non_webui_agent_live_events_is_404(self):
        status, _ = request(self.app, "GET", "/agents/alice/live/events", token="secret")
        self.assertEqual(status, 404)

    def test_live_events_json_poll_returns_relayed_envelopes(self):
        inbox_key = prefix(self.pod, self.tenant, "webui1", "inbox")
        envelope = {"kind": "Progress", "payload": {"event": "claude_sdk_turn", "detail": "stop_reason=end_turn"}}
        self.r.xadd(inbox_key, {"envelope": json.dumps(envelope)})

        status, body = request(self.app, "GET", "/agents/webui1/live/events", token="secret")
        self.assertEqual(status, 200)
        self.assertEqual(body["agent"], "webui1")
        self.assertEqual(len(body["events"]), 1)
        self.assertEqual(body["events"][0]["kind"], "Progress")
        self.assertEqual(body["events"][0]["payload"]["event"], "claude_sdk_turn")

    def test_live_page_returns_html_for_a_webui_agent(self):
        sent = []
        received = False

        async def receive():
            nonlocal received
            if received:
                return {"type": "http.disconnect"}
            received = True
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/agents/webui1/live",
            "raw_path": b"/agents/webui1/live",
            "query_string": b"",
            "headers": [(b"authorization", b"Bearer secret")],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 8080),
            "root_path": "",
        }
        asyncio.run(self.app(scope, receive, send))
        start = next(item for item in sent if item["type"] == "http.response.start")
        raw_body = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
        self.assertEqual(start["status"], 200)
        content_type = dict(start["headers"])[b"content-type"]
        self.assertIn(b"text/html", content_type)
        self.assertIn(b"webui1", raw_body)
        # No EventSource here -- native EventSource cannot set the
        # Authorization header this same app requires, so the page must use
        # fetch() (which can) instead, never widening the auth boundary.
        self.assertIn(b"fetch(", raw_body)
        self.assertNotIn(b"new EventSource", raw_body)

    def test_live_stream_emits_keepalive_when_idle(self):
        from modules.webui import routes as webui_routes_module

        first = True

        async def receive():
            nonlocal first
            if first:
                first = False
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.sleep(3600)
            return {"type": "http.disconnect"}

        body_chunks = []

        async def send(message):
            if message["type"] == "http.response.body":
                body_chunks.append(message.get("body", b""))

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/agents/webui1/live/stream",
            "raw_path": b"/agents/webui1/live/stream",
            "query_string": b"",
            "headers": [(b"authorization", b"Bearer secret")],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 8080),
            "root_path": "",
        }

        async def run():
            with patch.object(webui_routes_module, "SSE_KEEPALIVE_INTERVAL_S", 0.05):
                task = asyncio.ensure_future(self.app(scope, receive, send))
                await asyncio.sleep(0.3)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        asyncio.run(run())

        combined = b"".join(body_chunks)
        self.assertIn(b": keepalive\n\n", combined)

    def test_requires_auth(self):
        status, _ = request(self.app, "GET", "/agents/webui1/live/events")
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
