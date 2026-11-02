"""Forward tenant egress queues without interpreting payloads."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable

import redis

from .envelope import (
    EnvelopeError,
    advance_hop,
    build,
    encode,
    header_record_fields,
    parse_for_switch,
    stamp_source,
)
from .keys import delivery_lock_key, prefix
from .logging import configure_logging, emit, log_record, publish
from .queues import admit_ingress
from .registry import member_types, members, port_type
from .retention import RetentionTrimmer
from .windowlog import WindowLogTailer


# Redis conditions that clear by themselves: the server is busy, loading, in
# failover, or the link dropped. Anything else (WRONGTYPE from a corrupted key,
# a script error, auth, OOM) means state or code is wrong and stays fatal -- a
# loop that retried those would hide the defect behind a healthy-looking daemon.
REDIS_BACKOFF_START = 0.2
REDIS_BACKOFF_CAP = 5.0
# Categories where Redis answered with an error BEFORE running the command, so
# the write provably did not happen and the same frame may be retried. connection
# and timeout are deliberately absent: the command may have executed, and core is
# at-most-once, so those are never blindly retried.
REJECTED_BEFORE_EXECUTION = frozenset(
    {"busy", "loading", "readonly", "tryagain", "masterdown"}
)
FRAME_RETRY_ATTEMPTS = 8  # backoff 0.2+0.4+0.8+1.6+3.2+5+5 = ~16s before dead-letter
_TRANSIENT_RESPONSE_PREFIXES = {
    "BUSY": "busy",
    "LOADING": "loading",
    "READONLY": "readonly",
    "TRYAGAIN": "tryagain",
    "MASTERDOWN": "masterdown",
}


def transient_redis_category(exc: BaseException) -> str | None:
    """Return a closed category name for a transient Redis error, else None."""
    # BusyLoadingError subclasses ConnectionError; check it before the family.
    if isinstance(exc, redis.exceptions.BusyLoadingError):
        return "loading"
    if isinstance(exc, redis.exceptions.ReadOnlyError):
        return "readonly"
    if isinstance(exc, redis.exceptions.TryAgainError):
        return "tryagain"
    if isinstance(exc, redis.exceptions.MasterDownError):
        return "masterdown"
    # Bad credentials subclass ConnectionError but never clear on their own.
    if isinstance(exc, redis.exceptions.AuthenticationError):
        return None
    if isinstance(exc, redis.exceptions.ConnectionError):
        return "connection"
    if isinstance(exc, redis.exceptions.TimeoutError):
        return "timeout"
    # redis-py has no class for BUSY: it arrives as a bare ResponseError whose
    # text starts with the code. Match the code word only, not arbitrary text.
    if type(exc) is redis.exceptions.ResponseError:
        code = str(exc).split(" ", 1)[0].upper()
        return _TRANSIENT_RESPONSE_PREFIXES.get(code)
    return None


class RetryExhausted(Exception):
    """A provably-rejected Redis command kept being rejected; the frame is unplaced."""

    def __init__(self, category: str, cause: Exception, attempts: int):
        super().__init__(f"{category} persisted through {attempts} attempts")
        self.category = category
        self.cause = cause
        self.attempts = attempts


def _emit_observation(event: str, envelope: dict, reason=None, count=None) -> None:
    """Keep stdout observation from changing switch custody decisions."""
    try:
        emit("switch", event, envelope, reason, count)
    except Exception:
        pass


def _log_observation(event: str, **fields) -> None:
    """Best-effort structured observation after a custody operation."""
    try:
        log_record("switch", event, **fields)
    except Exception:
        pass


def _port_diagnostic_path() -> str:
    run_dir = os.environ.get("H_MESH_RUN_DIR")
    if not run_dir:
        tenant = os.environ.get("TENANT", "default")
        run_dir = str(os.path.expanduser(f"~/.h-mesh/run/{tenant}"))
    os.makedirs(run_dir, exist_ok=True)
    return os.path.join(run_dir, "ports.log")


def _forward_port_custody(read_fd: int, *, agent: str) -> None:
    """Validate a port's custody-only pipe before publishing each record."""
    with os.fdopen(read_fd, "rb") as source:
        for raw in source:
            try:
                line = raw.decode("utf-8").rstrip("\n")
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("custody record is not a JSON object")
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                log_record(
                    "switch",
                    "port_custody_parse_error",
                    destination=agent,
                    reason=str(exc),
                    byte_count=len(raw),
                )
                continue
            publish(line)


