import ast
import contextlib
import inspect
import io
import json
import os
import re
import shutil
import sys
import tempfile
import textwrap
import unittest
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import redis

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from core.envelope import parse
from core.keys import prefix
from modules.watchdog import service
from modules.watchdog.service import Watchdog, run_observers


POD = "acme"
TENANT = "hq"
NOW = datetime(2026, 8, 9, 14, 0, tzinfo=timezone.utc)


class FakeRedis:
    """Minimal in-memory double covering what Watchdog touches: lists (board
    resources, ingress), hashes (presence/blocked/acks/*.alerted), a stream
    (alerts) and `eval` for core.queues.admit_ingress's Lua script.
    """

    def __init__(self, agents=("architect", "sme-2", "frontend", "backend"), fails_on=None):
        self.values = {}
        self.hashes = defaultdict(dict)
        self.lists = defaultdict(list)
        self.streams = defaultdict(list)
        self.writes = []
        self.fails_on = dict(fails_on) if fails_on else {}
        registry_key = prefix(POD, TENANT, resource="registry")
        self.hashes[registry_key] = {agent: "tmux" for agent in agents}

    def _fault(self, name):
        if name in self.fails_on:
            err = self.fails_on[name]
            raise err() if isinstance(err, type) else err

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex=None):
        self.values[key] = value

    def delete(self, *keys):
        count = 0
        for key in keys:
            if key in self.hashes:
                del self.hashes[key]
                count += 1
        return count

    def rpush(self, key, *values):
        self.writes.append(("rpush", key, values))
        self.lists[key].extend(values)
        return len(self.lists[key])

    def lpop(self, key):
        return self.lists[key].pop(0) if self.lists.get(key) else None

    def lrange(self, key, start, stop):
        lst = self.lists.get(key, [])
        if stop == -1:
            return list(lst[start:])
        return list(lst[start : stop + 1])

    def lindex(self, key, index):
        lst = self.lists.get(key, [])
        if lst and -len(lst) <= index < len(lst):
            return lst[index]
        return None

    def lmove(self, source, destination, src="LEFT", dest="RIGHT"):
        if not self.lists.get(source):
            return None
        value = self.lists[source].pop(0)
        self.lists[destination].append(value)
        return value

    def blmove(self, source, destination, timeout, src="LEFT", dest="RIGHT"):
        return self.lmove(source, destination, src=src, dest=dest)

    def lrem(self, key, count, value):
        try:
            self.lists[key].remove(value)
        except (KeyError, ValueError):
            return 0
        return 1

    def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    def hset(self, key, field=None, value=None, mapping=None):
        if mapping is not None:
            self.hashes[key].update(mapping)
            return len(mapping)
        self.hashes[key][field] = value
        return 1

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hdel(self, key, *fields):
        h = self.hashes.get(key, {})
        count = 0
        for field in fields:
            if field in h:
                del h[field]
                count += 1
        return count

    def hkeys(self, key):
        return list(self.hashes.get(key, {}).keys())

    def hexists(self, key, field):
        return field in self.hashes.get(key, {})

    def xadd(self, key, fields, maxlen=None, approximate=True):
        stream = self.streams[key]
        entry_id = f"{len(stream) + 1}-0"
        stream.append((entry_id, dict(fields)))
        return entry_id

    def xrevrange(self, key, max="+", min="-", count=None):
        entries = list(reversed(self.streams.get(key, [])))
        return entries[:count] if count is not None else entries

    def eval(self, script, numkeys, *args):
        self._fault("eval")
        keys = args[:numkeys]
        rest = args[numkeys:]
        if "core ingress admission v1" in script:
            limit = int(rest[0])
            raw = rest[1]
            for index, key in enumerate(keys, start=1):
                depth = len(self.lists.get(key, []))
                if depth >= limit:
                    return [0, index, depth]
            result = [1]
            for key in keys:
                self.lists.setdefault(key, []).append(raw)
                result.append(len(self.lists[key]))
            return result
        if "core receive processing-to-dead" in script:
            processing, dead = keys
            raw = rest[0]
            if self.lrem(processing, 1, raw) != 1:
                return 0
            self.rpush(dead, raw)
            return 1
        raise AssertionError(f"unexpected Lua script: {script}")


def _key(agent, resource):
    return prefix(POD, TENANT, agent, resource)


def _watchdog(r):
    return Watchdog(r, pod=POD, tenant=TENANT, session_name=TENANT)


def _stalled_agent(r, agent="sme-2", *, state="idle"):
    r.lists[_key(agent, "tasks.doing")] = [
        json.dumps({
            "id": "ticket-1",
            "title": "review the auth change",
            "started_ts": "2026-08-09T13:46:00Z",
        })
    ]
    r.hashes[_key(agent, "presence")] = {
        "state": state,
        "last_activity": "2026-08-09T13:51:00Z" if state != "unknown" else "",
    }


def _quiet_windows(now=NOW):
    """A list-windows reply with fresh activity, so the 3-signal stall check
    never fires and doesn't contaminate a doing/todo/hold/unreplied/ack-loop
    assertion."""
    ts = int(now.timestamp())

    def _run(*args, socket=None):
        if args[0] == "list-windows":
            return 0, f"architect\t{ts}\nsme-2\t{ts}", ""
        return 0, "", ""

    return _run


def _doing_agent(r, agent="sme-2", *, started="2026-08-09T13:45:00Z", ticket_id="ticket-1", title="review the auth change"):
    r.lists[_key(agent, "tasks.doing")] = [
        json.dumps({"id": ticket_id, "title": title, "started_ts": started})
    ]


def _lead(r, name="architect"):
    r.values[prefix(POD, TENANT, resource="lead")] = name


def _ack(r, source, destination, *, streak, last_ts="2026-08-09T13:59:00Z"):
    r.hashes.setdefault(_key(source, "acks"), {})[destination] = json.dumps(
        {"streak": streak, "last_ts": last_ts}
    )


def _todo_agent(r, agent="sme-2", *, created="2026-08-09T13:55:00Z", ticket_id="ticket-1", title="pick up the auth review", append=False):
    entry = json.dumps({"id": ticket_id, "title": title, "created_ts": created})
    key = _key(agent, "tasks.todo")
    if append:
        r.lists.setdefault(key, []).append(entry)
    else:
        r.lists[key] = [entry]


def _hold_agent(r, agent="sme-2", *, held="2026-08-09T13:00:00Z", ticket_id="ticket-1", title="wait on the vendor reply", append=False, created=None, reason=None):
    entry = {"id": ticket_id, "title": title}
    if held is not None:
        entry["held_ts"] = held
    if created is not None:
        entry["created_ts"] = created
    if reason is not None:
        entry["hold_reason"] = reason
    key = _key(agent, "tasks.hold")
    if append:
        r.lists.setdefault(key, []).append(json.dumps(entry))
    else:
        r.lists[key] = [json.dumps(entry)]


def _unreplied_agent(r, agent="sme-2", *, client="telegram", since="2026-08-09T13:59:00Z", count=1):
    key = _key(agent, "unreplied")
    r.hashes.setdefault(key, {})[client] = json.dumps({"count": count, "since": since})


def _capture(fn):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    return buf.getvalue()


def _watchdog_records(out, destination="architect"):
    """Parse watchdog-module log lines from captured stdout, addressed to
    `destination`. Used to assert on the CLAIM a record makes, not on one
    implementation's choice of event-name vocabulary for making it."""
    records = []
    for line in out.splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("module") == "watchdog" and event.get("destination") == destination:
            records.append(event)
    return records


# The canonical, closed evidence-level contract (core.logging.log_record's
# `evidence` field -- see modules/watchdog/service.py's _log_lead_alert for
# the writer side). A CLOSED allowlist, not a denylist of overclaiming
# words: scanning specific fields (or even every field, for specific words)
# still lets a genuinely novel claim-shaped vocabulary slip through
# unnoticed ("paste landed", anything nobody has enumerated yet). An
# allowlist has the opposite failure mode: anything NOT explicitly
# recognized is rejected, including vocabulary nobody has written yet.
_ADMISSION_ONLY_EVIDENCE = frozenset({"admitted", "rejected", "unknown", "no_lead"})

# ⚠ `evidence` alone is not sufficient -- kept as a scenario-level backstop
# below even though `_log_lead_alert` now makes the contradiction reviewer
# found (evidence="admitted" next to outcome="delivered"/reason="alert
# sent") structurally unreachable through the real code path: `event`,
# `evidence` and the `reason` template are all sourced together from one
# entry in `Watchdog._LEAD_ALERT_TEMPLATES`, keyed by a closed `kind`
# string, so nothing calling through the wrapper can set any of them
# independently. `reason`'s runtime-interpolated *values* (a lead name, an
# exception's text) are still free text, so this scan stays as defense in
# depth against a future template whose fixed wording itself overclaims.
_OVERCLAIM_WORD = re.compile(r"\b(sent|delivered)\b", re.IGNORECASE)
_NEGATION_BEFORE = re.compile(r"\b(?:not|never|no)\s+(?:\w+\s+){0,2}$", re.IGNORECASE)


def _reason_contradicts_evidence(record):
    reason = record.get("reason")
    if not isinstance(reason, str):
        return False
    for match in _OVERCLAIM_WORD.finditer(reason):
        preceding = reason[max(0, match.start() - 24):match.start()]
        if _NEGATION_BEFORE.search(preceding):
            continue
        return True
    return False


def _assert_admission_only_evidence(testcase, records):
    """Assert every record's `evidence` tag is one of the admission-only
    values -- never `delivered`/`sent`/`created`, and never silently absent
    -- AND that `reason` (the one field _log_lead_alert cannot structurally
    close off) does not independently contradict it. Could a system that
    still produces the harmful output pass this check? A record with a
    correct `evidence` tag and a contradicting `reason` must fail here, not
    just one with a wrong `evidence` value -- that is the exact gap reviewer
    found in the version of this helper before it checked `reason` too."""
    testcase.assertTrue(records, "an alert attempt must leave a record, not silence")
    for record in records:
        testcase.assertIn(
            record.get("evidence"), _ADMISSION_ONLY_EVIDENCE,
            f"record {record!r} does not carry a closed, admission-only evidence tag",
        )
        testcase.assertFalse(
            _reason_contradicts_evidence(record),
            f"record {record!r} carries a correct evidence tag but its reason text overclaims",
        )


