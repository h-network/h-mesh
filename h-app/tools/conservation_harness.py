"""Conservation harness for the custody chain -- NOT A FIX.

The custody chain: popped -> forwarded -> kick_started -> received -> opened,
with dead_lettered as the terminal alternative. This script is a MEASURING
INSTRUMENT, run deliberately against a real Redis, never wired into the
pytest suite. See tickets 017c52bf (built against main) and ba69f7e2 (this
follow-up round, rebuilt against switch-agent's receive-durable-custody
branch): a red suite on main helps nobody and trains people to ignore red,
so a scenario that finds loss reports it as text on stdout (and a nonzero
exit code for scripting), never as a failing test.

⚠ THE PROPERTY IS CONSERVATION BY IDENTITY, NOT BY COUNT. A conservation
check usually goes wrong exactly where totals balance while a specific
message is silently replaced by a different one -- "the same number came
out as went in" is satisfied by an unrelated envelope arriving. Every
scenario below tracks named envelopes by their own stream_id and asserts
"envelope X ended HERE" (or, when conservation fails, "envelope X ended
NOWHERE, and here is where I looked"), never a bare count.

⚠ SHAPE-DETECTED, NOT VERSION-PINNED. Round one built this against main's
receive() (BLPOP, nothing durable, then _open_received). Running the SAME
worker against switch-agent's fix -- BLMOVE into a durable per-agent
`processing` list, dead-lettering via one atomic Lua eval -- would have
tested a stale assumption and reported a verdict about code it never
touched. This version inspects _open_received's own signature at runtime
to pick the correct failure-injection shape automatically ("legacy": no
durable claim at all; "processing": a claim, but a single opened/dead
outcome; "phases": processing -> opening -> opened/dead, with a tenant
unresolved sink for outcomes that can't be safely replayed or discarded)
-- so this stays correct as the implementation evolves instead of
silently drifting the way a hardcoded worker would.

Run: REDIS_URL=redis://127.0.0.1:6379/0 python tools/conservation_harness.py
"""

import inspect
import io
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from contextlib import redirect_stdout

import redis

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import channels as _channels  # noqa: E402
from core.channels import _open_received, receive, send  # noqa: E402
from core.envelope import HEADER_WIDTH, build, encode, parse_for_switch  # noqa: E402
from core.keys import prefix  # noqa: E402
from core.service import Switch  # noqa: E402

try:
    from core.keys import receive_processing_key
except ImportError:
    receive_processing_key = None  # not present on current main

try:
    from core.keys import receive_opened_key, receive_opening_key, receive_unresolved_key
except ImportError:
    receive_opened_key = receive_opening_key = receive_unresolved_key = None  # pre-phases shape

try:
    from core.keys import receive_undeliverable_key
except ImportError:
    receive_undeliverable_key = None  # pre-retirement-conservation shape

try:
    from core.keys import retired_inbox_key
except ImportError:
    retired_inbox_key = None  # pre-inbox-conservation shape

try:
    from lib.agentlifecycle.lifecycle import stop_agent
except ImportError:
    stop_agent = None  # not present on this tree


def _redis_url() -> str:
    return os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")


def _default_pod_tenant() -> tuple[str, str]:
    """A unique namespace per invocation by default -- reviewer's exact
    finding at c2800e6: a fixed default namespace ("ci-conservation" for
    both pod and tenant) meant two concurrent invocations against the same
    Redis wrote, read, and cleaned up the SAME keys. The instrument's
    result became non-attributable exactly when it mattered most --
    concurrent, multi-agent use is the normal case in this office, not an
    edge case. Reproduced and confirmed before this fix existed: two
    copies running concurrently produced "receive lost ownership" and
    "worker never reported CLAIMED" errors that were namespace collisions,
    not custody defects.

    POD/TENANT set explicitly in the environment are honored as given --
    an acknowledged advanced option for a caller who deliberately wants a
    fixed, inspectable namespace (comparing two runs by hand, for
    instance) and accepts the collision risk that comes with sharing it.
    Each dimension is independent: setting only one still gets a random
    component on the other.
    """
    token = uuid.uuid4().hex[:12]
    pod = os.environ.get("POD") or f"ci-conservation-{token}"
    tenant = os.environ.get("TENANT") or f"ci-conservation-{token}"
    return pod, tenant


