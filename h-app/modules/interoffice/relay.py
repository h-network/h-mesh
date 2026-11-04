"""Reply-only inter-office SSE relay with post-admission cursor checkpoints."""

import json
import logging
import random
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from core.channels import send
from core.keys import validate_segment
from lib.tls_context import TlsConfigError, build_context, resolve_ca_file
from modules.interoffice.store import AdmissionRedis, Office, RelayStateError, SEEN_LEDGER_LIMIT, Store, cursor_order

logger = logging.getLogger(__name__)
READ_TIMEOUT = 5.0
MAX_FRAME_BYTES = 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A bearer credential must never follow a redirect to another endpoint.
        return None


def sse_events(response, stop=None, on_keepalive=None):
    """Yield bounded, complete frames; comments never produce a checkpoint."""
    event, event_id, data = "message", None, []
    size = 0
    while True:
        if stop is not None and stop.is_set():
            return
        raw = response.readline(MAX_FRAME_BYTES + 1)
        if not raw:
            return  # An unterminated frame is not an event.
        size += len(raw)
        if size > MAX_FRAME_BYTES:
            raise RelayStateError("SSE frame exceeds limit")
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                yield event, event_id, "\n".join(data)
            event, event_id, data, size = "message", None, [], 0
            continue
        if line.startswith(":"):
            if line == ": keepalive" and not data and event_id is None and on_keepalive is not None:
                on_keepalive()
            continue
        key, sep, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if sep and key == "event":
            event = value
        elif sep and key == "id":
            event_id = value
        elif sep and key == "data":
            data.append(value)


class OfficeWorker:
    def __init__(
        self, store: Store, office: Office, *, stop: threading.Event | None = None,
        send_fn=send, mapping_wait: float = 2.0, read_timeout: float = READ_TIMEOUT,
    ):
        self.store, self.office = store, office
        self.stop = stop if stop is not None else threading.Event()
        self.send_fn, self.mapping_wait, self.read_timeout = send_fn, mapping_wait, read_timeout
        self.opener = None
        self.failure_state = None
        self.progress = False
        self.thread = threading.Thread(target=self.run, name=f"relay-{office.name}", daemon=True)

    def _opener(self):
        handlers = [_NoRedirect()]
        if urllib.parse.urlsplit(self.office.url).scheme == "https":
            handlers.append(urllib.request.HTTPSHandler(
                context=build_context(resolve_ca_file(self.office.ca_cert)),
            ))
        return urllib.request.build_opener(*handlers)

    def _failure(self, event, reason):
        state = event, reason
        if self.failure_state != state:
            if event == "http-error":
                logger.warning("relay office=%s event=http-error status=%s", self.office.name, reason)
            else:
                logger.warning("relay office=%s event=%s reason=%s", self.office.name, event, reason)
            self.failure_state = state

    def _connection_failure(self, exc):
        # Never format exceptions: URLs, certificate subjects and paths are
        # untrusted and can contain credentials. Only closed categories escape.
        if isinstance(exc, urllib.error.URLError):
            exc = exc.reason
        if isinstance(exc, TlsConfigError):
            self._failure("tls-error", "invalid-ca-file")
        elif isinstance(exc, ssl.SSLCertVerificationError):
            reason = {62: "hostname-mismatch", 64: "hostname-mismatch",
                      10: "expired-certificate", 9: "certificate-not-yet-valid"}.get(
                          getattr(exc, "verify_code", None), "untrusted-certificate")
            self._failure("tls-error", reason)
        elif isinstance(exc, ssl.SSLError):
            self._failure("tls-error", "tls-handshake")
        else:
            self._failure("disconnected", "connection-or-stream")

    def _admit(self, message: dict, cursor: str) -> bool:
        """Return only after admission, or a deliberate unsupported-kind rejection."""
        if message.get("kind") != "Message":
            logger.warning("relay office=%s event=rejected reason=unsupported-kind", self.office.name)
            return True
        payload = message.get("payload")
        try:
            agent = validate_segment(message.get("l2", {}).get("source"))
        except (KeyError, AttributeError):
            raise RelayStateError("invalid remote source") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            raise RelayStateError("invalid remote message")
        reply_id = message.get("in_reply_to")
        deadline = time.monotonic() + self.mapping_wait if reply_id is not None else 0
        while not self.stop.is_set():
            if not self.store.current(self.office):
                return False
            self.store.require_relay()
            target = self.store.reply_target(self.office, reply_id, agent) if reply_id is not None else None
            destination = target.sender if target is not None else None
            if destination is None and time.monotonic() < deadline:
                self.stop.wait(min(0.1, max(0, deadline - time.monotonic())))
                continue
            destination = destination or self.store.lead()
            if destination is None:
                self.stop.wait(0.25)
                continue
            admission = AdmissionRedis(self.store, self.office, message["stream_id"], cursor)
            local_id = self.send_fn(
                admission, pod=self.store.pod, tenant=self.store.tenant,
                source="relay", destination=destination, kind="Message",
                payload={"text": f"[from {self.office.name}/{agent}] {payload['text']}"},
                correlation_id=target.correlation_id if target is not None else None,
                module="interoffice",
            )
            if admission.admitted is not True:
                if admission.admitted is False:
                    logger.info("relay office=%s event=duplicate", self.office.name)
                    return True
                raise RelayStateError("local admission did not append")
            logger.info(
                "relay office=%s event=admitted destination=%s stream_id=%s",
                self.office.name, destination, local_id,
            )
            return True
        return False

    def receive(self, response, *, backfill_cursor: str | None = None) -> bool:
        """Commit only admitted/rejected entries; backfill never enqueues history."""
        remaining = backfill_cursor
        backfilled = 0
        def finish():
            nonlocal remaining
            if remaining is not None:
                if not self.store.finish_backfill(self.office, remaining):
                    raise RelayStateError("cooperation or cursor changed during backfill")
                remaining = None
                logger.info("relay office=%s event=legacy-backfill-complete", self.office.name)
        for event, event_id, data in sse_events(response, self.stop, on_keepalive=finish):
            if self.stop.is_set() or not self.store.current(self.office):
                return False
            if event == "error":
                raise RelayStateError("remote stream error")
            if event != "message":
                continue
            order = cursor_order(event_id)
            prior = self.store.cursor(self.office)
            historical = remaining is not None and order <= cursor_order(remaining)
            if remaining is not None and not historical:
                finish()
            if not historical and prior is not None and order <= cursor_order(prior):
                continue
            try:
                message = json.loads(data)
            except ValueError:
                raise RelayStateError("invalid SSE JSON") from None
            if not isinstance(message, dict) or message.get("cursor", event_id) != event_id:
                raise RelayStateError("invalid SSE message")
            if historical:
                backfilled += 1
                if backfilled > SEEN_LEDGER_LIMIT:
                    raise RelayStateError("legacy mailbox exceeds replay ledger bound")
                if message.get("kind") == "Message":
                    self.store.remember(self.office, message.get("stream_id"), event_id, remaining)
                if order == cursor_order(remaining):
                    finish()
                continue
            if message.get("kind") == "Message" and self.store.seen(
                self.office, message.get("stream_id"), event_id,
            ):
                logger.info("relay office=%s event=duplicate", self.office.name)
            elif not self._admit(message, event_id):
                return False
            if not self.store.checkpoint(self.office, event_id):
                return False
            self.progress = True
        return True

    def run(self):
        backoff = 1.0
        while not self.stop.is_set():
            self.progress = False
            try:
                if not self.store.current(self.office):
                    return
                cursor = self.store.cursor(self.office)
                backfill_cursor = self.store.backfill_cursor(self.office)
                path = f"/agents/{urllib.parse.quote(self.office.participant, safe='')}/messages/stream"
                if cursor and backfill_cursor is None:
                    path += "?after=" + urllib.parse.quote(cursor, safe="")
                req = urllib.request.Request(
                    self.office.url + path,
                    headers={"Authorization": "Bearer " + self.office.token, "Accept": "text/event-stream"},
                    method="GET",
                )
                # Rebuild from current files on reconnect, allowing repaired CA
                # material to recover without a daemon restart or weaker trust.
                opener = self.opener if self.opener is not None else self._opener()
                with opener.open(req, timeout=self.read_timeout) as response:
                    if response.headers.get_content_type() != "text/event-stream":
                        raise RelayStateError("invalid stream content type")
                    if self.failure_state is not None:
                        logger.info("relay office=%s event=reconnected", self.office.name)
                        self.failure_state = None
                    if not self.receive(response, backfill_cursor=backfill_cursor):
                        return
            except urllib.error.HTTPError as exc:
                # Only status is logged. Exception text/URL/headers/body are secret-bearing.
                self._failure("http-error", str(exc.code))
                exc.close()
            except Exception as exc:
                self._connection_failure(exc)
            if self.progress:
                backoff = 1.0
            if self.stop.wait(backoff + random.uniform(0, backoff * 0.2)):
                return
            backoff = min(backoff * 2, 30.0)


