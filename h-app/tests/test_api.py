import ast
import asyncio
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import redis

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from core.channels import send
from core.envelope import build, encode, parse
from core.keys import prefix
from modules.api import server as server_module
from modules.api.port import deliver_api
from modules.api.server import ApiSettings, create_app

_UNMODELLED_CLASS_BODY = object()


def _reason_argument_violations(source: str) -> list[str]:
    """Syntactic invariant, not a runtime one: every `reason` argument at
    every call to `_record(...)` in `source` must be a string/None
    constant, a reference to a name that is ONLY EVER bound to string/None
    constants by EVERY binding form REACHABLE FROM THAT CALL SITE'S OWN
    LEXICAL SCOPE (its enclosing function, else module level), or a call
    to a same-module function whose every definition sharing that name has
    every `return` be one of those two things. Deliberately no
    control-flow modelling (no branch, loop, or exception reasoning) --
    a name or function is "safe" only if every occurrence *in scope*
    qualifies, independent of which branch actually runs.

    LEXICAL SCOPING, not just flat name-matching, is required for
    soundness: `_record`'s own `reason` parameter and an unrelated
    caller's local variable also named `reason` are different bindings
    and must not be conflated -- a first version of this checker did
    exactly that and produced a false positive against the real,
    correctly-written production file the moment function-parameter
    bindings were tracked at all. Resolution is local-scope-first, then
    module scope, matching ordinary Python name resolution -- but ONLY at
    depth 0 (module level) or depth 1 (a function directly under module),
    where those are the only two scopes that can possibly be involved. A
    `_record` call found inside a closure nested two or more levels deep
    is rejected OUTRIGHT, unconditionally, before its reason argument is
    even inspected -- reviewer's exact finding: a name referenced from
    such a closure can require walking through an INTERMEDIATE enclosing-
    function scope, and resolving straight from the innermost scope to
    module (skipping that middle scope) can certify the wrong binding.
    Rather than build and separately re-verify a full lexical parent-chain
    resolver with nonlocal/global semantics -- a fifth mechanism to get
    wrong after four rounds of exactly that shape of bug -- refusing to
    certify what isn't modelled closes the entire class at once: there is
    no partial chain-walk left in this design to be incomplete.

    "Every binding form" is exhaustive, not just `Assign`: `AnnAssign`,
    `AugAssign` (always dynamic -- a mutation is never trusted regardless
    of what it mutates), the walrus operator, loop and comprehension
    targets, `except ... as`, `with ... as`, imports, class names, and
    function/lambda parameters all count. A single dynamic binding
    anywhere in the relevant scope disqualifies the name there, including
    a same-named function defined twice where only one definition is safe
    -- the later definition is the one that actually binds at call time,
    so every definition sharing a name must independently qualify. A
    `_record` call using `**kwargs`/`*args` expansion is rejected outright:
    its keyword contents cannot be verified by this analysis at all. A
    class body is not a real closure scope in Python either (a method
    cannot see a class-body local unqualified) and gets the identical
    treatment: any `_record` call reachable from inside one, at any depth,
    is rejected outright rather than resolved against the wrong scope.

    ⚠ STOPPING CONDITION, stated here so it survives past the conversation
    that produced it: this checker has been wrong about scope twice --
    once a false positive (parameter tracking without scoping), once a
    false negative (an unmodelled intermediate closure scope). Both were
    fixed by making the model's boundary EXPLICIT and refusing everything
    outside it, not by extending an open-ended enumeration. If a review
    ever finds ANOTHER shape this checker gets wrong that the depth
    boundary and the class-body rejection do not already cover -- a
    genuinely new category, not another instance of one already named
    above -- that is the signal to stop patching this checker and fall
    back to the weaker, honest claim: the runtime fuzz test above pins
    today's known branches, and module-wide coverage rests on manual
    enumeration. Do not ship a further round of case-by-case patches to
    this function past that point.
    """
    tree = ast.parse(source)

    def is_literal(node: ast.AST) -> bool:
        return isinstance(node, ast.Constant) and (node.value is None or isinstance(node.value, str))

    def names_in(target: ast.AST) -> list[str]:
        return [n.id for n in ast.walk(target) if isinstance(n, ast.Name)]

    def returns_of(func_node) -> list[ast.AST]:
        found: list[ast.AST] = []

        class _ReturnCollector(ast.NodeVisitor):
            def visit_FunctionDef(self, node):
                pass  # do not attribute a nested def's returns to this one

            def visit_AsyncFunctionDef(self, node):
                pass

            def visit_Lambda(self, node):
                pass

            def visit_Return(self, node):
                found.append(node.value)
                self.generic_visit(node)

        collector = _ReturnCollector()
        for stmt in func_node.body:
            collector.visit(stmt)
        return found

    def params_of(func_or_lambda) -> list[str]:
        a = func_or_lambda.args
        names = [arg.arg for arg in a.posonlyargs + a.args + a.kwonlyargs]
        if a.vararg:
            names.append(a.vararg.arg)
        if a.kwarg:
            names.append(a.kwarg.arg)
        return names

    functions_by_name: dict[str, list] = {}
    # scope_of[id(call_node)] -> enclosing FunctionDef/AsyncFunctionDef, or
    # None for module level.
    scope_of: dict[int, object] = {}
    # bindings[scope_key] -> (literal_only_names, dynamic_names) owned
    # DIRECTLY by that scope -- module level uses key None.
    bindings: dict[object, tuple[set, set]] = {}
    # parent_of[scope_key] -> the scope_key of its immediately enclosing
    # scope (module's own parent is never present -- depth stops there).
    parent_of: dict[object, object] = {}
    record_calls: list[ast.Call] = []

    def scope_key(scope_node) -> object:
        return None if scope_node is None else id(scope_node)

    def depth(scope_node) -> int:
        """0 = module level, 1 = a function directly under module, 2+ = a
        closure nested inside another function. Only depth 0 and 1 are
        ever fully resolved (at most one hop to module); depth 2+ is
        rejected outright rather than partially modelled -- see
        `_reason_argument_violations`'s docstring."""
        d = 0
        key = scope_key(scope_node)
        while key is not None:
            key = parent_of.get(key)
            d += 1
        return d

    def collect(scope_node, stmts, seed_dynamic: list[str] = (), parent=None) -> None:
        """Populate bindings[scope_key(scope_node)] from the statements
        directly in this scope's own body (not descending into nested
        function/lambda scopes, which get their own `collect` call).
        `seed_dynamic` pre-marks this scope's own parameters dynamic
        before any body statement is examined, so a parameter's identity
        is fixed at scope creation, never inferred from an outer scope."""
        parent_of[scope_key(scope_node)] = scope_key(parent)
        literal_only: set[str] = set()
        dynamic: set[str] = set(seed_dynamic)
        bindings[scope_key(scope_node)] = (literal_only, dynamic)

        def literal_candidate(name: str, value_node) -> None:
            (literal_only if is_literal(value_node) else dynamic).add(name)

        def mark_dynamic(name: str) -> None:
            dynamic.add(name)

        class _Binder(ast.NodeVisitor):
            def visit_FunctionDef(self, node):
                functions_by_name.setdefault(node.name, []).append(node)
                collect(node, node.body, params_of(node), parent=scope_node)

            def visit_AsyncFunctionDef(self, node):
                self.visit_FunctionDef(node)

            def visit_Lambda(self, node):
                collect(node, [], params_of(node), parent=scope_node)
                self.visit(node.body)  # a lambda body is one expression, may hold a call

            def visit_ClassDef(self, node):
                mark_dynamic(node.name)
                # Class bodies are a distinct scoping regime this checker
                # does not model: their own locals are NOT visible to
                # methods defined inside them (unlike a real closure), so
                # neither the depth-based closure gate nor ordinary
                # local/module resolution describes them correctly. Any
                # `_record` call anywhere inside a class body -- at class
                # level or inside any of its methods, at any nesting depth
                # -- is captured here and rejected outright, the same
                # "refuse what isn't modelled" rule applied to closures.
                for inner in ast.walk(node):
                    if (
                        isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Name)
                        and inner.func.id == "_record"
                    ):
                        scope_of[id(inner)] = _UNMODELLED_CLASS_BODY
                        record_calls.append(inner)

            def visit_Assign(self, node):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        literal_candidate(target.id, node.value)
                    else:
                        for name in names_in(target):
                            mark_dynamic(name)
                self.generic_visit(node)

            def visit_AnnAssign(self, node):
                if isinstance(node.target, ast.Name):
                    if node.value is not None:
                        literal_candidate(node.target.id, node.value)
                    else:
                        mark_dynamic(node.target.id)
                self.generic_visit(node)

            def visit_AugAssign(self, node):
                if isinstance(node.target, ast.Name):
                    mark_dynamic(node.target.id)
                self.generic_visit(node)

            def visit_NamedExpr(self, node):
                if isinstance(node.target, ast.Name):
                    literal_candidate(node.target.id, node.value)
                self.generic_visit(node)

            def visit_For(self, node):
                for name in names_in(node.target):
                    mark_dynamic(name)
                self.generic_visit(node)

            visit_AsyncFor = visit_For

            def visit_comprehension(self, node):
                for name in names_in(node.target):
                    mark_dynamic(name)
                self.generic_visit(node)

            def visit_ExceptHandler(self, node):
                if node.name:
                    mark_dynamic(node.name)
                self.generic_visit(node)

            def visit_withitem(self, node):
                if node.optional_vars is not None:
                    for name in names_in(node.optional_vars):
                        mark_dynamic(name)
                self.generic_visit(node)

            def visit_Import(self, node):
                for alias in node.names:
                    mark_dynamic(alias.asname or alias.name.split(".")[0])

            def visit_ImportFrom(self, node):
                for alias in node.names:
                    mark_dynamic(alias.asname or alias.name.split(".")[0])

            def visit_Call(self, node):
                if isinstance(node.func, ast.Name) and node.func.id == "_record":
                    scope_of[id(node)] = scope_node
                    record_calls.append(node)
                self.generic_visit(node)

        binder = _Binder()
        for stmt in stmts:
            binder.visit(stmt)
        literal_only -= dynamic

    collect(None, tree.body)

    literal_only_functions: set[str] = set()
    for name, defs in functions_by_name.items():
        # EVERY definition sharing this name must independently qualify --
        # a later redefinition is the one that actually binds at call
        # time, so one safe definition cannot license the name (reviewer's
        # exact counterexample: two `def helper` with the same name).
        if all(returns_of(d) and all(is_literal(r) for r in returns_of(d)) for d in defs):
            literal_only_functions.add(name)
    # A name also rebound dynamically anywhere at module level (e.g.
    # `helper = lambda ...` after `def helper(): ...`) is not trustworthy.
    module_literal, module_dynamic = bindings[None]
    literal_only_functions -= module_dynamic

    def resolve(name: str, scope_node) -> bool | None:
        """True/False if this scope (or module, as a fallback) has an
        opinion on `name`; None if truly never bound anywhere reachable."""
        local_literal, local_dynamic = bindings.get(scope_key(scope_node), (set(), set()))
        if name in local_dynamic:
            return False
        if name in local_literal:
            return True
        if scope_node is not None:
            if name in module_dynamic:
                return False
            if name in module_literal:
                return True
        return None

    def is_safe(node: ast.AST, scope_node) -> bool:
        if is_literal(node):
            return True
        if isinstance(node, ast.Name):
            return resolve(node.id, scope_node) is True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and not any(isinstance(a, ast.Starred) for a in node.args)
            and not any(kw.arg is None for kw in node.keywords)
        ):
            called_name = node.func.id
            local_literal, local_dynamic = bindings.get(scope_key(scope_node), (set(), set()))
            if called_name in local_literal or called_name in local_dynamic:
                # Locally rebound in the call site's own scope -- ordinary
                # Python name resolution would use THAT binding, not the
                # module-level function of the same name, so trusting
                # literal_only_functions here would resolve the wrong one.
                return False
            return called_name in literal_only_functions
        if isinstance(node, ast.IfExp):
            return is_safe(node.body, scope_node) and is_safe(node.orelse, scope_node)
        return False

    violations = []
    for node in record_calls:
        scope_node = scope_of[id(node)]
        if scope_node is _UNMODELLED_CLASS_BODY:
            violations.append(f"line {node.lineno}: _record call inside a class body is not modelled")
            continue
        if depth(scope_node) >= 2:
            # A closure nested inside another function: reviewer's exact
            # finding. Resolving a name here can require walking through
            # an intermediate enclosing-function scope this checker does
            # not model at all -- `resolve` only ever tries the call's own
            # immediate scope and then module scope directly, which
            # silently skips any scope in between and can certify the
            # wrong binding. Rather than build and re-verify a full
            # lexical parent chain (a fifth thing to get wrong after four
            # rounds of exactly that), refuse to certify anything at this
            # depth at all: unconditional violation, checked before even
            # looking at what the reason argument is.
            violations.append(f"line {node.lineno}: _record call inside a nested closure scope is not modelled")
            continue
        if any(kw.arg is None for kw in node.keywords) or any(isinstance(a, ast.Starred) for a in node.args):
            violations.append(f"line {node.lineno}: unverifiable *args/**kwargs expansion in _record call")
            continue
        reason_node = next((kw.value for kw in node.keywords if kw.arg == "reason"), None)
        if reason_node is None and len(node.args) >= 4:
            reason_node = node.args[3]
        if reason_node is None:
            continue  # not provided -- defaults to None, safe
        if not is_safe(reason_node, scope_node):
            violations.append(f"line {node.lineno}: {ast.dump(reason_node)}")
    return violations


