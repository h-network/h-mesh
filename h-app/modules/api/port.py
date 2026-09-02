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


def _dead_letter_reason(exc: EnvelopeError) -> str:
    """A safe, closed reason for a rejected envelope -- never str(exc).

    core/envelope.py's parse() raises exactly one exception class,
    EnvelopeError, for every rejection reason -- but several of its raise
    sites (_segment, _address) interpolate the remote value itself
    (`{value!r}`) directly into the message, e.g. "invalid agent name:
    'whatever the wire said'". str(exc) is therefore remote-influenced by
    construction, same shape as the telegram client and Watchdog leaks:
    neither an exception's message nor a hostile class's own __name__ can
    be trusted just because it reached a `raise` our code wrote. Nothing
    here is derived from the exception object; "malformed envelope" is the
    only category, because parse() gives us no safe, closed set of
    sub-types to branch on the way Watchdog's caught exceptions did -- one
    literal is the correct amount of information to carry when the object
    itself cannot be trusted for more. The rejected raw bytes are still
    preserved verbatim in the dead-letter queue for anyone who deliberately
    goes looking; only the passive log line is closed to this content.
    """
    return "malformed envelope"


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

    Presence is checked before the value, deliberately: a wire frame with
    `"in_reply_to": null` (or any other malformed-but-present value) is
    PRESENT and untrustworthy, not absent -- `envelope.get(...) is None`
    would conflate the two and let a literal null through to storage.
    is_valid_reply_id already rejects None, "", and non-strings, so once
    presence is checked correctly it collapses null/empty/wrong-type into
    the same "malformed" branch as any other bad value.

    Provenance is checked against `agent` (this deliver_api call's own
    destination -- the API client whose mailbox this is), not just against
    "was this id ever delivered to the replying agent, from anywhere".
    Without that binding, an agent talked to by two different API clients
    could claim in_reply_to for a message one of them sent while replying
    to the *other*, and the wrong client would get a confident, wrong
    correlation -- see lib/reply_correlation.py.

    was_delivered can return None (could not verify, e.g. storage
    unreachable) as well as False (verified absent/mismatched). Both drop
    the field -- fail-safe either way -- but they are logged as distinct
    reasons: reporting an infrastructure outage as "was never delivered"
    would be a different, false claim about what happened.
    """
    if "in_reply_to" not in envelope:
        return
    in_reply_to = envelope["in_reply_to"]
    if not is_valid_reply_id(in_reply_to):
        envelope.pop("in_reply_to", None)
        _record("reply_correlation_dropped", envelope, agent, reason="malformed in_reply_to")
        return
    reply_source = envelope.get("l2", {}).get("source")
    if not reply_source:
        envelope.pop("in_reply_to", None)
        _record("reply_correlation_dropped", envelope, agent, reason="in_reply_to present but reply has no l2 source")
        return
    verdict = was_delivered(r, pod=pod, tenant=tenant, agent=reply_source, stream_id=in_reply_to, source=agent)
    if verdict is True:
        return
    envelope.pop("in_reply_to", None)
    # Closed literals only -- never the id or either agent name interpolated
    # into free text. is_valid_reply_id bounds SHAPE (32 lowercase hex), not
    # provenance: a remote sender picks the bytes freely within that shape,
    # so a syntactically valid in_reply_to is still remote data by origin,
    # same predicate as a malformed one (reviewer's finding against the
    # first version of this fix). reply_source and agent are already in
    # _record's dedicated source/destination fields below -- repeating them
    # in `reason` adds no diagnostic value, only a second copy of the same
    # remote-content-in-free-text problem this ticket exists to close.
    if verdict is None:
        reason = "in_reply_to provenance unavailable (storage unreachable)"
    else:
        reason = "in_reply_to was never delivered to the claimed source"
    _record("reply_correlation_dropped", envelope, agent, reason=reason)


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
            _record("dead_lettered", header, agent, _dead_letter_reason(exc))
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