def _connect() -> redis.Redis:
    r = redis.Redis.from_url(_redis_url())
    try:
        r.ping()
    except Exception as exc:
        print(
            f"error: cannot reach Redis at {_redis_url()} ({exc}) -- this "
            "harness refuses to certify anything without a real, reachable "
            "Redis, since the property under test is about real durable "
            "writes, not a fake's approximation of them.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return r


def _custody_shape() -> str:
    """'phases' if receive() splits safely-replayable processing custody
    from possibly-effectful opening custody, with a tenant unresolved
    sink; 'processing' if it durably claims into a per-agent processing
    list but has only one opened/dead outcome; 'legacy' if it pops
    directly with nothing durable between the pop and _open_received
    (current main). Detected by inspecting _open_received's own
    signature -- structural, not a branch name or version string, so
    this stays correct automatically as the implementation evolves."""
    params = inspect.signature(_channels._open_received).parameters
    if "opening_key" in params:
        return "phases"
    if "processing_key" in params:
        return "processing"
    return "legacy"


def _processing_key(pod: str, tenant: str, agent: str) -> str:
    if receive_processing_key is not None:
        return receive_processing_key(pod, tenant, agent)
    return prefix(pod, tenant, agent, "processing")


def _opening_key(pod: str, tenant: str, agent: str) -> str:
    return receive_opening_key(pod, tenant, agent)


def _opened_key(pod: str, tenant: str, agent: str) -> str:
    return receive_opened_key(pod, tenant, agent)


def _unresolved_key(pod: str, tenant: str) -> str:
    return receive_unresolved_key(pod, tenant)


def _decode_evidence_envelope(record: dict):
    """The envelope field of a tenant evidence record (unresolved or
    undeliverable), decoded the same way `office`'s own reader does: hex
    when the record says `encoding: "hex"` (switch-agent's stop_agent Lua
    script -- Lua strings can hold arbitrary bytes that don't always
    round-trip through JSON's own string encoding, so it hex-encodes
    rather than risk that), otherwise the plain text core.channels.py's
    own crash-recovery path writes directly. Getting this wrong silently
    drops every hex-encoded record from every stream-id lookup below --
    found and fixed here, not assumed: an earlier version of this
    function always treated `envelope` as plain text and would have
    reported every stop-retirement record as absent."""
    envelope_field = record["envelope"]
    if record.get("encoding") == "hex":
        return bytes.fromhex(envelope_field)
    return envelope_field


def _unresolved_stream_ids(r, pod: str, tenant: str) -> dict[str, dict]:
    """Parse every record currently in the tenant unresolved sink, keyed by
    the stream_id of the envelope it names -- the same read receive()'s
    own successor-recovery path and `office unresolved` both do: an
    {agent, reason, envelope} JSON record whose `envelope` field is the
    original encoded raw frame (plain text or hex, see
    _decode_evidence_envelope)."""
    result = {}
    for stored in r.lrange(_unresolved_key(pod, tenant), 0, -1):
        try:
            record = json.loads(stored.decode() if isinstance(stored, bytes) else stored)
            header = parse_for_switch(_decode_evidence_envelope(record))
        except Exception:
            continue
        result[header["stream_id"]] = record
    return result


def _undeliverable_stream_ids(r, pod: str, tenant: str) -> dict[str, dict]:
    """Parse every record in the tenant undeliverable sink -- the same
    shape as unresolved, but for identities proven never to have begun
    (moved there when their destination retired before opening)."""
    if receive_undeliverable_key is None:
        return {}
    result = {}
    for stored in r.lrange(receive_undeliverable_key(pod, tenant), 0, -1):
        try:
            record = json.loads(stored.decode() if isinstance(stored, bytes) else stored)
            header = parse_for_switch(_decode_evidence_envelope(record))
        except Exception:
            continue
        result[header["stream_id"]] = record
    return result


def _stream_id_occurrences(raw_records: list, is_evidence_wrapper: bool) -> list[tuple[str, dict | None]]:
    """Every occurrence of a stream_id parsed from `raw_records`, WITH
    duplicates preserved -- never a dict/set keyed by stream_id.

    Reviewer's finding against an earlier version of this file:
    `_undeliverable_stream_ids`/`_unresolved_stream_ids` (dicts) and
    `_stream_ids_in` (a set) each silently collapse two occurrences of the
    same identity into one. A scenario built on those can only prove "at
    least one parseable record exists in the expected sink" -- never
    "exactly once" -- and a MISATTRIBUTED duplicate followed by a
    correctly-attributed one collapses to the good record, hiding both the
    duplication and the bad one. This is the only shape that can actually
    support an exactly-once claim: keep every parse, one entry per raw
    record, so counting occurrences of an identity means something.

    `is_evidence_wrapper` -- True for undeliverable/unresolved (each raw is
    a JSON evidence record whose `envelope` field, hex-or-plain, wraps the
    real frame -- see `_decode_evidence_envelope`), False for a raw custody
    list like `opened` (each raw IS the encoded frame directly). Returns
    `(stream_id, record_or_none)` pairs -- `record` is the evidence dict
    for the wrapper case, `None` otherwise. A record this harness cannot
    parse contributes no occurrence at all (never miscounted as present).
    """
    occurrences = []
    for raw in raw_records:
        try:
            if is_evidence_wrapper:
                record = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                header = parse_for_switch(_decode_evidence_envelope(record))
            else:
                record = None
                header = parse_for_switch(raw)
        except Exception:
            continue
        occurrences.append((header["stream_id"], record))
    return occurrences


def _is_hex_string(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return True


_RETIRED_INBOX_RECOGNIZED_KEYS = frozenset({"agent", "reason", "entry_id", "encoding", "fields"})


def _matches_recognized_inbox_shape(record: dict) -> bool:
    """True ONLY if `record` matches the EXACT closed schema
    `_REMOVE_MEMBERSHIP_AND_OWN_LEAD_LUA`'s inbox-conservation branch
    actually produces -- reviewer's finding, the fifth false-clean in this
    instrument: checking key PRESENCE (`entry_id` and `fields` exist,
    `envelope` doesn't) is not the same as validating the schema those
    keys claim. Both of reviewer's reproductions have the right KEYS and
    would have passed a presence-only check; neither has the right
    VALUES, and this function rejects both:
        {"entry_id": [], "fields": "not-pairs"}
        {"entry_id": "1-0", "fields": [], "encoding": "plain",
         "agent": 7, "reason": null}
    Requires: the top-level key set is EXACTLY {agent, reason, entry_id,
    encoding, fields} (no more, no fewer); `agent`, `reason`, `entry_id`
    are strings; `encoding` is the literal string `"hex"`; `fields` is a
    list where every element is a two-element list of hex strings (the
    `[field_hex, value_hex]` pairs the Lua's `hex()` helper produces).
    """
    if set(record.keys()) != _RETIRED_INBOX_RECOGNIZED_KEYS:
        return False
    if not isinstance(record["agent"], str):
        return False
    if not isinstance(record["reason"], str):
        return False
    if not isinstance(record["entry_id"], str):
        return False
    if record["encoding"] != "hex":
        return False
    fields = record["fields"]
    if not isinstance(fields, list):
        return False
    for pair in fields:
        if not (isinstance(pair, list) and len(pair) == 2):
            return False
        if not (_is_hex_string(pair[0]) and _is_hex_string(pair[1])):
            return False
    return True


def _retired_inbox_occurrences(raw_records: list) -> tuple[list[tuple[str, dict]], list[str]]:
    """Read, classify, and validate every record in the tenant retired-inbox
    sink -- never left unread, and never recognized by key presence alone.

    Reviewer's findings, in order, each closing the gap the previous one
    left: (1) an earlier version reasoned retired-inbox records can never
    carry a `stream_id` and used that to justify SKIPPING THE KEY
    ENTIRELY -- "we do not read it" is not an enforced boundary. (2) once
    the key was read, "recognized" meant only `entry_id` and `fields`
    keys were PRESENT, `envelope` was not -- a record with the right keys
    and completely wrong values (`entry_id` a list, `fields` a string, a
    non-hex `encoding`, a non-string `agent`/`reason`) passed as
    recognized anyway. `_matches_recognized_inbox_shape` now validates
    the actual closed schema, not just its key names.

    Every record here is now read and classified into exactly one of
    three outcomes:
    - RECOGNIZED inbox-conservation shape (the exact closed schema
      `_matches_recognized_inbox_shape` validates): contributes no
      stream_id occurrence -- correct, not assumed.
    - ENVELOPE-BEARING (has `envelope`): decoded and its stream_id IS
      returned as a real occurrence, exactly like undeliverable/
      unresolved -- a genuine envelope duplicated into this sink, by any
      cause, is now counted and can trigger DUPLICATED like anywhere
      else.
    - ANYTHING ELSE (fails to parse as JSON, isn't a JSON object, or
      matches neither recognized shape -- including a lookalike with the
      right keys and wrong values) is a SCHEMA ANOMALY -- returned
      separately and always reported as a failure by the caller, never
      silently skipped.

    Returns `(occurrences, anomalies)` -- `occurrences` matches
    `_stream_id_occurrences`'s shape; `anomalies` is a list of
    human-readable strings, one per record this function could not
    positively classify as either recognized shape.
    """
    occurrences: list[tuple[str, dict]] = []
    anomalies: list[str] = []
    for raw in raw_records:
        try:
            record = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        except Exception as exc:
            anomalies.append(f"retired_inbox record failed to parse as JSON: {raw!r} ({exc})")
            continue
        if not isinstance(record, dict):
            anomalies.append(f"retired_inbox record is not a JSON object: {record!r}")
            continue
        has_envelope = "envelope" in record
        is_recognized_inbox_shape = _matches_recognized_inbox_shape(record)
        if has_envelope and is_recognized_inbox_shape:
            # Structurally unreachable given the two shapes' disjoint key
            # sets (an exact-match on the 5 non-envelope keys cannot also
            # contain "envelope") -- kept as a defensive check rather
            # than assumed impossible, matching this whole file's own
            # rule against trusting reasoning it hasn't verified.
            anomalies.append(
                f"retired_inbox record matches BOTH the recognized "
                f"inbox-conservation schema and has an 'envelope' key -- "
                f"ambiguous, not trusted either way: {record!r}"
            )
            continue
        if has_envelope:
            try:
                header = parse_for_switch(_decode_evidence_envelope(record))
            except Exception as exc:
                anomalies.append(
                    f"retired_inbox record has an 'envelope' key but failed "
                    f"to parse as one: {record!r} ({exc})"
                )
                continue
            occurrences.append((header["stream_id"], record))
            continue
        if is_recognized_inbox_shape:
            continue  # recognized non-envelope inbox-conservation shape
        anomalies.append(
            f"retired_inbox record matches neither the recognized "
            f"inbox-conservation schema (exact keys agent/reason/"
            f"entry_id/encoding/fields, string agent/reason/entry_id, "
            f"encoding=='hex', fields as ordered two-element hex-string "
            f"pairs) nor an envelope shape -- schema drift, not silently "
            f"accepted: {record!r}"
        )
    return occurrences, anomalies


def _setup(r, pod: str, tenant: str, agents: list[str]) -> None:
    registry = prefix(pod, tenant, resource="registry")
    r.hset(registry, mapping={agent: "tmux" for agent in agents})


def _cleanup(r, pod: str, tenant: str, agents: list[str]) -> None:
    registry = prefix(pod, tenant, resource="registry")
    r.hdel(registry, *agents)
    keys = [
        prefix(pod, tenant, agent, resource)
        for agent in agents
        for resource in ("egress", "ingress", "dead", "unreplied", "processing", "opening", "opened", "inbox")
    ]
    if receive_unresolved_key is not None:
        keys.append(receive_unresolved_key(pod, tenant))
    if receive_undeliverable_key is not None:
        keys.append(receive_undeliverable_key(pod, tenant))
    if retired_inbox_key is not None:
        keys.append(retired_inbox_key(pod, tenant))
    r.delete(*keys)


def _stream_ids_in(raws: list) -> set[str]:
    ids = set()
    for raw in raws:
        try:
            ids.add(parse_for_switch(raw)["stream_id"])
        except Exception:
            continue
    return ids


# ---------------------------------------------------------------------------
# Scenario 1: baseline conservation across several named envelopes, happy
# path. Extends tools/smoke_delivery.py's single-envelope round trip to many
# interleaved envelopes, and to per-identity assertions instead of a fixed
# expected-events list -- the thing worth catching here is one envelope's
# identity leaking onto another's record, which a single-envelope smoke test
# structurally cannot expose. Shape-independent: send()/Switch.step() are
# unchanged by the receive()-side fix, so this exercises whichever receive()
# is actually installed without needing to know which shape it is.
# ---------------------------------------------------------------------------

def scenario_baseline(r, pod: str, tenant: str) -> list[str]:
    sender, recipient = "harness-sender-1", "harness-recipient-1"
    _setup(r, pod, tenant, [sender, recipient])
    try:
        names = [f"conservation-{i}" for i in range(8)]
        stream_ids: dict[str, str] = {}
        captured = io.StringIO()
        with redirect_stdout(captured):
            for name in names:
                stream_ids[name] = send(
                    r, pod=pod, tenant=tenant, source=sender, destination=recipient,
                    payload={"text": name},
                )
            switch = Switch(r, pod=pod, tenant=tenant, kick=lambda *a: None)
            for _ in names:
                if not switch.step(timeout=1):
                    raise AssertionError("switch did not forward a queued envelope")
            opened: list[dict] = []
            receive(
                r, pod=pod, tenant=tenant, agent=recipient,
                openers={"Message": opened.append}, timeout=1,
            )
        records = [json.loads(line) for line in captured.getvalue().splitlines() if line.strip()]
        opened_by_id = {e["stream_id"]: e for e in opened}
        expected_stages = {"sent", "popped", "forwarded", "kick_started", "received", "opened"}
        failures = []
        for name, sid in stream_ids.items():
            stages = {rec["event"] for rec in records if rec.get("stream_id") == sid}
            missing = expected_stages - stages
            if missing:
                failures.append(f"{name} ({sid}) missing custody stages: {sorted(missing)}")
            envelope = opened_by_id.get(sid)
            if envelope is None:
                failures.append(f"{name} ({sid}) never reached 'opened'")
                continue
            if envelope["payload"].get("text") != name:
                failures.append(
                    f"IDENTITY MISMATCH: {sid} opened carrying payload "
                    f"{envelope['payload']!r}, expected text={name!r} -- "
                    "this is exactly the failure mode a count-only check "
                    "cannot see (8 sent, 8 opened, wrong ones paired)"
                )
        return failures
    finally:
        _cleanup(r, pod, tenant, [sender, recipient])


# ---------------------------------------------------------------------------
# Scenario 2 (legacy shape): process death in receive()'s own pop-then-open
# gap on current main, via a REAL SIGKILL, not a modelled interleaving. The
# worker subprocess performs EXACTLY the two operations receive() performs
# back to back with nothing in between (BLPOP, then _open_received) -- the
# only addition is a stdout sync line emitted immediately after the pop
# returns, so the parent can choose the moment to kill deterministically
# without altering the order or nature of any operation receive() itself
# performs.
# ---------------------------------------------------------------------------

def _worker_pop_and_die_legacy() -> None:
    ingress_key = sys.argv[2]
    url = sys.argv[3]
    r = redis.Redis.from_url(url)
    item = r.blpop(ingress_key, timeout=5)
    if item is None:
        print("NO_ITEM", flush=True)
        return
    print("POPPED", flush=True)
    # If the parent's kill signal is somehow missed, fall through to real
    # processing after a grace period rather than hanging -- a harness bug
    # must never look like proof of anything.
    time.sleep(5)
    _open_received(
        r, pod=os.environ["H_POD"], tenant=os.environ["H_TENANT"],
        agent=os.environ["H_AGENT"], openers={}, raw=item[1], module="port",
    )
    print("FELL_THROUGH", flush=True)


def _scenario_process_death_legacy(r, pod: str, tenant: str, sender: str, recipient: str) -> list[str]:
    captured = io.StringIO()
    with redirect_stdout(captured):
        stream_id = send(
            r, pod=pod, tenant=tenant, source=sender, destination=recipient,
            payload={"text": "process-death-target"},
        )
        switch = Switch(r, pod=pod, tenant=tenant, kick=lambda *a: None)
        if not switch.step(timeout=1):
            raise AssertionError("switch did not forward the queued envelope")

    ingress_key = prefix(pod, tenant, recipient, "ingress")
    raw_before = r.lindex(ingress_key, 0)
    if raw_before is None or parse_for_switch(raw_before)["stream_id"] != stream_id:
        raise AssertionError("harness setup error: unexpected envelope at ingress head")

    env = dict(os.environ)
    env["H_POD"], env["H_TENANT"], env["H_AGENT"] = pod, tenant, recipient
    proc = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--worker-legacy", ingress_key, _redis_url()],
        stdout=subprocess.PIPE, text=True, bufsize=1, env=env,
    )
    killed = _kill_on_sync(proc, "POPPED")
    if not killed:
        return [
            f"HARNESS ERROR: worker never reported POPPED for {stream_id} "
            "-- this scenario did not exercise the gap it claims to; "
            "narrowing the claim rather than reporting a result"
        ]

    dead_key = prefix(pod, tenant, recipient, "dead")
    in_ingress = stream_id in _stream_ids_in(r.lrange(ingress_key, 0, -1))
    in_dead = stream_id in _stream_ids_in(r.lrange(dead_key, 0, -1))
    if in_ingress or in_dead:
        return []
    return [
        f"LOSS CONFIRMED: stream_id {stream_id} ('process-death-target') is "
        "in neither ingress nor dead after a real SIGKILL delivered in the "
        "exact gap between receive()'s own pop and its call into "
        "_open_received. No terminal state exists for this identity "
        "anywhere this harness can look -- unrecoverable."
    ]


# ---------------------------------------------------------------------------
# Scenario 2 (processing shape): process death AFTER the durable BLMOVE
# claim but BEFORE _open_received completes. The fix's whole claim is that
# this is now recoverable -- the raw is durably in `processing`, not just
# in the dead worker's memory -- so this scenario does two things a
# meaningful test of a recovery mechanism must do: (a) confirm the claim
# itself survives the kill, and (b) confirm a SUCCESSOR receive() call
# -- the real function, not a re-inspection of the list -- actually
# recovers and finishes it. Proving (a) without (b) would only show the
# raw sits inertly in a list forever, which is not the property claimed.
# ---------------------------------------------------------------------------

def _worker_claim_and_die_processing() -> None:
    ingress_key = sys.argv[2]
    processing_key = sys.argv[3]
    url = sys.argv[4]
    r = redis.Redis.from_url(url)
    raw = r.blmove(ingress_key, processing_key, 5, "LEFT", "RIGHT")
    if raw is None:
        print("NO_ITEM", flush=True)
        return
    print("CLAIMED", flush=True)
    time.sleep(5)
    _open_received(
        r, pod=os.environ["H_POD"], tenant=os.environ["H_TENANT"],
        agent=os.environ["H_AGENT"], openers={}, raw=raw,
        processing_key=processing_key, module="port",
    )
    print("FELL_THROUGH", flush=True)


_WORKER_GIVE_UP_LINES = {"NO_ITEM", "FELL_THROUGH"}


def _kill_on_sync(proc: subprocess.Popen, sync_line: str) -> bool:
    """Read the worker's stdout until it reports the sync line, kill it
    (real SIGKILL) the instant it does, and return whether that happened.

    ⚠ Ignores every OTHER line rather than stopping on the first one that
    doesn't match. A worker that calls the real receive() (as the
    opening-boundary scenarios do) prints its own custody JSON records
    -- "received", "kick_started" and so on -- before the opener's sync
    line ever runs; stopping early on the first of those would have
    reported every one of those scenarios as "the gap was never
    exercised" without ever reaching the actual gap. Only the two known
    give-up markers (the worker explicitly reporting it will never reach
    the sync point) end the wait early; anything else not recognized
    keeps reading rather than guessing.
    """
    killed = False
    try:
        for line in proc.stdout:
            line = line.strip()
            if line == sync_line:
                proc.send_signal(signal.SIGKILL)
                killed = True
                break
            if line in _WORKER_GIVE_UP_LINES:
                break
    finally:
        proc.wait(timeout=10)
    return killed


def _scenario_process_death_processing(r, pod: str, tenant: str, sender: str, recipient: str) -> list[str]:
    captured = io.StringIO()
    with redirect_stdout(captured):
        stream_id = send(
            r, pod=pod, tenant=tenant, source=sender, destination=recipient,
            payload={"text": "process-death-target"},
        )
        switch = Switch(r, pod=pod, tenant=tenant, kick=lambda *a: None)
        if not switch.step(timeout=1):
            raise AssertionError("switch did not forward the queued envelope")

    ingress_key = prefix(pod, tenant, recipient, "ingress")
    processing_key = _processing_key(pod, tenant, recipient)
    raw_before = r.lindex(ingress_key, 0)
    if raw_before is None or parse_for_switch(raw_before)["stream_id"] != stream_id:
        raise AssertionError("harness setup error: unexpected envelope at ingress head")

    env = dict(os.environ)
    env["H_POD"], env["H_TENANT"], env["H_AGENT"] = pod, tenant, recipient
    proc = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--worker-processing",
         ingress_key, processing_key, _redis_url()],
        stdout=subprocess.PIPE, text=True, bufsize=1, env=env,
    )
    killed = _kill_on_sync(proc, "CLAIMED")
    if not killed:
        return [
            f"HARNESS ERROR: worker never reported CLAIMED for {stream_id} "
            "-- this scenario did not exercise the gap it claims to; "
            "narrowing the claim rather than reporting a result"
        ]

    # Step (a): did the durable claim itself survive the kill?
    if stream_id not in _stream_ids_in(r.lrange(processing_key, 0, -1)):
        return [
            f"LOSS CONFIRMED: stream_id {stream_id} ('process-death-target') "
            "is not in the processing list after a real SIGKILL delivered "
            "immediately after the BLMOVE claim returned -- the durable "
            "claim itself did not survive the kill."
        ]

    # Step (b): does a REAL successor receive() call recover it to a
    # terminal state? A raw sitting inertly in `processing` forever is not
    # the property the fix claims -- at-least-once delivery, not merely
    # at-least-once storage.
    recovered = []
    successor_out = io.StringIO()
    with redirect_stdout(successor_out):
        receive(
            r, pod=pod, tenant=tenant, agent=recipient,
            openers={"Message": recovered.append}, timeout=1, blocking=False,
        )
    recovered_ids = {e["stream_id"] for e in recovered}
    dead_key = prefix(pod, tenant, recipient, "dead")
    in_dead = stream_id in _stream_ids_in(r.lrange(dead_key, 0, -1))
    if stream_id in recovered_ids or in_dead:
        return []
    return [
        f"LOSS CONFIRMED: stream_id {stream_id} ('process-death-target') "
        "survived the durable claim (still in processing after the kill) "
        "but a successor receive() call did not recover it to any terminal "
        f"state -- recovered={sorted(recovered_ids)!r}, in_dead={in_dead}. "
        "The claim held, but the recovery path did not."
    ]