class FakeRedis:
    def __init__(self):
        self.registry = {"api": "api", "alice": "tmux", "telegram": "api"}
        self.hashes = {}
        self.lists = {}
        self.streams = {}
        self.kv = {}

    def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value

    def hkeys(self, key):
        if key.endswith(":registry"):
            return [name.encode() for name in self.registry]
        return list(self.hashes.get(key, {}))

    def hexists(self, key, field):
        return field in self.registry if key.endswith(":registry") else field in self.hashes.get(key, {})

    def hget(self, key, field):
        if key.endswith(":registry"):
            return self.registry.get(field)
        return self.hashes.get(key, {}).get(field)

    def hgetall(self, key):
        return self.hashes.get(key, {})

    def hdel(self, key, *fields):
        count = 0
        for field in fields:
            if self.hashes.get(key, {}).pop(field, None) is not None:
                count += 1
        return count

    def get(self, key):
        return self.kv.get(key)

    def set(self, key, value, ex=None):
        self.kv[key] = value
        return True

    def llen(self, key):
        return len(self.lists.get(key, []))

    def lrange(self, key, start, end):
        values = self.lists.get(key, [])
        return values[start:] if end == -1 else values[start : end + 1]

    def lpop(self, key):
        values = self.lists.get(key, [])
        return values.pop(0) if values else None

    def rpush(self, key, *values):
        self.lists.setdefault(key, []).extend(values)
        return len(self.lists[key])

    def xadd(self, key, fields, maxlen=None, approximate=True):
        entries = self.streams.setdefault(key, [])
        entry_id = f"{len(entries) + 1}-0"
        entries.append((entry_id, fields))
        if maxlen and len(entries) > maxlen:
            del entries[:-maxlen]
        return entry_id

    def xrange(self, key, min="-", max="+", count=None):
        return self.streams.get(key, [])[:count]

    def eval(self, script, numkeys, *args):
        keys = args[:numkeys]
        argv = args[numkeys:]
        if "LRANGE" in script and "DEL" in script:
            return self.lists.pop(keys[0], [])
        return 1

    def pipeline(self, transaction=False):
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.calls = []

    def lrange(self, key, start, end):
        self.calls.append((key, start, end))
        return self

    def execute(self):
        return [self.redis.lrange(*call) for call in self.calls]


