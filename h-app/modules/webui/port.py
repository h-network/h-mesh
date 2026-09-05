"""webui port: relay Progress/Message envelopes to connected browser tabs.

Registered directly in the tenant registry (port_type ``webui``), the same
way ``claude_sdk`` is -- no ``office hire`` path needed yet (see that
module's own README for the same gap). Delivery itself does nothing but
append each received envelope onto this agent's own "inbox" Redis Stream --
the exact mailbox shape ``modules/api/port.py``'s ``deliver_api`` already
writes (same key, same ``{"envelope": json.dumps(envelope)}`` field) -- so
the browser-facing routes this ships with (``modules/webui/routes.py``,
mounted onto the already-running ``api`` service rather than a second HTTP
daemon) reuse ``lib/sse_stream.py``'s poll/keepalive machinery unchanged.

Mounting onto the existing api service, instead of a standalone
``services/webui.py`` daemon, is deliberate: ``services/daemons.py``'s own
module docstring documents watchdog and session both shipping once as a
console script nothing in the documented start path ever actually invoked
-- a new always-forgettable daemon is a known, previously-real risk here,
not a hypothetical one, and this ticket does not need a second process to
solve "relay to a browser tab" when one HTTP server already exists.

Any envelope kind other than Progress/Message is out of scope and is
dead-lettered by ``core.channels``'s own "unknown kind" handling, same as
every other port module -- nothing webui-specific to build for that.
``tools/smoke_webui.py`` proves this directly, using a ``Progress`` envelope
sent to a ``claude_sdk`` agent (which has no ``Progress`` opener) as the
concrete vehicle.
"""

from __future__ import annotations

import json
import os
import signal
import sys

import redis

from core.channels import receive
from core.dispatch import delivery_lock
from core.keys import prefix
from core.logging import configure_logging, log_record

MAILBOX_MAXLEN = 1000


def _relay(r, pod: str, tenant: str, agent: str, envelope: dict) -> None:
    inbox_key = prefix(pod, tenant, agent=agent, resource="inbox")
    r.xadd(
        inbox_key,
        {"envelope": json.dumps(envelope)},
        maxlen=MAILBOX_MAXLEN,
        approximate=True,
    )
    log_record(
        "webui", "relayed",
        stream_id=envelope.get("stream_id"),
        correlation_id=envelope.get("correlation_id"),
        source=envelope.get("l2", {}).get("source"),
        destination=agent,
    )


def deliver_webui(
    r,
    pod: str,
    tenant: str,
    agent: str,
    timeout: int = 0,
    blocking: bool = False,
    **kwargs,
) -> None:
    """Drain one agent's ingress, relaying every Progress/Message it receives."""
    openers = {
        "Progress": lambda env: _relay(r, pod, tenant, agent, env),
        "Message": lambda env: _relay(r, pod, tenant, agent, env),
    }

    receive(
        r,
        pod=pod,
        tenant=tenant,
        agent=agent,
        openers=openers,
        timeout=timeout,
        blocking=blocking,
        module="webui",
    )


def main(argv: list[str] | None = None) -> None:
    # First thing in the process, and only in the process: this is the entry
    # point, so it is the one place allowed to set the root logger's level.
    configure_logging()
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("usage: python -m modules.webui.port <agent>", file=sys.stderr)
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
        deliver_webui(r, pod=pod, tenant=tenant, agent=agent)


if __name__ == "__main__":
    main()