class AdmissionEvidenceContractTests(unittest.TestCase):
    """Counterfixtures for _assert_admission_only_evidence itself, per
    reviewer's finding that the earlier field-scanning version needed
    tests exercising the HELPER's own correctness directly, not only
    indirectly through the watchdog scenario tests that happen to call it.
    Each of these constructs a record by hand -- no Watchdog, no FakeRedis
    -- so the contract enforcement is pinned in isolation."""

    def test_accepts_every_known_admission_only_value(self):
        for value in _ADMISSION_ONLY_EVIDENCE:
            _assert_admission_only_evidence(self, [{"evidence": value}])

    def test_rejects_a_delivered_claim(self):
        with self.assertRaises(AssertionError):
            _assert_admission_only_evidence(self, [{"evidence": "delivered"}])

    def test_rejects_a_sent_claim(self):
        with self.assertRaises(AssertionError):
            _assert_admission_only_evidence(self, [{"evidence": "sent"}])

    def test_rejects_reviewers_exact_contradictory_record(self):
        """Reviewer's literal counterexample, verbatim, with the evidence
        tag left CORRECT: evidence="admitted" alongside outcome="delivered"
        and reason="alert sent". A first version of this fixture changed
        `evidence` itself to "sent" and asserted rejection -- that tested
        the allowlist mechanism (an illegal value in the field the helper
        reads is caught), not the actual harm reviewer demonstrated: a
        record whose CANONICAL TAG IS TRUTHFUL can still contain a false
        delivery claim elsewhere. This is the fixture that must fail if
        `_assert_admission_only_evidence` only checks `evidence`."""
        record = {
            "event": "lead_alert_admitted",
            "evidence": "admitted",
            "outcome": "delivered",
            "reason": "alert sent",
        }
        with self.assertRaises(AssertionError):
            _assert_admission_only_evidence(self, [record])

    def test_accepts_a_correct_evidence_tag_with_ordinary_reason_text(self):
        """The contradiction check must not be so broad it rejects ordinary,
        non-overclaiming reason text alongside a correct tag."""
        _assert_admission_only_evidence(self, [{
            "evidence": "no_lead",
            "reason": "lead 'retired-lead' is not a registered agent",
        }])

    def test_rejects_a_missing_evidence_tag(self):
        """A record with no `evidence` field at all is a contract
        violation, not a free pass -- the contract requires presence, not
        just non-overclaiming content."""
        with self.assertRaises(AssertionError):
            _assert_admission_only_evidence(self, [{"event": "lead_alert_admitted", "reason": "ok"}])

    def test_rejects_an_unrecognized_future_value(self):
        """A vocabulary nobody has thought of yet -- an evidence tag that
        is neither a known-safe value nor a word this file specifically
        bans -- must still fail. This is what makes a closed allowlist
        different from scanning for specific overclaiming words: a NEW
        claim-shaped value that was never enumerated anywhere still cannot
        slip through silently, because the allowlist is closed rather than
        the denylist being (necessarily incompletely) open."""
        with self.assertRaises(AssertionError):
            _assert_admission_only_evidence(self, [{"evidence": "paste_landed"}])

    def test_rejects_an_empty_record_list(self):
        """Silence is still the original defect this whole family exists to
        fix -- no records at all must fail exactly like an overclaiming one,
        not pass by vacuous truth."""
        with self.assertRaises(AssertionError):
            _assert_admission_only_evidence(self, [])

    def test_accepts_multiple_records_only_if_all_are_admission_only(self):
        _assert_admission_only_evidence(
            self, [{"evidence": "no_lead"}, {"evidence": "admitted"}]
        )
        with self.assertRaises(AssertionError):
            _assert_admission_only_evidence(
                self, [{"evidence": "admitted"}, {"evidence": "delivered"}]
            )


def _log_emitting_calls(func):
    """Every call to `log_record`/`_log_lead_alert` in `func`'s own source,
    as raw AST nodes. Shared by the two structural checks below: one reads
    the call target's name, the other reads its first argument -- neither
    can be right if this collection is wrong, so it exists once."""
    source = textwrap.dedent(inspect.getsource(func))
    tree = ast.parse(source)
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Attribute) and node.func.attr in ("log_record", "_log_lead_alert"))
            or (isinstance(node.func, ast.Name) and node.func.id in ("log_record", "_log_lead_alert"))
        )
    ]


class LeadAlertLoggingStructureTests(unittest.TestCase):
    """Structural pins on _log_lead_alert itself and on _notify_lead's
    source, answering reviewer's "ninth call site" question: what
    necessarily catches a future lead-alert log call that overclaims,
    independent of whether any test scenario happens to exercise that
    specific code path?

    ⚠ This is the SECOND version of the AST check below. The first one
    verified only that every log-emitting call in _notify_lead named
    `_log_lead_alert` as its target -- it never inspected that call's
    arguments at all. Reviewer's exact finding: "the AST test checks only
    that calls are named `_log_lead_alert`; it does NOT check that the
    `evidence=` keyword is present." A call missing its required argument
    still passed this check statically and would only have failed the
    first time that exact line executed at runtime -- a guarantee asserted
    at a boundary (the callee name) wider than the mechanism it claimed to
    enforce (evidence always present) is not a guarantee. Falsified by hand
    before being trusted this time, the same discipline already proven on
    the Lua-preflight branch: a real call site in _notify_lead was
    temporarily edited to drop its `kind` argument entirely, the rewritten
    test below (`test_every_log_emitting_call_in_notify_lead_goes_through_
    the_wrapper_with_a_registered_kind`) was run and confirmed to fail with
    exactly the missing-argument assertion, and the call site was then
    restored -- recorded in the branch's commit message rather than kept as
    a permanent fixture in this file, since a test asserting its own
    assertion logic against a synthetic broken function would not actually
    exercise this test the way mutating the real source and rerunning it
    did.

    Four guarantees now, three static (checked by parsing source, not by
    running it) and one enforced by Python itself:
    1. `_log_lead_alert` cannot be called without `kind` (no default) --
       Python raises TypeError immediately, before any record is built.
    2. An unrecognized `kind` is a KeyError inside `_log_lead_alert` itself
       -- there is no way to reach the log_record call with a `kind` that
       is not a real entry in `_LEAD_ALERT_TEMPLATES`.
    3. `_log_lead_alert`'s own log_record call passes only stream_id,
       destination, reason and evidence -- never a caller-supplied
       `outcome`/`title`/`old_title`, regardless of what extra `**context`
       a caller passes (those keys can only feed a reason template's named
       placeholders, and are silently unused otherwise).
    4. A static AST walk of _notify_lead's source confirms every
       log-emitting call in it (a) targets `_log_lead_alert`, not
       `log_record` directly, and (b) passes a `kind` whose literal string
       value is an actual key in `_LEAD_ALERT_TEMPLATES` -- so neither a
       bypass of the wrapper NOR a call through the wrapper with a
       fabricated/unregistered kind string can ship unnoticed.
    """

    def test_log_lead_alert_requires_kind(self):
        watchdog = service.Watchdog(object(), pod="p", tenant="t", session_name="t")
        with self.assertRaises(TypeError):
            watchdog._log_lead_alert()

    def test_log_lead_alert_rejects_an_unregistered_kind(self):
        watchdog = service.Watchdog(object(), pod="p", tenant="t", session_name="t")
        with self.assertRaises(KeyError):
            watchdog._log_lead_alert("paste_landed", "lead", "stream-1")

    def test_log_lead_alert_never_forwards_outcome_title_or_old_title(self):
        """`_log_lead_alert` accepts arbitrary **context (to fill a reason
        template's named placeholders), so passing outcome/title/old_title
        as context does not raise -- str.format silently ignores unused
        keywords. The actual guarantee is that they never reach the
        record regardless: log_record captures stdout here and the parsed
        line must not carry any of the three keys reviewer's counterexample
        used, no matter what a caller hands _log_lead_alert."""
        watchdog = service.Watchdog(object(), pod="p", tenant="t", session_name="t")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            watchdog._log_lead_alert(
                "admitted", "lead", "stream-1",
                outcome="delivered", title="t", old_title="o",
            )
        record = json.loads(out.getvalue().strip())
        for forbidden_key in ("outcome", "title", "old_title"):
            self.assertNotIn(forbidden_key, record, f"record {record!r} leaked {forbidden_key!r}")

    def test_templates_are_all_admission_only_and_free_of_overclaim_words(self):
        """Every entry in `_LEAD_ALERT_TEMPLATES` is the whole vocabulary
        `_log_lead_alert` can ever emit -- a closed, hardcoded table, not
        runtime input. Checking it once here covers every record that
        table can ever produce, forever, the same way checking a type's
        constructor once covers every instance -- no scenario test needs
        to independently re-earn this for each `kind`."""
        for kind, (event, evidence, reason_template) in service.Watchdog._LEAD_ALERT_TEMPLATES.items():
            self.assertIn(
                evidence, _ADMISSION_ONLY_EVIDENCE,
                f"template {kind!r} carries a non-admission-only evidence tag {evidence!r}",
            )
            self.assertFalse(
                _OVERCLAIM_WORD.search(event),
                f"template {kind!r}'s event name {event!r} overclaims",
            )
            if reason_template is not None:
                self.assertFalse(
                    _OVERCLAIM_WORD.search(reason_template),
                    f"template {kind!r}'s reason template {reason_template!r} overclaims",
                )

    def test_every_log_emitting_call_in_notify_lead_goes_through_the_wrapper_with_a_registered_kind(self):
        calls = _log_emitting_calls(service.Watchdog._notify_lead)
        self.assertTrue(calls, "expected at least one lead-alert log call in _notify_lead")
        known_kinds = set(service.Watchdog._LEAD_ALERT_TEMPLATES)
        for call in calls:
            name = call.func.attr if isinstance(call.func, ast.Attribute) else call.func.id
            self.assertEqual(
                name, "_log_lead_alert",
                f"_notify_lead calls {name}(...) directly at line {call.lineno} "
                "(source-relative) instead of going through _log_lead_alert -- "
                "this bypasses every guarantee the wrapper owns entirely",
            )
            # `self` is the first positional arg of a bound-method AST call
            # only when written as `self._log_lead_alert(...)`, which every
            # call site here is -- so `args[0]` is the `kind` argument, not
            # `self` (that's implicit in the Attribute access, not a
            # separate call argument).
            self.assertTrue(
                call.args, f"_log_lead_alert call at line {call.lineno} passes no `kind` argument",
            )
            kind_arg = call.args[0]
            self.assertIsInstance(
                kind_arg, ast.Constant,
                f"_log_lead_alert call at line {call.lineno} passes a non-literal `kind` "
                "-- this check can only verify a literal string against the template table",
            )
            self.assertIn(
                kind_arg.value, known_kinds,
                f"_log_lead_alert call at line {call.lineno} passes kind={kind_arg.value!r}, "
                "which is not a key in _LEAD_ALERT_TEMPLATES",
            )


class AdmissionErrorCategoryTests(unittest.TestCase):
    """Counterfixtures for _admission_error_category itself -- the closed
    mapping that replaced `detail=str(exc)` after reviewer's exact
    reproduction (`_log_lead_alert('unknown', ..., detail='REMOTE_SECRET_
    EXCEPTION_TEXT')` -> that text landing verbatim in the durable
    `reason`). Pinned in isolation, no Watchdog/FakeRedis needed, the same
    reasoning as AdmissionEvidenceContractTests above: the classifier's own
    correctness should not depend only on which scenario tests happen to
    exercise it."""

    def test_a_redis_error_is_categorized_as_redis_error(self):
        self.assertEqual(
            service.Watchdog._admission_error_category(redis.exceptions.ResponseError("x")),
            "redis_error",
        )

    def test_a_builtin_connection_error_is_categorized_as_connection_error(self):
        self.assertEqual(
            service.Watchdog._admission_error_category(ConnectionError("x")),
            "connection_error",
        )

    def test_a_builtin_timeout_error_is_categorized_as_timeout(self):
        self.assertEqual(
            service.Watchdog._admission_error_category(TimeoutError("x")),
            "timeout",
        )

    def test_an_unrecognized_exception_falls_back_to_unexpected_error(self):
        """A closed mapping must fail CLOSED, not open: an exception type
        nobody has enumerated here yet must still get a safe, generic
        category rather than falling through to something that exposes its
        message."""
        self.assertEqual(
            service.Watchdog._admission_error_category(ValueError("x")),
            "unexpected_error",
        )

    def test_the_categorys_own_message_never_appears_in_the_category_value(self):
        """The classifier must return a fixed literal from its own closed
        set -- never anything derived from the exception's message, even
        indirectly. If this ever changed to build the category FROM
        `str(exc)` (e.g. a prefix of it), this is the fixture that would
        catch it."""
        secret = "REMOTE_SECRET_EXCEPTION_TEXT"
        category = service.Watchdog._admission_error_category(ConnectionError(secret))
        self.assertNotIn(secret, category)
        self.assertEqual(category, "connection_error")