def scenario_process_death(r, pod: str, tenant: str) -> list[str]:
    sender, recipient = "harness-sender-2", "harness-recipient-2"
    _setup(r, pod, tenant, [sender, recipient])
    try:
        shape = _custody_shape()
        if shape in ("processing", "phases"):
            return _scenario_process_death_processing(r, pod, tenant, sender, recipient)
        return _scenario_process_death_legacy(r, pod, tenant, sender, recipient)
    finally:
        _cleanup(r, pod, tenant, [sender, recipient])


# ---------------------------------------------------------------------------
# Scenario 4 (phases shape only): death AFTER the opening transition -- the
# critical truth boundary switch-agent's design draws. Reviewer's
# reproduction showed universal at-least-once replay is unsafe (no receive
# consumer has effect-specific idempotency: a tmux Command, a Message
# paste, an AddTicket, an OpenShell exec can all duplicate). So the CORRECT
# behaviour here is the opposite of scenario 3's: the identity must NOT be
# silently reopened by a successor. It must land in the tenant-level
# unresolved sink, nameable, and stay there.
#
# The worker calls the REAL receive() with a custom opener that syncs and
# sleeps -- not a hand-rolled replica of receive()'s internals. Because
# _open_received calls transfer(processing_key, opening_key) BEFORE calling
# the opener, the sync line firing from inside the opener is proof the
# real code has already crossed the boundary, using receive()'s own
# production code path rather than an assumption about where the boundary
# is.
# ---------------------------------------------------------------------------

