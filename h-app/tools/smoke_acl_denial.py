"""Prove a disjoint-tag send is refused and logged against real Redis."""

import io
import json
import os
import sys
from contextlib import redirect_stdout

import redis

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.channels import send  # noqa: E402
from core.envelope import EnvelopeError  # noqa: E402
from core.keys import prefix  # noqa: E402
from core.policy import tags_key  # noqa: E402


def main() -> None:
    url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    pod = os.environ.get("POD", "ci-acl-denial")
    tenant = os.environ.get("TENANT", "ci-acl-denial")
    sender = "acl-sender"
    recipient = "acl-recipient"
    r = redis.Redis.from_url(url)
    r.ping()

    registry = prefix(pod, tenant, resource="registry")
    sender_tags = tags_key(pod, tenant, sender)
    recipient_tags = tags_key(pod, tenant, recipient)
    sender_egress = prefix(pod, tenant, sender, "egress")
    recipient_egress = prefix(pod, tenant, recipient, "egress")
    owned_keys = [sender_tags, recipient_tags, sender_egress, recipient_egress]

    def cleanup() -> None:
        r.hdel(registry, sender, recipient)
        r.delete(*owned_keys)

    cleanup()
    try:
        r.hset(registry, mapping={sender: "tmux", recipient: "tmux"})
        r.hset(sender_tags, "export", json.dumps(["alerts"]))
        r.hset(recipient_tags, "import", json.dumps(["work"]))

        captured = io.StringIO()
        with redirect_stdout(captured):
            try:
                send(
                    r,
                    pod=pod,
                    tenant=tenant,
                    source=sender,
                    destination=recipient,
                    payload={"text": "must be refused"},
                )
            except EnvelopeError as exc:
                refusal = exc
            else:
                raise AssertionError("disjoint policy tags did not refuse send")

        expected_reason = (
            f"policy denied {sender!r} -> {recipient!r}: "
            "no shared export/import tag"
        )
        if str(refusal) != expected_reason:
            raise AssertionError(f"unexpected refusal: {refusal}")
        if r.llen(sender_egress) != 0 or r.llen(recipient_egress) != 0:
            raise AssertionError("policy-refused send wrote an egress queue")

        records = [json.loads(line) for line in captured.getvalue().splitlines()]
        if len(records) != 1:
            raise AssertionError(f"expected one refusal record, got {records!r}")
        record = records[0]
        expected_fields = {
            "event": "send_refused",
            "source": sender,
            "destination": recipient,
            "reason": expected_reason,
        }
        actual_fields = {field: record.get(field) for field in expected_fields}
        if actual_fields != expected_fields:
            raise AssertionError(
                f"unexpected refusal record: expected {expected_fields!r}, "
                f"got {actual_fields!r}"
            )
        if "stream_id" in record:
            raise AssertionError("pre-construction refusal must not invent a stream_id")
    finally:
        cleanup()

    print("real Redis ACL denial smoke passed: send_refused, no enqueue")


if __name__ == "__main__":
    main()
