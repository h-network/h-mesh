import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import redis

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from core.keys import incarnation_key, prefix
from lib import reply_correlation
from lib.reply_correlation import (
    DELIVERED_TTL_SECONDS,
    is_valid_reply_id,
    record_delivered,
    was_delivered,
)


class FakeRedis:
    """A plain string keyspace with TTLs -- SET/GET/DELETE/TTL, nothing
    else. There is no eviction, ordering, or type-contract machinery left
    to fake; this fake exists only so the fast in-memory tests don't need
    real Redis for the cases that don't specifically require it (TTL
    behavior, WRONGTYPE self-healing, digit-only ids)."""

    def __init__(self):
        self.store = {}  # key -> (value, expires_at or None)

    def set(self, key, value, ex=None):
        expires_at = (time.monotonic() + ex) if ex is not None else None
        self.store[key] = (value, expires_at)
        return True

    def get(self, key):
        entry = self.store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at is not None and time.monotonic() >= expires_at:
            self.store.pop(key, None)
            return None
        return value

    def eval(self, script, numkeys, *args):
        # Generic enough to stand in for _VERIFY_DELIVERY_LUA specifically
        # (the only script was_delivered ever runs): KEYS[1]/ARGV[1] are
        # the incarnation key and its expected current value; KEYS[2] is
        # the claim key returned only if that comparison still holds.
        keys = args[:numkeys]
        argv = args[numkeys:]
        current = self.get(keys[0])
        if current is None or current != argv[0]:
            return None
        return self.get(keys[1])

    def ttl(self, key):
        entry = self.store.get(key)
        if entry is None:
            return -2
        _, expires_at = entry
        if expires_at is None:
            return -1
        return max(0, round(expires_at - time.monotonic()))


VALID_ID = "a" * 32
DIGIT_ONLY_ID = "1" * 32  # all-digit but still a valid 32-hex-char id
INCARNATION = "1" * 32  # a placeholder current-incarnation value, digit-only
# on purpose -- covers the same all-digit-segment hazard VALID_ID/
# DIGIT_ONLY_ID exercise, for the "i" prefix rather than the "s" one.


def _key(pod, tenant, agent, stream_id, incarnation=INCARNATION):
    return prefix(pod, tenant, agent=agent, resource=f"delivered.s{stream_id}.i{incarnation}")


