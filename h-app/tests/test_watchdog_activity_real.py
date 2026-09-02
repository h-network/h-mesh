"""Real-Redis regression tests for _EMIT_USAGE_LUA's partial-state fix.

⚠ A FakeRedis cannot reproduce mid-script WRONGTYPE faithfully -- it has no
real Lua interpreter, so a test built on it can only ever exercise the
Python-side exception handling around eval(), never the script's own
control flow. The property this module exists to protect (no partial
state on a runtime error) is invisible to any test that does not run the
real script against a real Redis. See test_watchdog_activity.py's
test_usage_emit_failure_is_logged_not_swallowed for the FakeRedis-level
observability test this complements, not replaces.
"""

import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest
import redis

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from modules.watchdog.activity import _EMIT_USAGE_LUA


REQUEST_ID = "req-1"
RAW_USAGE = '{"cli":"claude","model":"x"}'
STREAM_ID = "delivery-stream-id"
MAXLEN = 10000


@pytest.fixture
def real_redis():
    r = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"))
    try:
        r.ping()
    except Exception:
        pytest.skip("real Redis server not available at REDIS_URL")
    return r


def _keys():
    tag = uuid4().hex[:12]
    return (f"lua-{tag}:usage", f"lua-{tag}:usage.requests", f"lua-{tag}:usage.attributed")


def _emit(real_redis, stream_key, seen_key, attributed_key):
    return real_redis.eval(
        _EMIT_USAGE_LUA, 3,
        stream_key, seen_key, attributed_key,
        REQUEST_ID, RAW_USAGE, STREAM_ID, MAXLEN,
    )


def test_attributed_key_wrongtype_is_no_orphan_state(real_redis):
    """The originally-reported defect: attributed_key holding the wrong
    type used to let XADD and the dedup SADD commit before the attribution
    SADD failed -- a real usage record survived with its dedup marker set
    but its delivery correlation silently lost. Harm-level assertion: no
    usage record exists at all, dedup was never marked either -- the whole
    script did nothing, not "everything except attribution"."""
    stream_key, seen_key, attributed_key = _keys()
    real_redis.set(attributed_key, "not-a-set")
    try:
        with pytest.raises(redis.exceptions.ResponseError):
            _emit(real_redis, stream_key, seen_key, attributed_key)

        # The exact old partial state this once produced (reproduced by
        # hand on 172.16.11.124 before this test existed): stream length 1,
        # dedup marker set, attribution silently absent. None of that here.
        assert real_redis.xlen(stream_key) == 0
        assert not real_redis.exists(seen_key)
        assert real_redis.type(attributed_key) == b"string"  # untouched, still wrong-typed
        assert real_redis.get(attributed_key) == b"not-a-set"
    finally:
        real_redis.delete(stream_key, seen_key, attributed_key)


def test_seen_key_wrongtype_is_no_orphan_state(real_redis):
    """seen_key wrong-typed fails at the SISMEMBER dedup check, before the
    preflight even runs -- but the harm-level guarantee (nothing written)
    must hold here too, not just for the key added by the preflight fix."""
    stream_key, seen_key, attributed_key = _keys()
    real_redis.set(seen_key, "not-a-set")
    try:
        with pytest.raises(redis.exceptions.ResponseError):
            _emit(real_redis, stream_key, seen_key, attributed_key)

        assert real_redis.xlen(stream_key) == 0
        assert real_redis.type(seen_key) == b"string"  # untouched
        assert real_redis.get(seen_key) == b"not-a-set"
        assert not real_redis.exists(attributed_key)
    finally:
        real_redis.delete(stream_key, seen_key, attributed_key)


def test_stream_key_wrongtype_is_no_orphan_state(real_redis):
    """stream_key wrong-typed fails at the XADD itself, the first
    mutation -- nothing before it to leave orphaned, but pinned explicitly
    so "all three keys" is executable evidence, not a claim."""
    stream_key, seen_key, attributed_key = _keys()
    real_redis.set(stream_key, "not-a-stream")
    try:
        with pytest.raises(redis.exceptions.ResponseError):
            _emit(real_redis, stream_key, seen_key, attributed_key)

        assert real_redis.type(stream_key) == b"string"  # untouched
        assert real_redis.get(stream_key) == b"not-a-stream"
        assert not real_redis.exists(seen_key)
        assert not real_redis.exists(attributed_key)
    finally:
        real_redis.delete(stream_key, seen_key, attributed_key)


def test_legitimate_types_still_emit_usage(real_redis):
    """The preflight must not reject the ordinary case: all three keys
    absent (first-ever emission), or already the correct type."""
    stream_key, seen_key, attributed_key = _keys()
    try:
        res = _emit(real_redis, stream_key, seen_key, attributed_key)
        assert res == 1
        assert real_redis.xlen(stream_key) == 1
        assert real_redis.sismember(seen_key, REQUEST_ID)
        assert real_redis.sismember(attributed_key, STREAM_ID)
    finally:
        real_redis.delete(stream_key, seen_key, attributed_key)

    # Second pass: all three keys already exist as the correct type
    # (stream/set/set) from the emission above -- re-seed them that way
    # explicitly rather than relying on leftover state, and confirm a
    # second, distinct request still emits (not deduped, different id).
    stream_key, seen_key, attributed_key = _keys()
    real_redis.xadd(stream_key, {"usage": "seed"})
    real_redis.sadd(seen_key, "some-other-request")
    real_redis.sadd(attributed_key, "some-other-stream-id")
    try:
        res = _emit(real_redis, stream_key, seen_key, attributed_key)
        assert res == 1
        assert real_redis.xlen(stream_key) == 2
        assert real_redis.sismember(seen_key, REQUEST_ID)
        assert real_redis.sismember(attributed_key, STREAM_ID)
    finally:
        real_redis.delete(stream_key, seen_key, attributed_key)


def test_dedup_short_circuits_before_any_type_check(real_redis):
    """An already-seen request_id returns 0 without touching the stream --
    confirms the preflight didn't change the dedup fast-path's behavior."""
    stream_key, seen_key, attributed_key = _keys()
    real_redis.sadd(seen_key, REQUEST_ID)
    try:
        res = _emit(real_redis, stream_key, seen_key, attributed_key)
        assert res == 0
        assert real_redis.xlen(stream_key) == 0
    finally:
        real_redis.delete(stream_key, seen_key, attributed_key)
