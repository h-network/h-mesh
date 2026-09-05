"""WebSocket relay port for live mesh envelopes."""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import redis
from websockets.sync.server import serve

from core.channels import receive
from core.dispatch import delivery_lock
from core.keys import prefix
from core.logging import configure_logging


class WebSocketRelay:
    """Thread-safe collection of connected WebSocket clients.

    The relay deliberately sends the complete envelope.  In particular it
    doesn't duplicate the producer's knowledge of the Progress payload, and
    browser consumers retain routing and correlation metadata.
    """

    def __init__(self) -> None:
        self._clients: set[object] = set()
        self._lock = threading.Lock()

    @contextmanager
    def connected(self, client: object) -> Iterator[None]:
        with self._lock:
            self._clients.add(client)
        try:
            yield
        finally:
            with self._lock:
                self._clients.discard(client)

    def push(self, envelope: dict) -> None:
        encoded = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False)
        with self._lock:
            clients = tuple(self._clients)
        for client in clients:
            try:
                client.send(encoded)
            except Exception:
                # A tab can disappear between the snapshot and send. Remove it
                # without failing delivery to every other connected tab.
                with self._lock:
                    self._clients.discard(client)

    def handler(self, websocket) -> None:
        with self.connected(websocket):
            # Browser messages have no meaning on this push-only port. Iterating
            # keeps the handler alive and notices an orderly disconnect.
            for _ in websocket:
                pass


def deliver_webui(
    r,
    pod: str,
    tenant: str,
    agent: str,
    relay: WebSocketRelay,
    timeout: int = 0,
    blocking: bool = False,
    **kwargs,
) -> None:
    """Drain supported envelopes and relay them to every connected tab."""
    receive(
        r,
        pod=pod,
        tenant=tenant,
        agent=agent,
        openers={"Message": relay.push, "Progress": relay.push},
        timeout=timeout,
        blocking=blocking,
        module="webui",
    )


def _delivery_loop(r, pod: str, tenant: str, agent: str, relay: WebSocketRelay) -> None:
    paused_key = prefix(pod, tenant, agent=agent, resource="paused")
    while True:
        with delivery_lock(r, pod=pod, tenant=tenant, agent=agent):
            if not r.get(paused_key):
                deliver_webui(
                    r, pod=pod, tenant=tenant, agent=agent, relay=relay,
                    timeout=1, blocking=True,
                )


def main(argv: list[str] | None = None) -> None:
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
    host = os.environ.get("WEBUI_HOST", "127.0.0.1")
    port = int(os.environ.get("WEBUI_PORT", "8765"))

    r = redis.Redis.from_url(redis_url)
    relay = WebSocketRelay()
    with serve(relay.handler, host, port) as server:
        # Bind before taking any envelope custody. If another long-lived
        # instance already owns this address, a switch kick exits here and the
        # established instance remains the only consumer.
        worker = threading.Thread(
            target=_delivery_loop,
            args=(r, pod, tenant, agent, relay),
            name=f"webui-delivery-{agent}",
            daemon=True,
        )
        worker.start()
        server.serve_forever()


if __name__ == "__main__":
    main()