def request(app, method, path, *, token=None, body=None):
    sent = []
    received = False
    encoded = json.dumps(body).encode() if body is not None else b""
    headers = [(b"content-type", b"application/json")]
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": encoded, "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 8080),
        "root_path": "",
    }
    asyncio.run(app(scope, receive, send))
    start = next(item for item in sent if item["type"] == "http.response.start")
    raw_body = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
    return start["status"], json.loads(raw_body)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        self.app = create_app(
            settings=ApiSettings(pod="test", tenant="office", api_token="secret"),
            redis_client=self.redis,
        )

    def test_auth_and_health(self):
        self.assertEqual(request(self.app, "GET", "/health")[0], 401)
        self.assertEqual(request(self.app, "GET", "/health", token="secret"), (200, {"status": "ok"}))

    def test_external_route_contract(self):
        routes = {(method, route.path) for route in self.app.routes for method in getattr(route, "methods", set())}
        expected = {
            ("GET", "/health"),
            ("GET", "/agents"),
            ("GET", "/agents/{agent}"),
            ("POST", "/agents/{agent}/envelopes"),
            ("GET", "/agents/{agent}/messages"),
            ("GET", "/agents/{agent}/messages/stream"),
            ("GET", "/agents/{agent}/activity"),
            ("GET", "/agents/{agent}/activity/stream"),
            ("GET", "/agents/{agent}/board"),
            ("GET", "/board"),
            ("GET", "/alerts"),
            ("GET", "/alerts/stream"),
            ("GET", "/restdoc"),
            ("GET", "/docs"),
            ("GET", "/redoc"),
            ("GET", "/openapi.json"),
        }
        self.assertTrue(expected.issubset(routes))

    def test_qualified_destination_routes_through_core_channel(self):
        status, body = request(
            self.app,
            "POST",
            "/agents/test:office:alice/envelopes",
            token="secret",
            body={"text": "hello"},
        )
        self.assertEqual(status, 202)
        self.assertIn("stream_id", body)
        queued = parse(self.redis.lists[prefix("test", "office", "api", "egress")][0])
        self.assertEqual(queued["l2"]["destination"], "alice")
        self.assertEqual(queued["l3"]["destination"], "test:office:alice")

    def test_post_envelope_does_not_report_a_write_failure_as_a_rejection(self):
        """CLASS 2, architect's provenance audit (ticket 51caad5f): the
        only except clause converting a post_envelope failure into an
        explicit HTTP 422 rejection catches EnvelopeError specifically --
        and core.channels.send()'s own contract (its comment above the
        rpush call: "Only RPUSH belongs inside the outcome-unknown
        window") proves EnvelopeError is raised only by validation that
        completes BEFORE the egress write. A failure from the write step
        itself must never be classified as a proven rejection: the caller
        cannot tell from a 422 whether their message was actually queued,
        so reporting a write failure that way would be a confident, wrong
        claim -- the exact harm this ticket's Class 2 predicate names.
        Simulates the write step itself failing with a plain exception
        (never EnvelopeError -- send() cannot raise that type from within
        the rpush try) and confirms it does NOT come back as this
        endpoint's 422 rejection status."""
        def failing_rpush(*args, **kwargs):
            raise ConnectionError("redis unreachable")

        with patch.object(self.redis, "rpush", side_effect=failing_rpush):
            with self.assertRaises(ConnectionError):
                request(
                    self.app, "POST", "/agents/test:office:alice/envelopes",
                    token="secret", body={"text": "hello"},
                )

    def test_nonlocal_and_malformed_destination_statuses(self):
        status, _ = request(
            self.app, "POST", "/agents/other:office:alice/envelopes",
            token="secret", body={"text": "hello"},
        )
        self.assertEqual(status, 422)
        status, _ = request(
            self.app, "POST", "/agents/test:office:alice:extra/envelopes",
            token="secret", body={"text": "hello"},
        )
        self.assertEqual(status, 404)

    def test_api_port_writes_mailbox_and_dead_letters_corrupt_input(self):
        ingress = prefix("test", "office", "telegram", "ingress")
        valid = encode(build("Message", "alice", "telegram", {"text": "reply"}, pod="test", tenant="office"))
        self.redis.lists[ingress] = [valid, "not an envelope"]

        deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")

        inbox = prefix("test", "office", "telegram", "inbox")
        dead = prefix("test", "office", "telegram", "dead")
        self.assertEqual(len(self.redis.streams[inbox]), 1)
        self.assertEqual(json.loads(self.redis.streams[inbox][0][1]["envelope"])["payload"], {"text": "reply"})
        self.assertEqual(self.redis.lists[dead], ["not an envelope"])

    def test_deliver_api_does_not_dead_letter_an_envelope_after_a_failed_inbox_write(self):
        """CLASS 2, architect's provenance audit (ticket 51caad5f), second
        candidate site: deliver_api's only except clause that classifies a
        rejection wraps parse(raw) alone, never the r.xadd inbox write that
        follows it -- parse() takes no Redis handle at all, so it cannot
        have touched storage, which is a stronger guarantee than an
        in-function comment (core.channels.send()'s case) because it holds
        structurally, by parse()'s own signature. The fragile part is the
        SHAPE of the try/except, not parse()'s purity: if a later change
        widened that except to also cover the xadd call, a write failure
        would be misclassified as a proven rejection (dead-lettered) when
        the caller cannot actually tell whether inbox storage received it.
        Simulates the write step failing and confirms it propagates
        uncaught -- never silently classified into the dead-letter queue."""
        ingress = prefix("test", "office", "telegram", "ingress")
        valid = encode(build("Message", "alice", "telegram", {"text": "reply"}, pod="test", tenant="office"))
        self.redis.lists[ingress] = [valid]

        def failing_xadd(*args, **kwargs):
            raise ConnectionError("redis unreachable")

        with patch.object(self.redis, "xadd", side_effect=failing_xadd):
            with self.assertRaises(ConnectionError):
                deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")

        dead = prefix("test", "office", "telegram", "dead")
        self.assertEqual(self.redis.lists.get(dead, []), [])

    def test_deliver_api_never_logs_the_raw_content_of_a_rejected_envelope(self):
        """CLASS 1, architect's provenance audit (ticket 51caad5f):
        parse()'s EnvelopeError messages are constructed in core/envelope.py
        from whatever the wire said -- _address/_segment interpolate the
        remote value itself (`{value!r}`) into several of them -- so
        str(exc) is remote-influenced by construction, exactly like the
        telegram client and watchdog leaks this ticket cites. Craft a wire
        frame with a VALID L2 header (parse_for_switch succeeds) but an L3
        source that fails segment validation, carrying a marker that must
        never reach the durable custody log deliver_api writes."""
        valid = encode(build("Message", "alice", "telegram", {"text": "hi"}, pod="test", tenant="office"))
        header, body = valid[:256], valid[256:]
        body_dict = json.loads(body)
        marker = "UNTRUSTED_REMOTE_MARKER_should_never_reach_logs"
        body_dict["l3"]["source"] = f"test:office:{marker}"
        tampered = header + json.dumps(body_dict, separators=(",", ":"))
        ingress = prefix("test", "office", "telegram", "ingress")
        self.redis.lists[ingress] = [tampered]

        out = io.StringIO()
        with redirect_stdout(out):
            deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")

        self.assertNotIn(marker, out.getvalue())
        dead = prefix("test", "office", "telegram", "dead")
        self.assertEqual(self.redis.lists[dead], [tampered])

    def test_deliver_api_never_logs_the_in_reply_to_value_or_reply_source_it_drops(self):
        """Reviewer's exact finding against 301ae87 (ticket 51caad5f):
        is_valid_reply_id restricts SHAPE (32 lowercase hex characters), not
        provenance -- a remote sender chooses the bytes freely within that
        shape, so a syntactically valid in_reply_to is still remote data by
        origin, same predicate as a malformed one. The prior fix closed
        EnvelopeError's str(exc) but left _drop_untrustworthy_reply_correlation
        interpolating in_reply_to, reply_source and agent directly into the
        free-text `reason` -- redundant with the dedicated source/destination
        fields _record already populates, and a second instance of the same
        leak class. Covers both branches that interpolated a value: verdict
        False ("was never delivered") and verdict None ("provenance
        unavailable")."""
        marker = "deadbeefdeadbeefdeadbeefdeadbeef"
        ingress = prefix("test", "office", "telegram", "ingress")
        envelope = build(
            "Message", "alice", "telegram", {"text": "hi"},
            pod="test", tenant="office", in_reply_to=marker,
        )
        self.redis.lists[ingress] = [encode(envelope)]

        out = io.StringIO()
        with redirect_stdout(out):
            deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")
        self.assertNotIn(marker, out.getvalue())

        self.redis.lists[ingress] = [encode(envelope)]

        def broken_get(key):
            raise ConnectionError("redis unavailable")

        out2 = io.StringIO()
        with patch.object(self.redis, "get", side_effect=broken_get), redirect_stdout(out2):
            deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")
        self.assertNotIn(marker, out2.getvalue())

    def test_reply_correlation_and_dead_letter_reasons_are_always_closed_literals(self):
        """EXAMPLE-LEVEL coverage, not a module-wide guarantee -- reviewer's
        exact correction: this drives every branch that exists TODAY (the
        ones enumerated below) with adversarial values and confirms none of
        them currently leak. It has no mechanism to discover a NEW caller
        added elsewhere in the file, so it cannot and does not prove "any
        third site would fail" -- that claim belongs to
        test_every_reason_argument_in_port_py_is_a_closed_literal_or_a_verified_helper
        below, which inspects the source directly. Keep both: this one
        pins today's specific branches' actual runtime output; the AST
        test enforces the module-wide shape going forward."""
        closed_reasons = {
            None,
            "malformed in_reply_to",
            "in_reply_to present but reply has no l2 source",
            "in_reply_to provenance unavailable (storage unreachable)",
            "in_reply_to was never delivered to the claimed source",
            "malformed envelope",
        }

        def reasons_from(out: str) -> set:
            return {json.loads(line).get("reason") for line in out.splitlines()}

        ingress = prefix("test", "office", "telegram", "ingress")
        adversarial_markers = [
            "deadbeefdeadbeefdeadbeefdeadbeef",
            "cafebabecafebabecafebabecafebabe",
            "0" * 32,
            "not-a-valid-hex-id-at-all-nope!!",
            "SECRET_LEAK_ATTEMPT_UPPER_CASE_XX",
        ]

        # Malformed and never-delivered branches: build(in_reply_to=...)
        # rejects anything not already 32 lowercase hex, so tamper the
        # wire form directly to reach deliver_api with whatever the
        # marker actually is, valid-shaped or not.
        for marker in adversarial_markers:
            envelope = build("Message", "alice", "telegram", {"text": "hi"}, pod="test", tenant="office")
            raw = self._tamper_in_reply_to(envelope, marker)
            self.redis.lists[ingress] = [raw]
            out = io.StringIO()
            with redirect_stdout(out):
                deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")
            self.assertNotIn(marker, out.getvalue())
            self.assertLessEqual(reasons_from(out.getvalue()), closed_reasons)

        # Provenance-unavailable branch: valid-shaped id, storage unreachable.
        for marker in ("deadbeefdeadbeefdeadbeefdeadbeef", "cafebabecafebabecafebabecafebabe"):
            envelope = build(
                "Message", "alice", "telegram", {"text": "hi"},
                pod="test", tenant="office", in_reply_to=marker,
            )
            self.redis.lists[ingress] = [encode(envelope)]

            def broken_get(key):
                raise ConnectionError("redis unavailable")

            out = io.StringIO()
            with patch.object(self.redis, "get", side_effect=broken_get), redirect_stdout(out):
                deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")
            self.assertNotIn(marker, out.getvalue())
            self.assertLessEqual(reasons_from(out.getvalue()), closed_reasons)

        # Dead-letter path: malformed frames hitting different EnvelopeError
        # raise sites in core/envelope.py -- a bad L2 header name, a bad L3
        # body address, and non-JSON body -- must all reduce to the single
        # "malformed envelope" literal, regardless of what remote text
        # triggered the rejection or which field carried it.
        valid = encode(build("Message", "alice", "telegram", {"text": "hi"}, pod="test", tenant="office"))
        header = valid[:256]
        marker = "LEAK_MARKER_FOR_DEAD_LETTER_PATH"
        malformed_raws = [
            "short",
            header + "not json",
            valid[:65] + marker.ljust(63) + valid[128:256] + valid[256:],
            header + json.dumps({
                "kind": "Message", "ts": "x",
                "l3": {"source": f"test:office:{marker}", "destination": "test:office:telegram"},
                "payload": {},
            }, separators=(",", ":")),
        ]
        for raw in malformed_raws:
            self.redis.lists[ingress] = [raw]
            out = io.StringIO()
            with redirect_stdout(out):
                deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")
            self.assertNotIn(marker, out.getvalue())
            self.assertLessEqual(reasons_from(out.getvalue()), closed_reasons)

    def test_every_reason_argument_in_port_py_is_a_closed_literal_or_a_verified_helper(self):
        """The real source-level invariant reviewer asked for, after
        proving the fuzz test above can't discover a NEW caller: adding

            def _future_log_path(remote_value: str) -> None:
                _record("future", {}, "api", reason=f"remote value: {remote_value}")

        anywhere in modules/api/port.py left the fuzz test green, because
        it only drives branches this file's author already knew about.
        This instead parses port.py's actual source and inspects every
        `reason` argument reaching `_record(...)`, by syntax, not by
        exercising it at runtime -- deliberately NOT modelling control
        flow (no branch/loop/exception semantics), only asking: is this
        argument expression a plain string constant, a reference to a name
        assigned ONLY string constants anywhere in the module, or a call to
        a function defined in this same file whose every `return` is
        itself one of those two things? Reviewer's exact counterexample is
        the harm test: fed as a source string (not written to disk), it
        must be flagged."""
        source = (H_APP / "modules" / "api" / "port.py").read_text()
        violations = _reason_argument_violations(source)
        self.assertEqual(violations, [], f"unverified reason argument(s): {violations}")

        hostile_source = source + (
            "\n\n"
            "def _future_log_path(remote_value: str) -> None:\n"
            '    _record("future", {}, "api", reason=f"remote value: {remote_value}")\n'
        )
        hostile_violations = _reason_argument_violations(hostile_source)
        self.assertTrue(
            hostile_violations,
            "the checker must flag reviewer's exact counterexample -- it did not",
        )

    def test_reason_argument_checker_rejects_reviewers_binding_shape_counterexamples(self):
        """Reviewer's second and third blockers, verbatim. The first four
        cases are exactly as reviewer supplied them -- three from round
        two (allow-by-default wearing a deny-by-default docstring: **kwargs
        expansion, AugAssign not invalidating a name, and same-named
        function definitions unioned instead of all required to qualify),
        plus round three's intermediate-closure-scope false negative
        (resolving straight from an inner closure to module level, skipping
        the enclosing function scope in between, certifies the wrong
        binding). The last two are proactive: found while verifying the
        closure fix converges rather than reported by reviewer, covering
        the same "scope this checker does not model" shape applied to a
        locally-shadowed helper name and to class bodies (which are not
        real closures in Python -- methods cannot see class-body locals
        unqualified -- and were falling through to the enclosing scope
        untouched before this). Committed per reviewer's instruction that
        counterexamples are the test suite, so a regression in any of
        these shapes is caught by name, not by re-deriving it later."""
        preamble = "def _record(event, envelope, agent, reason=None):\n    pass\n\n"

        expanded_kwargs = preamble + (
            "def f(remote):\n"
            '    _record("x", {}, "a", **{"reason": f"leak {remote}"})\n'
        )
        self.assertTrue(
            _reason_argument_violations(expanded_kwargs),
            "expanded **kwargs reaching _record must be rejected, not silently unseen",
        )

        augmented_assignment = preamble + (
            'reason = "safe"\n'
            "def f(remote):\n"
            "    global reason\n"
            "    reason += remote\n"
            '    _record("x", {}, "a", reason=reason)\n'
        )
        self.assertTrue(
            _reason_argument_violations(augmented_assignment),
            "AugAssign must invalidate a name even after a prior literal Assign",
        )

        redefined_function = preamble + (
            "def helper():\n"
            '    return "safe"\n'
            "def helper(remote):\n"
            '    return f"leak {remote}"\n'
            "def f(remote):\n"
            '    _record("x", {}, "a", reason=helper(remote))\n'
        )
        self.assertTrue(
            _reason_argument_violations(redefined_function),
            "one unsafe definition among same-named functions must fail the whole name, not be unioned away",
        )

        intermediate_closure = preamble + (
            'reason = "safe"\n'
            "def outer(remote):\n"
            '    reason = f"leak {remote}"\n'
            "    def inner():\n"
            '        _record("x", {}, "a", reason=reason)\n'
            "    inner()\n"
        )
        self.assertTrue(
            _reason_argument_violations(intermediate_closure),
            "a closure must not resolve straight past its enclosing function scope to module level",
        )

        locally_shadowed_helper = preamble + (
            "def helper():\n"
            '    return "safe"\n'
            "def f(remote):\n"
            '    helper = lambda: f"leak {remote}"\n'
            '    _record("x", {}, "api", reason=helper())\n'
        )
        self.assertTrue(
            _reason_argument_violations(locally_shadowed_helper),
            "a same-named local rebinding must not let a module-level function's safety apply",
        )

        class_body_direct = preamble + (
            "class X:\n"
            '    reason = f"leak"\n'
            '    _record("x", {}, "a", reason=reason)\n'
        )
        self.assertTrue(
            _reason_argument_violations(class_body_direct),
            "a class body is not a closure scope this checker models -- must be rejected, not resolved",
        )

        class_method = preamble + (
            "class X:\n"
            '    reason = "safe"\n'
            "    def f(self):\n"
            '        _record("x", {}, "a", reason=reason)\n'
        )
        self.assertTrue(
            _reason_argument_violations(class_method),
            "a method does not see class-body locals unqualified -- must be rejected, not resolved",
        )

    def _tamper_in_reply_to(self, envelope, value):
        """Bypass build()/encode()'s strict validation to simulate an
        already-parsed, permissive frame carrying whatever the wire said --
        the shape deliver_api actually has to defend against."""
        raw = encode(envelope)
        header, body = raw[:256], raw[256:]
        body_dict = json.loads(body)
        body_dict["in_reply_to"] = value
        return header + json.dumps(body_dict, separators=(",", ":"))

    def test_deliver_api_keeps_in_reply_to_when_delivered_by_the_claimed_client(self):
        from lib.reply_correlation import record_delivered

        target = "a" * 32
        # alice was really sent `target` by telegram, and is now replying
        # to telegram -- the claimed source matches deliver_api's own agent.
        record_delivered(self.redis, pod="test", tenant="office", agent="alice", stream_id=target, source="telegram")
        ingress = prefix("test", "office", "telegram", "ingress")
        envelope = build(
            "Message", "alice", "telegram", {"text": "reply"},
            pod="test", tenant="office", in_reply_to=target,
        )
        self.redis.lists[ingress] = [encode(envelope)]

        deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")

        inbox = prefix("test", "office", "telegram", "inbox")
        stored = json.loads(self.redis.streams[inbox][0][1]["envelope"])
        self.assertEqual(stored["in_reply_to"], target)

    def test_deliver_api_drops_in_reply_to_that_was_never_delivered(self):
        # Well-formed, but this agent never received it -- the confident-lie
        # case, not the format-error case. Must be dropped just like a
        # malformed id, and must not be surfaced to the client at all.
        target = "b" * 32
        ingress = prefix("test", "office", "telegram", "ingress")
        envelope = build(
            "Message", "alice", "telegram", {"text": "reply"},
            pod="test", tenant="office", in_reply_to=target,
        )
        self.redis.lists[ingress] = [encode(envelope)]

        deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")

        inbox = prefix("test", "office", "telegram", "inbox")
        stored = json.loads(self.redis.streams[inbox][0][1]["envelope"])
        self.assertNotIn("in_reply_to", stored)

    def test_deliver_api_drops_in_reply_to_delivered_by_a_different_client(self):
        # The cross-client case: telegram really sent `target` to alice.
        # alice now replies to webconsole (a different API client) naming
        # it. was_delivered must check WHO delivered it, not just whether
        # it was delivered to alice from anywhere -- otherwise webconsole
        # would receive a confident correlation to a turn it never
        # originated.
        from lib.reply_correlation import record_delivered

        target = "c" * 32
        record_delivered(self.redis, pod="test", tenant="office", agent="alice", stream_id=target, source="telegram")
        ingress = prefix("test", "office", "webconsole", "ingress")
        envelope = build(
            "Message", "alice", "webconsole", {"text": "reply"},
            pod="test", tenant="office", in_reply_to=target,
        )
        self.redis.lists[ingress] = [encode(envelope)]

        deliver_api(r=self.redis, pod="test", tenant="office", agent="webconsole")

        inbox = prefix("test", "office", "webconsole", "inbox")
        stored = json.loads(self.redis.streams[inbox][0][1]["envelope"])
        self.assertNotIn("in_reply_to", stored)

    def test_deliver_api_drops_a_peer_tmux_originated_id_toward_any_api_client(self):
        # The second direction of the cross-client fix, distinct from the
        # test above: `target` was never sent by ANY api client -- alice
        # sent it to bob, tmux-to-tmux, entirely off the api door's radar.
        # bob must not be able to launder that peer message into a
        # validated correlation by naming it in a reply to telegram (or any
        # other api client). This is not "wrong client", it's "no client at
        # all originated it" -- a distinct case from the one above, and the
        # one this fix is most likely to have missed if the binding were
        # only checked against a specific known-wrong client rather than
        # against the true recorded source in general.
        from lib.reply_correlation import record_delivered

        target = "e" * 32
        record_delivered(self.redis, pod="test", tenant="office", agent="bob", stream_id=target, source="alice")
        ingress = prefix("test", "office", "telegram", "ingress")
        envelope = build(
            "Message", "bob", "telegram", {"text": "reply"},
            pod="test", tenant="office", in_reply_to=target,
        )
        self.redis.lists[ingress] = [encode(envelope)]

        deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")

        inbox = prefix("test", "office", "telegram", "inbox")
        stored = json.loads(self.redis.streams[inbox][0][1]["envelope"])
        self.assertNotIn("in_reply_to", stored)

    def test_real_tmux_delivery_then_deliver_api_rejects_peer_originated_correlation(self):
        # Same case as above, but through the actual delivery path rather
        # than calling record_delivered directly -- removes any doubt that
        # this only holds because the unit test constructed the provenance
        # record by hand rather than the way message_opener really would.
        from unittest.mock import MagicMock
        from modules.tmux.port import message_opener

        target = send(
            self.redis, pod="test", tenant="office", source="alice",
            destination="bob", payload={"text": "peer message"},
        )
        raw = self.redis.lpop(prefix("test", "office", "alice", "egress"))
        envelope = parse(raw)
        with patch("modules.tmux.port.list_windows", return_value={"bob"}), \
             patch("modules.tmux.port.submit_text"):
            message_opener(self.redis, "test", "office", "bob", envelope, "sess", socket=None)

        reply_ingress = prefix("test", "office", "telegram", "ingress")
        reply_envelope = build(
            "Message", "bob", "telegram", {"text": "claiming the peer message"},
            pod="test", tenant="office", in_reply_to=target,
        )
        self.redis.lists[reply_ingress] = [encode(reply_envelope)]

        deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")

        inbox = prefix("test", "office", "telegram", "inbox")
        stored = json.loads(self.redis.streams[inbox][0][1]["envelope"])
        self.assertNotIn("in_reply_to", stored)

    def test_overlapping_prompts_answered_out_of_order_correlate_independently(self):
        # The scenario that actually motivated this feature: two prompts
        # delivered to the same agent before either is answered, answered
        # in reverse order. The harm this checks for is not "does
        # correlation exist" but "can one concurrently-delivered id's
        # provenance contaminate another's" -- the confident-lie failure
        # this whole feature exists to prevent, specifically under the
        # concurrency that motivated it, not just sequentially.
        from modules.tmux.port import message_opener

        target_a = send(
            self.redis, pod="test", tenant="office", source="telegram",
            destination="bob", payload={"text": "question A"},
        )
        raw_a = self.redis.lpop(prefix("test", "office", "telegram", "egress"))
        target_b = send(
            self.redis, pod="test", tenant="office", source="telegram",
            destination="bob", payload={"text": "question B"},
        )
        raw_b = self.redis.lpop(prefix("test", "office", "telegram", "egress"))

        with patch("modules.tmux.port.list_windows", return_value={"bob"}), \
             patch("modules.tmux.port.submit_text"):
            # Both delivered before either is answered.
            message_opener(self.redis, "test", "office", "bob", parse(raw_a), "sess", socket=None)
            message_opener(self.redis, "test", "office", "bob", parse(raw_b), "sess", socket=None)

        # Answered in reverse order: B first, then A.
        ingress = prefix("test", "office", "telegram", "ingress")
        reply_b = build("Message", "bob", "telegram", {"text": "answer B"}, pod="test", tenant="office", in_reply_to=target_b)
        self.redis.lists[ingress] = [encode(reply_b)]
        deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")

        reply_a = build("Message", "bob", "telegram", {"text": "answer A"}, pod="test", tenant="office", in_reply_to=target_a)
        self.redis.lists[ingress] = [encode(reply_a)]
        deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")

        inbox = prefix("test", "office", "telegram", "inbox")
        by_text = {
            json.loads(fields["envelope"])["payload"]["text"]: json.loads(fields["envelope"]).get("in_reply_to")
            for _, fields in self.redis.streams[inbox]
        }
        self.assertEqual(by_text["answer B"], target_b)
        self.assertEqual(by_text["answer A"], target_a)
        self.assertNotEqual(by_text["answer B"], target_a)
        self.assertNotEqual(by_text["answer A"], target_b)

    def test_deliver_api_drops_malformed_in_reply_to(self):
        ingress = prefix("test", "office", "telegram", "ingress")
        envelope = build("Message", "alice", "telegram", {"text": "reply"}, pod="test", tenant="office")
        self.redis.lists[ingress] = [self._tamper_in_reply_to(envelope, "not-a-valid-id")]

        deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")

        inbox = prefix("test", "office", "telegram", "inbox")
        stored = json.loads(self.redis.streams[inbox][0][1]["envelope"])
        self.assertNotIn("in_reply_to", stored)

    def test_deliver_api_drops_present_null_in_reply_to(self):
        # A key PRESENT with value null is not the same as absent -- must
        # be caught and dropped, not passed through as a stored null.
        ingress = prefix("test", "office", "telegram", "ingress")
        envelope = build("Message", "alice", "telegram", {"text": "reply"}, pod="test", tenant="office")
        self.redis.lists[ingress] = [self._tamper_in_reply_to(envelope, None)]

        deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")

        inbox = prefix("test", "office", "telegram", "inbox")
        stored = json.loads(self.redis.streams[inbox][0][1]["envelope"])
        self.assertNotIn("in_reply_to", stored)

    def test_deliver_api_drops_present_empty_string_in_reply_to(self):
        ingress = prefix("test", "office", "telegram", "ingress")
        envelope = build("Message", "alice", "telegram", {"text": "reply"}, pod="test", tenant="office")
        self.redis.lists[ingress] = [self._tamper_in_reply_to(envelope, "")]

        deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")

        inbox = prefix("test", "office", "telegram", "inbox")
        stored = json.loads(self.redis.streams[inbox][0][1]["envelope"])
        self.assertNotIn("in_reply_to", stored)

    def test_deliver_api_leaves_absent_in_reply_to_untouched(self):
        ingress = prefix("test", "office", "telegram", "ingress")
        envelope = build("Message", "alice", "telegram", {"text": "reply"}, pod="test", tenant="office")
        self.redis.lists[ingress] = [encode(envelope)]

        deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")

        inbox = prefix("test", "office", "telegram", "inbox")
        stored = json.loads(self.redis.streams[inbox][0][1]["envelope"])
        self.assertNotIn("in_reply_to", stored)

    def test_deliver_api_drops_and_logs_distinct_reason_when_provenance_unavailable(self):
        # A storage outage must not be recorded as "was never delivered" --
        # that would be a false claim about what actually happened.
        target = "d" * 32
        ingress = prefix("test", "office", "telegram", "ingress")
        envelope = build(
            "Message", "alice", "telegram", {"text": "reply"},
            pod="test", tenant="office", in_reply_to=target,
        )
        self.redis.lists[ingress] = [encode(envelope)]

        def broken_get(key):
            raise ConnectionError("redis unavailable")

        captured = io.StringIO()
        with patch.object(self.redis, "get", side_effect=broken_get), \
             redirect_stdout(captured):
            deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")

        inbox = prefix("test", "office", "telegram", "inbox")
        stored = json.loads(self.redis.streams[inbox][0][1]["envelope"])
        self.assertNotIn("in_reply_to", stored)
        dropped_lines = [
            line for line in captured.getvalue().splitlines()
            if json.loads(line).get("event") == "reply_correlation_dropped"
        ]
        self.assertEqual(len(dropped_lines), 1)
        self.assertIn("provenance unavailable", dropped_lines[0])
        self.assertNotIn("was never delivered", dropped_lines[0])

    def test_idle_sse_stream_emits_keepalive_without_new_entries(self):
        """No existing test opened an SSE stream and left it idle -- every
        prior test either never connects to /alerts/stream or /agents/{a}/
        activity/stream at all (test_external_route_contract just checks the
        route is registered), or the client-side bot.py tests feed entries
        into a mocked stream_fn immediately. So the silent-idle path -- an
        open connection with nothing ever written to the underlying Redis
        stream -- was never exercised by anything. This drives the real
        ASGI app with a receive() that never signals disconnect, shrinks the
        keepalive interval so the test doesn't wait multiple real seconds,
        and asserts a comment line actually reaches the wire while idle.
        """
        first = True

        async def receive():
            nonlocal first
            if first:
                first = False
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.sleep(3600)
            return {"type": "http.disconnect"}

        body_chunks = []

        async def send(message):
            if message["type"] == "http.response.body":
                body_chunks.append(message.get("body", b""))

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/alerts/stream",
            "raw_path": b"/alerts/stream",
            "query_string": b"",
            "headers": [(b"authorization", b"Bearer secret")],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 8080),
            "root_path": "",
        }

        async def run():
            with patch.object(server_module, "SSE_KEEPALIVE_INTERVAL_S", 0.05):
                task = asyncio.ensure_future(self.app(scope, receive, send))
                await asyncio.sleep(0.3)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        asyncio.run(run())

        combined = b"".join(body_chunks)
        self.assertIn(b": keepalive\n\n", combined)


