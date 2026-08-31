"""API port: drain inbound envelopes into a participant's mailbox stream."""

import json

from core.envelope import EnvelopeError, parse, parse_for_switch
from core.keys import prefix
from core.logging import log_record
from lib.ingress_snapshot import snapshot_ingress

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

        _record("received", envelope, agent)
        r.xadd(
            inbox_key,
            {"envelope": json.dumps(envelope)},
            maxlen=MAILBOX_MAXLEN,
            approximate=True,
        )
        _record("opened", envelope, agent)
