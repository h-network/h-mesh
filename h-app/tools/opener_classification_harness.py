"""Verifier for OPENER CLASSIFICATION -- NOT A FIX.

Ticket 62ac7a13. The custody-conservation harness (tools/conservation_harness.py)
proves channels.py's transfer mechanics -- processing -> opening -> opened/
dead/unresolved -- GIVEN a raise or return the harness itself dictates. It
says NOTHING about whether a REAL opener sorts its own failures correctly.
That is a distinct property, checked here by driving the real, decorated
openers (lifecycle's start_agent through _record_lifecycle and office/port's
_lifecycle_opener; openshell's guarded()) through real Redis and real
custody transfer, never a synthetic raise standing in for the opener's own
classification logic.

⚠ THE PROPERTY: an envelope may reach `dead` ONLY when the failure is PROVEN
to have occurred BEFORE the external effect. Anything else -- unknown,
partial, response-lost -- must reach `unresolved`. Exception TYPE is not
proof of the mutation boundary; two real defects (reviewer's findings, this
session) show a type-based classifier can be lied to by an incidental type
match. This instrument asserts DESTINATIONS by envelope IDENTITY -- this one
reached dead, that one reached unresolved -- never which exception type was
raised, because the exception type is exactly the thing being questioned.

Like tools/conservation_harness.py: a script under tools/, run deliberately
against a real Redis, never wired into the pytest suite. It WILL fail
against branches carrying the unfixed defects -- that is the finding, not a
reason to suppress it, and it stays out of the suite until a fix lands, the
same discipline applied there.

Run: REDIS_URL=redis://127.0.0.1:6379/0 python tools/opener_classification_harness.py
"""

import os
import sys
from contextlib import redirect_stdout
import io
import json
from types import SimpleNamespace

import redis

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.channels import receive, send  # noqa: E402
from core.envelope import parse_for_switch  # noqa: E402
from core.keys import prefix  # noqa: E402
from core.service import Switch  # noqa: E402

try:
    from core.keys import receive_unresolved_key
except ImportError:
    receive_unresolved_key = None  # pre-phases shape -- no unresolved sink exists


def _redis_url() -> str:
    return os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")