def _worker_open_and_die() -> None:
    pod, tenant, agent = os.environ["H_POD"], os.environ["H_TENANT"], os.environ["H_AGENT"]
    url = sys.argv[2]
    r = redis.Redis.from_url(url)

    def opener(envelope: dict) -> None:
        print("OPENING", flush=True)
        time.sleep(5)

    try:
        receive(r, pod=pod, tenant=tenant, agent=agent, openers={"Message": opener}, timeout=5)
    except Exception:
        pass
    print("FELL_THROUGH", flush=True)


def scenario_death_after_opening(r, pod: str, tenant: str) -> list[str] | None:
    if _custody_shape() != "phases":
        return None  # not applicable to this shape -- distinct from "held"

    sender, recipient = "harness-sender-4", "harness-recipient-4"
    _setup(r, pod, tenant, [sender, recipient])
    try:
        captured = io.StringIO()
        with redirect_stdout(captured):
            stream_id = send(
                r, pod=pod, tenant=tenant, source=sender, destination=recipient,
                payload={"text": "opening-death-target"},
            )
            switch = Switch(r, pod=pod, tenant=tenant, kick=lambda *a: None)
            if not switch.step(timeout=1):
                raise AssertionError("switch did not forward the queued envelope")

        env = dict(os.environ)
        env["H_POD"], env["H_TENANT"], env["H_AGENT"] = pod, tenant, recipient
        proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--worker-open", _redis_url()],
            stdout=subprocess.PIPE, text=True, bufsize=1, env=env,
        )
        killed = _kill_on_sync(proc, "OPENING")
        if not killed:
            return [
                f"HARNESS ERROR: worker never reported OPENING for {stream_id} "
                "-- this scenario did not exercise the boundary it claims to; "
                "narrowing the claim rather than reporting a result"
            ]

        opening_key = _opening_key(pod, tenant, recipient)
        opened_key = _opened_key(pod, tenant, recipient)
        # Immediately after the kill, the identity is expected to still be
        # sitting in `opening` -- the successor hasn't run yet. This is a
        # sanity check on the harness's own timing, not the property under
        # test.
        if stream_id not in _stream_ids_in(r.lrange(opening_key, 0, -1)):
            return [
                f"HARNESS ERROR: stream_id {stream_id} is not in `opening` "
                "immediately after the kill -- either the kill landed before "
                "the real transition, or after it already moved past opening "
                "on its own; this run's timing cannot support a conclusion, "
                "narrowing the claim rather than reporting one"
            ]

        # The property: a successor's receive() must NOT reopen it. It must
        # surface it to tenant unresolved, and never invoke the opener for
        # this identity.
        reopened = []
        successor_out = io.StringIO()
        with redirect_stdout(successor_out):
            receive(
                r, pod=pod, tenant=tenant, agent=recipient,
                openers={"Message": reopened.append}, timeout=1, blocking=False,
            )
        reopened_ids = {e["stream_id"] for e in reopened}
        failures = []
        if stream_id in reopened_ids:
            failures.append(
                f"DUPLICATE-EXECUTION HARM: stream_id {stream_id} ('opening-death-target') "
                "was reopened by a successor receive() call after a real "
                "process death mid-opener -- this is exactly the replay "
                "reviewer's reproduction demonstrated (a real external effect "
                "duplicated), not a hypothetical."
            )

        unresolved = _unresolved_stream_ids(r, pod, tenant)
        if stream_id not in unresolved:
            in_opened = stream_id in _stream_ids_in(r.lrange(opened_key, 0, -1))
            failures.append(
                f"LOSS OR MISCLASSIFICATION: stream_id {stream_id} is not in "
                f"tenant unresolved after a death mid-opener (in_opened={in_opened}, "
                f"reopened={sorted(reopened_ids)!r}) -- an uncertain outcome must "
                "be nameable in unresolved, not silently absent."
            )
        else:
            record = unresolved[stream_id]
            if record.get("agent") != recipient:
                failures.append(
                    f"IDENTITY MISMATCH: unresolved record for {stream_id} names "
                    f"agent {record.get('agent')!r}, expected {recipient!r}"
                )
        return failures
    finally:
        _cleanup(r, pod, tenant, [sender, recipient])


# ---------------------------------------------------------------------------
# Scenario 5 (phases shape only): a stopped-and-rehired name must not
# inherit predecessor custody (covered by scenario_death_after_opening --
# a fresh receive() call IS the "rehire" case) nor erase EXISTING tenant
# unresolved evidence for a DIFFERENT identity that was already recorded.
# Unresolved is tenant-level, shared across every agent in the tenant, so
# this is the one property that specifically needs two independent
# identities under two different agents to be meaningful.
# ---------------------------------------------------------------------------