class ErrorCategoryTests(unittest.TestCase):
    """Counterfixtures for _error_category -- the closed classifier that
    replaced _error()'s f"{type(exc).__name__}: {exc}" across all 14
    call sites. Architect's exact finding: it emitted BOTH halves of the
    leak class, the exception's own message (str(exc)) AND its class name
    (type(exc).__name__), and a dynamically constructed exception type can
    put attacker-chosen text in its own __name__ -- so neither half was
    safe to trust. Pinned in isolation, no Watchdog/FakeRedis needed, same
    reasoning as AdmissionErrorCategoryTests above."""

    def test_a_genuine_internal_bug_reports_its_own_exact_type_name(self):
        """isinstance decides the branch, exact type identity decides the
        name -- a real, directly-raised TypeError is exactly one of our
        own closed literals, so its name is trusted."""
        self.assertEqual(service._error_category(TypeError("x")), "internal-error (TypeError)")

    def test_every_internal_type_reports_its_own_name(self):
        for exc_type in service._INTERNAL_ERROR_TYPES:
            self.assertEqual(
                service._error_category(exc_type("x")),
                f"internal-error ({exc_type.__name__})",
            )

    def test_a_dynamically_constructed_subclass_reports_derived_not_its_own_name(self):
        """The trap clients-agent hit and warned about: type(sentinel, (Name
        Error,), {}) is an instance of NameError (isinstance passes, so this
        still routes to internal-error) but its OWN class name is attacker-
        chosen -- so the name must not be trusted just because isinstance
        matched. Only an EXACT type() match may report the real name."""
        SentinelSubclass = type("REMOTE_SECRET_CLASS_NAME", (NameError,), {})
        self.assertTrue(issubclass(SentinelSubclass, NameError))
        self.assertNotIn(SentinelSubclass, service._INTERNAL_ERROR_TYPES)
        category = service._error_category(SentinelSubclass("x"))
        self.assertEqual(category, "internal-error (derived)")
        self.assertNotIn("REMOTE_SECRET_CLASS_NAME", category)

    def test_a_hostile_exception_metaclass_does_not_crash_the_classifier(self):
        """Reviewer's exact finding: `type(exc) in _INTERNAL_ERROR_TYPES`
        used equality (`in` on a tuple), and `type(exc) == some_builtin`
        invokes `type(exc)`'s own METACLASS `__eq__` -- the metaclass of a
        class is what defines how that class object itself compares, the
        same way an instance's class defines how the instance compares. A
        custom exception whose metaclass raises out of __eq__ crashed this
        function entirely under the `in`-based version. Fixed with `is`
        (a pointer comparison, no method dispatch, cannot be hijacked by
        any override). This constructs exactly that hostile metaclass and
        confirms the classifier still returns its closed fallback instead
        of propagating the crash."""

        class HostileMeta(type):
            def __eq__(cls, other):
                raise RuntimeError("hostile metaclass __eq__")

            def __hash__(cls):
                return 0

        class HostileException(Exception, metaclass=HostileMeta):
            pass

        self.assertEqual(service._error_category(HostileException("x")), "external-error")

    def test_a_genuine_transport_error_is_not_classified_as_internal(self):
        """The mirror clients-agent specifically warned not to skip: without
        this, "ours" could quietly widen until it swallows "theirs", passing
        every test written for the other direction while the classification
        stops meaning anything."""
        for exc in (
            redis.exceptions.ConnectionError("x"),
            ConnectionError("x"),
            TimeoutError("x"),
            OSError("x"),
        ):
            category = service._error_category(exc)
            self.assertFalse(
                category.startswith("internal-error"),
                f"{exc!r} was classified as {category!r}, but this is not our own coding mistake",
            )

    def test_a_redis_error_is_categorized_as_redis_error(self):
        self.assertEqual(service._error_category(redis.exceptions.ResponseError("x")), "redis-error")

    def test_a_builtin_connection_error_is_categorized_as_connection_error(self):
        self.assertEqual(service._error_category(ConnectionError("x")), "connection-error")

    def test_a_builtin_timeout_error_is_categorized_as_timeout(self):
        self.assertEqual(service._error_category(TimeoutError("x")), "timeout")

    def test_an_os_error_is_categorized_as_os_error(self):
        """Covers subprocess.run failures from run_tmux -- e.g. FileNotFoundError
        (a subclass of OSError) when the tmux binary itself is missing."""
        self.assertEqual(service._error_category(FileNotFoundError("x")), "os-error")

    def test_value_error_and_runtime_error_are_deliberately_not_internal(self):
        """Both are builtins, and both are routinely raised by libraries for
        malformed/remote-caused input (json decode, a Redis protocol error)
        -- classifying either as "ours" would send an operator hunting a
        defect in this module's code for someone else's bad data."""
        self.assertEqual(service._error_category(ValueError("x")), "external-error")
        self.assertEqual(service._error_category(RuntimeError("x")), "external-error")

    def test_an_unrecognized_exception_falls_back_to_external_error(self):
        self.assertEqual(service._error_category(ArithmeticError("x")), "external-error")

    def test_the_exceptions_own_message_never_appears_in_the_category(self):
        """Reviewer's exact reproduction shape, reapplied here: plant a
        secret in the exception MESSAGE and confirm it is absent from the
        resulting category, for both an internal and an external exception."""
        secret = "REMOTE_SECRET_EXCEPTION_TEXT"
        internal_category = service._error_category(TypeError(secret))
        external_category = service._error_category(ConnectionError(secret))
        self.assertNotIn(secret, internal_category)
        self.assertNotIn(secret, external_category)
        self.assertEqual(internal_category, "internal-error (TypeError)")
        self.assertEqual(external_category, "connection-error")


class ErrorSinkContentTests(unittest.TestCase):
    """The full _error() sink, not just the classifier -- confirms the
    JSON record it actually emits carries neither half of the original
    leak (message or a spoofed class name), for a scenario planting a
    sentinel in BOTH at once, the combination architect's ticket named
    explicitly."""

    def test_error_emits_neither_the_message_nor_a_spoofed_class_name(self):
        """SpoofedClass is a dynamically constructed subclass of NameError
        (one of our own closed types) -- isinstance passes, so this must
        still route to internal-error, but its OWN class name is attacker-
        chosen and must not be trusted."""
        secret_message = "REMOTE_SECRET_EXCEPTION_TEXT"
        SpoofedClass = type("REMOTE_SECRET_CLASS_NAME", (NameError,), {})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            service.Watchdog._error("some_job", SpoofedClass(secret_message))
        record = json.loads(out.getvalue().strip())
        self.assertNotIn(secret_message, out.getvalue())
        self.assertNotIn("REMOTE_SECRET_CLASS_NAME", out.getvalue())
        self.assertEqual(record["job"], "some_job")
        self.assertEqual(record["reason"], "internal-error (derived)")

    def test_error_category_never_raises_on_hostile_input(self):
        """A guard's correctness is only a claim until it's proved -- this
        proves _error_category is TOTAL, not just argued to be. It uses
        `type(exc) in ...` (tuple membership) and `isinstance`, never a
        dict/set/frozenset lookup on `exc` itself, so it cannot be crashed
        by a hostile __eq__/__hash__ the way a membership test on the
        instance could be. Exercises that directly: a class whose __eq__
        and __hash__ both raise, plus None and a plain non-exception
        object, none of which are BaseException instances at all."""

        class Hostile:
            def __eq__(self, other):
                raise RuntimeError("hostile __eq__")

            def __hash__(self):
                raise RuntimeError("hostile __hash__")

        for hostile_input in (None, object(), Hostile(), "not an exception", 42):
            self.assertEqual(service._error_category(hostile_input), "external-error")

    def test_error_never_raises_even_when_json_dumps_fails(self):
        """The LAST layer: nothing downstream of _error() catches its own
        failure, so it must be allowed to fail silently rather than crash
        whatever called it -- including main()'s own outermost per-phase
        catch. Forces json.dumps itself to fail (the one step inside
        _error() that is not already provably total) and confirms _error()
        absorbs it instead of propagating."""
        with patch.object(service.json, "dumps", side_effect=RuntimeError("boom")):
            try:
                service.Watchdog._error("some_job", ValueError("x"))
            except Exception as exc:
                self.fail(f"_error() propagated {exc!r} instead of failing silently")

    def test_a_hostile_exception_metaclass_still_reaches_a_closed_fallback_record(self):
        """Reviewer's exact ask, not just the classifier in isolation:
        confirm the SINK still emits a real record with the closed
        fallback category for this exact input, rather than the record
        silently vanishing into _error()'s own try/except: pass. Before
        the `is`-identity fix, this exact input crashed _error_category,
        which was then swallowed whole by _error()'s outer guard -- zero
        output, not a fallback -- a worse failure than the original leak:
        not just unsafe content, but no diagnostic at all."""

        class HostileMeta(type):
            def __eq__(cls, other):
                raise RuntimeError("hostile metaclass __eq__")

            def __hash__(cls):
                return 0

        class HostileException(Exception, metaclass=HostileMeta):
            pass

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            service.Watchdog._error("some_job", HostileException("x"))
        self.assertTrue(out.getvalue().strip(), "the record must not vanish")
        record = json.loads(out.getvalue().strip())
        self.assertEqual(record["job"], "some_job")
        self.assertEqual(record["reason"], "external-error")


def _kicks():
    """Return (list, fake_deliver_tmux) -- records the kicked agent name."""
    kicks = []

    def fake_deliver_tmux(r, pod, tenant, agent, session_name=None, socket=None, **kw):
        kicks.append(agent)

    return kicks, fake_deliver_tmux


