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
        self.sets = {}
        self.lists = {}

    def sadd(self, key, *values):
        self.sets.setdefault(key, set()).update(values)

    def srem(self, key, *values):
        for value in values:
            self.sets.get(key, set()).discard(value)

    def sismember(self, key, value):
        return value in self.sets.get(key, set())

    def rpush(self, key, *values):
        self.lists.setdefault(key, []).extend(values)
        return len(self.lists[key])

    def lpop(self, key):
        values = self.lists.get(key, [])
        return values.pop(0) if values else None

    def llen(self, key):
        return len(self.lists.get(key, []))


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

    def test_delivered_id_is_later_found(self):
        record_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=VALID_ID)
        self.assertTrue(was_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=VALID_ID))

    def test_undelivered_id_is_not_found(self):
        self.assertFalse(was_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=VALID_ID))

    def test_delivered_to_one_agent_is_not_found_for_another(self):
        record_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=VALID_ID)
        self.assertFalse(was_delivered(self.r, pod="p", tenant="t", agent="alice", stream_id=VALID_ID))

    def test_malformed_id_is_never_recorded_or_matched(self):
        record_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id="not-an-id")
        self.assertFalse(was_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id="not-an-id"))
        self.assertEqual(self.r.lists, {})
        self.assertEqual(self.r.sets, {})

    def test_bounded_to_maxlen_oldest_evicted_first(self):
        ids = [format(i, "032x") for i in range(DELIVERED_MAXLEN + 5)]
        for stream_id in ids:
            record_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=stream_id)
        self.assertFalse(was_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=ids[0]))
        self.assertTrue(was_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=ids[-1]))

    def test_record_delivered_swallows_redis_errors(self):
        # Same policy as mark_delivery_pending and _record elsewhere: a
        # bookkeeping write failure must never propagate and fail the
        # delivery it's recording. Callers (tmux's message_opener,
        # openshell's _reply) call this inline in the delivery path itself.
        class BrokenRedis(FakeRedis):
            def sadd(self, key, *values):
                raise ConnectionError("redis unavailable")

        broken = BrokenRedis()
        record_delivered(broken, pod="p", tenant="t", agent="bob", stream_id=VALID_ID)  # must not raise
        self.assertFalse(was_delivered(broken, pod="p", tenant="t", agent="bob", stream_id=VALID_ID))

    def test_was_delivered_fails_toward_false_on_redis_error(self):
        class BrokenRedis(FakeRedis):
            def sismember(self, key, value):
                raise ConnectionError("redis unavailable")

        broken = BrokenRedis()
        # Must not raise -- the caller (deliver_api) is in a per-envelope
        # loop and a Redis hiccup here must degrade to "don't trust it",
        # not abort the rest of the batch.
        self.assertFalse(was_delivered(broken, pod="p", tenant="t", agent="bob", stream_id=VALID_ID))


if __name__ == "__main__":
    unittest.main()
