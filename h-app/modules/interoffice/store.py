"""Redis records shared with the inter-office CLI; token values stay private."""

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from redis.exceptions import WatchError

from core.keys import incarnation_key, prefix, validate_segment
from core.registry import is_member, port_type

_TOKEN = re.compile(r"coop\.[0-9a-f]{16}\.[A-Za-z0-9_-]{43}\Z")
_CURSOR = re.compile(r"[0-9]{1,20}-[0-9]{1,20}\Z")
_REPLY_ID = re.compile(r"[0-9a-f]{32}\Z")


class RelayStateError(RuntimeError):
    """A closed error category, containing no stored or remote content."""


def text(value):
    return value.decode("utf-8") if isinstance(value, bytes) else value


def cursor_order(value: str) -> tuple[int, int]:
    if not isinstance(value, str) or not _CURSOR.fullmatch(value):
        raise RelayStateError("invalid cursor")
    pair = tuple(map(int, value.split("-")))
    if any(part > 2**64 - 1 for part in pair):
        raise RelayStateError("invalid cursor")
    return pair


@dataclass(frozen=True)
class Office:
    name: str
    url: str = field(repr=False)
    participant: str
    token: str = field(repr=False)


def parse_office(name, raw, token, *, allow_plaintext: bool) -> Office:
    try:
        name = validate_segment(text(name))
        record = json.loads(text(raw))
        if not isinstance(record, dict):
            raise ValueError
        url = record["url"]
        participant = validate_segment(record["participant"])
        token = text(token)
        if not isinstance(url, str) or any(ord(c) < 33 or ord(c) == 127 for c in url):
            raise ValueError
        parts = urlsplit(url)
        if (
            parts.scheme not in ("http", "https") or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment
            or (parts.scheme == "http" and not allow_plaintext)
        ):
            raise ValueError
        # Force parsing/validation of a malformed port without logging its URL.
        _ = parts.port
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise ValueError
        return Office(name, url.rstrip("/"), participant, token)
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise RelayStateError("invalid office configuration") from None


class Store:
    def __init__(self, r, *, pod: str, tenant: str, allow_plaintext: bool = False):
        self.r, self.pod, self.tenant = r, pod, tenant
        self.allow_plaintext = allow_plaintext
        self.offices_key = prefix(pod, tenant, resource="offices")
        self.tokens_key = prefix(pod, tenant, resource="offices.tokens")
        self.cursor_key = prefix(pod, tenant, resource="offices.cursor")

    def snapshot(self) -> tuple[dict[str, Office], int]:
        with self.r.pipeline(transaction=True) as pipe:
            records, tokens = pipe.hgetall(self.offices_key).hgetall(self.tokens_key).execute()
        tokens = {text(key): value for key, value in tokens.items()}
        offices = {}
        invalid = 0
        for name, record in records.items():
            try:
                office = parse_office(
                    name, record, tokens.get(text(name)), allow_plaintext=self.allow_plaintext,
                )
                offices[office.name] = office
            except (RelayStateError, UnicodeError):
                invalid += 1
        return offices, invalid

    def current(self, office: Office, *, pipe=None) -> bool:
        client = self.r if pipe is None else pipe
        raw = client.hget(self.offices_key, office.name)
        token = client.hget(self.tokens_key, office.name)
        try:
            return parse_office(
                office.name, raw, token, allow_plaintext=self.allow_plaintext,
            ) == office
        except RelayStateError:
            return False

    def cursor(self, office: Office) -> str | None:
        value = text(self.r.hget(self.cursor_key, office.name))
        if value is not None:
            cursor_order(value)
        return value

    def reset_cursor(self, office: Office) -> None:
        self.r.hdel(self.cursor_key, office.name)

    def checkpoint(self, office: Office, cursor: str) -> bool:
        """Never let an old worker restore a cursor after credentials rotate."""
        order = cursor_order(cursor)
        with self.r.pipeline() as pipe:
            while True:
                try:
                    pipe.watch(self.offices_key, self.tokens_key, self.cursor_key)
                    if not self.current(office, pipe=pipe):
                        pipe.unwatch()
                        return False
                    prior = text(pipe.hget(self.cursor_key, office.name))
                    if prior is not None and cursor_order(prior) >= order:
                        pipe.unwatch()
                        return True
                    pipe.multi()
                    pipe.hset(self.cursor_key, office.name, cursor)
                    pipe.execute()
                    return True
                except WatchError:
                    continue

    def sender(self, office: Office, reply_id: str, remote_agent: str) -> str | None:
        if not isinstance(reply_id, str) or not _REPLY_ID.fullmatch(reply_id):
            return None
        raw = self.r.get(prefix(self.pod, self.tenant, resource=f"xsent.s{reply_id}"))
        if raw is None:
            return None
        try:
            record = json.loads(text(raw))
            if record["office"] != office.name or record["agent"] != remote_agent:
                return None
            sender = validate_segment(record["sender"])
            incarnation = record.get("sender_incarnation")
            if not isinstance(incarnation, str) or not _REPLY_ID.fullmatch(incarnation):
                return None
            current = text(self.r.get(incarnation_key(self.pod, self.tenant, sender)))
            if current != incarnation or not is_member(
                self.r, pod=self.pod, tenant=self.tenant, agent=sender,
            ):
                return None
            return sender
        except (KeyError, TypeError, ValueError, UnicodeError):
            return None

    def lead(self) -> str | None:
        value = text(self.r.get(prefix(self.pod, self.tenant, resource="lead")))
        try:
            lead = validate_segment(value)
        except KeyError:
            return None
        return lead if is_member(self.r, pod=self.pod, tenant=self.tenant, agent=lead) else None

    def require_relay(self) -> None:
        if port_type(self.r, pod=self.pod, tenant=self.tenant, agent="relay") != "api":
            raise RelayStateError("relay participant is not enrolled as api")