def scenario_rehire_preserves_unresolved_evidence(r, pod: str, tenant: str) -> list[str] | None:
    if _custody_shape() != "phases":
        return None  # not applicable to this shape -- distinct from "held"

    bystander_sender, bystander_agent = "harness-sender-5a", "harness-recipient-5a"
    sender, recipient = "harness-sender-5b", "harness-recipient-5b"
    _setup(r, pod, tenant, [bystander_sender, bystander_agent, sender, recipient])
    try:
        # Seed one genuine, independently-produced unresolved record for a
        # DIFFERENT agent -- not hand-written JSON, the real death-mid-
        # opener path (same worker mechanics as scenario_death_after_opening,
        # inlined here since that scenario manages its own agent pair),
        # so it's the same shape a real prior incident would have left.
        captured = io.StringIO()
        with redirect_stdout(captured):
            bystander_stream_id = send(
                r, pod=pod, tenant=tenant, source=bystander_sender, destination=bystander_agent,
                payload={"text": "bystander-target"},
            )
            switch = Switch(r, pod=pod, tenant=tenant, kick=lambda *a: None)
            if not switch.step(timeout=1):
                raise AssertionError("switch did not forward the bystander envelope")
        env = dict(os.environ)
        env["H_POD"], env["H_TENANT"], env["H_AGENT"] = pod, tenant, bystander_agent
        proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--worker-open", _redis_url()],
            stdout=subprocess.PIPE, text=True, bufsize=1, env=env,
        )
        if not _kill_on_sync(proc, "OPENING"):
            return [
                "HARNESS ERROR: could not seed the bystander unresolved record "
                "(worker never reported OPENING) -- narrowing the claim"
            ]
        bystander_out = io.StringIO()
        with redirect_stdout(bystander_out):
            receive(
                r, pod=pod, tenant=tenant, agent=bystander_agent,
                openers={"Message": lambda e: None}, timeout=1, blocking=False,
            )
        seeded = _unresolved_stream_ids(r, pod, tenant)
        if bystander_stream_id not in seeded:
            return [
                "HARNESS ERROR: bystander unresolved record was not seeded -- "
                "cannot test whether a later rehire preserves it, narrowing "
                "the claim"
            ]

        # Now: a completely independent "stopped and rehired" agent. The
        # rehire itself is just a fresh receive() call -- there is no
        # separate "hire" code path in receive() to simulate; the property
        # is that this fresh call does not touch the bystander's evidence.
        opened = []
        rehire_out = io.StringIO()
        with redirect_stdout(rehire_out):
            target_stream_id = send(
                r, pod=pod, tenant=tenant, source=sender, destination=recipient,
                payload={"text": "rehire-target"},
            )
            switch = Switch(r, pod=pod, tenant=tenant, kick=lambda *a: None)
            switch.step(timeout=1)
            receive(
                r, pod=pod, tenant=tenant, agent=recipient,
                openers={"Message": opened.append}, timeout=1, blocking=False,
            )

        after = _unresolved_stream_ids(r, pod, tenant)
        failures = []
        if bystander_stream_id not in after:
            failures.append(
                f"EVIDENCE ERASED: bystander stream_id {bystander_stream_id}'s "
                "unresolved record is gone after an unrelated agent's fresh "
                "receive() call -- a rehire (or any other agent's normal "
                "operation) must not be able to erase tenant unresolved "
                "evidence for a different identity."
            )
        elif after[bystander_stream_id] != seeded[bystander_stream_id]:
            failures.append(
                f"EVIDENCE MUTATED: bystander stream_id {bystander_stream_id}'s "
                f"unresolved record changed from {seeded[bystander_stream_id]!r} "
                f"to {after[bystander_stream_id]!r}"
            )
        if target_stream_id not in {e["stream_id"] for e in opened}:
            failures.append(
                f"HARNESS ERROR: the rehire target {target_stream_id} was not "
                "opened normally by its own fresh receive() call -- the "
                "control case for this scenario didn't behave as expected, "
                "narrowing the claim on the main assertion above"
            )
        return failures
    finally:
        _cleanup(r, pod, tenant, [bystander_sender, bystander_agent, sender, recipient])


# ---------------------------------------------------------------------------
# Scenario 6 (phases shape only): retirement must not turn admitted custody
# into ABSENCE. Reviewer's blocker on the parent 6c85c72 was that the
# membership Lua DELETED ingress, processing, opening, and opened outright,
# destroying the envelope and the phase evidence needed to classify its
# outcome; that test asserted the old per-agent keys were empty afterward,
# which proves CLEANUP, not PRESERVATION, and so pinned the harm rather than
# catching it. This seeds one distinct, independently-named identity per
# phase and reconciles where EACH ONE ended up, by identity.
# ---------------------------------------------------------------------------

