"""Redis records shared with the inter-office CLI; token values stay private."""

import hashlib
import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from redis.exceptions import WatchError

from core.keys import (
    incarnation_key, office_cursor_key, office_sent_key, office_tokens_key,
    offices_key, prefix, validate_segment,
)
from core.registry import is_member, port_type

_TOKEN = re.compile(r"coop\.[0-9a-f]{16}\.[A-Za-z0-9_-]{43}\Z")
_CURSOR = re.compile(r"[0-9]{1,20}-[0-9]{1,20}\Z")
_REPLY_ID = re.compile(r"[0-9a-f]{32}\Z")

# Four times modules.api.port.MAILBOX_MAXLEN (1000, approximate count trim).
# No time expiry: an idle API mailbox has no age limit either.
SEEN_LEDGER_LIMIT = 4_000

_ADMIT_ONCE = """
-- interoffice bounded replay ledger and local admission v1
if redis.call('HGET', KEYS[3], ARGV[1]) ~= ARGV[2]
   or redis.call('HGET', KEYS[4], ARGV[1]) ~= ARGV[3] then return -1 end
if ARGV[7] == 'remember' and redis.call('HGET', KEYS[6], ARGV[1]) ~= ARGV[9] then return -1 end
local ranked = redis.call('TYPE', KEYS[1])['ok']
local indexed = redis.call('TYPE', KEYS[2])['ok']
if (ranked ~= 'none' and ranked ~= 'zset')
   or (indexed ~= 'none' and indexed ~= 'hash') then
    return redis.error_reply('invalid relay ledger type')
end
local prior = redis.call('HGET', KEYS[2], ARGV[4])
if prior then
    -- Repeated identities at a newer remote cursor must remain recent.
    if ARGV[6] > prior then
        redis.call('ZREM', KEYS[1], prior)
        redis.call('ZADD', KEYS[1], 0, ARGV[6])
        redis.call('HSET', KEYS[2], ARGV[4], ARGV[6])
    end
    return 0
end
if ARGV[7] == 'check' then return 2 end
if ARGV[7] == 'enqueue' then
    local queue = redis.call('TYPE', KEYS[5])['ok']
    if queue ~= 'none' and queue ~= 'list' then
        return redis.error_reply('invalid relay egress type')
    end
    redis.call('RPUSH', KEYS[5], ARGV[8])
    redis.call('SET', KEYS[7], '1')
end
redis.call('ZADD', KEYS[1], 0, ARGV[6])
redis.call('HSET', KEYS[2], ARGV[4], ARGV[6])
local excess = redis.call('ZCARD', KEYS[1]) - tonumber(ARGV[5])
if excess > 0 then
    local oldest = redis.call('ZRANGE', KEYS[1], 0, excess - 1)
    for _, member in ipairs(oldest) do
        redis.call('HDEL', KEYS[2], string.sub(member, -32))
    end
    redis.call('ZREMRANGEBYRANK', KEYS[1], 0, excess - 1)
end
return 1
"""


class AdmissionRedis:
    """Delegate send's policy/envelope work; guard only its single RPUSH.

    The caller inspects ``admitted`` to distinguish a concurrent replay from
    a new append. No policy or envelope implementation is duplicated here.
    """
    def __init__(self, store, office, stream_id, cursor):
        self.store, self.office = store, office
        self.stream_id, self.cursor = stream_id, cursor
        self.admitted = None

    def __getattr__(self, name):
        return getattr(self.store.r, name)

    def rpush(self, key, *values):
        expected = prefix(self.store.pod, self.store.tenant, "relay", "egress")
        if key != expected or len(values) != 1:
            raise RelayStateError("unexpected relay admission write")
        self.admitted = self.store.enqueue_once(
            self.office, self.stream_id, self.cursor, values[0],
        )
        return int(self.admitted)


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