def _no_kick():
    def fake_deliver_tmux(*a, **kw):
        raise AssertionError("should not kick")

    return fake_deliver_tmux


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self._live_patches = []

    def tearDown(self):
        for p in self._live_patches:
            p.stop()

    def _set(self, target, name, value):
        p = patch.object(target, name, value)
        p.start()
        self._live_patches.append(p)

    # -- stalls -----------------------------------------------------------

    def test_stall_requires_old_ticket_nonworking_presence_and_silent_window(self):
        r = FakeRedis()
        _stalled_agent(r)
        captures = []

        def tmux(*args, socket=None):
            if args[0] == "list-windows":
                return 0, "architect\t1786283999\nsme-2\t1786283580", ""
            captures.append(args[-1])
            return 0, "healthy pane", ""

        self._set(service, "run_tmux", tmux)
        watchdog = _watchdog(r)
        out = _capture(lambda: watchdog.poll(now=NOW))

        alert = json.loads(r.streams[prefix(POD, TENANT, resource="alerts")][0][1]["alert"])
        self.assertEqual(alert, {
            "v": 1,
            "ts": "2026-08-09T14:00:00.000Z",
            "kind": "stalled",
            "writer": "watchdog",
            "agent": "sme-2",
            "ticket": "review the auth change",
            "doing_age_s": 840,
            "no_activity_s": 540,
            "no_output_s": 420,
            "unchecked": [],
        })
        self.assertEqual(json.loads(out), alert)
        self.assertEqual(captures, [])

        watchdog.poll(now=NOW)
        self.assertEqual(len(r.streams[prefix(POD, TENANT, resource="alerts")]), 1)

    def test_stall_alert_carries_last_activity_kind_as_context_not_suppression(self):
        """The three-signal stall check cannot tell "stuck" from "waiting on
        a reply" -- an agent whose last action was `output` may just be
        waiting on someone. That ambiguity is surfaced as extra context on
        the alert, not used to silence it (the goal is fewer false alerts,
        not fewer alerts)."""
        r = FakeRedis()
        _stalled_agent(r)
        r.streams[_key("sme-2", "activity")] = [
            ("1-0", {"event": json.dumps({
                "v": 1, "agent": "sme-2", "ts": "2026-08-09T13:51:00Z", "kind": "output",
            })}),
        ]
        self._set(service, "run_tmux", lambda *args, socket=None: (
            (0, "architect\t1786283999\nsme-2\t1786283580", "") if args[0] == "list-windows" else (0, "", "")
        ))

        _watchdog(r).poll(now=NOW)

        alert = json.loads(r.streams[prefix(POD, TENANT, resource="alerts")][0][1]["alert"])
        self.assertEqual(alert["last_activity_kind"], "output")
        self.assertNotIn("last_activity_tool", alert)

    def test_stall_alert_carries_the_tool_name_when_last_activity_was_a_tool_call(self):
        r = FakeRedis()
        _stalled_agent(r)
        r.streams[_key("sme-2", "activity")] = [
            ("1-0", {"event": json.dumps({
                "v": 1, "agent": "sme-2", "ts": "2026-08-09T13:51:00Z",
                "kind": "tool", "tool": "Bash",
            })}),
        ]
        self._set(service, "run_tmux", lambda *args, socket=None: (
            (0, "architect\t1786283999\nsme-2\t1786283580", "") if args[0] == "list-windows" else (0, "", "")
        ))

        _watchdog(r).poll(now=NOW)

        alert = json.loads(r.streams[prefix(POD, TENANT, resource="alerts")][0][1]["alert"])
        self.assertEqual(alert["last_activity_kind"], "tool")
        self.assertEqual(alert["last_activity_tool"], "Bash")

    def test_printing_window_or_working_presence_suppresses_stall(self):
        r = FakeRedis()
        _stalled_agent(r)
        self._set(service, "run_tmux", lambda *args, socket=None: (
            (0, "architect\t1786283999\nsme-2\t1786283990", "") if args[0] == "list-windows" else (0, "", "")
        ))
        watchdog = _watchdog(r)
        watchdog.poll(now=NOW)
        self.assertNotIn(prefix(POD, TENANT, resource="alerts"), r.streams)

        r.hashes[_key("sme-2", "presence")]["state"] = "working"
        self._set(service, "run_tmux", lambda *args, socket=None: (
            (0, "architect\t1786283999\nsme-2\t1786283580", "") if args[0] == "list-windows" else (0, "", "")
        ))
        watchdog.poll(now=NOW)
        self.assertNotIn(prefix(POD, TENANT, resource="alerts"), r.streams)

    def test_unknown_activity_is_named_as_unchecked(self):
        r = FakeRedis()
        _stalled_agent(r, state="unknown")
        self._set(service, "run_tmux", lambda *args, socket=None: (
            (0, "architect\t1786283999\nsme-2\t1786283580", "") if args[0] == "list-windows" else (0, "", "")
        ))
        _watchdog(r).poll(now=NOW)
        alert = json.loads(r.streams[prefix(POD, TENANT, resource="alerts")][0][1]["alert"])
        self.assertIsNone(alert["no_activity_s"])
        self.assertEqual(alert["unchecked"], ["activity"])

    def test_missing_window_is_reported_for_otherwise_stalled_agent(self):
        r = FakeRedis()
        _stalled_agent(r)
        self._set(service, "run_tmux", lambda *args, socket=None: (0, "architect\t1786283999", ""))

        _watchdog(r).poll(now=NOW)

        alert = json.loads(r.streams[prefix(POD, TENANT, resource="alerts")][0][1]["alert"])
        self.assertEqual(alert["kind"], "stalled")
        self.assertEqual(alert["agent"], "sme-2")
        self.assertIsNone(alert["no_output_s"])
        self.assertTrue(alert["window_missing"])

    # -- blocked ------------------------------------------------------------

    def test_blocked_alert_reads_router_verdict_without_scraping(self):
        r = FakeRedis()
        r.hashes[_key("sme-2", "blocked")] = {
            "since": "2026-08-09T13:53:00Z",
            "stream_id": "delivery-1",
        }
        calls = []
        self._set(service, "run_tmux", lambda *args, socket=None: (
            calls.append(args) or (0, "architect\t1786284000\nsme-2\t1786284000", "")
        ))
        watchdog = _watchdog(r)
        watchdog.poll(now=NOW)

        self.assertTrue(all(call[0] == "list-windows" for call in calls))
        alerts = r.streams[prefix(POD, TENANT, resource="alerts")]
        alert = json.loads(alerts[0][1]["alert"])
        self.assertEqual(alert, {
            "v": 1,
            "ts": "2026-08-09T14:00:00.000Z",
            "kind": "blocked",
            "writer": "watchdog",
            "agent": "sme-2",
            "since": "2026-08-09T13:53:00Z",
            "stream_id": "delivery-1",
            "unconsumed_s": 420,
        })
        self.assertFalse(any("egress" in str(write) for write in r.writes))

        watchdog.poll(now=NOW)
        self.assertEqual(len(r.streams[prefix(POD, TENANT, resource="alerts")]), 1)

    def test_stall_alert_includes_blocked_verdict(self):
        r = FakeRedis()
        _stalled_agent(r)
        r.hashes[_key("sme-2", "blocked")] = {
            "since": "2026-08-09T13:53:00Z",
            "stream_id": "delivery-1",
        }
        self._set(service, "run_tmux", lambda *args, socket=None: (
            0, "architect\t1786283999\nsme-2\t1786283580", ""
        ))
        _watchdog(r).poll(now=NOW)
        alert = json.loads(r.streams[prefix(POD, TENANT, resource="alerts")][0][1]["alert"])
        self.assertEqual(alert["kind"], "stalled")
        self.assertEqual(alert["blocked"], {
            "since": "2026-08-09T13:53:00Z",
            "stream_id": "delivery-1",
            "unconsumed_s": 420,
        })

    # -- doing duration -------------------------------------------------------

    def test_doing_duration_messages_the_lead_directly_not_the_alerts_stream(self):
        r = FakeRedis()
        _doing_agent(r)
        _lead(r)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)

        _watchdog(r).poll(now=NOW)

        self.assertNotIn(prefix(POD, TENANT, resource="alerts"), r.streams)
        ingress = r.lists[_key("architect", "ingress")]
        self.assertEqual(len(ingress), 1)
        envelope = parse(ingress[0])
        self.assertEqual(envelope["kind"], "Message")
        self.assertEqual(envelope["l2"]["source"], "watchdog")
        self.assertEqual(envelope["l2"]["destination"], "architect")
        self.assertEqual(envelope["payload"]["text"], (
            '[alert from watchdog] sme-2 has been working on '
            '"review the auth change" for 15 min, request an update'
        ))
        self.assertEqual(kicks, ["architect"])

    def test_doing_duration_does_not_fire_before_fifteen_minutes(self):
        r = FakeRedis()
        _doing_agent(r, started="2026-08-09T13:46:00Z")  # 840s old, under the 900s default
        _lead(r)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", _no_kick())

        _watchdog(r).poll(now=NOW)

        self.assertNotIn(_key("architect", "ingress"), r.lists)

    def test_doing_duration_does_not_repeat_within_the_same_threshold_crossing(self):
        r = FakeRedis()
        _doing_agent(r)
        _lead(r)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)
        watchdog = _watchdog(r)

        watchdog.poll(now=NOW)
        watchdog.poll(now=NOW)
        watchdog.poll(now=NOW + timedelta(seconds=60))

        self.assertEqual(len(kicks), 1)
        self.assertEqual(len(r.lists[_key("architect", "ingress")]), 1)

    def test_doing_duration_re_alerts_at_the_next_threshold_crossing(self):
        r = FakeRedis()
        _doing_agent(r)
        _lead(r)
        kicks, fake = _kicks()
        watchdog = _watchdog(r)

        self._set(service, "run_tmux", _quiet_windows(NOW))
        self._set(service, "deliver_tmux", fake)
        watchdog.poll(now=NOW)
        self.assertEqual(len(kicks), 1)

        later = NOW + timedelta(seconds=900)
        self._set(service, "run_tmux", _quiet_windows(later))
        watchdog.poll(now=later)
        self.assertEqual(len(kicks), 2)
        envelope = parse(r.lists[_key("architect", "ingress")][-1])
        self.assertIn("30 min", envelope["payload"]["text"])

    def test_doing_duration_different_ticket_resets_and_re_alerts(self):
        r = FakeRedis()
        _doing_agent(r)
        _lead(r)
        kicks, fake = _kicks()
        watchdog = _watchdog(r)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)
        watchdog.poll(now=NOW)
        self.assertEqual(len(kicks), 1)

        _doing_agent(r, started="2026-08-09T13:45:00Z", ticket_id="ticket-2", title="fix the flaky test")
        watchdog.poll(now=NOW)
        self.assertEqual(len(kicks), 2)

    def test_doing_duration_does_nothing_without_a_configured_lead(self):
        r = FakeRedis()
        _doing_agent(r)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", _no_kick())

        _watchdog(r).poll(now=NOW)

        self.assertNotIn(_key("architect", "ingress"), r.lists)

    def test_notify_lead_drops_the_alert_when_the_lead_ingress_is_full(self):
        r = FakeRedis()
        _doing_agent(r)
        _lead(r)
        r.lists[_key("architect", "ingress")] = ["x"] * 300  # INGRESS_MAX default
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", _no_kick())

        out = _capture(lambda: _watchdog(r).poll(now=NOW))

        self.assertEqual(len(r.lists[_key("architect", "ingress")]), 300)
        events = [json.loads(line) for line in out.splitlines()]
        self.assertTrue(any(event.get("event") == "lead_alert_capacity" for event in events))
        _assert_admission_only_evidence(self, _watchdog_records(out))

    def test_notify_lead_logs_unknown_and_does_not_kick_on_a_redis_fault(self):
        r = FakeRedis(fails_on={"eval": ConnectionError})
        _doing_agent(r)
        _lead(r)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", _no_kick())

        out = _capture(lambda: _watchdog(r).poll(now=NOW))

        events = [json.loads(line) for line in out.splitlines()]
        self.assertTrue(any(event.get("event") == "lead_alert_unknown" for event in events))
        _assert_admission_only_evidence(self, _watchdog_records(out))

    def test_notify_lead_never_logs_the_admission_exceptions_own_message(self):
        """Reviewer's exact reproduction: an admission-time exception whose
        MESSAGE carries content nothing here can bound (a connection
        string, a key name, arbitrary backend text) must never reach the
        durable record. Only a closed CATEGORY this module names may cross
        that boundary -- not `str(exc)`, not anywhere in the record."""
        r = FakeRedis(fails_on={"eval": ConnectionError("REMOTE_SECRET_EXCEPTION_TEXT")})
        _doing_agent(r)
        _lead(r)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", _no_kick())

        out = _capture(lambda: _watchdog(r).poll(now=NOW))

        self.assertNotIn("REMOTE_SECRET_EXCEPTION_TEXT", out)
        events = [json.loads(line) for line in out.splitlines()]
        unknown = [event for event in events if event.get("event") == "lead_alert_unknown"]
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0].get("reason"), "admission outcome UNKNOWN after a connection_error")

    def test_notify_lead_logs_a_record_when_the_lead_is_not_a_registered_agent(self):
        """A dangling `lead` key pointing at a retired agent must not vanish
        silently -- there is currently no way to transfer leadership, so this
        is a reachable state, not a hypothetical."""
        r = FakeRedis()
        _doing_agent(r)
        _lead(r, name="retired-lead")  # never registered in this FakeRedis
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", _no_kick())

        out = _capture(lambda: _watchdog(r).poll(now=NOW))

        self.assertNotIn(_key("retired-lead", "ingress"), r.lists)
        events = [json.loads(line) for line in out.splitlines()]
        no_lead = [e for e in events if e.get("event") == "lead_alert_no_lead"]
        self.assertEqual(len(no_lead), 1)
        self.assertEqual(no_lead[0]["destination"], "retired-lead")
        self.assertIn("not a registered agent", no_lead[0]["reason"])
        self.assertTrue(no_lead[0].get("stream_id"))
        _assert_admission_only_evidence(self, _watchdog_records(out, destination="retired-lead"))

    def test_notify_lead_logs_a_record_when_the_lead_is_not_a_tmux_agent(self):
        r = FakeRedis()
        _doing_agent(r)
        _lead(r, name="api")  # registered, but as the api client, not tmux
        r.hashes[prefix(POD, TENANT, resource="registry")]["api"] = "api"
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", _no_kick())

        out = _capture(lambda: _watchdog(r).poll(now=NOW))

        self.assertNotIn(_key("api", "ingress"), r.lists)
        events = [json.loads(line) for line in out.splitlines()]
        no_lead = [e for e in events if e.get("event") == "lead_alert_no_lead"]
        self.assertEqual(len(no_lead), 1)
        self.assertEqual(no_lead[0]["destination"], "api")
        self.assertIn("port_type is not tmux", no_lead[0]["reason"])
        _assert_admission_only_evidence(self, _watchdog_records(out, destination="api"))

    def test_lead_window_missing_dead_letters_with_a_real_record_not_replayed(self):
        """A registered tmux lead whose window is merely missing right now
        (recreate in progress) still gets a real custody record: admitted to
        ingress, then dead-lettered by modules.tmux.port's own DeadLetter
        handling when the pop finds no window. Deliberately not replayed
        when the window returns -- see _notify_lead's docstring."""
        r = FakeRedis()
        _doing_agent(r)
        _lead(r)
        self._set(service, "run_tmux", _quiet_windows())
        with patch("modules.tmux.port.list_windows", return_value=set()):
            out = _capture(lambda: _watchdog(r).poll(now=NOW))

        self.assertEqual(r.lists[_key("architect", "ingress")], [])
        dead = r.lists[_key("architect", "dead")]
        self.assertEqual(len(dead), 1)
        events = [json.loads(line) for line in out.splitlines()]
        dead_lettered = [e for e in events if e.get("event") == "dead_lettered"]
        self.assertEqual(len(dead_lettered), 1)
        self.assertEqual(dead_lettered[0]["reason"], "window_missing")
        self.assertEqual(dead_lettered[0]["destination"], "architect")
        # Something was recorded about the alert attempt (the original
        # defect was silence, not a naming choice) -- but it must not claim
        # delivery either, since admission is genuinely all that's known
        # here. See test_admission_only_logs_as_admitted_never_as_sent_or_
        # delivered below for the dedicated claim-vs-vocabulary check.
        _assert_admission_only_evidence(self, _watchdog_records(out))

    def test_admission_only_logs_as_admitted_never_as_sent_or_delivered(self):
        """ALLOCATED/ADMITTED/CREATED discipline: at the point admit_ingress
        succeeds, only ADMITTED (durably queued) is known -- deliver_tmux
        has not even been called yet, let alone confirmed. Something must
        be recorded (silence was the original defect), but whatever it says
        must not claim delivery.

        Asserts the CONTRACT, not a word list. Scanning specific fields (or
        even every field, for specific overclaiming words) still lets a
        genuinely novel claim-shaped vocabulary slip through unnoticed --
        an implementation could always find a new way to say "delivered"
        that a hardcoded word list never enumerated. The actual fix: every
        watchdog record now carries a canonical `evidence` tag
        (core.logging.log_record's `evidence` field) drawn from a CLOSED
        allowlist -- `_assert_admission_only_evidence` rejects anything not
        explicitly in it, including a vocabulary nobody has thought of yet,
        not just the two words this ticket happened to start with. See
        AdmissionEvidenceContractTests below for direct counterfixtures
        pinning that the allowlist itself actually rejects an overclaim
        (in `evidence` specifically, and a missing tag entirely) rather
        than only being exercised indirectly through this scenario.

        History: an earlier version of this test asserted the literal
        string `lead_alert_admitted` as the event name; a correct alternate
        implementation using one event name with a `state` field would have
        broken it. The version after that scanned "event"/"state" fields
        for overclaiming words; reviewer's counterexample
        (outcome="delivered", reason="alert sent") showed a fixed field
        list has the same shape of gap with more entries. This version is
        the third: a closed contract instead of an enumerated one.

        Same window-missing scenario as the dead-letter test above -- this
        one exists specifically to pin the claim discipline, not the
        dead-letter mechanics."""
        r = FakeRedis()
        _doing_agent(r)
        _lead(r)
        self._set(service, "run_tmux", _quiet_windows())
        with patch("modules.tmux.port.list_windows", return_value=set()):
            out = _capture(lambda: _watchdog(r).poll(now=NOW))

        _assert_admission_only_evidence(self, _watchdog_records(out))

    def test_no_delivery_claim_follows_a_successful_deliver_tmux_call(self):
        """No claim stronger than ADMITTED follows a successful deliver_tmux
        call either. deliver_tmux -> core.channels.receive() catches
        DeadLetter internally and returns normally either way, so "no
        exception raised" cannot distinguish a real delivery from an
        internal dead-letter -- there is no honest claim to make beyond
        ADMITTED at this call site. What actually happened during delivery
        is already recorded by channels.receive() itself
        (received/dead_lettered/opened, under module="tmux")."""
        r = FakeRedis()
        _doing_agent(r)
        _lead(r)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)

        out = _capture(lambda: _watchdog(r).poll(now=NOW))

        _assert_admission_only_evidence(self, _watchdog_records(out))

    # -- ack loop -------------------------------------------------------------

    def test_ack_loop_fires_when_both_directions_cross_the_threshold(self):
        r = FakeRedis()
        _lead(r)
        _ack(r, "frontend", "backend", streak=3)
        _ack(r, "backend", "frontend", streak=3)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)

        _watchdog(r).poll(now=NOW)

        self.assertEqual(len(kicks), 1)
        envelope = parse(r.lists[_key("architect", "ingress")][0])
        self.assertEqual(envelope["l2"]["source"], "watchdog")
        self.assertIn("backend", envelope["payload"]["text"])
        self.assertIn("frontend", envelope["payload"]["text"])
        self.assertIn("ack-looping", envelope["payload"]["text"])
        self.assertIn("3 closing replies", envelope["payload"]["text"])

    def test_ack_loop_does_not_fire_when_only_one_direction_crosses_the_threshold(self):
        r = FakeRedis()
        _lead(r)
        _ack(r, "frontend", "backend", streak=5)
        _ack(r, "backend", "frontend", streak=1)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", _no_kick())

        _watchdog(r).poll(now=NOW)

        self.assertNotIn(_key("architect", "ingress"), r.lists)

    def test_ack_loop_ignores_a_stale_streak(self):
        r = FakeRedis()
        _lead(r)
        stale = (NOW - timedelta(seconds=300)).isoformat().replace("+00:00", "Z")
        _ack(r, "frontend", "backend", streak=3, last_ts=stale)
        _ack(r, "backend", "frontend", streak=3, last_ts=stale)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", _no_kick())

        _watchdog(r).poll(now=NOW)

        self.assertNotIn(_key("architect", "ingress"), r.lists)

    def test_ack_loop_does_not_repeat_within_the_same_crossing(self):
        r = FakeRedis()
        _lead(r)
        _ack(r, "frontend", "backend", streak=3)
        _ack(r, "backend", "frontend", streak=3)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)
        watchdog = _watchdog(r)

        watchdog.poll(now=NOW)
        watchdog.poll(now=NOW)

        self.assertEqual(len(kicks), 1)

    def test_ack_loop_re_alerts_after_the_streak_doubles(self):
        r = FakeRedis()
        _lead(r)
        _ack(r, "frontend", "backend", streak=3)
        _ack(r, "backend", "frontend", streak=3)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)
        watchdog = _watchdog(r)
        watchdog.poll(now=NOW)
        self.assertEqual(len(kicks), 1)

        _ack(r, "frontend", "backend", streak=6)
        _ack(r, "backend", "frontend", streak=6)
        watchdog.poll(now=NOW)
        self.assertEqual(len(kicks), 2)

        # Still below the next crossing (12) -- no third alert.
        _ack(r, "frontend", "backend", streak=7)
        _ack(r, "backend", "frontend", streak=7)
        watchdog.poll(now=NOW)
        self.assertEqual(len(kicks), 2)

    def test_ack_loop_clears_state_once_a_side_sends_real_content(self):
        r = FakeRedis()
        _lead(r)
        _ack(r, "frontend", "backend", streak=3)
        _ack(r, "backend", "frontend", streak=3)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)
        watchdog = _watchdog(r)
        watchdog.poll(now=NOW)
        self.assertEqual(len(kicks), 1)

        # backend sent something substantive -- send() would have hdel'd this edge.
        del r.hashes[_key("backend", "acks")]["frontend"]
        _ack(r, "frontend", "backend", streak=4)
        watchdog.poll(now=NOW)
        self.assertEqual(len(kicks), 1)
        self.assertFalse(r.hashes.get(_key("frontend", "ack-loop.alerted")))

    def test_ack_loop_does_nothing_without_a_configured_lead(self):
        r = FakeRedis()
        _ack(r, "frontend", "backend", streak=3)
        _ack(r, "backend", "frontend", streak=3)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", _no_kick())

        _watchdog(r).poll(now=NOW)

    # -- todo duration --------------------------------------------------------

    def test_todo_duration_messages_the_lead_directly_not_the_alerts_stream(self):
        r = FakeRedis()
        _todo_agent(r)
        _lead(r)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)

        _watchdog(r).poll(now=NOW)

        self.assertNotIn(prefix(POD, TENANT, resource="alerts"), r.streams)
        ingress = r.lists[_key("architect", "ingress")]
        self.assertEqual(len(ingress), 1)
        envelope = parse(ingress[0])
        self.assertEqual(envelope["l2"]["source"], "watchdog")
        self.assertEqual(envelope["l2"]["destination"], "architect")
        self.assertEqual(envelope["payload"]["text"], (
            '[alert from watchdog] sme-2 has an unpicked ticket '
            '"pick up the auth review" waiting 5 min'
        ))
        self.assertEqual(kicks, ["architect"])

    def test_todo_duration_does_not_fire_for_a_ticket_queued_behind_active_work(self):
        """A todo ticket behind an agent's current `doing` ticket cannot be
        started yet no matter how old it gets -- that is queueing, not
        neglect (confirmed live: this fired on tickets deliberately queued
        behind active work)."""
        r = FakeRedis()
        _todo_agent(r)
        # started recently -- must not also cross doing-duration's own
        # threshold and confound this test with a different alert.
        _doing_agent(r, started="2026-08-09T13:55:00Z")
        _lead(r)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", _no_kick())

        _watchdog(r).poll(now=NOW)

        self.assertNotIn(_key("architect", "ingress"), r.lists)

    def test_todo_duration_fires_once_the_agent_finishes_its_doing_ticket(self):
        r = FakeRedis()
        _todo_agent(r)
        _doing_agent(r, started="2026-08-09T13:55:00Z")
        _lead(r)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", _no_kick())
        watchdog = _watchdog(r)
        watchdog.poll(now=NOW)
        self.assertNotIn(_key("architect", "ingress"), r.lists)

        r.lists[_key("sme-2", "tasks.doing")] = []  # finished or held
        kicks, fake = _kicks()
        self._set(service, "deliver_tmux", fake)
        watchdog.poll(now=NOW)
        self.assertEqual(len(kicks), 1)

    def test_todo_duration_does_not_fire_before_five_minutes(self):
        r = FakeRedis()
        _todo_agent(r, created="2026-08-09T13:56:00Z")  # 240s old, under the 300s default
        _lead(r)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", _no_kick())

        _watchdog(r).poll(now=NOW)

        self.assertNotIn(_key("architect", "ingress"), r.lists)

    def test_todo_duration_does_not_repeat_within_the_same_threshold_crossing(self):
        r = FakeRedis()
        _todo_agent(r)
        _lead(r)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)
        watchdog = _watchdog(r)

        watchdog.poll(now=NOW)
        watchdog.poll(now=NOW)
        watchdog.poll(now=NOW + timedelta(seconds=60))

        self.assertEqual(len(kicks), 1)
        self.assertEqual(len(r.lists[_key("architect", "ingress")]), 1)

    def test_todo_duration_re_alerts_at_the_next_threshold_crossing(self):
        r = FakeRedis()
        _todo_agent(r)
        _lead(r)
        kicks, fake = _kicks()
        watchdog = _watchdog(r)

        self._set(service, "run_tmux", _quiet_windows(NOW))
        self._set(service, "deliver_tmux", fake)
        watchdog.poll(now=NOW)
        self.assertEqual(len(kicks), 1)

        later = NOW + timedelta(seconds=300)
        self._set(service, "run_tmux", _quiet_windows(later))
        watchdog.poll(now=later)
        self.assertEqual(len(kicks), 2)
        envelope = parse(r.lists[_key("architect", "ingress")][-1])
        self.assertIn("10 min", envelope["payload"]["text"])

    def test_todo_duration_different_ticket_resets_and_re_alerts(self):
        r = FakeRedis()
        _todo_agent(r)
        _lead(r)
        kicks, fake = _kicks()
        watchdog = _watchdog(r)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)
        watchdog.poll(now=NOW)
        self.assertEqual(len(kicks), 1)

        _todo_agent(r, created="2026-08-09T13:55:00Z", ticket_id="ticket-2", title="fix the flaky test")
        watchdog.poll(now=NOW)
        self.assertEqual(len(kicks), 2)

    def test_todo_duration_tracks_each_queued_ticket_independently(self):
        r = FakeRedis()
        _todo_agent(r, created="2026-08-09T13:55:00Z", ticket_id="ticket-1", title="old enough")
        _todo_agent(r, created="2026-08-09T13:58:00Z", ticket_id="ticket-2", title="too new", append=True)
        _lead(r)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)

        _watchdog(r).poll(now=NOW)

        self.assertEqual(len(kicks), 1)
        envelope = parse(r.lists[_key("architect", "ingress")][-1])
        self.assertIn("old enough", envelope["payload"]["text"])

    def test_todo_duration_drops_state_for_a_ticket_no_longer_in_todo(self):
        r = FakeRedis()
        _todo_agent(r)
        _lead(r)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", lambda *a, **kw: None)
        watchdog = _watchdog(r)
        watchdog.poll(now=NOW)
        self.assertEqual(r.hashes[_key("sme-2", "todo.alerted")], {"ticket-1": "1"})

        r.lists[_key("sme-2", "tasks.todo")] = []  # taken, cancelled, or deleted
        watchdog.poll(now=NOW)
        self.assertEqual(r.hashes[_key("sme-2", "todo.alerted")], {})

    def test_todo_duration_does_nothing_without_a_configured_lead(self):
        r = FakeRedis()
        _todo_agent(r)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", _no_kick())

        _watchdog(r).poll(now=NOW)

        self.assertNotIn(_key("architect", "ingress"), r.lists)

    # -- hold duration --------------------------------------------------------

    def test_hold_duration_messages_the_lead_directly_not_the_alerts_stream(self):
        r = FakeRedis()
        _hold_agent(r)
        _lead(r)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)

        _watchdog(r).poll(now=NOW)

        self.assertNotIn(prefix(POD, TENANT, resource="alerts"), r.streams)
        ingress = r.lists[_key("architect", "ingress")]
        self.assertEqual(len(ingress), 1)
        envelope = parse(ingress[0])
        self.assertEqual(envelope["l2"]["source"], "watchdog")
        self.assertEqual(envelope["l2"]["destination"], "architect")
        self.assertEqual(envelope["payload"]["text"], (
            '[alert from watchdog] sme-2 has had '
            '"wait on the vendor reply" on hold for 60 min'
        ))
        self.assertEqual(kicks, ["architect"])

    def test_hold_duration_carries_the_hold_reason_into_the_alert_text(self):
        r = FakeRedis()
        _hold_agent(r, reason="waiting on vendor confirmation")
        _lead(r)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)

        _watchdog(r).poll(now=NOW)

        envelope = parse(r.lists[_key("architect", "ingress")][-1])
        self.assertEqual(envelope["payload"]["text"], (
            '[alert from watchdog] sme-2 has had "wait on the vendor reply" '
            'on hold for 60 min (reason: waiting on vendor confirmation)'
        ))
        self.assertEqual(kicks, ["architect"])

    def test_hold_duration_normalizes_embedded_whitespace_in_the_reason(self):
        """Matches office list's own normalization of `hold_reason` -- an
        unnormalized multi-line reason would make one alert read like
        several separate messages."""
        r = FakeRedis()
        _hold_agent(r, reason="waiting on vendor\n\nconfirmation   still   pending")
        _lead(r)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)

        _watchdog(r).poll(now=NOW)

        envelope = parse(r.lists[_key("architect", "ingress")][-1])
        self.assertEqual(envelope["payload"]["text"], (
            '[alert from watchdog] sme-2 has had "wait on the vendor reply" '
            'on hold for 60 min (reason: waiting on vendor confirmation still pending)'
        ))

    def test_hold_duration_still_fires_without_a_reason_on_a_legacy_entry(self):
        """A ticket held before --reason was mandatory has no `hold_reason`
        at all -- must degrade gracefully, not crash or suppress the alert."""
        r = FakeRedis()
        _hold_agent(r)  # no reason= passed
        _lead(r)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)

        _watchdog(r).poll(now=NOW)

        envelope = parse(r.lists[_key("architect", "ingress")][-1])
        self.assertEqual(envelope["payload"]["text"], (
            '[alert from watchdog] sme-2 has had "wait on the vendor reply" '
            'on hold for 60 min'
        ))
        self.assertEqual(kicks, ["architect"])

    def test_hold_duration_still_fires_with_a_reason_present(self):
        """The goal is fewer false alerts, not fewer alerts -- a reason
        explains why the wait started, not that it is still justified;
        having one must not silence or delay the alert."""
        r = FakeRedis()
        _hold_agent(r, reason="deliberately parked, revisit later")
        _lead(r)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)

        _watchdog(r).poll(now=NOW)

        self.assertEqual(len(kicks), 1)

    def test_hold_duration_does_not_fire_before_sixty_minutes(self):
        r = FakeRedis()
        _hold_agent(r, held="2026-08-09T13:01:00Z")  # 3540s old, under the 3600s default
        _lead(r)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", _no_kick())

        _watchdog(r).poll(now=NOW)

        self.assertNotIn(_key("architect", "ingress"), r.lists)

    def test_hold_duration_falls_back_to_created_ts_when_held_ts_is_missing(self):
        """Same precedent as office/cli.py's own `_ticket_age` for `hold`."""
        r = FakeRedis()
        _hold_agent(r, held=None, created="2026-08-09T13:00:00Z")
        _lead(r)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)

        _watchdog(r).poll(now=NOW)

        self.assertEqual(len(kicks), 1)

    def test_hold_duration_does_not_repeat_within_the_same_threshold_crossing(self):
        r = FakeRedis()
        _hold_agent(r)
        _lead(r)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)
        watchdog = _watchdog(r)

        watchdog.poll(now=NOW)
        watchdog.poll(now=NOW)
        watchdog.poll(now=NOW + timedelta(seconds=60))

        self.assertEqual(len(kicks), 1)
        self.assertEqual(len(r.lists[_key("architect", "ingress")]), 1)

    def test_hold_duration_re_alerts_at_the_next_threshold_crossing(self):
        r = FakeRedis()
        _hold_agent(r)
        _lead(r)
        kicks, fake = _kicks()
        watchdog = _watchdog(r)

        self._set(service, "run_tmux", _quiet_windows(NOW))
        self._set(service, "deliver_tmux", fake)
        watchdog.poll(now=NOW)
        self.assertEqual(len(kicks), 1)

        later = NOW + timedelta(seconds=3600)
        self._set(service, "run_tmux", _quiet_windows(later))
        watchdog.poll(now=later)
        self.assertEqual(len(kicks), 2)
        envelope = parse(r.lists[_key("architect", "ingress")][-1])
        self.assertIn("120 min", envelope["payload"]["text"])

    def test_hold_duration_different_ticket_resets_and_re_alerts(self):
        r = FakeRedis()
        _hold_agent(r)
        _lead(r)
        kicks, fake = _kicks()
        watchdog = _watchdog(r)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)
        watchdog.poll(now=NOW)
        self.assertEqual(len(kicks), 1)

        _hold_agent(r, held="2026-08-09T13:00:00Z", ticket_id="ticket-2", title="wait on the other vendor")
        watchdog.poll(now=NOW)
        self.assertEqual(len(kicks), 2)

    def test_hold_duration_tracks_each_held_ticket_independently(self):
        r = FakeRedis()
        _hold_agent(r, held="2026-08-09T13:00:00Z", ticket_id="ticket-1", title="old enough")
        _hold_agent(r, held="2026-08-09T13:30:00Z", ticket_id="ticket-2", title="too new", append=True)
        _lead(r)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)

        _watchdog(r).poll(now=NOW)

        self.assertEqual(len(kicks), 1)
        envelope = parse(r.lists[_key("architect", "ingress")][-1])
        self.assertIn("old enough", envelope["payload"]["text"])

    def test_hold_duration_drops_state_for_a_ticket_no_longer_on_hold(self):
        r = FakeRedis()
        _hold_agent(r)
        _lead(r)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", lambda *a, **kw: None)
        watchdog = _watchdog(r)
        watchdog.poll(now=NOW)
        self.assertEqual(r.hashes[_key("sme-2", "hold.alerted")], {"ticket-1": "1"})

        r.lists[_key("sme-2", "tasks.hold")] = []  # resumed, cancelled, or deleted
        watchdog.poll(now=NOW)
        self.assertEqual(r.hashes[_key("sme-2", "hold.alerted")], {})

    def test_hold_duration_does_nothing_without_a_configured_lead(self):
        r = FakeRedis()
        _hold_agent(r)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", _no_kick())

        _watchdog(r).poll(now=NOW)

        self.assertNotIn(_key("architect", "ingress"), r.lists)

    # -- unreplied duration -----------------------------------------------------

    def test_unreplied_duration_messages_the_lead_directly_not_the_alerts_stream(self):
        r = FakeRedis()
        _unreplied_agent(r)  # 60s old, at the 60s default
        _lead(r)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)

        _watchdog(r).poll(now=NOW)

        self.assertNotIn(prefix(POD, TENANT, resource="alerts"), r.streams)
        ingress = r.lists[_key("architect", "ingress")]
        self.assertEqual(len(ingress), 1)
        envelope = parse(ingress[0])
        self.assertEqual(envelope["l2"]["source"], "watchdog")
        self.assertEqual(envelope["l2"]["destination"], "architect")
        self.assertEqual(envelope["payload"]["text"],
            "[alert from watchdog] sme-2 has 1 unanswered message from telegram, oldest 1 min old"
        )
        self.assertEqual(kicks, ["architect"])

    def test_unreplied_duration_pluralizes_the_count(self):
        r = FakeRedis()
        _unreplied_agent(r, count=3)
        _lead(r)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)

        _watchdog(r).poll(now=NOW)

        envelope = parse(r.lists[_key("architect", "ingress")][-1])
        self.assertIn("3 unanswered messages from telegram", envelope["payload"]["text"])

    def test_unreplied_duration_does_not_fire_before_one_minute(self):
        r = FakeRedis()
        _unreplied_agent(r, since="2026-08-09T13:59:30Z")  # 30s old, under the 60s default
        _lead(r)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", _no_kick())

        _watchdog(r).poll(now=NOW)

        self.assertNotIn(_key("architect", "ingress"), r.lists)

    def test_unreplied_duration_does_not_repeat_before_the_backoff_doubles(self):
        r = FakeRedis()
        _unreplied_agent(r)  # 60s old at NOW
        _lead(r)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)
        watchdog = _watchdog(r)

        watchdog.poll(now=NOW)
        watchdog.poll(now=NOW)
        watchdog.poll(now=NOW + timedelta(seconds=30))  # 90s old, under the next 120s threshold

        self.assertEqual(len(kicks), 1)
        self.assertEqual(len(r.lists[_key("architect", "ingress")]), 1)

    def test_unreplied_duration_re_alerts_back_off_exponentially(self):
        """A quick miss still nags fast; a long one does not nag every minute."""
        r = FakeRedis()
        _unreplied_agent(r)  # 60s old at NOW
        _lead(r)
        kicks, fake = _kicks()
        watchdog = _watchdog(r)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)

        watchdog.poll(now=NOW)  # age 60s: first nag, next threshold 120s
        self.assertEqual(len(kicks), 1)

        watchdog.poll(now=NOW + timedelta(seconds=59))  # age 119s: still under 120s
        self.assertEqual(len(kicks), 1)

        watchdog.poll(now=NOW + timedelta(seconds=60))  # age 120s: second nag, next threshold 240s
        self.assertEqual(len(kicks), 2)
        envelope = parse(r.lists[_key("architect", "ingress")][-1])
        self.assertIn("2 min", envelope["payload"]["text"])

        watchdog.poll(now=NOW + timedelta(seconds=179))  # age 239s: still under 240s
        self.assertEqual(len(kicks), 2)

        watchdog.poll(now=NOW + timedelta(seconds=180))  # age 240s: third nag, next threshold 480s
        self.assertEqual(len(kicks), 3)
        envelope = parse(r.lists[_key("architect", "ingress")][-1])
        self.assertIn("4 min", envelope["payload"]["text"])

    def test_unreplied_duration_tracks_each_client_independently(self):
        r = FakeRedis()
        _unreplied_agent(r, client="telegram", since="2026-08-09T13:57:00Z")  # old enough
        _unreplied_agent(r, client="signal", since="2026-08-09T13:59:30Z")  # too new
        _lead(r)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)

        _watchdog(r).poll(now=NOW)

        self.assertEqual(len(kicks), 1)
        envelope = parse(r.lists[_key("architect", "ingress")][-1])
        self.assertIn("telegram", envelope["payload"]["text"])

    def test_unreplied_duration_drops_state_once_the_agent_replies(self):
        r = FakeRedis()
        _unreplied_agent(r)
        _lead(r)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", lambda *a, **kw: None)
        watchdog = _watchdog(r)
        watchdog.poll(now=NOW)
        self.assertEqual(r.hashes[_key("sme-2", "unreplied.alerted")], {"telegram": "60"})

        del r.hashes[_key("sme-2", "unreplied")]["telegram"]  # sme-2 replied
        watchdog.poll(now=NOW)
        self.assertEqual(r.hashes[_key("sme-2", "unreplied.alerted")], {})

    def test_unreplied_duration_does_nothing_without_a_configured_lead(self):
        r = FakeRedis()
        _unreplied_agent(r)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", _no_kick())

        _watchdog(r).poll(now=NOW)

        self.assertNotIn(_key("architect", "ingress"), r.lists)

    # -- run_observers ----------------------------------------------------------

    def test_watchdog_observers_each_get_their_own_try(self):
        """One failing observer must not silence the others.

        In the switch all five used to share a single try, so a throw in the
        first skipped the rest of the pass, and the record named only the
        exception class -- from a five-job block, which was close to
        undiagnosable.
        """
        calls, errors = [], []

        class Boom:
            def poll(self, agents):
                calls.append("boom")
                raise RuntimeError("nope")

        class Fine:
            def __init__(self, name):
                self.name = name

            def poll(self, agents):
                calls.append(self.name)

        class Recorder:
            def _error(self, job, exc):
                errors.append((job, str(exc)))

        failed = run_observers(
            Recorder(),
            (("activity", Boom()), ("presence", Fine("presence")), ("verification", Fine("verify"))),
            {"sme-2"},
        )

        self.assertEqual(calls, ["boom", "presence", "verify"], "a throw must not skip the rest")
        self.assertEqual(failed, ["activity"])
        self.assertEqual(errors, [("activity", "nope")], "the failing job is named, not just its class")


