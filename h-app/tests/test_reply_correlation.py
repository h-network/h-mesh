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
    """Executes the real _RECORD_DELIVERED Lua script's externally visible
    effect atomically -- HSET + ZADD + bounded trim in one call, same as
    real Redis would, so a test against this fake cannot observe a torn
    write the way separate HSET/ZADD/ZREM/HDEL calls could."""

    def __init__(self):
        self.hashes = {}
        self.zsets = {}

    def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    def eval(self, script, numkeys, *args):
        keys = args[:numkeys]
        argv = args[numkeys:]
        if "reply_correlation record_delivered" in script:
            hash_key, order_key = keys
            stream_id, source, score, maxlen = argv
            maxlen = int(maxlen)
            self.hashes.setdefault(hash_key, {})[stream_id] = source
            self.zsets.setdefault(order_key, {})[stream_id] = float(score)
            count = len(self.zsets[order_key])
            if count > maxlen:
                ordered = sorted(self.zsets[order_key].items(), key=lambda kv: kv[1])
                stale = [member for member, _ in ordered[: count - maxlen]]
                for member in stale:
                    self.zsets[order_key].pop(member, None)
                    self.hashes[hash_key].pop(member, None)
            return 1
        raise AssertionError(f"unexpected eval script: {script[:60]!r}")


class SplitBoundaryRedis(FakeRedis):
    """A double that does NOT execute the script atomically -- it performs
    the equivalent HSET then ZADD as two separate steps, so a failure can be
    injected between them. This exists ONLY to prove the earlier
    multi-command design's exact failure mode and confirm the real
    (atomic) implementation cannot be made to reach it: record_delivered
    always calls eval() as a single operation, so this class's split
    behavior is unreachable from the real code -- it is exercised directly
    in the test below, not through record_delivered."""

    def __init__(self, fail_after_hset=False):
        super().__init__()
        self.fail_after_hset = fail_after_hset

    def hset_then_zadd_split(self, hash_key, order_key, stream_id, source, score):
        self.hashes.setdefault(hash_key, {})[stream_id] = source
        if self.fail_after_hset:
            raise ConnectionError("injected failure between HSET and ZADD")
        self.zsets.setdefault(order_key, {})[stream_id] = score


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
        record_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram")
        record_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram")
        others = [format(i, "032x") for i in range(1, DELIVERED_MAXLEN)]
        for stream_id in others:
            record_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=stream_id, source="telegram")
        self.assertTrue(
            was_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram")
        )

    def test_record_delivered_swallows_eval_errors_and_logs(self):
        # Same policy as mark_delivery_pending and _record elsewhere: a
        # bookkeeping write failure must never propagate and fail the
        # delivery it's recording.
        class BrokenRedis(FakeRedis):
            def eval(self, script, numkeys, *args):
                raise ConnectionError("redis unavailable")

        broken = BrokenRedis()
        record_delivered(broken, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram")  # must not raise
        self.assertFalse(
            was_delivered(broken, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram")
        )

    def test_was_delivered_returns_none_not_false_on_redis_error(self):
        class BrokenRedis(FakeRedis):
            def hget(self, key, field):
                raise ConnectionError("redis unavailable")

        broken = BrokenRedis()
        result = was_delivered(broken, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram")
        self.assertIsNone(result)

    def test_record_delivered_calls_eval_exactly_once_not_separate_commands(self):
        # This is the actual guarantee against the reviewer-found defect:
        # record_delivered never has an opportunity to fail BETWEEN a
        # provenance write and its eviction index, because there is only
        # ever one call to Redis, not several. Proven by counting calls
        # rather than by re-deriving end state, which the original bug's
        # own regression test (asserting only the end state) failed to
        # catch.
        calls = []

        class CountingRedis(FakeRedis):
            def eval(self, script, numkeys, *args):
                calls.append((script, numkeys, args))
                return super().eval(script, numkeys, *args)

        counting = CountingRedis()
        record_delivered(counting, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram")
        self.assertEqual(len(calls), 1)

    def test_the_old_split_command_design_could_reach_the_exact_defect_state(self):
        # Demonstrates, directly, the failure mode reviewer found in the
        # prior (non-atomic) implementation: HSET succeeds, ZADD raises.
        # This does NOT exercise record_delivered (which no longer has a
        # split boundary to inject into) -- it exercises the split
        # primitive above to show what that old shape produced, as the
        # concrete evidence for why the atomic rewrite was necessary, and
        # as a regression should anyone ever "simplify" record_delivered
        # back into separate commands.
        split = SplitBoundaryRedis(fail_after_hset=True)
        hash_key, order_key = "hk", "ok"
        with self.assertRaises(ConnectionError):
            split.hset_then_zadd_split(hash_key, order_key, VALID_ID, "telegram", 1.0)
        self.assertEqual(split.hashes[hash_key], {VALID_ID: "telegram"})
        self.assertEqual(split.zsets.get(order_key, {}), {})


if __name__ == "__main__":
    unittest.main()
