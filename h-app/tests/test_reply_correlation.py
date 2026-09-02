import json
import os
import sys
import unittest
from pathlib import Path

import redis

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from core.keys import prefix
from lib.reply_correlation import (
    DELIVERED_MAXLEN,
    is_valid_reply_id,
    record_delivered,
    was_delivered,
)


class FakeRedis:
    """Executes the real _RECORD_DELIVERED Lua script's externally visible
    effect for well-typed keys -- one hash plus an INCR counter, HSET +
    bounded trim in a single eval() call. This fake has no notion of
    Redis's own per-key type system, so it cannot reproduce the
    WRONGTYPE-preflight scenario; that regression lives in
    RealRedisRecordDeliveredTests below, against actual Redis,
    deliberately."""

    def __init__(self):
        self.hashes = {}
        self.counters = {}

    def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    def eval(self, script, numkeys, *args):
        keys = args[:numkeys]
        argv = args[numkeys:]
        if "reply_correlation record_delivered" in script:
            key, counter_key = keys
            stream_id, source, maxlen = argv
            maxlen = int(maxlen)
            bucket = self.hashes.setdefault(key, {})
            # Mirrors the real script's ordering: decode and validate every
            # existing entry (excluding, not failing on, an undecodable or
            # non-numeric score) before deciding what to evict, then write.
            decodable = []
            for field, raw in bucket.items():
                try:
                    decoded = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if isinstance(decoded, dict) and isinstance(decoded.get("score"), (int, float)):
                    decodable.append((field, decoded["score"]))
            self.counters[counter_key] = self.counters.get(counter_key, 0) + 1
            score = self.counters[counter_key]
            decodable.append((stream_id, score))
            decodable.sort(key=lambda item: item[1])
            to_evict = [field for field, _ in decodable[: len(decodable) - maxlen] if field != stream_id] \
                if len(decodable) > maxlen else []
            bucket[stream_id] = json.dumps({"source": source, "score": score})
            for field in to_evict:
                bucket.pop(field, None)
            return 1
        raise AssertionError(f"unexpected eval script: {script[:60]!r}")


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

    def test_record_delivered_calls_eval_exactly_once(self):
        # Necessary, not sufficient: this only proves record_delivered
        # issues one Redis command rather than several. It does NOT by
        # itself prove that command can't half-apply -- Redis Lua provides
        # isolation, not transactional rollback, so a runtime error partway
        # through a script can still leave earlier writes in that same
        # script applied (see RealRedisRecordDeliveredTests below, and the
        # module docstring). The type preflight is what actually closes
        # that gap for this script's specific single-key design.
        calls = []

        class CountingRedis(FakeRedis):
            def eval(self, script, numkeys, *args):
                calls.append((script, numkeys, args))
                return super().eval(script, numkeys, *args)

        counting = CountingRedis()
        record_delivered(counting, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram")
        self.assertEqual(len(calls), 1)


class RealRedisRecordDeliveredTests(unittest.TestCase):
    """Exercises real Redis, not a fake. The regression below reproduces a
    genuine bug found on real Redis: a Lua script's redis.call()s are not
    rolled back on a later runtime error within the same script, so a
    two-key version of this script (HSET a provenance hash, then ZADD a
    separate eviction-order zset) left permanent, unindexed, always-valid
    provenance if the second key ever held the wrong type -- reproduced by
    seeding that key as a plain string before calling record_delivered. No
    in-memory fake enforces Redis's own per-key type system, so this cannot
    be exercised any other way.
    """

    def setUp(self):
        self.redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
        self.r = redis.Redis.from_url(self.redis_url)
        self.r.ping()
        self.pod = "real-reply-correlation-test"
        self.tenant = f"tenant-{os.urandom(4).hex()}"

    def tearDown(self):
        keys = self.r.keys(f"pod:{self.pod}:tenant:{self.tenant}:*")
        if keys:
            self.r.delete(*keys)

    def test_eviction_boundary_is_exact_against_real_redis(self):
        # The collapse from hash+zset to a single hash moved recency
        # entirely into HGETALL + Lua table.sort, run fresh on every write
        # once the hash is at capacity -- a materially different mechanism
        # from a maintained sorted-set index, worth confirming precisely
        # against the real interpreter rather than trusting it generalizes
        # from the fake's separate Python reimplementation of the same
        # intent (FakeRedis.eval() cannot prove what the actual Lua does).
        ids = [format(i, "032x") for i in range(DELIVERED_MAXLEN + 3)]
        for stream_id in ids:
            record_delivered(self.r, pod=self.pod, tenant=self.tenant, agent="bob", stream_id=stream_id, source="telegram")

        # Exactly the 3 oldest are gone; everything from index 3 onward,
        # including the boundary entries immediately on either side of the
        # cut, survives.
        for stream_id in ids[:3]:
            self.assertFalse(
                was_delivered(self.r, pod=self.pod, tenant=self.tenant, agent="bob", stream_id=stream_id, source="telegram"),
                f"{stream_id} should have been evicted",
            )
        for stream_id in ids[3:]:
            self.assertTrue(
                was_delivered(self.r, pod=self.pod, tenant=self.tenant, agent="bob", stream_id=stream_id, source="telegram"),
                f"{stream_id} should still be present",
            )
        key = prefix(self.pod, self.tenant, agent="bob", resource="delivered")
        self.assertEqual(self.r.hlen(key), DELIVERED_MAXLEN)

    def test_redelivery_refreshes_recency_against_real_redis(self):
        # Recording the same id again must move it to the "most recent"
        # end, protecting it from eviction it would otherwise be due for --
        # confirmed against real Redis, not the fake's own bookkeeping.
        # Enough total distinct ids are recorded to force eviction (unlike
        # a run that never exceeds the cap, which would pass this
        # assertion regardless of whether refresh actually works).
        record_delivered(self.r, pod=self.pod, tenant=self.tenant, agent="bob", stream_id=VALID_ID, source="telegram")
        others = [format(i, "032x") for i in range(1, DELIVERED_MAXLEN + 6)]
        for i, stream_id in enumerate(others):
            record_delivered(self.r, pod=self.pod, tenant=self.tenant, agent="bob", stream_id=stream_id, source="telegram")
            if i == 5:
                # Re-deliver VALID_ID right after the 6 oldest "others" --
                # its refreshed recency should now rank it ahead of all 6
                # of them, so when the 6 oldest are eventually evicted,
                # they (not VALID_ID) are the ones that go.
                record_delivered(self.r, pod=self.pod, tenant=self.tenant, agent="bob", stream_id=VALID_ID, source="telegram")

        self.assertTrue(
            was_delivered(self.r, pod=self.pod, tenant=self.tenant, agent="bob", stream_id=VALID_ID, source="telegram")
        )
        # The 6 recorded immediately before the refresh, never themselves
        # re-recorded, should have aged out in its place.
        for stream_id in others[:6]:
            self.assertFalse(
                was_delivered(self.r, pod=self.pod, tenant=self.tenant, agent="bob", stream_id=stream_id, source="telegram")
            )

    def test_delivered_id_is_later_found_with_matching_source(self):
        record_delivered(self.r, pod=self.pod, tenant=self.tenant, agent="bob", stream_id=VALID_ID, source="telegram")
        self.assertTrue(
            was_delivered(self.r, pod=self.pod, tenant=self.tenant, agent="bob", stream_id=VALID_ID, source="telegram")
        )
        self.assertFalse(
            was_delivered(self.r, pod=self.pod, tenant=self.tenant, agent="bob", stream_id=VALID_ID, source="webconsole")
        )

    def test_wrong_type_key_is_rejected_before_any_write_not_after(self):
        # The exact reviewer reproduction, against the CURRENT single-key
        # design: seed the one key this script would use as a plain
        # string (simulating the class of key-collision/corruption that
        # produced the original bug), then call record_delivered.
        key = prefix(self.pod, self.tenant, agent="bob", resource="delivered")
        self.r.set(key, "not-a-hash")

        record_delivered(self.r, pod=self.pod, tenant=self.tenant, agent="bob", stream_id=VALID_ID, source="telegram")

        # The preflight must reject this before the script's first
        # mutation -- the key must be untouched (still the string we
        # seeded, not partially overwritten), and the id must not be
        # trusted afterward.
        self.assertEqual(self.r.type(key), b"string")
        self.assertEqual(self.r.get(key), b"not-a-hash")
        result = was_delivered(self.r, pod=self.pod, tenant=self.tenant, agent="bob", stream_id=VALID_ID, source="telegram")
        # was_delivered's own HGET against a string-typed key also raises
        # WRONGTYPE -- correctly reported as "could not verify" (None),
        # not a confirmed negative, since the true cause is a corrupted
        # key, not an absence of provenance.
        self.assertIsNone(result)

    def test_wrong_type_key_failure_is_logged(self):
        import contextlib
        import io

        key = prefix(self.pod, self.tenant, agent="bob", resource="delivered")
        self.r.set(key, "not-a-hash")

        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            record_delivered(self.r, pod=self.pod, tenant=self.tenant, agent="bob", stream_id=VALID_ID, source="telegram")

        lines = [json.loads(line) for line in captured.getvalue().splitlines()]
        failures = [line for line in lines if line.get("event") == "record_delivered_failed"]
        self.assertEqual(len(failures), 1)
        self.assertIn("wrong type", failures[0]["reason"])

    def test_a_nonnumeric_score_among_existing_entries_does_not_abort_after_writing(self):
        # openshell-agent's follow-up audit: an earlier version of the
        # script decoded and sorted existing entries AFTER already writing
        # the new one, so a legacy/corrupted entry with a non-numeric
        # score reached the sort comparator and raised there -- with the
        # write already committed. The write succeeding either way means
        # asserting only "was_delivered is True afterward" does not
        # distinguish old from new behavior (confirmed: it passes against
        # both). What actually differs is whether a real, successful write
        # gets reported as a failure -- the old code logged
        # record_delivered_failed for a call whose HSET had, in fact,
        # already landed. That false failure report is what this asserts
        # against, captured directly rather than inferred from end state.
        import contextlib
        import io

        key = prefix(self.pod, self.tenant, agent="bob", resource="delivered")
        self.r.hset(key, "b" * 32, json.dumps({"source": "telegram", "score": "oops"}))
        for i in range(DELIVERED_MAXLEN - 1):
            record_delivered(
                self.r, pod=self.pod, tenant=self.tenant, agent="bob",
                stream_id=format(i, "032x"), source="telegram",
            )

        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            record_delivered(self.r, pod=self.pod, tenant=self.tenant, agent="bob", stream_id=VALID_ID, source="telegram")

        failures = [
            json.loads(line) for line in captured.getvalue().splitlines()
            if json.loads(line).get("event") == "record_delivered_failed"
        ]
        self.assertEqual(failures, [], f"record_delivered reported failure for a write that succeeded: {failures}")
        self.assertTrue(
            was_delivered(self.r, pod=self.pod, tenant=self.tenant, agent="bob", stream_id=VALID_ID, source="telegram")
        )
        # The undecodable entry is excluded from eviction targeting (never
        # selected for removal, since its recency can't be determined) --
        # it should still be sitting there, untouched, not silently lost.
        self.assertIsNotNone(self.r.hget(key, "b" * 32))


if __name__ == "__main__":
    unittest.main()