@dataclass(frozen=True)
class ReplyTarget:
    sender: str
    correlation_id: str | None


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
        self.offices_key = offices_key(pod, tenant)
        self.tokens_key = office_tokens_key(pod, tenant)
        self.cursor_key = office_cursor_key(pod, tenant)

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

    def ledger_keys(self, office: Office) -> tuple[str, str]:
        identity = json.dumps([office.name, office.url, office.participant], separators=(",", ":"))
        digest = hashlib.sha256(identity.encode()).hexdigest()[:32]
        return (
            prefix(self.pod, self.tenant, resource="relay-seen.o" + digest),
            prefix(self.pod, self.tenant, resource="relay-seen-index.o" + digest),
        )

    def ready_key(self, office: Office) -> str:
        suffix = self.ledger_keys(office)[0].rsplit(".", 1)[1]
        return prefix(self.pod, self.tenant, resource="relay-seen-ready." + suffix)

    def backfill_cursor(self, office: Office) -> str | None:
        with self.r.pipeline(transaction=True) as pipe:
            ready, cursor = pipe.exists(self.ready_key(office)).hget(self.cursor_key, office.name).execute()
        if ready or cursor is None:
            return None
        cursor = text(cursor)
        cursor_order(cursor)
        return cursor

    def finish_backfill(self, office: Office, committed: str) -> bool:
        """Only a still-current, unchanged committed cursor proves old admission."""
        ready = self.ready_key(office)
        with self.r.pipeline() as pipe:
            while True:
                try:
                    pipe.watch(self.offices_key, self.tokens_key, self.cursor_key, ready)
                    if not self.current(office, pipe=pipe) or text(pipe.hget(self.cursor_key, office.name)) != committed:
                        pipe.unwatch()
                        return False
                    pipe.multi()
                    pipe.set(ready, "1")
                    pipe.execute()
                    return True
                except WatchError:
                    continue

    def remember(self, office: Office, stream_id: str, cursor: str, committed: str) -> None:
        if cursor_order(cursor) > cursor_order(committed):
            raise RelayStateError("backfill entry exceeds committed cursor")
        self._admission(office, stream_id, cursor, mode="remember", committed=committed)

    def _admission(self, office: Office, stream_id: str, cursor: str, raw=None,
                   *, mode: str | None = None, committed: str = "") -> int:
        if not isinstance(stream_id, str) or not _REPLY_ID.fullmatch(stream_id):
            raise RelayStateError("invalid remote stream id")
        milliseconds, sequence = cursor_order(cursor)
        # Lexicographic members avoid IEEE-754 loss for full uint64 cursors.
        member = f"{milliseconds:020d}-{sequence:020d}:{stream_id}"
        with self.r.pipeline(transaction=True) as pipe:
            record, token = pipe.hget(self.offices_key, office.name).hget(self.tokens_key, office.name).execute()
        if parse_office(office.name, record, token, allow_plaintext=self.allow_plaintext) != office:
            raise RelayStateError("cooperation changed during admission")
        ranked, indexed = self.ledger_keys(office)
        result = self.r.eval(
            _ADMIT_ONCE, 7, ranked, indexed, self.offices_key, self.tokens_key,
            prefix(self.pod, self.tenant, "relay", "egress"), self.cursor_key, self.ready_key(office),
            office.name, record, token, stream_id, SEEN_LEDGER_LIMIT, member,
            mode or ("check" if raw is None else "enqueue"), "" if raw is None else raw, committed,
        )
        if result == -1:
            raise RelayStateError("cooperation or cursor changed during admission")
        return result

    def seen(self, office: Office, stream_id: str, cursor: str) -> bool:
        return self._admission(office, stream_id, cursor) == 0

    def enqueue_once(self, office: Office, stream_id: str, cursor: str, raw) -> bool:
        return self._admission(office, stream_id, cursor, raw) == 1

    def reply_target(self, office: Office, reply_id: str, remote_agent: str) -> ReplyTarget | None:
        """Bind routing and trace metadata to the same validated local receipt."""
        if not isinstance(reply_id, str) or not _REPLY_ID.fullmatch(reply_id):
            return None
        raw = self.r.get(office_sent_key(self.pod, self.tenant, reply_id))
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
            correlation_id = record.get("correlation_id")
            if not isinstance(correlation_id, str) or not _REPLY_ID.fullmatch(correlation_id):
                correlation_id = None
            return ReplyTarget(sender, correlation_id)
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
