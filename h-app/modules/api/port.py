"""API port: drain inbound envelopes into a participant's mailbox stream."""

import json
import os
import signal
import sys

import redis

from core.dispatch import delivery_lock
from core.envelope import EnvelopeError, parse, parse_for_switch
from core.keys import prefix
from core.logging import configure_logging, log_record
from lib.ingress_snapshot import snapshot_ingress
from lib.reply_correlation import is_valid_reply_id, was_delivered

MAILBOX_MAXLEN = 1000


def _record(event: str, envelope: dict, agent: str, reason: str | None = None) -> None:
    """Keep observation failures from changing mailbox custody."""
    try:
        log_record(
            "api",
            event,
            stream_id=envelope.get("stream_id"),
            correlation_id=envelope.get("correlation_id"),
            source=envelope.get("l2", {}).get("source"),
            destination=agent,
            reason=reason,
        )
    except Exception:
        pass


def _drop_untrustworthy_reply_correlation(r, *, pod: str, tenant: str, agent: str, envelope: dict) -> None:
    """Strip an in_reply_to that doesn't validate, before this envelope is
    ever stored in a client's mailbox.

    This is THE trust boundary for the field: core/envelope.py's parse()
    deliberately carries whatever the wire said, malformed or not (see the
    comment on its _validate_body) -- an optional field on the wire is not
    a reason to dead-letter an otherwise-good message. Here, it is a reason
    to drop just the field. Failing toward absent rather than toward wrong
    is the point: a client that reads no correlation behaves exactly as it
    did before this feature existed; a client that reads a wrong one would
    confidently mislabel a turn, which is worse than the bug this replaces.
    Format and provenance are logged as distinct reasons -- they are
    different failures, and only provenance means an agent claimed to
    answer something that was never actually delivered to it.
    """
    in_reply_to = envelope.get("in_reply_to")
    if in_reply_to is None:
        return
    if not is_valid_reply_id(in_reply_to):
        envelope.pop("in_reply_to", None)
        _record("reply_correlation_dropped", envelope, agent, reason="malformed in_reply_to")
        return
    source = envelope.get("l2", {}).get("source")
    if not source or not was_delivered(r, pod=pod, tenant=tenant, agent=source, stream_id=in_reply_to):
        envelope.pop("in_reply_to", None)
        _record(
            "reply_correlation_dropped", envelope, agent,
            reason=f"in_reply_to {in_reply_to!r} was never delivered to {source!r}",
        )


def deliver_api(*, r, pod: str, tenant: str, agent: str) -> None:
    """Deliver the current ingress snapshot to one API participant's inbox."""
    ingress_key = prefix(pod, tenant, agent=agent, resource="ingress")
    dead_key = prefix(pod, tenant, agent=agent, resource="dead")
    inbox_key = prefix(pod, tenant, agent=agent, resource="inbox")

    for raw in snapshot_ingress(r, ingress_key):
        try:
            envelope = parse(raw)
        except EnvelopeError as exc:
            r.rpush(dead_key, raw)
            try:
                header = parse_for_switch(raw)
            except EnvelopeError:
                header = {}
            _record("dead_lettered", header, agent, str(exc))
            continue

        _drop_untrustworthy_reply_correlation(r, pod=pod, tenant=tenant, agent=agent, envelope=envelope)
        _record("received", envelope, agent)
        r.xadd(
            inbox_key,
            {"envelope": json.dumps(envelope)},
            maxlen=MAILBOX_MAXLEN,
            approximate=True,
        )
        _record("opened", envelope, agent)


def main(argv: list[str] | None = None) -> None:
    # First thing in the process, and only in the process: this is the entry
    # point, so it is the one place allowed to set the root logger's level.
    configure_logging()
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("usage: python -m modules.api.port <agent>", file=sys.stderr)
        sys.exit(1)
    agent = args[0]
    pod = os.environ["POD"]
    tenant = os.environ["TENANT"]
    redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")

    r = redis.Redis.from_url(redis_url)
    with delivery_lock(r, pod=pod, tenant=tenant, agent=agent):
        paused_key = prefix(pod, tenant, agent=agent, resource="paused")
        if r.get(paused_key):
            return
        deliver_api(r=r, pod=pod, tenant=tenant, agent=agent)


if __name__ == "__main__":
    main()