class InterofficeRelay:
    def __init__(self, store: Store, *, poll_seconds: float = 1.0, worker_factory=OfficeWorker):
        self.store, self.poll_seconds, self.worker_factory = store, poll_seconds, worker_factory
        self.stop = threading.Event()
        self.workers: dict[str, OfficeWorker] = {}
        self.known: dict[str, Office] = {}

    def reconcile(self):
        offices, invalid = self.store.snapshot()
        if invalid:
            logger.warning("relay event=invalid-config count=%d", invalid)
        for name, worker in list(self.workers.items()):
            if offices.get(name) != worker.office or not worker.thread.is_alive():
                worker.stop.set()
                worker.thread.join(timeout=READ_TIMEOUT + 1)
                if worker.thread.is_alive():
                    continue
                del self.workers[name]
        for name, office in offices.items():
            if name in self.workers:
                continue
            if name in self.known and self.known[name] != office:
                self.store.reset_cursor(office)
            self.known[name] = office
            worker = self.worker_factory(self.store, office)
            self.workers[name] = worker
            worker.thread.start()
        # Keep the previous configuration for removed names: if an alias is
        # re-added with changed credentials, never inherit its old cursor.

    def run_forever(self):
        try:
            while not self.stop.is_set():
                try:
                    self.reconcile()
                except Exception:
                    logger.warning("relay event=store-unavailable")
                self.stop.wait(self.poll_seconds)
        finally:
            for worker in self.workers.values():
                worker.stop.set()
            for worker in self.workers.values():
                worker.thread.join(timeout=READ_TIMEOUT + 1)