def scenario_retirement_conserves_admitted_envelopes(r, pod: str, tenant: str) -> list[str] | None:
    if _custody_shape() != "phases":
        return None  # not applicable -- no opening/unresolved concept here
    if stop_agent is None or receive_undeliverable_key is None:
        return None  # not applicable -- this tree has no retirement path yet

    sender, target = "harness-sender-6", "harness-recipient-6"
    _setup(r, pod, tenant, [sender, target])
    # Retirement's inbox-conservation branch is unconditional in the Lua,
    # but real api-type inbox content is what actually exercises it, not
    # a synthetic reason string -- overriding _setup's default "tmux" so
    # stop_agent's real inbox-conservation branch runs against something
    # real on every run of this scenario (see step 2b below).
    r.hset(prefix(pod, tenant, resource="registry"), target, "api")
    try:
        # 1. One genuine, complete round trip FIRST, so `opened` is
        #    populated by the real send/step/receive/open mechanics before
        #    anything else is seeded -- receive()'s own opening-sweep-on-
        #    startup would otherwise consume anything placed directly into
        #    the opening key ahead of this.
        opened_seen = []
        with redirect_stdout(io.StringIO()):
            opened_stream_id = send(
                r, pod=pod, tenant=tenant, source=sender, destination=target,
                payload={"text": "retirement-opened-target"},
            )
            switch = Switch(r, pod=pod, tenant=tenant, kick=lambda *a: None)
            if not switch.step(timeout=1):
                raise AssertionError("switch did not forward the opened-control envelope")
            receive(
                r, pod=pod, tenant=tenant, agent=target,
                openers={"Message": opened_seen.append}, timeout=1, blocking=False,
            )
        if opened_stream_id not in {e["stream_id"] for e in opened_seen}:
            return [
                "HARNESS ERROR: control envelope was not opened normally before "
                "seeding the other three phases -- the setup for this scenario "
                "didn't behave as expected, narrowing the claim"
            ]

        # 2. Seed one distinct envelope directly into each of the other
        #    three phase keys, built and encoded the same way
        #    core.envelope.build()/encode() always do. Direct RPUSH, not
        #    send()+Switch (which always targets ingress, and LMOVE always
        #    takes the oldest item regardless of which envelope was
        #    "intended") -- this is what keeps each phase's identity
        #    unambiguous.
        def _seed(key: str, name: str) -> str:
            envelope = build("Message", sender, target, {"text": name}, pod=pod, tenant=tenant)
            r.rpush(key, encode(envelope))
            return envelope["stream_id"]

        ingress_key = prefix(pod, tenant, target, "ingress")
        processing_key = _processing_key(pod, tenant, target)
        opening_key = _opening_key(pod, tenant, target)

        ingress_stream_id = _seed(ingress_key, "retirement-ingress-target")
        processing_stream_id = _seed(processing_key, "retirement-processing-target")
        opening_stream_id = _seed(opening_key, "retirement-opening-target")

        # 2b. Real inbox content too -- a valid retired-inbox CONTROL case,
        #     committed, not a script run once by hand and never landed.
        #     Reviewer's finding: an earlier claim that this was checked
        #     rested on exactly that -- run once on one box, reported, and
        #     never in the diff. This exercises stop_agent's real
        #     inbox-conservation branch on every run.
        inbox_key = prefix(pod, tenant, target, "inbox")
        if retired_inbox_key is not None:
            r.xadd(inbox_key, {"payload": "retirement-inbox-control-1", "kind": "Message"})
            r.xadd(inbox_key, {"payload": "retirement-inbox-control-2", "kind": "Message"})

        # Reviewer's finding against 67f42a1: a dict/set keyed by identity
        # silently collapses two occurrences of the same identity into
        # one, so a scenario built on that can only prove "at least one
        # parseable record exists in the expected sink" -- never
        # "exactly once", which is this scenario's own name and claim.
        # `_stream_id_occurrences` keeps every parse instead, so counting
        # actually means something.
        #
        # ⚠ Reviewer's SECOND finding, against 5442e4e: the occurrence scan
        # must cover every REAL terminal custody sink an identity could
        # land in, not just the three this scenario's own happy path
        # names. `dead` (per-agent) is a real terminus -- core.channels.py
        # dead-letters straight into it -- and stop_agent's own Lua never
        # touches it (not in its KEYS list at all), so it is silently
        # unwatched unless scanned explicitly. Falsified: appending a
        # duplicate raw envelope directly to the target's own `dead` list
        # after a normal retirement reported clean before this fix.
        dead_key = prefix(pod, tenant, target, "dead")

        # ⚠ Reviewer's THIRD finding, against 1b47c71: reasoning that
        # retired_inbox records can never carry a stream_id (a different
        # top-level shape, {entry_id, fields} vs {envelope}) is not the
        # same as READING the key. An earlier version used that reasoning
        # to justify never scanning retired_inbox at all -- so a genuine
        # envelope record duplicated into it (by a real bug, or reviewer's
        # injection) was never seen, not correctly excluded. "We do not
        # read it" and "we read it and deliberately treat this recognized
        # shape as contributing no identity" are different claims,
        # confirmed the hard way. `_retired_inbox_occurrences` reads and
        # classifies every record explicitly, failing loudly (an anomaly,
        # not a silent skip) on anything that isn't one of the two
        # recognized shapes.
        def _occurrence_lists():
            retired_inbox_occurrences, retired_inbox_anomalies = (
                _retired_inbox_occurrences(r.lrange(retired_inbox_key(pod, tenant), 0, -1))
                if retired_inbox_key is not None else ([], [])
            )
            return (
                _stream_id_occurrences(r.lrange(receive_undeliverable_key(pod, tenant), 0, -1), True),
                _stream_id_occurrences(r.lrange(_unresolved_key(pod, tenant), 0, -1), True),
                _stream_id_occurrences(r.lrange(_opened_key(pod, tenant, target), 0, -1), False),
                _stream_id_occurrences(r.lrange(dead_key, 0, -1), False),
                retired_inbox_occurrences,
                retired_inbox_anomalies,
            )

        (
            before_undeliverable, before_unresolved, _before_opened, _before_dead,
            _before_retired_inbox, before_inbox_anomalies,
        ) = _occurrence_lists()
        if before_inbox_anomalies:
            return [
                f"HARNESS ERROR: retired_inbox already had unclassifiable "
                f"record(s) BEFORE stop_agent ran, narrowing the claim: "
                f"{before_inbox_anomalies!r}"
            ]
        seeded_ids = {ingress_stream_id, processing_stream_id, opening_stream_id}
        for label, occurrences in (("undeliverable", before_undeliverable), ("unresolved", before_unresolved)):
            collision = [sid for sid, _ in occurrences if sid in seeded_ids]
            if collision:
                return [
                    f"HARNESS ERROR: seeded identity {collision[0]} was already in "
                    f"tenant {label} BEFORE stop_agent ran -- seeding collided with "
                    "prior state, narrowing the claim"
                ]

        # 3. The real stop_agent -- not a description of it, not a
        #    synthetic Lua stand-in.
        with redirect_stdout(io.StringIO()):
            stop_agent(
                r, pod=pod, tenant=tenant, envelope={"payload": {"agent": target}},
                kill_window=lambda agent: None,
            )

        failures: list[str] = []
        (
            after_undeliverable, after_unresolved, after_opened, after_dead,
            after_retired_inbox, after_inbox_anomalies,
        ) = _occurrence_lists()
        if after_inbox_anomalies:
            failures.append(
                f"SCHEMA ANOMALY: retired_inbox has record(s) this harness "
                f"cannot classify as either the recognized inbox-"
                f"conservation shape or an envelope shape -- fails loudly "
                f"rather than assuming absence: {after_inbox_anomalies!r}"
            )
        if retired_inbox_key is not None:
            retired_inbox_raw = r.lrange(retired_inbox_key(pod, tenant), 0, -1)
            if len(retired_inbox_raw) < 2:
                failures.append(
                    f"HARNESS ERROR: the two genuine inbox entries seeded in "
                    f"step 2b were not conserved into retired_inbox by the "
                    f"real stop_agent (found {len(retired_inbox_raw)} "
                    "record(s)) -- the control case didn't exercise what it "
                    "was meant to, narrowing the claim on the rest of this "
                    "scenario"
                )
        all_occurrences = (
            [(sid, "undeliverable", rec) for sid, rec in after_undeliverable]
            + [(sid, "unresolved", rec) for sid, rec in after_unresolved]
            + [(sid, "opened", rec) for sid, rec in after_opened]
            + [(sid, "dead", rec) for sid, rec in after_dead]
            + [(sid, "retired_inbox", rec) for sid, rec in after_retired_inbox]
        )

        def _check_exactly_once(stream_id: str, label: str, expected_sink: str) -> None:
            matches = [(sink, rec) for sid, sink, rec in all_occurrences if sid == stream_id]
            if not matches:
                failures.append(
                    f"CUSTODY LOST: {label} identity {stream_id} appears in NONE "
                    "of undeliverable/unresolved/opened/dead/retired_inbox "
                    "after stop_agent -- retirement must not turn admitted "
                    "custody into absence."
                )
                return
            if len(matches) > 1:
                sinks = [sink for sink, _ in matches]
                failures.append(
                    f"DUPLICATED: {label} identity {stream_id} appears "
                    f"{len(matches)} times across undeliverable/unresolved/"
                    f"opened/dead/retired_inbox ({sinks!r}) -- retirement "
                    "must move each identity to exactly one terminal "
                    "location, not duplicate it."
                )
                return
            sink, record = matches[0]
            if sink != expected_sink:
                failures.append(
                    f"MISCLASSIFIED: {label} identity {stream_id} landed in "
                    f"{sink!r}, expected {expected_sink!r} -- a record in the "
                    "wrong sink lies about what's known just as badly as no "
                    "record at all."
                )
                return
            if record is not None and record.get("agent") != target:
                failures.append(
                    f"MISATTRIBUTED: {label} identity {stream_id}'s {sink} "
                    f"record names agent {record.get('agent')!r}, expected "
                    f"{target!r}"
                )

        _check_exactly_once(ingress_stream_id, "ingress", "undeliverable")
        _check_exactly_once(processing_stream_id, "processing", "undeliverable")
        _check_exactly_once(opening_stream_id, "opening", "unresolved")
        _check_exactly_once(opened_stream_id, "opened (pre-existing)", "opened")

        for key, label in ((ingress_key, "ingress"), (processing_key, "processing"), (opening_key, "opening")):
            remaining = r.llen(key)
            if remaining:
                failures.append(
                    f"HARNESS ERROR: per-agent {label} key still has {remaining} "
                    "item(s) after stop_agent -- expected the membership Lua to "
                    "have moved everything out; narrowing the claim on the "
                    "assertions above"
                )

        if failures:
            return failures

        # 4. A same-name successor must inherit none of the retired agent's
        #    custody, and must not disturb the evidence stop_agent just
        #    wrote for it.
        evidence_before_undeliverable = r.lrange(receive_undeliverable_key(pod, tenant), 0, -1)
        evidence_before_unresolved = r.lrange(_unresolved_key(pod, tenant), 0, -1)
        evidence_before_retired_inbox = (
            r.lrange(retired_inbox_key(pod, tenant), 0, -1) if retired_inbox_key is not None else []
        )

        _setup(r, pod, tenant, [target])  # re-hire under the same name
        successor_opened = []
        with redirect_stdout(io.StringIO()):
            successor_stream_id = send(
                r, pod=pod, tenant=tenant, source=sender, destination=target,
                payload={"text": "retirement-successor-target"},
            )
            switch = Switch(r, pod=pod, tenant=tenant, kick=lambda *a: None)
            switch.step(timeout=1)
            receive(
                r, pod=pod, tenant=tenant, agent=target,
                openers={"Message": successor_opened.append}, timeout=1, blocking=False,
            )

        successor_opened_ids = {e["stream_id"] for e in successor_opened}
        predecessor_ids = {ingress_stream_id, processing_stream_id, opening_stream_id, opened_stream_id}
        if predecessor_ids & successor_opened_ids:
            failures.append(
                "SUCCESSOR INHERITED CUSTODY: the rehired agent's own fresh "
                "receive() call opened one of the PREDECESSOR's stream_ids -- a "
                "same-name successor must consume none of a retired "
                "predecessor's custody."
            )
        if successor_stream_id not in successor_opened_ids:
            failures.append(
                f"HARNESS ERROR: the successor's own target envelope "
                f"{successor_stream_id} was not opened normally -- the control "
                "case for this half of the scenario didn't behave as expected, "
                "narrowing the claim on the inheritance assertion above"
            )

        evidence_after_undeliverable = r.lrange(receive_undeliverable_key(pod, tenant), 0, -1)
        evidence_after_unresolved = r.lrange(_unresolved_key(pod, tenant), 0, -1)
        if evidence_after_undeliverable != evidence_before_undeliverable:
            failures.append(
                "EVIDENCE MUTATED: tenant undeliverable changed shape across a "
                "same-name rehire -- retirement evidence must survive a "
                "successor's ordinary operation untouched."
            )
        if evidence_after_unresolved != evidence_before_unresolved:
            failures.append(
                "EVIDENCE MUTATED: tenant unresolved changed shape across a "
                "same-name rehire -- retirement evidence must survive a "
                "successor's ordinary operation untouched."
            )
        if retired_inbox_key is not None:
            evidence_after_retired_inbox = r.lrange(retired_inbox_key(pod, tenant), 0, -1)
            if evidence_after_retired_inbox != evidence_before_retired_inbox:
                failures.append(
                    "EVIDENCE MUTATED: tenant retired_inbox changed shape "
                    "across a same-name rehire -- retirement evidence must "
                    "survive a successor's ordinary operation untouched."
                )

        return failures
    finally:
        _cleanup(r, pod, tenant, [sender, target])