class WatchdogCredentialTests(unittest.TestCase):
    def setUp(self):
        self._live_patches = []
        self.tmp_path = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp_path, ignore_errors=True)

    def tearDown(self):
        for p in self._live_patches:
            p.stop()

    def _set(self, target, name, value):
        p = patch.object(target, name, value)
        p.start()
        self._live_patches.append(p)

    def _delenv(self, name):
        p = patch.dict(os.environ)
        p.start()
        self._live_patches.append(p)
        os.environ.pop(name, None)

    def _setenv(self, name, value):
        p = patch.dict(os.environ)
        p.start()
        self._live_patches.append(p)
        os.environ[name] = value

    def test_credentials_warn_on_claude_refresh_expiry_and_codex_is_unknown(self):
        r = FakeRedis()
        self._delenv("CLAUDE_OAUTH_TOKEN_DEFAULT")
        r.values[_key("architect", "launch")] = "claude"
        r.values[_key("sme-2", "launch")] = "codex"
        claude = self.tmp_path / ".claude"
        claude.mkdir()
        (claude / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"refreshTokenExpiresAt": "2026-08-12T14:00:00Z"}})
        )
        codex = self.tmp_path / ".codex"
        codex.mkdir()
        (codex / "auth.json").write_text("{}")

        watchdog = Watchdog(r, pod=POD, tenant=TENANT, session_name=TENANT, home_root=self.tmp_path)
        out = _capture(lambda: watchdog.check_credentials(now=NOW))

        alerts = [json.loads(fields["alert"]) for _, fields in r.streams[prefix(POD, TENANT, resource="alerts")]]
        self.assertEqual([(alert["cli"], alert["status"]) for alert in alerts], [
            ("claude", "expiring"),
            ("codex", "unknown"),
        ])
        self.assertTrue(all(alert["account"] == "default" for alert in alerts))
        self.assertEqual(len(out.splitlines()), 2)

    def test_profile_codex_is_unknown_even_without_an_auth_file(self):
        r = FakeRedis()
        r.values[_key("sme-2", "launch")] = "codex"
        r.values[_key("sme-2", "profile")] = "work"
        (self.tmp_path / ".codex-work").mkdir()
        _capture(lambda: Watchdog(
            r, pod=POD, tenant=TENANT, session_name=TENANT, home_root=self.tmp_path
        ).check_credentials(now=NOW))
        alerts = [json.loads(fields["alert"]) for _, fields in r.streams[prefix(POD, TENANT, resource="alerts")]]
        self.assertTrue(any(
            alert["account"] == "work" and alert["cli"] == "codex" and alert["status"] == "absent"
            for alert in alerts
        ))

    def test_claude_profile_token_is_authenticated_without_credentials_file(self):
        r = FakeRedis()
        r.values[_key("architect", "launch")] = "claude"
        r.values[_key("architect", "profile")] = "work"
        self._setenv("CLAUDE_OAUTH_TOKEN_WORK", "token-authenticated")

        out = _capture(lambda: Watchdog(
            r, pod=POD, tenant=TENANT, session_name=TENANT, home_root=self.tmp_path
        ).check_credentials(now=NOW))

        self.assertNotIn(prefix(POD, TENANT, resource="alerts"), r.streams)
        self.assertEqual(r.hashes.get(prefix(POD, TENANT, resource="credential.alerted"), {}), {})
        self.assertEqual(out, "")

    def test_claude_without_token_or_credentials_still_alerts_absent(self):
        r = FakeRedis()
        r.values[_key("architect", "launch")] = "claude"
        self._delenv("CLAUDE_OAUTH_TOKEN_DEFAULT")

        _capture(lambda: Watchdog(
            r, pod=POD, tenant=TENANT, session_name=TENANT, home_root=self.tmp_path
        ).check_credentials(now=NOW))

        alert = json.loads(r.streams[prefix(POD, TENANT, resource="alerts")][0][1]["alert"])
        self.assertEqual(alert["status"], "absent")

    def test_provider_agent_needs_no_vendor_credential_and_clears_stale_status(self):
        r = FakeRedis()
        r.values[_key("architect", "launch")] = "claude"
        r.values[_key("architect", "provider")] = "local-vllm"
        alerted_key = prefix(POD, TENANT, resource="credential.alerted")
        r.hashes[alerted_key] = {"default:claude": "absent"}

        Watchdog(
            r, pod=POD, tenant=TENANT, session_name=TENANT, home_root=self.tmp_path
        ).check_credentials(now=NOW)

        self.assertNotIn(prefix(POD, TENANT, resource="alerts"), r.streams)
        self.assertEqual(r.hashes[alerted_key], {})

    def test_credential_alert_retracted_when_credential_recovers(self):
        """Build 105 Sec.1: when a credential recovers, watchdog emits status=present and clears alerted hash."""
        r = FakeRedis()
        r.values[_key("architect", "launch")] = "claude"
        self._delenv("CLAUDE_OAUTH_TOKEN_DEFAULT")

        watchdog = Watchdog(r, pod=POD, tenant=TENANT, session_name=TENANT, home_root=self.tmp_path)
        alerted_key = prefix(POD, TENANT, resource="credential.alerted")
        alerts_key = prefix(POD, TENANT, resource="alerts")

        _capture(lambda: watchdog.check_credentials(now=NOW))
        alerts = [json.loads(fields["alert"]) for _, fields in r.streams[alerts_key]]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["status"], "absent")
        self.assertEqual(alerts[0]["cli"], "claude")
        self.assertEqual(alerts[0]["account"], "default")
        self.assertEqual(r.hashes[alerted_key], {"default:claude": "absent"})

        claude = self.tmp_path / ".claude"
        claude.mkdir()
        (claude / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"refreshTokenExpiresAt": "2026-09-12T14:00:00Z"}})
        )

        _capture(lambda: watchdog.check_credentials(now=NOW))
        alerts = [json.loads(fields["alert"]) for _, fields in r.streams[alerts_key]]
        self.assertEqual(len(alerts), 2)
        self.assertEqual(alerts[1]["status"], "present")
        self.assertEqual(alerts[1]["cli"], "claude")
        self.assertEqual(alerts[1]["account"], "default")
        self.assertEqual(alerts[1]["expires_ts"], "2026-09-12T14:00:00.000Z")
        self.assertEqual(r.hashes.get(alerted_key, {}), {})

        _capture(lambda: watchdog.check_credentials(now=NOW))
        alerts = [json.loads(fields["alert"]) for _, fields in r.streams[alerts_key]]
        self.assertEqual(len(alerts), 2)
        self.assertEqual(r.hashes.get(alerted_key, {}), {})

    def test_credential_alert_retracted_from_expiring_when_token_refreshed(self):
        """Build 105 Sec.1: when an expiring credential is refreshed, watchdog emits status=present."""
        r = FakeRedis()
        self._delenv("CLAUDE_OAUTH_TOKEN_DEFAULT")
        r.values[_key("architect", "launch")] = "claude"
        claude = self.tmp_path / ".claude"
        claude.mkdir()
        (claude / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"refreshTokenExpiresAt": "2026-08-12T14:00:00Z"}})
        )

        watchdog = Watchdog(r, pod=POD, tenant=TENANT, session_name=TENANT, home_root=self.tmp_path)
        alerted_key = prefix(POD, TENANT, resource="credential.alerted")
        alerts_key = prefix(POD, TENANT, resource="alerts")

        _capture(lambda: watchdog.check_credentials(now=NOW))
        alerts = [json.loads(fields["alert"]) for _, fields in r.streams[alerts_key]]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["status"], "expiring")
        self.assertEqual(r.hashes[alerted_key], {"default:claude": "expiring"})

        (claude / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"refreshTokenExpiresAt": "2026-09-09T14:00:00Z"}})
        )
        _capture(lambda: watchdog.check_credentials(now=NOW))
        alerts = [json.loads(fields["alert"]) for _, fields in r.streams[alerts_key]]
        self.assertEqual(len(alerts), 2)
        self.assertEqual(alerts[1]["status"], "present")
        self.assertEqual(alerts[1]["expires_ts"], "2026-09-09T14:00:00.000Z")
        self.assertEqual(r.hashes.get(alerted_key, {}), {})

        _capture(lambda: watchdog.check_credentials(now=NOW))
        self.assertEqual(len(r.streams[alerts_key]), 2)

    def test_agy_is_unknown_because_its_expiry_is_an_access_token(self):
        """Only claude records a refresh-token expiry.

        agy's `token.expiry` tracks its ACCESS token. Measured: the same file
        read hours apart showed the value moved forward while the login
        stayed valid -- the CLI refreshes it itself. Alerting on it fires
        constantly and correctly, which is the cry-wolf failure the
        credential check exists to avoid.
        """
        agy = self.tmp_path / ".gemini" / "antigravity-cli"
        agy.mkdir(parents=True)
        (agy / "antigravity-oauth-token").write_text(
            json.dumps({"token": {"access_token": "x", "refresh_token": "y",
                                  "expiry": "2020-01-01T00:00:00Z"}})
        )

        r = FakeRedis()
        r.values[_key("architect", "launch")] = "agy"
        _capture(lambda: Watchdog(
            r, pod=POD, tenant=TENANT, session_name=TENANT, home_root=self.tmp_path
        ).check_credentials(now=NOW))

        alerts = [json.loads(f["alert"]) for _, f in r.streams.get(prefix(POD, TENANT, resource="alerts"), [])]
        agy_alerts = [a for a in alerts if a.get("cli") == "agy"]
        self.assertTrue(agy_alerts, "agy should still be reported")
        self.assertEqual(agy_alerts[0]["status"], "unknown", "never 'expiring' from an access token")

    def test_missing_credentials_alert_once_per_account_in_use_and_clear_on_reseed(self):
        r = FakeRedis()
        r.values[_key("architect", "launch")] = "claude"
        r.values[_key("architect", "profile")] = "work"
        r.values[_key("sme-2", "launch")] = "claude"
        r.values[_key("sme-2", "profile")] = "work"
        watchdog = Watchdog(r, pod=POD, tenant=TENANT, session_name=TENANT, home_root=self.tmp_path)

        watchdog.check_credentials(now=NOW)
        watchdog.check_credentials(now=NOW)
        alerts_key = prefix(POD, TENANT, resource="alerts")
        alerts = [json.loads(fields["alert"]) for _, fields in r.streams[alerts_key]]
        self.assertEqual([(alert["account"], alert["cli"], alert["status"]) for alert in alerts], [
            ("work", "claude", "absent")
        ])

        credentials = self.tmp_path / ".claude-work" / ".credentials.json"
        credentials.parent.mkdir()
        credentials.write_text(
            json.dumps({"claudeAiOauth": {"refreshTokenExpiresAt": "2026-09-12T14:00:00Z"}})
        )
        watchdog.check_credentials(now=NOW)
        watchdog.check_credentials(now=NOW)
        alerts = [json.loads(fields["alert"]) for _, fields in r.streams[alerts_key]]
        self.assertEqual([(alert["account"], alert["cli"], alert["status"]) for alert in alerts], [
            ("work", "claude", "absent"),
            ("work", "claude", "present"),
        ])
        self.assertEqual(r.hashes[prefix(POD, TENANT, resource="credential.alerted")], {})

    def test_missing_credentials_alert_for_each_cli_account_in_use(self):
        r = FakeRedis()
        self._delenv("CLAUDE_OAUTH_TOKEN_DEFAULT")
        r.hashes[prefix(POD, TENANT, resource="registry")]["sme-3"] = "tmux"
        r.values[_key("architect", "launch")] = "claude"
        r.values[_key("sme-2", "launch")] = "codex"
        r.values[_key("sme-3", "launch")] = "agy"

        Watchdog(
            r, pod=POD, tenant=TENANT, session_name=TENANT, home_root=self.tmp_path
        ).check_credentials(now=NOW)

        alerts = [
            json.loads(fields["alert"])
            for _, fields in r.streams[prefix(POD, TENANT, resource="alerts")]
        ]
        self.assertEqual({(alert["cli"], alert["status"]) for alert in alerts}, {
            ("agy", "absent"),
            ("claude", "absent"),
            ("codex", "absent"),
        })

    def test_unused_profile_directory_does_not_alert(self):
        r = FakeRedis()
        r.values[_key("architect", "launch")] = "claude"
        default = self.tmp_path / ".claude"
        default.mkdir()
        (default / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"refreshTokenExpiresAt": "2026-09-12T14:00:00Z"}})
        )
        (self.tmp_path / ".claude-unused").mkdir()

        Watchdog(
            r, pod=POD, tenant=TENANT, session_name=TENANT, home_root=self.tmp_path
        ).check_credentials(now=NOW)

        self.assertNotIn(prefix(POD, TENANT, resource="alerts"), r.streams)