def _connect() -> redis.Redis:
    r = redis.Redis.from_url(_redis_url())
    try:
        r.ping()
    except Exception as exc:
        print(
            f"error: cannot reach Redis at {_redis_url()} ({exc}) -- this "
            "harness refuses to certify anything without a real, reachable "
            "Redis.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return r


def _setup(r, pod: str, tenant: str, agents: list[str]) -> None:
    registry = prefix(pod, tenant, resource="registry")
    r.hset(registry, mapping={agent: "tmux" for agent in agents})


def _cleanup(r, pod: str, tenant: str, agents: list[str]) -> None:
    registry = prefix(pod, tenant, resource="registry")
    r.hdel(registry, *agents)
    keys = [
        prefix(pod, tenant, agent, resource)
        for agent in agents
        for resource in (
            "egress", "ingress", "dead", "unreplied", "processing", "opening",
            "opened", "launch", "profile", "provider",
        )
    ]
    if receive_unresolved_key is not None:
        keys.append(receive_unresolved_key(pod, tenant))
    r.delete(*keys)


def _stream_ids_in(raws: list) -> set[str]:
    ids = set()
    for raw in raws:
        try:
            ids.add(parse_for_switch(raw)["stream_id"])
        except Exception:
            continue
    return ids


def _unresolved_stream_ids(r, pod: str, tenant: str) -> set[str]:
    if receive_unresolved_key is None:
        return set()
    ids = set()
    for stored in r.lrange(receive_unresolved_key(pod, tenant), 0, -1):
        try:
            record = json.loads(stored.decode() if isinstance(stored, bytes) else stored)
            ids.add(parse_for_switch(record["envelope"])["stream_id"])
        except Exception:
            continue
    return ids


def _destination(r, pod: str, tenant: str, agent: str, stream_id: str) -> str:
    """Where an identity actually ended up, by direct inspection -- never by
    trusting a caller's claim about it. One of: 'dead', 'unresolved',
    'opened', 'processing', 'opening', or 'nowhere' (lost)."""
    dead_key = prefix(pod, tenant, agent, "dead")
    if stream_id in _stream_ids_in(r.lrange(dead_key, 0, -1)):
        return "dead"
    if stream_id in _unresolved_stream_ids(r, pod, tenant):
        return "unresolved"
    opened_key = prefix(pod, tenant, agent, "opened")
    if stream_id in _stream_ids_in(r.lrange(opened_key, 0, -1)):
        return "opened"
    processing_key = prefix(pod, tenant, agent, "processing")
    if stream_id in _stream_ids_in(r.lrange(processing_key, 0, -1)):
        return "processing"
    opening_key = prefix(pod, tenant, agent, "opening")
    if stream_id in _stream_ids_in(r.lrange(opening_key, 0, -1)):
        return "opening"
    return "nowhere"


# ---------------------------------------------------------------------------
# Scenario 1: lifecycle StartAgent, a mid-mutation write fails with a type
# that happens to be one _record_lifecycle/_lifecycle_opener treat as a
# proven pre-mutation rejection (ValueError, or KeyError on the branch that
# added that too). The failure happens AFTER a prior desired-state write
# (profile) has already committed -- an outcome-unknown continuation, not a
# validation failure -- so classifying it as a proven rejection is the exact
# lie the property forbids: the effect (profile publish) already happened,
# and whether the SECOND write (launch) happened is genuinely unknown, not
# proven not to have.
#
# Drives the REAL modules.office.port.deliver_office, which builds the REAL
# openers dict (including whatever office/port's classifier currently is)
# and calls the REAL core.channels.receive() -- the envelope's final resting
# place is read from Redis directly, never inferred from which exception
# propagated.
# ---------------------------------------------------------------------------

class _FailOnKey:
    """A real Redis client, transparent except SET calls to one exact key,
    which raise a caller-chosen exception. Everything else -- HSET, GET,
    EVAL, LRANGE, ping -- passes straight through via __getattr__. Models
    "some redis-side operation raised an unexpected type" without needing
    to reproduce a specific driver's own failure mode; the property under
    test does not depend on WHY the client raised, only on what type it
    raised and what the system did with that fact."""

    def __init__(self, real, fail_key: str, exc_factory):
        self._real = real
        self._fail_key = fail_key
        self._exc_factory = exc_factory

    def set(self, key, *args, **kwargs):
        if key == self._fail_key:
            raise self._exc_factory()
        return self._real.set(key, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def scenario_lifecycle_outcome_unknown_misclassified(r, pod: str, tenant: str) -> list[str] | None:
    if receive_unresolved_key is None:
        return None  # this custody shape has no unresolved sink at all --
        # everything an opener rejects reaches `dead` by design, not by a
        # specific classifier decision. That is a DIFFERENT, broader finding
        # (main's whole custody model predates this distinction) than "a
        # classifier exists and got this one case wrong" -- reported
        # separately by tools/conservation_harness.py, not asserted here.

    from modules.office import port as office_port

    sender, office_agent = "opener-sender-1", "opener-office-1"
    target = "opener-target-1"
    _setup(r, pod, tenant, [sender, office_agent])
    try:
        launch_key = prefix(pod, tenant, agent=target, resource="launch")
        proxy = _FailOnKey(r, launch_key, lambda: ValueError("simulated redis-side value error"))

        captured = io.StringIO()
        with redirect_stdout(captured):
            stream_id = send(
                r, pod=pod, tenant=tenant, source=sender, destination=office_agent,
                payload={"agent": target, "profile": "opener-verifier-profile"},
                kind="StartAgent",
            )
            switch = Switch(r, pod=pod, tenant=tenant, kick=lambda *a: None)
            if not switch.step(timeout=1):
                raise AssertionError("switch did not forward the StartAgent envelope")

        profile_key = prefix(pod, tenant, agent=target, resource="profile")
        pre_existing_profile = r.get(profile_key)
        if pre_existing_profile is not None:
            raise AssertionError("harness setup error: profile key already present before the run")

        deliver_out = io.StringIO()
        with redirect_stdout(deliver_out):
            try:
                office_port.deliver_office(
                    proxy, pod, tenant, office_agent, timeout=1, blocking=False,
                )
            except Exception:
                pass  # the destination on disk is the evidence, not whether this call raised

        profile_committed = r.get(profile_key) is not None
        destination = _destination(r, pod, tenant, office_agent, stream_id)

        if not profile_committed:
            return [
                "HARNESS ERROR: the profile write (meant to commit BEFORE the "
                "injected launch failure) never landed -- this run cannot "
                "distinguish outcome-unknown from proven-pre-mutation; "
                "narrowing the claim rather than reporting a result"
            ]

        if destination == "dead":
            return [
                "MUTATION-BOUNDARY LIE CONFIRMED: stream_id "
                f"{stream_id} (StartAgent, target={target!r}) reached `dead` "
                "-- claiming the effect PROVABLY did not begin -- after a "
                "prior desired-state write (profile) already committed and "
                "a LATER write (launch) failed with an injected ValueError. "
                "The profile publish is real, durable, and irreversible from "
                "here; nothing about this failure proves the launch write "
                "did not also land. This is exactly the outcome-unknown-as-"
                "proven-rejection defect."
            ]
        if destination != "unresolved":
            return [
                f"UNEXPECTED DESTINATION: stream_id {stream_id} ended up "
                f"{destination!r}, neither dead nor unresolved -- narrowing "
                "the claim rather than asserting a specific verdict this "
                "run does not support"
            ]
        return []
    finally:
        _cleanup(r, pod, tenant, [sender, office_agent])
        r.delete(prefix(pod, tenant, agent=target, resource="profile"))
        r.delete(prefix(pod, tenant, agent=target, resource="launch"))
        r.hdel(prefix(pod, tenant, resource="registry"), target)


# ---------------------------------------------------------------------------
# Scenario 2: openshell guarded() converts EVERY OpenShellUnavailable to
# DeadLetter, unconditionally -- confirmed by reading modules/openshell/
# port.py and client.py directly: exec_sandbox catches (grpc.RpcError,
# SandboxError) and wraps BOTH "never reached the sandbox" and "ran, then
# the response was lost" into the same OpenShellUnavailable type. This
# scenario's fake client PROVES the command executed (records it, with
# certainty this harness controls) before raising -- so a `dead` outcome is
# a direct, checkable contradiction: the effect ran, and the record claims
# it provably did not.
# ---------------------------------------------------------------------------

class _ExecutesThenFailsClient:
    """A minimal OpenShellClient double: get_sandbox succeeds, exec_sandbox
    performs its one real side effect (append to `executed`, visible to the
    harness with certainty no gRPC error could ever provide) and THEN
    raises OpenShellUnavailable -- reproducing "the command ran; the
    response was lost" without needing an actual OpenShell/gRPC backend."""

    def __init__(self):
        self.executed: list[str] = []

    def get_sandbox(self, name):
        return SimpleNamespace(id=f"sandbox-for-{name}")

    def exec_sandbox(self, sandbox_id, command, **kwargs):
        from modules.openshell.client import OpenShellUnavailable
        self.executed.append(sandbox_id)
        raise OpenShellUnavailable("simulated response-loss after execution")

    def close(self):
        pass


def scenario_openshell_response_loss_misclassified(r, pod: str, tenant: str) -> list[str] | None:
    if receive_unresolved_key is None:
        return None  # same reasoning as the lifecycle scenario above: no
        # unresolved sink exists on this shape, so "reached dead" is main's
        # whole custody model, not a specific classifier decision this
        # scenario can isolate.

    from modules.openshell import port as openshell_port

    sender, target = "opener-sender-2", "opener-target-2"
    _setup(r, pod, tenant, [sender, target])
    try:
        captured = io.StringIO()
        with redirect_stdout(captured):
            stream_id = send(
                r, pod=pod, tenant=tenant, source=sender, destination=target,
                payload={"text": "run something"},
            )
            switch = Switch(r, pod=pod, tenant=tenant, kick=lambda *a: None)
            if not switch.step(timeout=1):
                raise AssertionError("switch did not forward the Message envelope")

        fake_client = _ExecutesThenFailsClient()
        deliver_out = io.StringIO()
        with redirect_stdout(deliver_out):
            try:
                openshell_port.deliver_openshell(
                    r, pod, tenant, target, timeout=1, blocking=False, client=fake_client,
                )
            except Exception:
                pass

        destination = _destination(r, pod, tenant, target, stream_id)

        if not fake_client.executed:
            return [
                "HARNESS ERROR: the fake client's exec_sandbox was never "
                "called -- this run did not exercise the path it claims to; "
                "narrowing the claim rather than reporting a result"
            ]

        if destination == "dead":
            return [
                f"MUTATION-BOUNDARY LIE CONFIRMED: stream_id {stream_id} "
                "(Message, openshell) reached `dead` -- claiming the effect "
                "PROVABLY did not begin -- but exec_sandbox actually ran "
                f"(recorded: {fake_client.executed!r}) before the response "
                "was lost. guarded() converts every OpenShellUnavailable to "
                "DeadLetter unconditionally, with no distinction between "
                "'never reached the sandbox' and 'ran, response lost'."
            ]
        if destination != "unresolved":
            return [
                f"UNEXPECTED DESTINATION: stream_id {stream_id} ended up "
                f"{destination!r}, neither dead nor unresolved -- narrowing "
                "the claim rather than asserting a specific verdict this "
                "run does not support"
            ]
        return []
    finally:
        _cleanup(r, pod, tenant, [sender, target])


# ---------------------------------------------------------------------------
# Scenario 3: lib/board_interaction.py's add_ticket -- a third, independently
# found instance of the same root cause. `r.rpush(todo_key, ...)` is a
# single Redis command; if it raises, the client cannot tell "never reached
# the server" from "landed, response lost". add_ticket's own log_record
# already says so honestly ("board write outcome UNKNOWN after {exc}") --
# and then raises DeadLetter regardless, one line later. The log is honest;
# the exception type it hands to the caller is not.
#
# This round asserts CLASSIFICATION BEHAVIOUR -- which exception type
# escapes, and whether that type is proof -- not a custody DESTINATION,
# per this round's explicit instruction: switch-agent's protocol changes
# what destinations exist, but add_ticket's own exception-raising logic is
# untouched by that and independently checkable. A thin proxy performs the
# REAL rpush first (so the effect is genuinely, checkably durable) and only
# then raises, simulating a response lost after a write that landed.
# ---------------------------------------------------------------------------

class _RpushLandsThenFails:
    """A real Redis client, transparent except RPUSH to one exact key: it
    performs the REAL rpush first -- the effect genuinely happens, durably,
    independently checkable -- and only then raises. Simulates a response
    lost after the write landed, not a connection that never reached the
    server. Everything else passes straight through via __getattr__."""

    def __init__(self, real, fail_key: str, exc_factory):
        self._real = real
        self._fail_key = fail_key
        self._exc_factory = exc_factory

    def rpush(self, key, *args, **kwargs):
        result = self._real.rpush(key, *args, **kwargs)
        if key == self._fail_key:
            raise self._exc_factory()
        return result

    def __getattr__(self, name):
        return getattr(self._real, name)


def scenario_board_write_lands_then_response_lost(r, pod: str, tenant: str) -> list[str]:
    from core.channels import DeadLetter
    from lib.board_interaction import add_ticket

    agent, sender = "opener-board-target-1", "opener-board-sender-1"
    todo_key = prefix(pod, tenant, agent=agent, resource="tasks.todo")
    r.delete(todo_key)
    try:
        proxy = _RpushLandsThenFails(
            r, todo_key, lambda: redis.exceptions.ConnectionError("simulated response loss")
        )
        envelope = {
            "correlation_id": "verifier-correlation-1",
            "l2": {"source": sender, "destination": agent},
            "payload": {"title": "verifier ticket", "description": "", "priority": "normal"},
        }
        raised = None
        try:
            add_ticket(proxy, pod=pod, tenant=tenant, agent=agent, envelope=envelope)
        except Exception as exc:
            raised = exc

        landed = r.llen(todo_key) == 1
        if not landed:
            return [
                "HARNESS ERROR: the ticket RPUSH never actually landed -- this "
                "run cannot distinguish outcome-unknown from proven-pre-mutation; "
                "narrowing the claim rather than reporting a result"
            ]
        if raised is None:
            return ["HARNESS ERROR: add_ticket did not raise at all -- unexpected given the injected failure"]
        if isinstance(raised, DeadLetter):
            return [
                "MUTATION-BOUNDARY LIE CONFIRMED: add_ticket raised DeadLetter "
                "-- claiming the write PROVABLY did not happen -- after the "
                "RPUSH genuinely landed, confirmed independently "
                f"(llen({todo_key!r}) == 1). Its own log_record even says "
                "'board write outcome UNKNOWN after {exc}' one line before "
                "raising DeadLetter regardless -- the log is honest, the "
                "exception type handed to the caller is not."
            ]
        return []
    finally:
        r.delete(todo_key)


# ---------------------------------------------------------------------------
# Scenario 4: modules/tmux/port.py -- established by reading the code first,
# then confirmed experimentally, NOT assumed either way. Every raise
# DeadLetter in message_opener/command_opener/attachment_opener is guarded
# by a check that runs BEFORE the external effect (window-existence,
# envelope/stream_id validation, or an all-or-nothing local filesystem
# operation whose failure means the visible final path was never created).
# The one case architect specifically named -- a paste that lands, then the
# submitting Enter keystroke fails -- is never wrapped in a DeadLetter
# conversion anywhere in the call chain: submit_text's own TmuxCommandError
# propagates unconverted out of every opener that calls it. Confirmed here
# by driving the REAL message_opener with the real tmux calls replaced only
# at the process boundary (list_windows/submit_text), not by reimplementing
# the opener's own logic.
# ---------------------------------------------------------------------------

def scenario_tmux_post_paste_failure_not_misclassified(r, pod: str, tenant: str) -> list[str]:
    from unittest.mock import patch

    from core.channels import DeadLetter
    from modules.tmux import port as tmux_port
    from modules.tmux.ops import TmuxCommandError

    agent, sender = "opener-tmux-target-1", "opener-tmux-sender-1"
    envelope = {
        "stream_id": "verifier-stream-1",
        "correlation_id": "verifier-correlation-2",
        "l2": {"source": sender, "destination": agent},
        "payload": {"text": "hello"},
    }

    def fake_submit_text(session_name, agent_name, text, stream_id="", socket=None):
        # paste-buffer already succeeded -- the text IS in the pane -- and
        # only the later send-keys (submitting it) fails. Exactly the harm
        # named: a landed paste is not a rejection.
        raise TmuxCommandError("send-keys", 1, "simulated: pane gone after paste landed")

    raised = None
    with (
        patch.object(tmux_port, "list_windows", return_value={agent}),
        patch.object(tmux_port, "submit_text", side_effect=fake_submit_text),
        patch.object(tmux_port, "mark_delivery_pending", lambda *a, **k: None),
    ):
        try:
            tmux_port.message_opener(
                r, pod, tenant, agent, envelope, session_name="verifier-session",
            )
        except Exception as exc:
            raised = exc

    if raised is None:
        return ["HARNESS ERROR: message_opener did not raise at all -- unexpected given the injected failure"]
    if isinstance(raised, DeadLetter):
        return [
            "MUTATION-BOUNDARY LIE CONFIRMED: message_opener raised DeadLetter "
            "after paste-buffer succeeded (text delivered to the pane) and "
            "only the submitting send-keys failed -- exactly the harm named: "
            "a paste that landed is not a rejection."
        ]
    if not isinstance(raised, TmuxCommandError):
        return [
            f"UNEXPECTED: message_opener raised {raised!r}, expected the raw "
            "TmuxCommandError to propagate unconverted -- narrowing the claim"
        ]
    return []


SCENARIOS = [
    ("lifecycle StartAgent: outcome-unknown write misclassified as proven rejection",
     scenario_lifecycle_outcome_unknown_misclassified),
    ("openshell: response-loss after real execution misclassified as proven rejection",
     scenario_openshell_response_loss_misclassified),
    ("board: RPUSH landed, response lost, misclassified as proven rejection",
     scenario_board_write_lands_then_response_lost),
    ("tmux: paste landed, send-keys failed, must not be misclassified",
     scenario_tmux_post_paste_failure_not_misclassified),
]


def _report_version() -> None:
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    try:
        import subprocess
        head = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        branch = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if head.returncode == 0:
            print(f"tree under test: {branch.stdout.strip() or '<detached>'} @ {head.stdout.strip()}")
    except Exception:
        pass
    print(f"unresolved sink present: {receive_unresolved_key is not None}")


def main() -> None:
    r = _connect()
    pod = os.environ.get("POD", "ci-opener-classification")
    tenant = os.environ.get("TENANT", "ci-opener-classification")
    print(f"opener classification harness against {_redis_url()}, pod={pod} tenant={tenant}")
    _report_version()
    print("NOT A FIX -- a verifier. See this file's module docstring.\n")

    overall_ok = True
    for name, fn in SCENARIOS:
        print(f"--- {name} ---")
        failures = fn(r, pod, tenant)
        if failures is None:
            print("SKIPPED: no unresolved sink on this custody shape -- see scenario docstring")
        elif failures:
            overall_ok = False
            print(f"MUTATION BOUNDARY VIOLATED ({len(failures)} finding(s)):")
            for failure in failures:
                print(f"  - {failure}")
        else:
            print("boundary held: this scenario's misclassification was not observed")
        print()

    print("=== SUMMARY ===")
    if overall_ok:
        print("PASS: opener classification held in every scenario")
    else:
        print("FAIL: opener classification violated in at least one scenario -- see findings above")
    sys.exit(0 if overall_ok else 1)


if __name__ == "__main__":
    main()