class ReplyCorrelationTests(unittest.TestCase):
    def setUp(self):
        self.r = FakeRedis()

    def _incarnate(self, agent, value=INCARNATION, pod="p", tenant="t"):
        self.r.set(incarnation_key(pod, tenant, agent), value)

    def test_is_valid_reply_id_accepts_only_32_lowercase_hex(self):
        self.assertTrue(is_valid_reply_id(VALID_ID))
        self.assertFalse(is_valid_reply_id("A" * 32))
        self.assertFalse(is_valid_reply_id("a" * 31))
        self.assertFalse(is_valid_reply_id("a" * 33))
        self.assertFalse(is_valid_reply_id("z" * 32))
        self.assertFalse(is_valid_reply_id(None))
        self.assertFalse(is_valid_reply_id(12345))

    def test_delivered_id_is_later_found_with_matching_source(self):
        self._incarnate("bob")
        record_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram")
        self.assertTrue(
            was_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram")
        )

    def test_undelivered_id_is_not_found(self):
        self._incarnate("bob")
        self.assertFalse(
            was_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram")
        )

    def test_delivered_to_one_agent_is_not_found_for_another(self):
        self._incarnate("bob")
        self._incarnate("alice", value="2" * 32)
        record_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram")
        self.assertFalse(
            was_delivered(self.r, pod="p", tenant="t", agent="alice", stream_id=VALID_ID, source="telegram")
        )

    def test_delivered_from_one_source_does_not_validate_a_different_source(self):
        # The cross-client case: bob was really sent this id by telegram.
        # A claim that it came from webconsole must not validate, even
        # though the id really was delivered to bob. This binding is the
        # entire point of storing source as the value rather than just
        # recording membership.
        self._incarnate("bob")
        record_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram")
        self.assertFalse(
            was_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="webconsole")
        )

    def test_malformed_id_is_never_recorded_or_matched(self):
        self._incarnate("bob")
        record_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id="not-an-id", source="telegram")
        self.assertFalse(
            was_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id="not-an-id", source="telegram")
        )
        # Only the incarnation key from setup should exist -- nothing was
        # ever written for the malformed id itself.
        self.assertEqual(len(self.r.store), 1)

    def test_digit_only_stream_id_can_still_be_recorded(self):
        # core.keys rejects an all-digit dotted-resource segment (tmux
        # resolves an all-digit agent name as a window index, an
        # unrelated module's concern using the same shared validator). A
        # 32-hex-char stream_id that happens to contain no a-f characters
        # is all-digits, and without the "s" prefix in _key(), this would
        # silently and permanently fail to record for exactly those ids.
        self._incarnate("bob")
        record_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=DIGIT_ONLY_ID, source="telegram")
        self.assertTrue(
            was_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=DIGIT_ONLY_ID, source="telegram")
        )

    def test_ttl_is_set_to_the_configured_window(self):
        self._incarnate("bob")
        record_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram")
        key = _key("p", "t", "bob", VALID_ID)
        ttl = self.r.ttl(key)
        self.assertGreater(ttl, 0)
        self.assertLessEqual(ttl, DELIVERED_TTL_SECONDS)

    def test_redelivery_resets_the_ttl(self):
        self._incarnate("bob")
        with patch.object(reply_correlation, "DELIVERED_TTL_SECONDS", 100):
            record_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram")
            key = _key("p", "t", "bob", VALID_ID)
            self.r.store[key] = (self.r.store[key][0], time.monotonic() + 1)  # simulate near-expiry
            record_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram")
            self.assertGreater(self.r.ttl(key), 1)

    def test_a_wrong_typed_existing_key_is_healed_by_the_next_record_not_left_broken(self):
        # SET overwrites a key unconditionally regardless of its prior
        # type -- unlike the deleted hash+zset/hash+counter designs,
        # there is no WRONGTYPE failure mode for the write side at all.
        class TypedRedis(FakeRedis):
            def __init__(self):
                super().__init__()
                self.wrong_typed = set()

            def set(self, key, value, ex=None):
                self.wrong_typed.discard(key)
                return super().set(key, value, ex=ex)

            def get(self, key):
                if key in self.wrong_typed:
                    raise redis.exceptions.ResponseError("WRONGTYPE Operation against a key holding the wrong kind of value")
                return super().get(key)

        r = TypedRedis()
        r.set(incarnation_key("p", "t", "bob"), INCARNATION)
        key = _key("p", "t", "bob", VALID_ID)
        r.wrong_typed.add(key)
        # Before any record_delivered call, reading is a verified failure
        # (could not check), not a false negative.
        self.assertIsNone(was_delivered(r, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram"))
        record_delivered(r, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram")
        self.assertTrue(was_delivered(r, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram"))

    def test_record_delivered_swallows_write_errors_and_logs(self):
        class BrokenRedis(FakeRedis):
            def set(self, key, value, ex=None):
                raise ConnectionError("redis unavailable")

        broken = BrokenRedis()
        # The incarnation lookup itself is a `get`, unaffected by this
        # broken `set` -- but there is nowhere durable to have established
        # it, so seed the store directly rather than through `set`.
        broken.store[incarnation_key("p", "t", "bob")] = (INCARNATION, None)
        record_delivered(broken, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram")  # must not raise
        self.assertFalse(
            was_delivered(broken, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram")
        )

    def test_was_delivered_returns_none_not_false_on_redis_error(self):
        class BrokenRedis(FakeRedis):
            def get(self, key):
                raise ConnectionError("redis unavailable")

        broken = BrokenRedis()
        result = was_delivered(broken, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram")
        self.assertIsNone(result)

    def test_no_incarnation_established_means_record_delivered_writes_nothing(self):
        # Ticket 97ad745c: a legacy agent (or the window between a stop
        # and its next hire) has no incarnation id. Writing a record that
        # could never be matched by anyone is pointless -- confirm
        # record_delivered recognizes that and skips the write entirely,
        # not just that was_delivered later refuses to trust it.
        record_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram")
        self.assertEqual(self.r.store, {})

    def test_no_incarnation_established_means_was_delivered_is_false_not_none(self):
        # Explicit per the module docstring: absent means "matches
        # nothing", a verified fact, not "could not check".
        self.assertFalse(
            was_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram")
        )

    def test_successor_incarnation_cannot_validate_predecessors_claim(self):
        # The exposure this ticket exists to close: a same-named successor
        # querying with ITS OWN current incarnation id must not match a
        # record the PREDECESSOR incarnation established, even though the
        # agent name, stream_id, and source are all identical.
        self._incarnate("bob", value="predecessor-incarnation")
        record_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram")
        self.assertTrue(
            was_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram")
        )

        # A same-named successor is hired: a fresh incarnation id replaces
        # the predecessor's (lifecycle.stop_agent deletes it, the next
        # start_agent mints a new one -- simulated here directly).
        self._incarnate("bob", value="successor-incarnation")
        self.assertFalse(
            was_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram")
        )

    def test_race_between_the_two_incarnation_reads_never_validates_as_true(self):
        # switch-agent's exact finding against the first version of this
        # fix: was_delivered did two SEPARATE reads (current incarnation,
        # then the incarnation-qualified claim), leaving a window where a
        # stop+rehire landing BETWEEN them lets a successor validate a
        # predecessor's stale claim as True -- the identical inheritance
        # bug this whole binding exists to close, reduced to a race
        # instead of eliminated. Deterministic, not timing-dependent:
        # forces the FIRST read (_incarnation) to return a value that is
        # already stale by the time the atomic verification actually
        # runs, simulating the race landing every time rather than
        # occasionally, so this test cannot flake into a false pass.
        self._incarnate("bob", value="predecessor-incarnation")
        record_delivered(self.r, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram")

        # The successor's hire completes (fresh incarnation minted) AFTER
        # was_delivered's first read already captured the predecessor's
        # value -- reproduced by making the first read return the stale
        # value while the store has already moved on.
        self._incarnate("bob", value="successor-incarnation")
        with patch.object(reply_correlation, "_incarnation", return_value="predecessor-incarnation"):
            result = was_delivered(
                self.r, pod="p", tenant="t", agent="bob", stream_id=VALID_ID, source="telegram"
            )
        self.assertIn(result, (False, None))
        self.assertIsNot(result, True)


class RealRedisRecordDeliveredTests(unittest.TestCase):
    """Exercises real Redis, not a fake -- TTL expiry and Redis's own
    WRONGTYPE enforcement are both things no in-memory double can prove on
    its own."""

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

    def _incarnate(self, agent, value=INCARNATION):
        self.r.set(incarnation_key(self.pod, self.tenant, agent), value)

    def test_delivered_id_is_later_found_with_matching_source_only(self):
        self._incarnate("bob")
        self._incarnate("alice", value="2" * 32)
        record_delivered(self.r, pod=self.pod, tenant=self.tenant, agent="bob", stream_id=VALID_ID, source="telegram")
        self.assertTrue(
            was_delivered(self.r, pod=self.pod, tenant=self.tenant, agent="bob", stream_id=VALID_ID, source="telegram")
        )
        self.assertFalse(
            was_delivered(self.r, pod=self.pod, tenant=self.tenant, agent="bob", stream_id=VALID_ID, source="webconsole")
        )
        self.assertFalse(
            was_delivered(self.r, pod=self.pod, tenant=self.tenant, agent="alice", stream_id=VALID_ID, source="telegram")
        )

    def test_digit_only_stream_id_round_trips_against_real_redis(self):
        self._incarnate("bob")
        record_delivered(self.r, pod=self.pod, tenant=self.tenant, agent="bob", stream_id=DIGIT_ONLY_ID, source="telegram")
        self.assertTrue(
            was_delivered(self.r, pod=self.pod, tenant=self.tenant, agent="bob", stream_id=DIGIT_ONLY_ID, source="telegram")
        )

    def test_ttl_is_set_against_real_redis(self):
        self._incarnate("bob")
        record_delivered(self.r, pod=self.pod, tenant=self.tenant, agent="bob", stream_id=VALID_ID, source="telegram")
        key = _key(self.pod, self.tenant, "bob", VALID_ID)
        ttl = self.r.ttl(key)
        self.assertGreater(ttl, 0)
        self.assertLessEqual(ttl, DELIVERED_TTL_SECONDS)

    def test_expiry_actually_removes_the_entry(self):
        # A short real TTL, waited out -- not a simulation of expiry, the
        # real thing, so this is evidence the mechanism actually forgets,
        # not just that it claims a TTL was requested.
        self._incarnate("bob")
        with patch.object(reply_correlation, "DELIVERED_TTL_SECONDS", 1):
            record_delivered(self.r, pod=self.pod, tenant=self.tenant, agent="bob", stream_id=VALID_ID, source="telegram")
        self.assertTrue(
            was_delivered(self.r, pod=self.pod, tenant=self.tenant, agent="bob", stream_id=VALID_ID, source="telegram")
        )
        time.sleep(1.5)
        self.assertFalse(
            was_delivered(self.r, pod=self.pod, tenant=self.tenant, agent="bob", stream_id=VALID_ID, source="telegram")
        )

    def test_redelivery_resets_ttl_against_real_redis(self):
        self._incarnate("bob")
        with patch.object(reply_correlation, "DELIVERED_TTL_SECONDS", 1):
            record_delivered(self.r, pod=self.pod, tenant=self.tenant, agent="bob", stream_id=VALID_ID, source="telegram")
            time.sleep(0.7)
            # Re-deliver right before the original TTL would have expired.
            record_delivered(self.r, pod=self.pod, tenant=self.tenant, agent="bob", stream_id=VALID_ID, source="telegram")
            time.sleep(0.7)
            # 1.4s have passed since the ORIGINAL record; it would already
            # be gone if the TTL hadn't been reset by the redelivery.
            self.assertTrue(
                was_delivered(self.r, pod=self.pod, tenant=self.tenant, agent="bob", stream_id=VALID_ID, source="telegram")
            )

    def test_a_wrong_typed_existing_key_does_not_break_the_write_and_self_heals(self):
        # SET overwrites unconditionally -- confirm against real Redis's
        # own type enforcement, not just the fake's simulation of it.
        self._incarnate("bob")
        key = _key(self.pod, self.tenant, "bob", VALID_ID)
        self.r.rpush(key, "unrelated-list-value")
        self.assertEqual(self.r.type(key), b"list")

        record_delivered(self.r, pod=self.pod, tenant=self.tenant, agent="bob", stream_id=VALID_ID, source="telegram")

        self.assertEqual(self.r.type(key), b"string")
        self.assertTrue(
            was_delivered(self.r, pod=self.pod, tenant=self.tenant, agent="bob", stream_id=VALID_ID, source="telegram")
        )

    def test_successor_incarnation_cannot_validate_predecessors_claim_against_real_redis(self):
        # Same property as the FakeRedis version above, confirmed against
        # real Redis's own key semantics rather than the in-memory double.
        self._incarnate("bob", value="predecessor-incarnation")
        record_delivered(self.r, pod=self.pod, tenant=self.tenant, agent="bob", stream_id=VALID_ID, source="telegram")
        self.assertTrue(
            was_delivered(self.r, pod=self.pod, tenant=self.tenant, agent="bob", stream_id=VALID_ID, source="telegram")
        )
        self._incarnate("bob", value="successor-incarnation")
        self.assertFalse(
            was_delivered(self.r, pod=self.pod, tenant=self.tenant, agent="bob", stream_id=VALID_ID, source="telegram")
        )


if __name__ == "__main__":
    unittest.main()