# ---------------------------------------------------------------------------
# Scenario 6b: NOT driven through a real stop_agent -- manufacturing these
# exact malformed shapes through the real Lua isn't possible (it only ever
# produces the correct one). Directly exercises _retired_inbox_occurrences'
# own schema validation instead, against reviewer's exact reproductions
# (the fifth false-clean this instrument has had) plus a genuine valid
# control, so the anomaly mechanism is proved neither permissive (accepts
# a malformed lookalike) nor noisy (rejects real production data) on every
# run, not just the one hand-verification round that found the gap.
# ---------------------------------------------------------------------------

def scenario_retired_inbox_schema_validation(r, pod: str, tenant: str) -> list[str] | None:
    if retired_inbox_key is None:
        return None  # not applicable -- this tree has no inbox-conservation shape yet

    malformed_lookalikes = [
        # Reviewer's exact reproductions: right KEYS, wrong VALUES. A
        # presence-only check ("entry_id and fields exist") passed both.
        {"entry_id": [], "fields": "not-pairs"},
        {"entry_id": "1-0", "fields": [], "encoding": "plain", "agent": 7, "reason": None},
        # A few more shapes in the same direction, checked rather than
        # assumed covered by the two reviewer named.
        {"agent": "x", "reason": "y", "entry_id": "1-0", "encoding": "hex", "fields": "not-a-list"},
        {"agent": "x", "reason": "y", "entry_id": "1-0", "encoding": "hex", "fields": [["not-hex", "62"]]},
        {"agent": "x", "reason": "y", "entry_id": "1-0", "encoding": "hex", "fields": [["61", "62", "63"]]},
        {"agent": "x", "reason": "y", "entry_id": "1-0", "encoding": "hex", "fields": [], "extra": "key"},
    ]
    failures: list[str] = []
    for lookalike in malformed_lookalikes:
        occurrences, anomalies = _retired_inbox_occurrences([json.dumps(lookalike)])
        if occurrences or not anomalies:
            failures.append(
                f"MALFORMED LOOKALIKE ACCEPTED: {lookalike!r} was not flagged "
                f"as a schema anomaly (occurrences={occurrences!r}, "
                f"anomalies={anomalies!r}) -- a record with the right KEYS "
                "but wrong types/values must not be silently treated as the "
                "recognized inbox-conservation shape."
            )

    # The other half reviewer named: the mechanism must not be noisy on
    # real production data either -- a genuinely valid record must pass.
    valid_record = {
        "agent": "harness-recipient-6", "reason": "destination retired with unread inbox content",
        "entry_id": "1758012345678-0", "encoding": "hex", "fields": [["7061796c6f6164", "68656c6c6f"]],
    }
    occurrences, anomalies = _retired_inbox_occurrences([json.dumps(valid_record)])
    if occurrences or anomalies:
        failures.append(
            f"FALSE POSITIVE: a genuinely valid recognized-shape record was "
            f"rejected or misclassified (occurrences={occurrences!r}, "
            f"anomalies={anomalies!r}) -- narrowing the claim on the "
            "malformed-lookalike checks above."
        )

    return failures


# ---------------------------------------------------------------------------
# Scenario 3 (legacy shape): a durable write failing -- the dead-letter
# RPUSH itself, for an envelope whose body fails to parse. _open_received's
# malformed-frame path is not wrapped in any try/except on main, so this
# failure propagates uncaught out of receive().
# ---------------------------------------------------------------------------

