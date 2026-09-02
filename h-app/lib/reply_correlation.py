"""Delivery provenance for opt-in reply correlation.

Answers exactly one question: was this stream_id actually delivered to this
agent, recently? Nothing here knows about turns, conversations, or replies --
that meaning is applied by the caller. An opener records a delivery; a
receipt-side validator checks a claimed `in_reply_to` against it before that
claim is ever stored or surfaced to a client. Deliberately not the same key
watchdog's `mark_delivery_pending` writes (modules/tmux/port.py): that one is
gated to verifiable CLIs for liveness verification, a different question,
and coupling this to it would tie an unrelated concern to its bookkeeping.
"""

import re

from core.keys import prefix

DELIVERED_MAXLEN = 200
_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def is_valid_reply_id(value: object) -> bool:
    """Format check only: a well-formed 32-hex-char stream_id."""
    return isinstance(value, str) and bool(_ID_RE.match(value))


def record_delivered(r, *, pod: str, tenant: str, agent: str, stream_id: str) -> None:
    """Remember that `stream_id` was delivered to `agent`, bounded to the
    most recent DELIVERED_MAXLEN ids so this never grows without limit.

    Best-effort, same policy as modules/tmux/port.py's mark_delivery_pending
    and modules/api/port.py's _record: a bookkeeping write failure here must
    never fail the delivery it's recording. Every opener (tmux's
    message_opener, openshell's _reply) calls this inline, synchronously,
    in the same path that already committed the delivery -- a Redis hiccup
    on this call is not a reason to dead-letter a message that already
    landed.
    """
    if not is_valid_reply_id(stream_id):
        return
    try:
        ids_key = prefix(pod, tenant, agent=agent, resource="delivered")
        order_key = prefix(pod, tenant, agent=agent, resource="delivered.order")
        r.sadd(ids_key, stream_id)
        r.rpush(order_key, stream_id)
        while r.llen(order_key) > DELIVERED_MAXLEN:
            oldest = r.lpop(order_key)
            if oldest is None:
                break
            oldest = oldest.decode() if isinstance(oldest, bytes) else oldest
            r.srem(ids_key, oldest)
    except Exception:
        pass


def was_delivered(r, *, pod: str, tenant: str, agent: str, stream_id: str) -> bool:
    """Whether `stream_id` was recorded as delivered to `agent` recently.

    Fails toward False, not toward raising: this is called from
    modules/api/port.py's deliver_api, inline, per envelope, in a loop --
    a Redis error here must degrade to "can't verify, so don't trust it"
    (which the caller already treats as "drop the field"), not abort the
    rest of that delivery batch.
    """
    if not is_valid_reply_id(stream_id):
        return False
    try:
        ids_key = prefix(pod, tenant, agent=agent, resource="delivered")
        return bool(r.sismember(ids_key, stream_id))
    except Exception:
        return False