class RealApiPortSubprocessTests(unittest.TestCase):
    """Runs `python -m modules.api.port` as a real subprocess, the way the
    switch actually invokes it. A bare unittest of deliver_api() -- or a mock
    asserting Popen was called with the right argv -- would both stay green
    even with no main()/__main__ guard at all, since neither ever imports the
    module as __main__ and lets it exit. This is the class of test that
    catches that: it fails loudly (nonzero exit, or ingress left undrained)
    if the module has nothing runnable behind `python -m`.
    """

    def setUp(self):
        self.redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
        self.r = redis.Redis.from_url(self.redis_url)
        self.r.ping()
        self.pod = "real-api-test"
        self.tenant = f"tenant-{os.urandom(4).hex()}"
        self.registry = prefix(self.pod, self.tenant, resource="registry")
        self.r.hset(self.registry, mapping={"harry": "tmux", "ivy": "api"})

    def tearDown(self):
        keys = self.r.keys(f"{self.pod}.{self.tenant}.*")
        if keys:
            self.r.delete(*keys)

    def test_real_api_module_subprocess_invocation(self):
        send(
            self.r,
            pod=self.pod,
            tenant=self.tenant,
            source="harry",
            destination="ivy",
            payload={"text": "invoked via python -m modules.api.port"},
        )
        raw = self.r.lpop(prefix(self.pod, self.tenant, "harry", "egress"))
        self.r.rpush(prefix(self.pod, self.tenant, "ivy", "ingress"), raw)

        env = dict(os.environ)
        env["PYTHONPATH"] = str(H_APP)
        env["POD"] = self.pod
        env["TENANT"] = self.tenant
        env["REDIS_URL"] = self.redis_url

        res = subprocess.run(
            [sys.executable, "-m", "modules.api.port", "ivy"],
            cwd=str(H_APP),
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"port main failed: {res.stderr}")

        self.assertIsNone(self.r.lpop(prefix(self.pod, self.tenant, "ivy", "ingress")))

        inbox_key = prefix(self.pod, self.tenant, "ivy", "inbox")
        entries = self.r.xrange(inbox_key, min="-", max="+")
        self.assertEqual(len(entries), 1)
        delivered = json.loads(entries[0][1][b"envelope"])
        self.assertEqual(delivered["payload"], {"text": "invoked via python -m modules.api.port"})

        delivering_key = prefix(self.pod, self.tenant, agent="ivy", resource="delivering")
        self.assertIsNone(self.r.get(delivering_key))


if __name__ == "__main__":
    unittest.main()