class WatchdogFailureIsolationTests(unittest.TestCase):
    def setUp(self):
        self._live_patches = []

    def tearDown(self):
        for p in self._live_patches:
            p.stop()

    def _set(self, target, name, value):
        p = patch.object(target, name, value)
        p.start()
        self._live_patches.append(p)

    def test_stall_failure_does_not_disable_blocked_check(self):
        r = FakeRedis()
        r.hashes[_key("sme-2", "blocked")] = {
            "since": "2026-08-09T13:53:00Z",
            "stream_id": "delivery-1",
        }
        watchdog = _watchdog(r)
        watchdog._window_activity = lambda: {}
        watchdog._check_stalls = lambda *args: (_ for _ in ()).throw(RuntimeError("bad board"))

        out = _capture(lambda: watchdog.poll(now=NOW))

        output = [json.loads(line) for line in out.splitlines()]
        self.assertEqual(output[0], {
            "module": "watchdog",
            "event": "error",
            "writer": "watchdog",
            "job": "stalls",
            "reason": "external-error",
        })
        self.assertTrue(any(record.get("kind") == "blocked" for record in output))


class WatchdogMainLoopTests(unittest.TestCase):
    def setUp(self):
        self._live_patches = []

    def tearDown(self):
        for p in self._live_patches:
            p.stop()

    def _set(self, target, name, value):
        p = patch.object(target, name, value)
        p.start()
        self._live_patches.append(p)

    def _env(self, **values):
        p = patch.dict(os.environ, values)
        p.start()
        self._live_patches.append(p)

    def _delenv(self, name):
        p = patch.dict(os.environ)
        p.start()
        self._live_patches.append(p)
        os.environ.pop(name, None)

    def test_observation_failure_does_not_disable_due_credential_check(self):
        calls = []

        class FailingWatchdog:
            def __init__(self, *args, **kwargs):
                pass

            def poll(self):
                calls.append("poll")
                raise RuntimeError("bad observations")

            def check_credentials(self):
                calls.append("credentials")

            def _agents(self):
                return set()

            _error = staticmethod(Watchdog._error)

        self._delenv("WATCHDOG_ENABLED")
        self._env(WATCHDOG_INTERVAL="0", REDIS_URL="redis://unused", POD=POD, TENANT=TENANT)
        self._set(service, "Watchdog", FailingWatchdog)
        self._set(service.redis.Redis, "from_url", lambda url: object())
        self._set(service.time, "monotonic", lambda: 0)

        def _boom_sleep(interval):
            raise StopIteration

        self._set(service.time, "sleep", _boom_sleep)

        with self.assertRaises(StopIteration):
            _capture_bytes = io.StringIO()
            with contextlib.redirect_stdout(_capture_bytes):
                service.main()
        out = _capture_bytes.getvalue()

        self.assertEqual(calls, ["poll", "credentials"])
        error = json.loads(out)
        self.assertEqual(error["job"], "observations")
        self.assertEqual(error["reason"], "external-error")

    def test_disabled_alerting_still_connects_because_observers_need_redis(self):
        """WATCHDOG_ENABLED=0 must still connect: the observers live in this
        process and read Redis, so exiting early would silence telemetry
        rather than alerts. The connection is the evidence that they still
        run even with alerting off.
        """
        connected = []
        self._env(WATCHDOG_ENABLED="0", REDIS_URL="redis://unused", POD=POD, TENANT=TENANT)
        self._set(service.redis.Redis, "from_url", lambda url: connected.append(url) or object())

        def _boom_sleep(s):
            raise StopIteration

        self._set(service.time, "sleep", _boom_sleep)
        self._set(service.time, "monotonic", lambda: 0)
        with self.assertRaises(StopIteration):
            service.main()
        self.assertTrue(connected, "observers need Redis even with alerting off")

    def test_alerting_disabled_still_runs_the_observers(self):
        """WATCHDOG_ENABLED silences ALERTS, not telemetry.

        Returning early would also stop ActivityTailer, PresenceSampler and
        DeliveryVerifier -- presence would read `unknown` forever, the
        activity stream would stay empty, and a client's progress indicator
        would break.
        """
        polled = []

        class Observer:
            def __init__(self, name):
                self.name = name

            def poll(self, agents):
                polled.append(self.name)
                raise StopIteration  # one pass, then out of the loop

        class QuietWatchdog:
            def __init__(self, *a, **kw):
                pass

            def poll(self):
                polled.append("ALERT")  # must never appear

            def check_credentials(self):
                polled.append("CREDENTIALS")

            def _agents(self):
                return {"sme-2"}

            _error = staticmethod(Watchdog._error)

        self._env(WATCHDOG_ENABLED="0", REDIS_URL="redis://unused", POD=POD, TENANT=TENANT)
        self._set(service, "Watchdog", QuietWatchdog)
        self._set(service.redis.Redis, "from_url", lambda url: object())
        self._set(service, "ActivityTailer", lambda *a, **kw: Observer("activity"))
        self._set(service, "PresenceSampler", lambda *a, **kw: Observer("presence"))
        self._set(service, "DeliveryVerifier", lambda *a, **kw: Observer("verify"))
        self._set(service.time, "monotonic", lambda: 0)

        def _boom_sleep(s):
            raise StopIteration

        self._set(service.time, "sleep", _boom_sleep)

        buf = io.StringIO()
        with self.assertRaises(StopIteration):
            with contextlib.redirect_stdout(buf):
                service.main()
        out = buf.getvalue()

        self.assertIn("activity", polled, "observers must run with alerting off")
        self.assertNotIn("ALERT", polled, "alerting must be silent")
        self.assertNotIn("CREDENTIALS", polled)
        self.assertIn('"event":"alerting_disabled"', out)


if __name__ == "__main__":
    unittest.main()