def transmission(agent: str, port_type_name: str | None, envelope: dict) -> None:
    """Start one module-owned port process; ingress carries the envelope.

    The registry port_type is currently assumed to be the module directory
    name, making the target ``modules.<port_type>.port``. The frame itself is
    never placed in argv; the child reads its own ingress queue from Redis.

    The switch defers broadcasts before calling here because their HKEYS-only
    path has no resolved port_type yet. Reject direct misuse rather than ever
    constructing ``modules.None.port``.
    """
    if port_type_name is None:
        raise ValueError("cannot transmit without a resolved port_type")
    custody_read, custody_write = os.pipe()
    child_env = dict(os.environ)
    child_env.update({
        # Port log_record() calls use this pipe instead of stdout. Arbitrary
        # prints and tracebacks go to ports.log below and cannot corrupt the
        # switch's structured stream.
        "H_MESH_LOG_FILE": f"/proc/self/fd/{custody_write}",
        "H_MESH_LOG_QUIET": "1",
    })
    child_env.pop("H_MESH_LOG_FILE_AGENT_ONLY", None)
    try:
        with open(_port_diagnostic_path(), "ab", buffering=0) as diagnostics:
            subprocess.Popen(
                [sys.executable, "-m", f"modules.{port_type_name}.port", agent],
                stdout=diagnostics,
                stderr=subprocess.STDOUT,
                env=child_env,
                pass_fds=(custody_write,),
            )
    except Exception:
        os.close(custody_read)
        raise
    finally:
        os.close(custody_write)
    threading.Thread(
        target=_forward_port_custody,
        kwargs={"read_fd": custody_read, "agent": agent},
        name=f"port-custody-{agent}",
        daemon=True,
    ).start()

# ⚠ activity, presence and verification are NOT here. They observe agents; the
# watchdog owns them. What is left runs on the forwarding thread because it is
# the switch's own housekeeping — the window spool it tails into its own stdout,
# and the queues it writes.