class _FailRpushTo:
    """A real Redis client, transparent except one key's RPUSH, which raises.

    Everything else -- BLPOP, LPOP, LLEN, LRANGE, LINDEX, HSET, EVAL, ping --
    passes straight through to the real client via __getattr__. This injects
    exactly one durable write failure without modelling anything about how
    or why it failed; a real Redis connection can and does refuse a write
    for reasons this harness does not need to reproduce to prove the gap.
    """

    def __init__(self, real, fail_key: str):
        self._real = real
        self._fail_key = fail_key

    def rpush(self, key, *args, **kwargs):
        if key == self._fail_key:
            raise redis.exceptions.ConnectionError("injected dead-letter write failure")
        return self._real.rpush(key, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _scenario_dead_letter_failure_legacy(r, pod: str, tenant: str, sender: str, recipient: str) -> list[str]:
    envelope = build(
        "Message", sender, recipient, {"text": "dead-write-failure-target"},
        pod=pod, tenant=tenant,
    )
    stream_id = envelope["stream_id"]
    raw = encode(envelope)
    corrupted = raw[:HEADER_WIDTH] + "not valid json"
    ingress_key = prefix(pod, tenant, recipient, "ingress")
    r.rpush(ingress_key, corrupted)

    dead_key = prefix(pod, tenant, recipient, "dead")
    proxy = _FailRpushTo(r, dead_key)
    captured = io.StringIO()
    raised = None
    with redirect_stdout(captured):
        try:
            receive(proxy, pod=pod, tenant=tenant, agent=recipient, openers={}, timeout=1)
        except Exception as exc:
            raised = exc

    records = [json.loads(line) for line in captured.getvalue().splitlines() if line.strip()]
    named = [rec for rec in records if rec.get("stream_id") == stream_id]
    in_ingress = stream_id in _stream_ids_in(r.lrange(ingress_key, 0, -1))
    in_dead = stream_id in _stream_ids_in(r.lrange(dead_key, 0, -1))
    if in_ingress or in_dead:
        return []
    return [
        f"LOSS CONFIRMED: stream_id {stream_id} ('dead-write-failure-target') "
        "is in neither ingress nor dead after an injected dead-letter RPUSH "
        f"failure. receive() {'raised ' + repr(raised) if raised else 'returned without raising'}; "
        f"custody records naming this identity: {named!r}. No terminal "
        "state exists for it anywhere this harness can look."
    ]


# ---------------------------------------------------------------------------
# Scenario 3 (processing and phases shapes): every durable custody transfer
# -- dead-letter, processing->opening, opening->opened, opening->unresolved
# -- goes through one atomic Lua eval (LREM the source, RPUSH the
# destination, both server-side in one execution). A client-side .rpush()
# proxy cannot touch that at all. Instead this intercepts .eval() calls
# whose script IS (by identity) that shared transfer script, and raises
# before the real script ever runs -- the same shape of failure as a
# connection dropping between sending the EVAL and receiving its reply,
# which a real client cannot distinguish from the script never having
# executed.
# ---------------------------------------------------------------------------

def _transfer_script():
    """The module-level Lua constant currently used for custody transfers,
    whichever name it has -- switch-agent renamed it from
    _MOVE_PROCESSING_TO_DEAD (dead-letter only) to _TRANSFER_RECEIVE_CUSTODY
    (processing->opening, opening->opened, any->dead, opening->unresolved,
    all through one script) between the round this harness last targeted
    and now. Checked by name, in the order a reader would expect a rename
    to happen, so this keeps working across that rename without needing to
    know it happened -- and raises loudly (not silently) if neither name
    exists, rather than quietly failing to inject anything."""
    for name in ("_TRANSFER_RECEIVE_CUSTODY", "_MOVE_PROCESSING_TO_DEAD"):
        script = getattr(_channels, name, None)
        if script is not None:
            return script
    return None


class _FailEvalMatching:
    """A real Redis client, transparent except .eval() calls whose script
    IS (by object identity, not text matching) the given target script,
    which raise instead of executing. Identity rather than a substring
    guess: immune to a comment rename inside the script changing what text
    a marker would need to match, since it compares the actual object the
    module's own code passes to eval(), not a copy of its source. Everything
    else -- BLMOVE, LMOVE, LINDEX, LRANGE, HSET, ping -- passes straight
    through via __getattr__."""

    def __init__(self, real, target_script):
        self._real = real
        self._target = target_script

    def eval(self, script, *args, **kwargs):
        if script is self._target:
            raise redis.exceptions.ConnectionError("injected custody-transfer eval failure")
        return self._real.eval(script, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _scenario_transfer_eval_failure(r, pod: str, tenant: str, sender: str, recipient: str) -> list[str]:
    """A malformed-body envelope forces the parse-failure path, which on
    every durable-custody shape ('processing' and 'phases' alike) transfers
    straight from processing to dead through the one shared Lua script --
    fail that script and confirm the identity survives in processing
    (the durable claim happened before the script was even attempted)."""
    envelope = build(
        "Message", sender, recipient, {"text": "dead-write-failure-target"},
        pod=pod, tenant=tenant,
    )
    stream_id = envelope["stream_id"]
    raw = encode(envelope)
    corrupted = raw[:HEADER_WIDTH] + "not valid json"
    ingress_key = prefix(pod, tenant, recipient, "ingress")
    r.rpush(ingress_key, corrupted)

    processing_key = _processing_key(pod, tenant, recipient)
    dead_key = prefix(pod, tenant, recipient, "dead")

    target = _transfer_script()
    if target is None:
        return [
            "HARNESS ERROR: no known custody-transfer Lua constant found on "
            "core.channels (checked _TRANSFER_RECEIVE_CUSTODY and "
            "_MOVE_PROCESSING_TO_DEAD) -- cannot confirm this scenario is "
            "targeting the right script; narrowing the claim rather than "
            "reporting a result"
        ]

    proxy = _FailEvalMatching(r, target)
    captured = io.StringIO()
    raised = None
    with redirect_stdout(captured):
        try:
            receive(proxy, pod=pod, tenant=tenant, agent=recipient, openers={}, timeout=1)
        except Exception as exc:
            raised = exc

    in_processing = stream_id in _stream_ids_in(r.lrange(processing_key, 0, -1))
    in_dead = stream_id in _stream_ids_in(r.lrange(dead_key, 0, -1))
    if in_processing or in_dead:
        return []
    return [
        f"LOSS CONFIRMED: stream_id {stream_id} ('dead-write-failure-target') "
        "is in neither processing nor dead after an injected custody-transfer "
        f"eval failure. receive() {'raised ' + repr(raised) if raised else 'returned without raising'}. "
        "No terminal state exists for it anywhere this harness can look."
    ]


def scenario_dead_letter_write_failure(r, pod: str, tenant: str) -> list[str]:
    sender, recipient = "harness-sender-3", "harness-recipient-3"
    _setup(r, pod, tenant, [sender, recipient])
    try:
        shape = _custody_shape()
        if shape in ("processing", "phases"):
            return _scenario_transfer_eval_failure(r, pod, tenant, sender, recipient)
        return _scenario_dead_letter_failure_legacy(r, pod, tenant, sender, recipient)
    finally:
        _cleanup(r, pod, tenant, [sender, recipient])


# ---------------------------------------------------------------------------
# Scenario 6: reviewer's exact requirement at c2800e6 -- prove the namespace
# fix works by actually running two full, independent invocations of this
# harness concurrently and requiring BOTH to report their correct result,
# not by reasoning about the fix in the abstract. Each child gets its own
# default (unset POD/TENANT) namespace, so this is also the regression test
# for the collision reviewer reproduced: before the fix, this scenario
# would have failed the same way reviewer's manual reproduction did.
#
# Guarded against recursion with an env var: a spawned child harness
# process must not itself try to spawn two more.
# ---------------------------------------------------------------------------

_CONCURRENCY_CHILD_ENV = "_CONSERVATION_HARNESS_CONCURRENCY_CHILD"


def scenario_concurrent_invocations_do_not_collide(r, pod: str, tenant: str) -> list[str] | None:
    if os.environ.get(_CONCURRENCY_CHILD_ENV) == "1":
        return None  # this IS a spawned child -- do not recurse

    env = dict(os.environ)
    env.pop("POD", None)
    env.pop("TENANT", None)
    env[_CONCURRENCY_CHILD_ENV] = "1"
    procs = [
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
        )
        for _ in range(2)
    ]
    outputs = [proc.communicate(timeout=120)[0] for proc in procs]
    codes = [proc.returncode for proc in procs]

    failures = []
    for index, (code, output) in enumerate(zip(codes, outputs)):
        if code != 0:
            tail = "\n".join(output.strip().splitlines()[-15:])
            failures.append(
                f"CONCURRENCY COLLISION: concurrent invocation #{index + 1} exited "
                f"{code}, expected 0 -- two independent default-namespace runs must "
                f"not interfere with each other. Last lines of its output:\n{tail}"
            )
    return failures


SCENARIOS = [
    ("baseline conservation, 8 named envelopes, happy path", scenario_baseline),
    ("process death in receive()'s claim-then-open gap (real SIGKILL)", scenario_process_death),
    ("dead-letter transfer failure during receive()", scenario_dead_letter_write_failure),
    ("process death AFTER opening lands in unresolved, never reopened (phases shape only)",
     scenario_death_after_opening),
    ("stopped-and-rehired agent preserves unrelated unresolved evidence (phases shape only)",
     scenario_rehire_preserves_unresolved_evidence),
    ("retirement conserves admitted envelopes by phase, exactly-once by identity (phases shape only)",
     scenario_retirement_conserves_admitted_envelopes),
    ("retired-inbox schema validation rejects malformed lookalikes, accepts genuine records",
     scenario_retired_inbox_schema_validation),
    ("two concurrent default-namespace invocations do not collide",
     scenario_concurrent_invocations_do_not_collide),
]


def _report_version(r) -> None:
    shape = _custody_shape()
    module_file = getattr(_channels, "__file__", "<unknown>")
    print(f"custody shape detected: {shape} (core/channels.py: {module_file})")
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    try:
        head = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        branch = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if head.returncode == 0:
            print(
                f"tree under test: {branch.stdout.strip() or '<detached>'} @ "
                f"{head.stdout.strip()}"
            )
    except Exception:
        pass


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--worker-legacy":
        _worker_pop_and_die_legacy()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "--worker-processing":
        _worker_claim_and_die_processing()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "--worker-open":
        _worker_open_and_die()
        return

    r = _connect()
    pod, tenant = _default_pod_tenant()
    print(f"conservation harness against {_redis_url()}, pod={pod} tenant={tenant}")
    _report_version(r)
    print("NOT A FIX -- a measuring instrument. See this file's module docstring.\n")

    overall_ok = True
    for name, fn in SCENARIOS:
        print(f"--- {name} ---")
        failures = fn(r, pod, tenant)
        if failures is None:
            print("SKIPPED: not applicable to this custody shape")
        elif failures:
            overall_ok = False
            print(f"CONSERVATION VIOLATED ({len(failures)} finding(s)):")
            for failure in failures:
                print(f"  - {failure}")
        else:
            print("conservation held: every named envelope traced to exactly one terminal state")
        print()

    print("=== SUMMARY ===")
    if overall_ok:
        print("PASS: conservation held in every scenario")
    else:
        print("FAIL: conservation violated in at least one scenario -- see findings above")
    sys.exit(0 if overall_ok else 1)


if __name__ == "__main__":
    main()
