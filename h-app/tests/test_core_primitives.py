import sys
import unittest
from collections import defaultdict, deque
from copy import deepcopy
from pathlib import Path


H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from core.envelope import (
    DESTINATION_START,
    HEADER_WIDTH,
    HOPS_START,
    RESERVED_START,
    SOURCE_START,
    TTL_START,
    EnvelopeError,
    advance_hop,
    build,
    encode,
    header_record_fields,
    parse,
    parse_for_switch,
    resolve_destination,
    resolve_source,
    stamp_source,
)
from core.keys import prefix
from core.queues import admit_ingress


POD = "mesh"
TENANT = "office"


class QueueRedis:
    """Execute the admission script's externally visible operation atomically."""

    def __init__(self):
        self.lists = defaultdict(deque)

    def eval(self, script, key_count, *args):
        self.last_script = script
        keys = args[:key_count]
        limit, raw = int(args[key_count]), args[key_count + 1]
        for index, key in enumerate(keys, start=1):
            depth = len(self.lists[key])
            if depth >= limit:
                return [0, index, depth]
        result = [1]
        for key in keys:
            self.lists[key].append(raw)
            result.append(len(self.lists[key]))
        return result


class EnvelopeTests(unittest.TestCase):
    def frame(self, **updates):
        frame = build(
            "Message", "alice", "bob", {"text": "hello", "nested": {"ok": True}},
            "a" * 32, pod=POD, tenant=TENANT,
        )
        frame.update(updates)
        return frame

    def test_build_encode_parse_round_trip(self):
        frame = self.frame()
        raw = encode(frame)
        parsed = parse(raw)
        self.assertEqual(parsed, frame)
        self.assertEqual(len(raw[:HEADER_WIDTH]), HEADER_WIDTH)
        self.assertEqual(parsed["l3"], {"source": "mesh:office:alice", "destination": "mesh:office:bob"})

    def test_advance_hop_splices_only_counters_for_text_and_bytes(self):
        for raw_factory in (lambda value: value, lambda value: value.encode("utf-8")):
            with self.subTest(wire_type=raw_factory.__name__):
                envelope = self.frame(ttl=2, hops=7)
                raw = raw_factory(encode(envelope))
                advanced = advance_hop(raw, envelope)
                self.assertEqual(envelope["ttl"], 1)
                self.assertEqual(envelope["hops"], 8)
                self.assertEqual(advanced[:TTL_START], raw[:TTL_START])
                self.assertEqual(advanced[RESERVED_START:], raw[RESERVED_START:])
                self.assertEqual(parse_for_switch(advanced)["ttl"], 1)
                self.assertEqual(parse_for_switch(advanced)["hops"], 8)

    def test_zero_ttl_remains_zero_for_switch_to_reject(self):
        envelope = self.frame(ttl=0, hops=3)
        advanced = advance_hop(encode(envelope), envelope)
        self.assertEqual((envelope["ttl"], envelope["hops"]), (0, 4))
        self.assertEqual((parse_for_switch(advanced)["ttl"], parse_for_switch(advanced)["hops"]), (0, 4))

    def test_hop_limit_is_rejected_without_mutation(self):
        envelope = self.frame(ttl=4, hops=999)
        before = deepcopy(envelope)
        with self.assertRaisesRegex(EnvelopeError, "hops cannot exceed 999"):
            advance_hop(encode(envelope), envelope)
        self.assertEqual(envelope, before)

    def test_stamp_source_changes_only_fixed_source_segment(self):
        raw = encode(self.frame())
        stamped = stamp_source(raw, "carol")
        self.assertEqual(stamped[:SOURCE_START], raw[:SOURCE_START])
        self.assertEqual(stamped[DESTINATION_START:], raw[DESTINATION_START:])
        self.assertEqual(parse(stamped)["l2"]["source"], "carol")
        stamped_bytes = stamp_source(raw.encode("utf-8"), "carol")
        self.assertIsInstance(stamped_bytes, bytes)
        self.assertEqual(parse(stamped_bytes)["l2"]["source"], "carol")

    def test_header_record_fields_reads_valid_header_and_ignores_short_input(self):
        frame = self.frame()
        raw = encode(frame)
        self.assertEqual(
            header_record_fields(raw),
            {
                "stream_id": frame["stream_id"],
                "correlation_id": "a" * 32,
                "source": "alice",
                "destination": "bob",
            },
        )
        self.assertEqual(header_record_fields("short"), {})
        self.assertEqual(header_record_fields(b"\xff" * HEADER_WIDTH), {})

    def test_resolve_local_and_broadcast_addresses(self):
        self.assertEqual(resolve_source(pod=POD, tenant=TENANT, source="alice"), ("mesh:office:alice", "alice"))
        self.assertEqual(resolve_destination(pod=POD, tenant=TENANT, destination="bob"), ("mesh:office:bob", "bob"))
        self.assertEqual(resolve_destination(pod=POD, tenant=TENANT, destination="mesh:office:bob"), ("mesh:office:bob", "bob"))
        self.assertEqual(resolve_destination(pod=POD, tenant=TENANT, destination="all"), ("mesh:office:all", "all"))

    def test_resolve_rejects_nonlocal_and_malformed_addresses(self):
        cases = (
            (resolve_source, {"pod": POD, "tenant": TENANT, "source": "mesh:office:alice"}, "invalid source name"),
            (resolve_destination, {"pod": POD, "tenant": TENANT, "destination": "other:office:bob"}, "no route"),
            (resolve_destination, {"pod": POD, "tenant": TENANT, "destination": "mesh:bob"}, "qualified"),
            (resolve_destination, {"pod": "BAD", "tenant": TENANT, "destination": "bob"}, "invalid pod name"),
        )
        for function, kwargs, message in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(EnvelopeError, message):
                    function(**kwargs)

    def test_malformed_frames_raise_specific_envelope_errors(self):
        valid = encode(self.frame())
        cases = (
            ("", "shorter than"),
            ("3" + valid[1:], "unsupported frame version"),
            (valid[:1] + "z" * 32 + valid[33:], "stream_id must be"),
            (valid[:TTL_START] + "x01" + valid[HOPS_START:], "ttl must be"),
            (valid[:HEADER_WIDTH] + "not-json", "body is not valid JSON"),
            (b"\xff" + valid.encode("utf-8")[1:], "frame is not UTF-8"),
        )
        for raw, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(EnvelopeError, message):
                    parse(raw)

    def test_encode_rejects_invalid_counters_and_body(self):
        cases = (
            ({"ttl": -1}, "ttl must be"),
            ({"ttl": True}, "ttl must be"),
            ({"hops": 1000}, "hops must be"),
            ({"payload": []}, "payload must be an object"),
            ({"l3": {"source": "bad", "destination": "mesh:office:bob"}}, "L3 source must be"),
        )
        for updates, message in cases:
            with self.subTest(updates=updates):
                with self.assertRaisesRegex(EnvelopeError, message):
                    encode(self.frame(**updates))


