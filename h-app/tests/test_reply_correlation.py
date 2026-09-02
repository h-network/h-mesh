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
    effect for a well-typed key -- one hash, HSET + bounded trim in a
    single eval() call. This fake has no notion of Redis's own per-key type
    system, so it cannot reproduce the WRONGTYPE-preflight scenario; that
    regression lives in RealRedisRecordDeliveredTests below, against actual
    Redis, deliberately."""

    def __init__(self):
        self.hashes = {}

    def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    def eval(self, script, numkeys, *args):
        keys = args[:numkeys]
        argv = args[numkeys:]
        if "reply_correlation record_delivered" in script:
            (key,) = keys
            stream_id, source, score, maxlen = argv
            maxlen = int(maxlen)
            bucket = self.hashes.setdefault(key, {})
            bucket[stream_id] = json.dumps({"source": source, "score": float(score)})
            if len(bucket) > maxlen:
                ordered = sorted(bucket.items(), key=lambda kv: json.loads(kv[1])["score"])
                for member, _ in ordered[: len(bucket) - maxlen]:
                    bucket.pop(member, None)
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


if __name__ == "__main__":
    unittest.main()
