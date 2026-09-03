"""Hot-tier conversation memory: a small, TTL-evicted per-chat turn buffer.

Adapted from h-nat's `h-memory` design (its `BoundedBufferStore`) to this
project's own keyspace (`core.keys.prefix()`) and synchronous Redis client --
nothing in h-app uses `redis.asyncio` anywhere else, so this doesn't
introduce the first instance.

Deliberately a plain importable class, not a port: nothing here owns
delivery, an envelope kind, or a `modules/` entry -- any module that already
has a Redis client and knows its own (pod, tenant, agent) can use it
directly, the same way `lib/reply_correlation.py` is used by several ports
without being one itself. See lib/chat_cycle.py for the read-dispatch-write
composite built on top of this.

Long-term/semantic memory -- what, if anything, happens to a turn once its
TTL elapses -- is explicitly out of scope here. A turn's data key expires by
plain Redis TTL and is gone; nothing in this file offers a hook to recover
it after the fact. That absence is deliberate (ticket 0902ee96): a
placeholder for a future sweep-before-expiry design, not a promise that
expired turns are recoverable.
"""
from __future__ import annotations

import json
import time

from core.keys import prefix


class ChatMemory:
    """Hot-tier turn buffer for one (pod, tenant, agent), chat_id-scoped.

    Turn key:  pod:<pod>:tenant:<tenant>:agent:<agent>:chat.c<chat_id>.t<ts_ns>
    Index key: pod:<pod>:tenant:<tenant>:agent:<agent>:chat-index.c<chat_id>

    The "c"/"t" segment prefixes exist for the same reason
    lib/reply_correlation.py's "s"/"i" prefixes do: core.keys rejects any
    resource segment that is entirely digits (elsewhere in this project an
    all-digit *agent* name is unaddressable in tmux, and the segment
    validator enforcing that doesn't know a chat_id or ts_ns isn't an agent
    name). ts_ns is always all-digit; a chat_id could be too, rarely. The
    letter prefixes make both structurally safe regardless of the value.
    """

    def __init__(self, client, pod: str, tenant: str, agent: str, ttl_seconds_max: int):
        self._client = client
        self._pod = pod
        self._tenant = tenant
        self._agent = agent
        self._ttl_max = ttl_seconds_max
        self._last_ts_ns = 0

    @property
    def ttl_seconds_max(self) -> int:
        return self._ttl_max

    def _turn_key(self, chat_id: str, ts_ns: int) -> str:
        return prefix(self._pod, self._tenant, agent=self._agent, resource=f"chat.c{chat_id}.t{ts_ns}")

    def _index_key(self, chat_id: str) -> str:
        return prefix(self._pod, self._tenant, agent=self._agent, resource=f"chat-index.c{chat_id}")

    def _next_ts_ns(self) -> int:
        # Monotonic guard: same-nanosecond writes (realistic on a fast
        # host) still get distinct keys and a strictly increasing ZSET
        # score. Per-instance only, same scope h-memory's own version
        # documents -- cross-process disambiguation isn't this class's job.
        ns = time.time_ns()
        if ns <= self._last_ts_ns:
            ns = self._last_ts_ns + 1
        self._last_ts_ns = ns
        return ns

    def write_turn(
        self,
        chat_id: str,
        role: str,
        content: str,
        ttl_seconds: int,
        *,
        hot_keep_count: int | None = None,
    ) -> str:
        """Write one turn to the hot tier; return the full turn key.

        Caller validates `ttl_seconds` is in `[1, ttl_seconds_max]` --
        this store applies whatever it's given, single source of truth
        stays with the caller, same contract h-memory's own version uses.

        One pipeline round-trip: SET the turn (with its own TTL), ZADD it
        into the chat's index, refresh the index's own EXPIRE to the max
        window (so the index outlives the longest possible turn TTL),
        ZREMRANGEBYSCORE to drop index entries that provably can't still
        have a live data key, and -- only when `hot_keep_count` is given
        -- ZREMRANGEBYRANK to also bound the index by count. Either
        cleanup step only ever prunes the index; a turn's own data key is
        removed solely by its own TTL, never by these.
        """
        ts_ns = self._next_ts_ns()
        ts_seconds = ts_ns // 1_000_000_000
        payload = json.dumps(
            {"role": role, "content": content, "ts": ts_seconds},
            separators=(",", ":"),
        )
        turn_key = self._turn_key(chat_id, ts_ns)
        index_key = self._index_key(chat_id)
        cutoff_ns = ts_ns - self._ttl_max * 1_000_000_000

        pipe = self._client.pipeline(transaction=False)
        pipe.set(turn_key, payload, ex=ttl_seconds)
        pipe.zadd(index_key, {turn_key: ts_ns})
        pipe.expire(index_key, self._ttl_max)
        pipe.zremrangebyscore(index_key, 0, cutoff_ns)
        if hot_keep_count is not None:
            pipe.zremrangebyrank(index_key, 0, -(hot_keep_count + 1))
        pipe.execute()

        return turn_key

    def read_turns(self, chat_id: str) -> list[dict]:
        """Live turns for `chat_id`, oldest first.

        A key already expired between the index read and this MGET is
        just skipped (`payload` is falsy) -- expiry racing a read is
        expected, not an error. A chat_id never written to reads back an
        empty list, the same shape a genuine one-off's first (and only)
        call sees -- there is no separate "no history" error case.
        """
        index_key = self._index_key(chat_id)
        keys = self._client.zrevrange(index_key, 0, -1)
        if not keys:
            return []
        payloads = self._client.mget(*keys)
        turns: list[dict] = []
        for payload in reversed(payloads):
            if not payload:
                continue
            try:
                turn = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(turn, dict):
                turns.append(turn)
        return turns