class Switch:
    def __init__(
        self,
        r,
        *,
        pod: str,
        tenant: str,
        poll_seconds: int = 5,
        ingress_max: int = 300,
        kick: Callable[[str, str | None, dict], None] | None = None,
        frame_retry_attempts: int = FRAME_RETRY_ATTEMPTS,
    ):
        if ingress_max < 1:
            raise ValueError("ingress_max must be positive")
        if frame_retry_attempts < 1:
            raise ValueError("frame_retry_attempts must be positive")
        self.frame_retry_attempts = frame_retry_attempts
        # Ambiguous write outcomes (connection reset, timeout) by closed category.
        self.unknown_outcomes: dict[str, int] = {}
        self.r = r
        self.pod = pod
        self.tenant = tenant
        self.poll_seconds = poll_seconds
        self.ingress_max = ingress_max
        self.kick = kick
        self._offset = 0

    def _agents(self) -> set[str]:
        return members(self.r, pod=self.pod, tenant=self.tenant)

    @staticmethod
    def _record_popped(raw, sender: str) -> None:
        """Record removal before validating the untrusted frame."""
        candidate = header_record_fields(raw)
        stream_id = candidate.get("stream_id")
        correlation_id = candidate.get("correlation_id")
        destination = candidate.get("destination")
        _log_observation(
            "popped",
            stream_id=stream_id if isinstance(stream_id, str) else None,
            correlation_id=correlation_id if isinstance(correlation_id, str) else None,
            source=sender,
            destination=destination if isinstance(destination, str) else None,
        )

    def _dead_letter_full(
        self, sender: str, destination: str, raw, envelope: dict, depth: int
    ) -> None:
        self._dead_letter(sender, raw)
        _log_observation(
            "dead_lettered",
            stream_id=envelope.get("stream_id"),
            correlation_id=envelope.get("correlation_id"),
            source=envelope.get("l2", {}).get("source"),
            destination=destination,
            reason=(
                f"ingress full for destination {destination!r}: "
                f"depth {depth} has reached INGRESS_MAX {self.ingress_max}"
            ),
        )

    def _admit(
        self, destinations: list[str], raw
    ) -> tuple[bool, str | None, int | None]:
        """Apply shared atomic admission with this switch's configured bound."""
        return admit_ingress(
            self.r,
            pod=self.pod,
            tenant=self.tenant,
            destinations=destinations,
            raw=raw,
            limit=self.ingress_max,
        )

    def _kick(
        self,
        agent: str,
        port_type_name: str | None,
        envelope: dict,
        *,
        started_event: str = "kick_started",
        started_reason: str | None = None,
    ) -> bool:
        if port_type_name is None:
            _log_observation(
                "kick_skipped",
                stream_id=envelope.get("stream_id"),
                correlation_id=envelope.get("correlation_id"),
                source=envelope.get("l2", {}).get("source"),
                destination=agent,
                reason="port_type is unresolved; no delivery attempt started",
            )
            return False
        if self.kick is None:
            _log_observation(
                "kick_skipped",
                stream_id=envelope.get("stream_id"),
                correlation_id=envelope.get("correlation_id"),
                source=envelope.get("l2", {}).get("source"),
                destination=agent,
                reason="no delivery kick callback configured",
            )
            return False
        try:
            self.kick(agent, port_type_name, envelope)
        except Exception as exc:
            _log_observation(
                "kick_unknown",
                stream_id=envelope.get("stream_id"),
                correlation_id=envelope.get("correlation_id"),
                source=envelope.get("l2", {}).get("source"),
                destination=agent,
                reason=f"delivery kick outcome UNKNOWN after {exc}",
            )
            return True
        # A callback return proves only that the switch started a delivery
        # attempt; it does not claim that the edge reached or popped ingress.
        fields = dict(
            stream_id=envelope.get("stream_id"),
            correlation_id=envelope.get("correlation_id"),
            source=envelope.get("l2", {}).get("source"),
            destination=agent,
        )
        if started_reason is not None:
            fields["reason"] = started_reason
        _log_observation(started_event, **fields)
        return True

    def _reconcile_ingress(self) -> None:
        """Restart abandoned ingress once its prior delivery lease is gone."""
        for agent, port_type_name in sorted(
            member_types(self.r, pod=self.pod, tenant=self.tenant).items()
        ):
            if self.r.get(prefix(self.pod, self.tenant, agent, "paused")) is not None:
                continue
            ingress_key = prefix(self.pod, self.tenant, agent, "ingress")
            if self.r.llen(ingress_key) < 1:
                continue
            if self.r.get(delivery_lock_key(self.pod, self.tenant, agent)) is not None:
                continue
            raw = self.r.lindex(ingress_key, 0)
            if raw is None:
                continue
            try:
                envelope = parse_for_switch(raw)
            except EnvelopeError:
                envelope = {}
            self._kick(
                agent,
                port_type_name or None,
                envelope,
                started_event="kick_restarted",
                started_reason="non-empty ingress found without a delivery lease",
            )

    def _notify_broadcast_sender(
        self, *, sender: str, sender_type: str | None,
        envelope: dict, skipped: list[str],
    ) -> None:
        """Best-effort fact-only feedback at the initiating participant."""
        notice = build(
            "Message",
            "switch",
            sender,
            {
                "text": (
                    "Broadcast notice: no delivery attempt was started for "
                    + ", ".join(skipped)
                    + "."
                )
            },
            envelope.get("stream_id"),
            pod=self.pod,
            tenant=self.tenant,
        )
        raw = encode(notice)
        try:
            admitted, _, depth = self._admit([sender], raw)
        except Exception as exc:
            _emit_observation(
                "forward_unknown", notice,
                f"broadcast notice ingress write outcome UNKNOWN after {exc}",
            )
            return
        if not admitted:
            _emit_observation(
                "dead_lettered", notice,
                f"broadcast notice ingress full at depth {depth}",
            )
            return
        _emit_observation("forwarded", notice)
        self._kick(sender, sender_type, notice)

    def step(self, timeout: float | None = None) -> bool:
        agents = sorted(self._agents())
        if not agents:
            delay = self.poll_seconds if timeout is None else timeout
            if delay > 0:
                time.sleep(delay)
            return False
        self._offset %= len(agents)
        agents = agents[self._offset :] + agents[: self._offset]
        self._offset = (self._offset + 1) % len(agents)
        keys = [prefix(self.pod, self.tenant, agent, "egress") for agent in agents]
        item = self.r.blpop(keys, timeout=self.poll_seconds if timeout is None else timeout)
        if item is None:
            return False
        source_key, raw = item
        if isinstance(source_key, bytes):
            source_key = source_key.decode()
        sender = source_key.split(":")[-2]
        self._record_popped(raw, sender)
        return self._route(sender, raw)

    def _backoff_sleep(self, previous: float) -> float:
        delay = REDIS_BACKOFF_START if previous == 0.0 else min(previous * 2, REDIS_BACKOFF_CAP)
        time.sleep(delay)
        return delay

    def _retrying(self, op, *, idempotent: bool = False):
        """Run one post-pop Redis operation, retrying only when that is safe.

        A popped frame exists nowhere else, so a failure here decides its fate.
        Retried: errors Redis raised before executing the command (BUSY, LOADING,
        READONLY, TRYAGAIN, MASTERDOWN), and -- for pure reads -- any transient
        error, since repeating a read cannot duplicate anything. Not retried:
        connection/timeout on a write, whose outcome is unknown (at-most-once).
        After FRAME_RETRY_ATTEMPTS the caller dead-letters the frame.
        """
        delay = 0.0
        for attempt in range(1, self.frame_retry_attempts + 1):
            try:
                return op()
            except Exception as exc:
                category = transient_redis_category(exc)
                if category is None or not (
                    category in REJECTED_BEFORE_EXECUTION or idempotent
                ):
                    raise
                if attempt == self.frame_retry_attempts:
                    raise RetryExhausted(category, exc, attempt) from exc
                _emit_observation(
                    "error", {},
                    reason=(
                        f"frame operation rejected [{category}] "
                        f"attempt {attempt}/{self.frame_retry_attempts}; retrying"
                    ),
                )
                delay = self._backoff_sleep(delay)

    def _dead_letter(self, sender: str, raw) -> None:
        """Append a frame to the sender's dead list, retrying rejected commands."""
        key = prefix(self.pod, self.tenant, sender, "dead")
        try:
            self._retrying(lambda: self.r.rpush(key, raw))
        except RetryExhausted as exc:
            # Nothing is left to try: say so with the identity, then fail loudly.
            _log_observation(
                "dead_lettered",
                **{
                    **{k: v for k, v in header_record_fields(raw).items()
                       if k in ("stream_id", "correlation_id") and isinstance(v, str)},
                    "source": sender,
                    "reason": f"frame LOST: dead-letter write rejected [{exc.category}] "
                              f"through {exc.attempts} attempts",
                },
            )
            raise exc.cause

    def _note_unknown(self, exc: Exception) -> str:
        category = transient_redis_category(exc) or "other"
        self.unknown_outcomes[category] = self.unknown_outcomes.get(category, 0) + 1
        return category

    def _route(self, sender: str, raw) -> bool:
        try:
            return self._forward(sender, raw)
        except RetryExhausted as exc:
            reason = (
                f"redis rejected the command [{exc.category}] through "
                f"{exc.attempts} attempts; frame dead-lettered, not forwarded"
            )
            try:
                envelope = parse_for_switch(raw)
            except EnvelopeError:
                envelope = {}
            self._dead_letter(sender, raw)
            _emit_observation("dead_lettered", envelope, reason)
            return True

    def _forward(self, sender: str, raw) -> bool:
        try:
            envelope = parse_for_switch(raw)
        except EnvelopeError as exc:
            self._dead_letter(sender, raw)
            _emit_observation("dead_lettered", {}, str(exc))
            return True
        # The forwarding decision reads L2 and the registry only. L3 rides through
        # untouched for a future switch; this local switch never parses it.
        claimed_producer = envelope["l2"]["source"]
        if claimed_producer != sender:
            # The popped queue is the ingress port and therefore the attribution
            # source of truth. Correct rather than reject: rejecting a mismatch
            # would let a raw queue writer destroy another participant's traffic.
            envelope["l2"]["source"] = sender
            raw = stamp_source(raw, sender)
        if claimed_producer != sender:
            _emit_observation(
                "source_stamped",
                envelope,
                reason=f"claimed source {claimed_producer!r} stamped from egress sender {sender!r}",
            )
        try:
            raw = advance_hop(raw, envelope)
        except EnvelopeError as exc:
            self._dead_letter(sender, raw)
            _emit_observation("dead_lettered", envelope, str(exc))
            return True
        if envelope["ttl"] == 0:
            self._dead_letter(sender, raw)
            _emit_observation("dead_lettered", envelope, "ttl expired at forward")
            return True
        destination = envelope["l2"]["destination"]
        if destination == "all":
            recipient_types = self._retrying(
                lambda: member_types(self.r, pod=self.pod, tenant=self.tenant),
                idempotent=True,
            )
            sender_type = recipient_types.pop(sender, None)
            recipients = sorted(recipient_types)
            if not recipients:
                _emit_observation("forwarded", envelope, count=0)
                return True
            try:
                admitted, _, depth = self._retrying(
                    lambda: self._admit(recipients, raw)
                )
            except RetryExhausted:
                raise
            except Exception as exc:
                category = self._note_unknown(exc)
                _emit_observation(
                    "forward_unknown", envelope,
                    f"broadcast ingress write outcome UNKNOWN [{category}] after {exc}",
                )
                raise
            if not admitted:
                self._dead_letter_full(sender, "all", raw, envelope, depth)
                return True
            _emit_observation("forwarded", envelope, count=len(recipients))
            skipped = []
            for agent in recipients:
                if not self._kick(agent, recipient_types[agent] or None, envelope):
                    skipped.append(agent)
            if skipped:
                self._notify_broadcast_sender(
                    sender=sender, sender_type=sender_type,
                    envelope=envelope, skipped=skipped,
                )
            return True
        destination_type = self._retrying(
            lambda: port_type(
                self.r, pod=self.pod, tenant=self.tenant, agent=destination
            ),
            idempotent=True,
        )
        if destination_type is None:
            self._dead_letter(sender, raw)
            _emit_observation("dead_lettered", envelope, "destination is not in tenant registry")
            return True
        try:
            admitted, _, depth = self._retrying(
                lambda: self._admit([destination], raw)
            )
        except RetryExhausted:
            raise
        except Exception as exc:
            category = self._note_unknown(exc)
            _emit_observation(
                "forward_unknown", envelope,
                f"ingress write outcome UNKNOWN [{category}] after {exc}",
            )
            raise
        if not admitted:
            self._dead_letter_full(sender, destination, raw, envelope, depth)
            return True
        _emit_observation("forwarded", envelope)
        self._kick(destination, destination_type, envelope)
        return True

    def run(
        self,
        window_log_tailer: WindowLogTailer | None = None,
        retention_trimmer: RetentionTrimmer | None = None,
        maintenance_poll_seconds: float = 2.0,
    ) -> None:
        """Forward, with the switch's own housekeeping between blocking pops.

        ⚠ Each job gets its own try. One failing job used to take the other four
        down silently, and the record named only the exception class — from a
        five-job block that was close to undiagnosable.
        """
        next_maintenance = 0.0
        backoff = 0.0
        while True:
            now = time.monotonic()
            if now >= next_maintenance:
                agents = None
                try:
                    self._reconcile_ingress()
                except Exception as exc:
                    _emit_observation(
                        "error", {},
                        reason=f"ingress recovery pass failed: {type(exc).__name__}: {exc}",
                    )
                if retention_trimmer is not None:
                    try:
                        agents = self._agents()
                        retention_trimmer.poll(agents)
                    except Exception as exc:
                        _emit_observation(
                            "error", {},
                            reason=f"retention pass failed: {type(exc).__name__}: {exc}",
                        )
                if window_log_tailer is not None:
                    try:
                        window_log_tailer.poll()
                    except Exception as exc:
                        _emit_observation(
                            "error", {},
                            reason=f"window log pass failed: {type(exc).__name__}: {exc}",
                        )
                next_maintenance = now + maintenance_poll_seconds
            timeout = min(self.poll_seconds, max(0.1, next_maintenance - time.monotonic()))
            try:
                self.step(timeout=timeout)
                backoff = 0.0
            except Exception as exc:
                # redis-py reconnects on the next command. Keep this bare-nohup
                # daemon alive across a transient Redis fault (BUSY while a
                # script runs, LOADING, failover, a dropped link) with a capped,
                # doubling pause so a still-unavailable server is not hammered.
                # Everything else is a defect and propagates.
                category = transient_redis_category(exc)
                if category is None:
                    raise
                backoff = (
                    REDIS_BACKOFF_START if backoff == 0.0
                    else min(backoff * 2, REDIS_BACKOFF_CAP)
                )
                _emit_observation(
                    "error", {},
                    reason=(
                        f"forwarding pass failed: transient redis error "
                        f"[{category}] {type(exc).__name__}: {exc}; "
                        f"retrying in {backoff:g}s"
                    ),
                )
                time.sleep(backoff)


