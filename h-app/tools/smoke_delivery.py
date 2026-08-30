"""Prove send -> switch -> receive and custody logging against real Redis."""

import io
import json
import os
import sys
from contextlib import redirect_stdout

import redis

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.channels import receive, send  # noqa: E402
from core.keys import prefix  # noqa: E402
from core.service import Switch  # noqa: E402


EXPECTED_EVENTS = [
    "sent",
    "popped",
    "forwarded",
    "kick_started",
    "received",
    "opened",
]


def main() -> None:
    url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    pod = os.environ.get("POD", "ci-delivery")
    tenant = os.environ.get("TENANT", "ci-delivery")
    sender = "smoke-sender"
    recipient = "smoke-recipient"
    payload = {"text": "real Redis round trip", "smoke": True}
    r = redis.Redis.from_url(url)
    r.ping()

    registry = prefix(pod, tenant, resource="registry")
    owned_keys = [
        prefix(pod, tenant, agent, resource)
        for agent in (sender, recipient)
        for resource in ("egress", "ingress", "dead", "unreplied", "acks")
    ]

    def cleanup() -> None:
        r.hdel(registry, sender, recipient)
        r.delete(*owned_keys)

    cleanup()
    try:
        r.hset(registry, mapping={sender: "tmux", recipient: "tmux"})
        opened = []
        kicks = []
        captured = io.StringIO()
        with redirect_stdout(captured):
            stream_id = send(
                r,
                pod=pod,
                tenant=tenant,
                source=sender,
                destination=recipient,
                payload=payload,
            )
            switch = Switch(
                r,
                pod=pod,
                tenant=tenant,
                kick=lambda agent, envelope: kicks.append(
                    (agent, envelope["stream_id"])
                ),
            )
            if not switch.step(timeout=1):
                raise AssertionError("switch did not forward the queued envelope")
            receive(
                r,
                pod=pod,
                tenant=tenant,
                agent=recipient,
                openers={"Message": opened.append},
                timeout=1,
            )

        records = [json.loads(line) for line in captured.getvalue().splitlines()]
        events = [record.get("event") for record in records]
        if events != EXPECTED_EVENTS:
            raise AssertionError(
                f"unexpected custody events: expected {EXPECTED_EVENTS!r}, got {events!r}"
            )
        if any(record.get("stream_id") != stream_id for record in records):
            raise AssertionError("custody records do not share the sent stream_id")
        if kicks != [(recipient, stream_id)]:
            raise AssertionError(f"unexpected kick callback: {kicks!r}")
        if len(opened) != 1 or opened[0].get("payload") != payload:
            raise AssertionError(f"payload did not round-trip: {opened!r}")
        if opened[0].get("l2") != {"source": sender, "destination": recipient}:
            raise AssertionError(f"address did not round-trip: {opened[0].get('l2')!r}")
        if (opened[0].get("ttl"), opened[0].get("hops")) != (15, 1):
            raise AssertionError("switch did not advance ttl/hops exactly once")
    finally:
        cleanup()

    print(
        "real Redis delivery smoke passed: "
        + " -> ".join(EXPECTED_EVENTS)
    )


if __name__ == "__main__":
    main()
