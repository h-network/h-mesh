import sys
import unittest
from pathlib import Path

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from lib.reply_correlation import (
    DELIVERED_MAXLEN,
    is_valid_reply_id,
    record_delivered,
    was_delivered,
)


class FakeRedis:
    def __init__(self):
        self.hashes = {}
        self.zsets = {}

    def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value

    def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    def hdel(self, key, *fields):
        h = self.hashes.get(key, {})
        for field in fields:
            h.pop(field, None)

    def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)

    def zcard(self, key):
        return len(self.zsets.get(key, {}))

    def zrange(self, key, start, end):
        members = sorted(self.zsets.get(key, {}).items(), key=lambda kv: kv[1])
        members = [m for m, _ in members]
        if end == -1:
            return members[start:]
        return members[start:end + 1]

    def zrem(self, key, *members):
        z = self.zsets.get(key, {})
        for member in members:
            z.pop(member, None)


VALID_ID = "a" * 32


class ReplyCorrelationTests(unittest.TestCase):
    def setUp(self):
        self.r = FakeRedis()

    def test_is_valid_reply_id_accepts_only_32_lowercase_hex(self):
        self.assertTrue(is_valid_reply_id(VALID_ID))
        self.assertFalse(is_valid_reply_id("A" * 32))
        self.assertFalse(is_valid_reply_id("a" * 31))
        self.assertFalse(is_valid_reply_id("a" * 33))
        self.assertFalse(is_valid_reply_id("z" * 32))
        self.assertFalse(is_valid_reply_id(None))
        self.assertFalse(is_valid_reply_id(12345))

    def test_delivered_id_is_later_found_with_matching_source(self):
        record_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram")
        self.assertTrue(
            was_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram")
        )

    def test_undelivered_id_is_not_found(self):
        self.assertFalse(
            was_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram")
        )

    def test_delivered_to_one_agent_is_not_found_for_another(self):
        record_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram")
        self.assertFalse(
            was_delivered(self.r, pod="p", tenant="t", agent="alice", stream_id=VALID_ID, source="telegram")
        )

    def test_delivered_from_one_source_does_not_validate_a_different_source(self):
        # The cross-client case: bob was really sent this id by telegram.
        # A claim that it came from webconsole must not validate, even
        # though the id really was delivered to bob.
        record_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram")
        self.assertFalse(
            was_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="webconsole")
        )

    def test_malformed_id_is_never_recorded_or_matched(self):
        record_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id="not-an-id", source="telegram")
        self.assertFalse(
            was_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id="not-an-id", source="telegram")
        )
        self.assertEqual(self.r.hashes, {})
        self.assertEqual(self.r.zsets, {})

    def test_bounded_to_maxlen_oldest_evicted_first(self):
        ids = [format(i, "032x") for i in range(DELIVERED_MAXLEN + 5)]
        for stream_id in ids:
            record_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=stream_id, source="telegram")
        self.assertFalse(was_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=ids[0], source="telegram"))
        self.assertTrue(was_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=ids[-1], source="telegram"))

    def test_redelivery_of_the_same_id_does_not_create_a_duplicate_that_mishandles_trim(self):
        # Regression for the set+list design's bug: recording the same
        # stream_id twice used to append a second entry to an order list
        # while the id-set stayed single-valued, so trimming the first
        # (now-stale) list entry could SREM the id out of the set even
        # though a "newer" record of it logically still existed. With the
        # hash+zset design, re-recording the same member is idempotent in
        # both structures -- it must never cost the id its place.
        record_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram")
        record_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram")
        others = [format(i, "032x") for i in range(1, DELIVERED_MAXLEN)]
        for stream_id in others:
            record_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=stream_id, source="telegram")
        # Total distinct ids recorded: VALID_ID + (DELIVERED_MAXLEN - 1) others = DELIVERED_MAXLEN exactly.
        self.assertTrue(
            was_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram")
        )

    def test_record_delivered_swallows_redis_errors_and_logs(self):
        # Same policy as mark_delivery_pending and _record elsewhere: a
        # bookkeeping write failure must never propagate and fail the
        # delivery it's recording. Callers (tmux's message_opener,
        # openshell's _reply) call this inline in the delivery path itself.
        class BrokenRedis(FakeRedis):
            def hset(self, key, field, value):
                raise ConnectionError("redis unavailable")

        broken = BrokenRedis()
        record_delivered(broken, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram")  # must not raise
        self.assertFalse(
            was_delivered(broken, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram")
        )

    def test_was_delivered_returns_none_not_false_on_redis_error(self):
        # None ("could not verify") and False ("verified absent") are both
        # "do not trust this" to a caller, but they are not the same fact --
        # the caller must be able to tell an infrastructure outage from a
        # genuine negative so it doesn't log a false claim about what
        # happened.
        class BrokenRedis(FakeRedis):
            def hget(self, key, field):
                raise ConnectionError("redis unavailable")

        broken = BrokenRedis()
        result = was_delivered(broken, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