def main() -> None:
    from services.daemon_identity import require_daemon_identity

    require_daemon_identity()
    configure_logging()
    # Delivery kicks are fire-and-forget: this process never calls wait() or
    # poll() on their children. Ignoring SIGCHLD makes the kernel auto-reap
    # exited port processes instead of leaving one zombie per delivery.
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)

    r = redis.Redis.from_url(os.environ["REDIS_URL"])
    switch = Switch(
        r,
        pod=os.environ["POD"],
        tenant=os.environ["TENANT"],
        poll_seconds=int(os.environ.get("REGISTRY_POLL_SECONDS", "5")),
        ingress_max=int(os.environ.get("INGRESS_MAX", "300")),
        kick=transmission,
    )
    # Config for the same reason REGISTRY_POLL_SECONDS is: two offices can
    # legitimately trade feed latency against filesystem polling. A knob beside
    # an existing knob is consistency; a knob on its own would be speculation.
    switch.run(
        window_log_tailer=WindowLogTailer(
            r,
            pod=switch.pod,
            tenant=switch.tenant,
            max_bytes=int(os.environ.get("WINDOW_LOG_MAX_BYTES", str(8 * 1024 * 1024))),
        ),
        retention_trimmer=RetentionTrimmer(
            r,
            pod=switch.pod,
            tenant=switch.tenant,
            board_done_max=int(os.environ.get("BOARD_DONE_MAX", "500")),
            dead_max=int(os.environ.get("DEAD_MAX", "500")),
        ),
        maintenance_poll_seconds=float(os.environ.get("MAINTENANCE_POLL_SECONDS", "2")),
    )


if __name__ == "__main__":
    main()
