"""Detect an ingress queue whose consumer has stopped draining it.

Ports drain ingress when kicked. If a port crashes in a loop, hangs while still
renewing its delivery lease, or has no resolvable handler, ingress keeps
accepting frames until ``INGRESS_MAX`` and then rejects senders. Nothing said so.

The signal here is progress, not liveness of any particular process: a drained
queue changes its head. If the same head frame stays at the front of a non-empty,
unpaused ingress for ``stall_seconds``, nothing is consuming it. The head is
identified by the stream_id in the fixed v4 header (``header_record_fields``),
the same bytes the switch already reads to forward; no payload is parsed.

This surfaces, it does not act: recovery stays with the owning runtime (the
existing ``kick_restarted`` re-kick) and with operators.
"""

import json
import time
from datetime import datetime, timezone

from .envelope import header_record_fields
from .keys import delivery_lock_key, ingress_stalled_key, prefix
from .logging import log_record as _log_record

DEFAULT_STALL_SECONDS = 60.0


def log_record(*args, **kwargs) -> None:
    """Observation must never undo the state change it describes."""
    try:
        _log_record(*args, **kwargs)
    except Exception:
        pass


def _text(value) -> str | None:
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else str(value)


class IngressStallMonitor:
    def __init__(
        self,
        r,
        *,
        pod: str,
        tenant: str,
        stall_seconds: float = DEFAULT_STALL_SECONDS,
        clock=time.monotonic,
    ):
        if stall_seconds <= 0:
            raise ValueError("stall_seconds must be positive")
        self.r = r
        self.pod = pod
        self.tenant = tenant
        self.stall_seconds = stall_seconds
        self.clock = clock
        # agent -> (head identity, monotonic time first seen at the head)
        self._watch: dict[str, tuple[str, float]] = {}
        self._stalled: set[str] = set()

    def poll(self, member_types: dict[str, str]) -> None:
        """Compare each member's ingress head with the previous pass."""
        now = self.clock()
        key = ingress_stalled_key(self.pod, self.tenant)
        for agent in sorted(set(self._watch) - set(member_types)):
            self._watch.pop(agent, None)
        # Entries for retired agents are cleared by the same rule as recovery.
        for agent in sorted(self._stalled - set(member_types)):
            self._clear(key, agent, "agent no longer registered")

        for agent, port_type in sorted(member_types.items()):
            head = self._head(agent)
            if head is None:
                self._watch.pop(agent, None)
                if agent in self._stalled:
                    self._clear(key, agent, "ingress drained")
                continue
            identity, depth = head
            seen = self._watch.get(agent)
            if seen is None or seen[0] != identity:
                self._watch[agent] = (identity, now)
                if agent in self._stalled:
                    self._clear(key, agent, "ingress is draining again")
                continue
            waited = now - seen[1]
            if waited < self.stall_seconds:
                continue
            self._report(key, agent, port_type, identity, depth, waited)

    def _head(self, agent: str) -> tuple[str, int] | None:
        """Head identity and depth, or None when there is nothing stuck to watch."""
        if self.r.get(prefix(self.pod, self.tenant, agent, "paused")) is not None:
            return None  # a deliberate pause is not a dead consumer
        ingress_key = prefix(self.pod, self.tenant, agent, "ingress")
        raw = self.r.lindex(ingress_key, 0)
        if raw is None:
            return None
        fields = header_record_fields(raw)
        identity = fields.get("stream_id")
        if not identity:
            # A malformed head has no header id; the raw bytes still identify it.
            identity = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        return identity, int(self.r.llen(ingress_key))

    def _report(self, key, agent, port_type, identity, depth, waited) -> None:
        lease = (
            "held"
            if self.r.get(delivery_lock_key(self.pod, self.tenant, agent)) is not None
            else "absent"
        )
        first = agent not in self._stalled
        existing = _text(self.r.hget(key, agent)) if first else None
        since = None
        if existing:
            try:
                since = json.loads(existing).get("since")
            except (ValueError, AttributeError):
                since = None
        if not since:
            since = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        record = {
            "since": since,
            "depth": depth,
            "head_stream_id": identity if len(identity) == 32 else None,
            "lease": lease,
            "port_type": port_type or None,
        }
        self.r.hset(key, agent, json.dumps(record, separators=(",", ":")))
        self._stalled.add(agent)
        if first:
            # Closed vocabulary: lease=held means a port holds the lease but is not
            # draining (hung); lease=absent means nothing is running or each kick
            # dies (crash loop, missing handler). Numbers ride in count/waited.
            log_record(
                "switch",
                "ingress_stalled",
                stream_id=record["head_stream_id"],
                destination=agent,
                reason=f"ingress head not consumed; delivery lease {lease}",
                count=depth,
                waited=int(waited),
            )

    def _clear(self, key, agent: str, reason: str) -> None:
        self.r.hdel(key, agent)
        self._stalled.discard(agent)
        log_record("switch", "ingress_recovered", destination=agent, reason=reason)
