"""Run two TemplatePort instances through a real h-mesh switch."""

import io
import json
import os
import sys
from contextlib import redirect_stdout

import redis

H_APP = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, H_APP)

from core.service import Switch  # noqa: E402
from tools.templates.template_port import TemplatePort  # noqa: E402


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
    pod = os.environ.get("POD", "template-demo")
    tenant = os.environ.get("TENANT", "template-demo")
    r = redis.Redis.from_url(url)
    r.ping()

    sender = TemplatePort(r, pod=pod, tenant=tenant)
    recipient = TemplatePort(r, pod=pod, tenant=tenant)
    payload = {"text": "hello from a reusable port", "template": True}
    try:
        sender.register("template-sender", "example")
        recipient.register("template-recipient", "example")
        opened = []
        kicks = []
        captured = io.StringIO()
        with redirect_stdout(captured):
            stream_id = sender.send("template-recipient", payload)
            switch = Switch(
                r,
                pod=pod,
                tenant=tenant,
                kick=lambda agent, port_type, envelope: kicks.append(
                    (agent, port_type, envelope["stream_id"])
                ),
            )
            if not switch.step(timeout=1):
                raise AssertionError("switch did not forward the template envelope")
            recipient.receive({"Message": opened.append}, timeout=1, blocking=True)

        records = [json.loads(line) for line in captured.getvalue().splitlines()]
        events = [record.get("event") for record in records]
        if events != EXPECTED_EVENTS:
            raise AssertionError(f"unexpected custody events: {events!r}")
        if any(record.get("stream_id") != stream_id for record in records):
            raise AssertionError("custody records do not share the sent stream_id")
        if kicks != [("template-recipient", "example", stream_id)]:
            raise AssertionError(f"unexpected kick callback: {kicks!r}")
        if len(opened) != 1 or opened[0].get("payload") != payload:
            raise AssertionError(f"template payload did not round-trip: {opened!r}")
        if opened[0].get("l2") != {
            "source": "template-sender",
            "destination": "template-recipient",
        }:
            raise AssertionError(f"template address did not round-trip: {opened!r}")
    finally:
        recipient.cleanup()
        sender.cleanup()

    print("template port demo passed: " + " -> ".join(EXPECTED_EVENTS))


if __name__ == "__main__":
    main()