class KeyTests(unittest.TestCase):
    def test_prefix_builds_tenant_agent_and_dotted_resource_boundaries(self):
        boundary = "a" + "b" * 62
        self.assertEqual(prefix(boundary, "tenant-2"), f"pod:{boundary}:tenant:tenant-2")
        self.assertEqual(
            prefix(POD, TENANT, agent="worker-2", resource="tasks.todo"),
            "pod:mesh:tenant:office:agent:worker-2:tasks.todo",
        )

    def test_prefix_rejects_reserved_names_at_every_position(self):
        for reserved in ("all", "pod", "tenant", "agent"):
            cases = (
                lambda: prefix(reserved, TENANT),
                lambda: prefix(POD, reserved),
                lambda: prefix(POD, TENANT, agent=reserved),
                lambda: prefix(POD, TENANT, resource=f"tasks.{reserved}"),
            )
            for call in cases:
                with self.subTest(reserved=reserved):
                    with self.assertRaises(KeyError):
                        call()

    def test_prefix_rejects_digit_only_invalid_and_oversized_segments(self):
        invalid = ("123", "Upper", "has_under", "has/slash", "-leading", "a" * 64, "")
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(KeyError):
                    prefix(POD, TENANT, agent=value)
                with self.assertRaises(KeyError):
                    prefix(POD, TENANT, resource=value)

    def test_prefix_rejects_empty_dotted_resource_parts(self):
        for resource in ("tasks.", ".tasks", "tasks..todo"):
            with self.subTest(resource=resource):
                with self.assertRaises(KeyError):
                    prefix(POD, TENANT, resource=resource)


class QueueTests(unittest.TestCase):
    def test_admits_when_depth_is_one_below_limit(self):
        redis = QueueRedis()
        key = prefix(POD, TENANT, "alice", "ingress")
        redis.lists[key].extend(["first", "second"])
        self.assertEqual(
            admit_ingress(
                redis, pod=POD, tenant=TENANT, destinations=["alice"],
                raw="third", limit=3,
            ),
            (True, None, None),
        )
        self.assertEqual(list(redis.lists[key]), ["first", "second", "third"])

    def test_blocks_at_limit_without_appending(self):
        redis = QueueRedis()
        key = prefix(POD, TENANT, "alice", "ingress")
        redis.lists[key].extend(["first", "second", "third"])
        self.assertEqual(
            admit_ingress(
                redis, pod=POD, tenant=TENANT, destinations=["alice"],
                raw="fourth", limit=3,
            ),
            (False, "alice", 3),
        )
        self.assertEqual(list(redis.lists[key]), ["first", "second", "third"])

    def test_broadcast_is_all_or_none_when_one_destination_is_full(self):
        redis = QueueRedis()
        alice = prefix(POD, TENANT, "alice", "ingress")
        bob = prefix(POD, TENANT, "bob", "ingress")
        redis.lists[alice].append("existing")
        before = {alice: list(redis.lists[alice]), bob: list(redis.lists[bob])}
        self.assertEqual(
            admit_ingress(
                redis, pod=POD, tenant=TENANT, destinations=["bob", "alice"],
                raw="broadcast", limit=1,
            ),
            (False, "alice", 1),
        )
        self.assertEqual(list(redis.lists[alice]), before[alice])
        self.assertEqual(list(redis.lists[bob]), before[bob])

    def test_broadcast_appends_exactly_once_to_every_destination(self):
        redis = QueueRedis()
        self.assertEqual(
            admit_ingress(
                redis, pod=POD, tenant=TENANT,
                destinations=["alice", "bob", "carol"], raw=b"frame", limit=2,
            ),
            (True, None, None),
        )
        for agent in ("alice", "bob", "carol"):
            self.assertEqual(
                list(redis.lists[prefix(POD, TENANT, agent, "ingress")]),
                [b"frame"],
            )

    def test_rejects_empty_destinations_and_nonpositive_limit_before_redis(self):
        redis = QueueRedis()
        with self.assertRaisesRegex(ValueError, "destinations must not be empty"):
            admit_ingress(redis, pod=POD, tenant=TENANT, destinations=[], raw="x", limit=1)
        with self.assertRaisesRegex(ValueError, "limit must be positive"):
            admit_ingress(redis, pod=POD, tenant=TENANT, destinations=["alice"], raw="x", limit=0)
        self.assertFalse(hasattr(redis, "last_script"))


if __name__ == "__main__":
    unittest.main()
