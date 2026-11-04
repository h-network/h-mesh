"""Inter-office office CLI: send --office, address book, cooperation grants.

The HTTP side runs against a REAL local http.server so headers, redirects,
refused connections and timeouts are the actual urllib behaviour, not a mock
of it. lib/coop_tokens (api-agent) is replaced by a stand-in with the agreed
interface until that module is merged.
"""

import io
import json
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest

from core.keys import incarnation_key, prefix
from modules.office import cli as office_cli
from modules.office.cli import main as office_main
from test_office_cli import POD, TENANT, FakeRedis, _env

SECRET = "S" * 43
TOKEN_ID = "ab12cd34ef567890"
TOKEN = f"coop.{TOKEN_ID}.{SECRET}"
STREAM_ID = "0123456789abcdef0123456789abcdef"
OFFICES = prefix(POD, TENANT, resource="offices")
OFFICE_TOKENS = prefix(POD, TENANT, resource="offices.tokens")
LEAD = prefix(POD, TENANT, resource="lead")


class Redis(FakeRedis):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.set_ttls = {}

    def set(self, key, value, ex=None):
        self.set_ttls[key] = ex
        return super().set(key, value, ex=ex)


class Remote:
    """A throwaway remote office api door."""

    def __init__(self, handler):
        self.requests = []
        remote = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                remote.requests.append({
                    "path": self.path, "auth": self.headers.get("Authorization"),
                    "body": json.loads(body),
                })
                handler(self)

            def do_GET(self):
                remote.requests.append({
                    "method": "GET", "path": self.path,
                    "auth": self.headers.get("Authorization"), "body": None,
                })
                handler(self)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def reply(code, payload=None, raw=None, headers=()):
    def handler(h):
        data = raw if raw is not None else json.dumps(payload).encode()
        h.send_response(code)
        for k, v in headers:
            h.send_header(k, v)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)
    return handler


@pytest.fixture
def remote():
    made = []

    def make(handler):
        rem = Remote(handler)
        made.append(rem)
        return rem
    yield make
    for rem in made:
        rem.close()


def book(r, url, name="beta", participant="alpha-office", token=TOKEN):
    r.hashes[OFFICES][name] = json.dumps({"url": url, "participant": participant,
                                           "added_by": "architect", "added_ts": "t"})
    r.hashes[OFFICE_TOKENS][name] = token


def run(r, argv, monkeypatch, stdin=None):
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(argv)


def exit_code(r, argv, monkeypatch, stdin=None):
    with pytest.raises(SystemExit) as info:
        run(r, argv, monkeypatch, stdin)
    return info.value.code


# --------------------------------------------------------------------- send

def test_send_office_posts_the_contract_and_records_the_sender(monkeypatch, capsys, remote):
    _env(monkeypatch)
    rem = remote(reply(202, {"stream_id": STREAM_ID, "correlation_id": "c" * 32}))
    r = Redis(registry={"architect": "tmux"})
    r.values[incarnation_key(POD, TENANT, "architect")] = "inc-1"
    book(r, rem.url + "/")
    run(r, ["send", "--office", "beta", "-a", "scout", "hello there"], monkeypatch)

    assert rem.requests == [{
        "path": "/agents/scout/envelopes", "auth": f"Bearer {TOKEN}",
        "body": {"text": "hello there", "as": "alpha-office"},
    }]
    assert capsys.readouterr().out == f"sent to beta/scout: 11 bytes ({STREAM_ID})\n"
    key = prefix(POD, TENANT, resource=f"xsent.s{STREAM_ID}")
    stored = json.loads(r.values[key])
    assert {k: stored[k] for k in ("sender", "sender_incarnation", "office", "agent")} == {
        "sender": "architect", "sender_incarnation": "inc-1", "office": "beta", "agent": "scout",
    }
    assert stored["ts"]
    assert stored["correlation_id"] == "c" * 32
    assert r.set_ttls[key] == 86400
    # Nothing went onto the local bus.
    assert not r.lists


def test_send_office_without_known_incarnation_records_null(monkeypatch, remote):
    _env(monkeypatch)
    rem = remote(reply(202, {"stream_id": STREAM_ID, "correlation_id": "c"}))
    r = Redis()
    book(r, rem.url)
    run(r, ["send", "--office", "beta", "-a", "scout", "hi"], monkeypatch)
    stored = json.loads(r.values[prefix(POD, TENANT, resource=f"xsent.s{STREAM_ID}")])
    assert stored["sender_incarnation"] is None
    assert stored["correlation_id"] is None


@pytest.mark.parametrize("fields", [{}, {"correlation_id": "invalid"}], ids=["missing", "malformed"])
def test_send_office_invalid_or_missing_correlation_is_not_trusted(monkeypatch, remote, fields):
    _env(monkeypatch)
    rem = remote(reply(202, {"stream_id": STREAM_ID, **fields}))
    r = Redis(registry={"architect": "tmux"})
    book(r, rem.url)
    run(r, ["send", "--office", "beta", "-a", "scout", "hi"], monkeypatch)
    stored = json.loads(r.values[prefix(POD, TENANT, resource=f"xsent.s{STREAM_ID}")])
    assert stored["correlation_id"] is None
    assert stored["sender"] == "architect"


@pytest.mark.parametrize("argv, fragment", [
    (["--office", "beta", "-a", "all", "x"], "invalid agent name"),
    (["--office", "beta", "-a", "api", "x"], "not an agent"),
    (["--office", "beta", "-a", "host", "x"], "not an agent"),
    (["--office", "Beta!", "-a", "scout", "x"], "invalid office name"),
    (["--office", "beta", "-a", "scout", "--reply-to", STREAM_ID, "x"], "cannot be combined"),
    (["--office", "beta", "-a", "scout", "--context", "c", "x"], "cannot be combined"),
])
def test_send_office_rejects_bad_arguments_before_any_io(argv, fragment, monkeypatch, capsys):
    _env(monkeypatch)
    r = Redis()
    with patch("modules.office.cli.urllib.request.OpenerDirector.open") as opened:
        assert exit_code(r, ["send", *argv], monkeypatch) == 1
    assert fragment in capsys.readouterr().err
    opened.assert_not_called()


def test_send_unknown_office_and_missing_token(monkeypatch, capsys):
    _env(monkeypatch)
    r = Redis()
    assert exit_code(r, ["send", "--office", "beta", "-a", "scout", "x"], monkeypatch) == 1
    assert "unknown office" in capsys.readouterr().err
    r.hashes[OFFICES]["beta"] = json.dumps({"url": "http://127.0.0.1:1", "participant": "p"})
    assert exit_code(r, ["send", "--office", "beta", "-a", "scout", "x"], monkeypatch) == 1
    assert "no token" in capsys.readouterr().err


@pytest.mark.parametrize("code, payload", [
    (401, {"detail": "invalid token"}), (403, {"detail": "as must be alpha-office"}),
    (404, {"detail": "unknown agent"}), (422, {"detail": "bad"}),
])
def test_send_refusals_exit_1_and_never_echo_the_token(code, payload, monkeypatch, capsys, remote):
    _env(monkeypatch)
    payload = dict(payload, detail=payload["detail"] + f" {TOKEN}")  # a hostile/careless server
    rem = remote(reply(code, payload))
    r = Redis()
    book(r, rem.url)
    assert exit_code(r, ["send", "--office", "beta", "-a", "scout", "x"], monkeypatch) == 1
    captured = capsys.readouterr()
    assert f"HTTP {code}" in captured.err
    assert TOKEN not in captured.err + captured.out and SECRET not in captured.err + captured.out
    assert not any(k.startswith(prefix(POD, TENANT, resource="xsent")) for k in r.values)


def test_send_connection_refused_is_a_provable_failure(monkeypatch, capsys):
    _env(monkeypatch)
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    r = Redis()
    book(r, f"http://127.0.0.1:{port}")
    assert exit_code(r, ["send", "--office", "beta", "-a", "scout", "x"], monkeypatch) == 1
    assert "cannot reach" in capsys.readouterr().err


@pytest.mark.parametrize("handler", [
    reply(500, {"detail": "boom"}),
    reply(200, {"stream_id": STREAM_ID}),            # not 202
    reply(202, raw=b"not json"),
    reply(202, {"stream_id": "short"}),
    reply(302, {}, headers=[("Location", "http://127.0.0.1:9/elsewhere")]),
])
def test_send_unknown_outcomes_exit_3(handler, monkeypatch, capsys, remote):
    _env(monkeypatch)
    rem = remote(handler)
    r = Redis()
    book(r, rem.url)
    assert exit_code(r, ["send", "--office", "beta", "-a", "scout", "x"], monkeypatch) == 3
    err = capsys.readouterr().err
    assert "do not blindly retry" in err and TOKEN not in err


def test_send_timeout_is_unknown_exit_3(monkeypatch, capsys, remote):
    _env(monkeypatch)
    monkeypatch.setattr(office_cli, "_OFFICE_HTTP_TIMEOUT_SECONDS", 0.3)
    release = threading.Event()
    rem = remote(lambda h: release.wait(3))
    r = Redis()
    book(r, rem.url)
    try:
        assert exit_code(r, ["send", "--office", "beta", "-a", "scout", "x"], monkeypatch) == 3
    finally:
        release.set()
    assert "do not blindly retry" in capsys.readouterr().err


def test_send_never_follows_a_redirect_with_the_token(monkeypatch, remote):
    _env(monkeypatch)
    elsewhere = remote(reply(202, {"stream_id": STREAM_ID}))
    rem = remote(reply(307, {}, headers=[("Location", elsewhere.url + "/steal")]))
    r = Redis()
    book(r, rem.url)
    assert exit_code(r, ["send", "--office", "beta", "-a", "scout", "x"], monkeypatch) == 3
    assert elsewhere.requests == []


def test_send_accepted_but_unrecordable_warns_and_still_succeeds(monkeypatch, capsys, remote):
    _env(monkeypatch)
    rem = remote(reply(202, {"stream_id": STREAM_ID}))

    class Broken(Redis):
        def set(self, *a, **k):
            raise RuntimeError("redis went away")
    r = Broken()
    book(r, rem.url)
    run(r, ["send", "--office", "beta", "-a", "scout", "x"], monkeypatch)
    captured = capsys.readouterr()
    assert STREAM_ID in captured.out and "cannot be routed back" in captured.err


# ------------------------------------------------------------- address book

def test_office_add_is_lead_only_and_reads_the_token_from_stdin(monkeypatch, capsys):
    _env(monkeypatch)
    r = Redis()
    argv = ["office-add", "beta", "https://beta.example:8080/", "--participant", "alpha-office",
            "--token-stdin", "--no-verify"]
    assert exit_code(r, argv, monkeypatch, TOKEN + "\n") == 1       # no lead at all
    assert "no lead" in capsys.readouterr().err
    r.values[LEAD] = "someone-else"
    assert exit_code(r, argv, monkeypatch, TOKEN + "\n") == 1
    assert "lead-only" in capsys.readouterr().err
    assert not r.hashes[OFFICES] and not r.hashes[OFFICE_TOKENS]

    r.values[LEAD] = "architect"
    with patch("modules.office.cli.send") as send:
        run(r, argv, monkeypatch, TOKEN + "\n")
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err
    assert json.loads(r.hashes[OFFICES]["beta"])["url"] == "https://beta.example:8080"
    assert r.hashes[OFFICE_TOKENS]["beta"] == TOKEN
    # the relay participant was enrolled as an api participant
    (call,) = send.call_args_list
    assert call.kwargs["kind"] == "StartAgent" and call.kwargs["destination"] == "host"
    assert call.kwargs["payload"] == {"agent": "relay", "port_type": "api"}


def test_office_add_does_not_reenrol_an_existing_relay(monkeypatch):
    _env(monkeypatch)
    r = Redis(registry={"relay": "api"})
    r.values[LEAD] = "architect"
    with patch("modules.office.cli.send") as send:
        run(r, ["office-add", "beta", "https://b.example", "--participant", "p", "--token-stdin",
                "--no-verify"], monkeypatch, TOKEN)
    send.assert_not_called()


def test_office_add_refuses_a_non_api_relay_before_writing(monkeypatch, capsys):
    _env(monkeypatch)
    r = Redis(registry={"relay": "tmux"})
    r.values[LEAD] = "architect"
    assert exit_code(r, ["office-add", "beta", "https://b.example", "--participant", "p",
                         "--token-stdin"], monkeypatch, TOKEN) == 1
    assert not r.hashes[OFFICES] and not r.hashes[OFFICE_TOKENS]


def test_office_add_has_no_token_argument(monkeypatch, capsys):
    _env(monkeypatch)
    r = Redis()
    r.values[LEAD] = "architect"
    assert exit_code(r, ["office-add", "beta", "https://b.example", "--participant", "p",
                         "--token", TOKEN], monkeypatch) == 2
    assert exit_code(r, ["office-add", "beta", "https://b.example", "--participant", "p"],
                     monkeypatch) == 2          # --token-stdin is mandatory
    assert TOKEN not in capsys.readouterr().out


@pytest.mark.parametrize("url, stdin, fragment", [
    ("ftp://b.example", TOKEN, "http(s)"),
    ("https://u:pw@b.example", TOKEN, "credentials"),
    ("https://b.example/?x=1", TOKEN, "credentials"),
    ("http://b.example:8080", TOKEN, "plaintext"),
    ("https://b.example", "not-a-token", "cooperation token"),
    ("https://b.example", "", "cooperation token"),
])
def test_office_add_validation(url, stdin, fragment, monkeypatch, capsys):
    _env(monkeypatch)
    monkeypatch.delenv("H_MESH_ALLOW_PLAINTEXT", raising=False)
    r = Redis()
    r.values[LEAD] = "architect"
    assert exit_code(r, ["office-add", "beta", url, "--participant", "p", "--token-stdin"],
                     monkeypatch, stdin) == 1
    err = capsys.readouterr().err
    assert fragment in err and TOKEN not in err
    assert not r.hashes[OFFICES]


def test_office_add_plaintext_needs_explicit_opt_in_and_warns(monkeypatch, capsys):
    _env(monkeypatch)
    monkeypatch.delenv("H_MESH_ALLOW_PLAINTEXT", raising=False)
    r = Redis(registry={"relay": "api"})
    r.values[LEAD] = "architect"
    run(r, ["office-add", "beta", "http://10.0.0.5:8080", "--participant", "p", "--token-stdin",
            "--allow-plaintext", "--no-verify"], monkeypatch, TOKEN)
    assert "plaintext" in capsys.readouterr().err
    run(r, ["office-add", "loop", "http://127.0.0.1:8080", "--participant", "p", "--token-stdin",
            "--no-verify"], monkeypatch, TOKEN)                       # loopback needs no opt-in


def test_offices_lists_everyone_and_never_prints_a_token(monkeypatch, capsys):
    _env(monkeypatch, agent="not-the-lead")
    r = Redis()
    book(r, "https://beta.example")
    r.hashes[OFFICES]["broken"] = "{not json"
    run(r, ["offices"], monkeypatch)
    out = capsys.readouterr()
    assert "beta\thttps://beta.example\tas alpha-office" in out.out
    assert "broken\t(unreadable entry)" in out.out
    assert TOKEN not in out.out + out.err and SECRET not in out.out + out.err
    assert "coop." not in out.out + out.err


def test_offices_empty(monkeypatch, capsys):
    _env(monkeypatch)
    run(Redis(), ["offices"], monkeypatch)
    assert capsys.readouterr().out == "no offices configured\n"


def test_office_rm_is_lead_only_and_removes_record_and_token(monkeypatch, capsys):
    _env(monkeypatch)
    r = Redis()
    book(r, "https://beta.example")
    assert exit_code(r, ["office-rm", "beta"], monkeypatch) == 1       # no lead
    r.values[LEAD] = "other"
    assert exit_code(r, ["office-rm", "beta"], monkeypatch) == 1
    assert "beta" in r.hashes[OFFICES]
    r.values[LEAD] = "architect"
    run(r, ["office-rm", "beta"], monkeypatch)
    assert "beta" not in r.hashes[OFFICES] and "beta" not in r.hashes[OFFICE_TOKENS]
    assert exit_code(r, ["office-rm", "beta"], monkeypatch) == 1       # already gone


# ---------------------------------------------------------- cooperation grants

class FakeCoop(types.ModuleType):
    def __init__(self):
        super().__init__("lib.coop_tokens")
        self.calls = []

    def grant(self, r, pod, tenant, participant, kinds=("Message",)):
        self.calls.append(("grant", participant, tuple(kinds)))
        return TOKEN_ID, TOKEN

    def revoke(self, r, pod, tenant, token_id):
        self.calls.append(("revoke", token_id))

    def list_tokens(self, r, pod, tenant):
        # a careless implementation that leaks extra fields must still not be printed
        return [{"token_id": TOKEN_ID, "participant": "alpha-office", "kinds": ["Message"],
                 "created_ts": 1, "revoked": False, "sha256": "deadbeef", "secret": SECRET}]


@pytest.fixture
def coop(monkeypatch):
    fake = FakeCoop()
    monkeypatch.setitem(sys.modules, "lib.coop_tokens", fake)
    return fake


def test_coop_grant_enrols_then_mints_and_prints_the_token_once(monkeypatch, capsys, coop):
    _env(monkeypatch)
    r = Redis()
    r.values[LEAD] = "architect"
    with patch("modules.office.cli.send") as send:
        run(r, ["coop-grant", "alpha-office"], monkeypatch)
    assert send.call_args.kwargs["payload"] == {"agent": "alpha-office", "port_type": "api"}
    assert send.call_args.kwargs["kind"] == "StartAgent"
    assert coop.calls == [("grant", "alpha-office", ("Message",))]
    out = capsys.readouterr()
    assert f"token: {TOKEN}" in out.out and f"token-id: {TOKEN_ID}" in out.out
    assert TOKEN not in out.err


@pytest.mark.parametrize("name", ["api", "host", "all", "Bad Name"])
def test_coop_grant_rejects_reserved_participants(name, monkeypatch, capsys, coop):
    _env(monkeypatch)
    r = Redis()
    r.values[LEAD] = "architect"
    assert exit_code(r, ["coop-grant", name], monkeypatch) == 1
    assert coop.calls == []


def test_coop_grant_refuses_non_lead_and_existing_non_api_agent(monkeypatch, capsys, coop):
    _env(monkeypatch)
    r = Redis(registry={"scout": "tmux"})
    assert exit_code(r, ["coop-grant", "alpha-office"], monkeypatch) == 1
    r.values[LEAD] = "architect"
    assert exit_code(r, ["coop-grant", "scout"], monkeypatch) == 1
    assert coop.calls == []


def test_coop_grant_without_the_module_fails_cleanly(monkeypatch, capsys):
    _env(monkeypatch)
    monkeypatch.setitem(sys.modules, "lib.coop_tokens", None)    # forces ImportError
    r = Redis()
    r.values[LEAD] = "architect"
    assert exit_code(r, ["coop-grant", "alpha-office"], monkeypatch) == 1
    assert "not available" in capsys.readouterr().err


def test_coop_revoke_is_lead_only_and_takes_the_id_not_the_token(monkeypatch, capsys, coop):
    _env(monkeypatch)
    r = Redis()
    assert exit_code(r, ["coop-revoke", TOKEN_ID], monkeypatch) == 1
    r.values[LEAD] = "architect"
    assert exit_code(r, ["coop-revoke", TOKEN], monkeypatch) == 1      # a full token is refused
    err = capsys.readouterr().err
    assert TOKEN not in err and SECRET not in err                       # ...and never echoed
    run(r, ["coop-revoke", TOKEN_ID], monkeypatch)
    assert coop.calls == [("revoke", TOKEN_ID)]


def test_coops_lists_only_whitelisted_fields(monkeypatch, capsys, coop):
    _env(monkeypatch, agent="not-the-lead")
    run(Redis(), ["coops"], monkeypatch)
    out = capsys.readouterr().out
    assert out == f"{TOKEN_ID}\talpha-office\tMessage\tactive\t1\n"
    assert "deadbeef" not in out and SECRET not in out


# ----------------------------------------------------------------- dispatch

def test_new_commands_are_registered_everywhere():
    for name in ("office-add", "offices", "office-rm", "coop-grant", "coop-revoke", "coops"):
        assert name in office_cli._COMMANDS and name in office_cli._DISPATCH


# ------------------------------------------- inter-office participant marker

PARTICIPANTS = prefix(POD, TENANT, resource="interoffice-participants")


def test_office_add_marks_the_relay_whether_or_not_it_is_already_enrolled(monkeypatch):
    _env(monkeypatch)
    for registry in ({}, {"relay": "api"}):
        r = Redis(registry=registry)
        r.values[LEAD] = "architect"
        with patch("modules.office.cli.send"):
            run(r, ["office-add", "beta", "https://b.example", "--participant", "p",
                    "--token-stdin", "--no-verify"], monkeypatch, TOKEN)
        assert r.hashes[PARTICIPANTS] == {"relay": "relay"}


def test_coop_grant_marks_the_participant_as_inter_office(monkeypatch, coop):
    _env(monkeypatch)
    r = Redis()
    r.values[LEAD] = "architect"
    with patch("modules.office.cli.send"):
        run(r, ["coop-grant", "alpha-office"], monkeypatch)
    assert r.hashes[PARTICIPANTS] == {"alpha-office": "coop"}


def test_a_refused_coop_grant_marks_nobody(monkeypatch, coop):
    _env(monkeypatch)
    r = Redis(registry={"scout": "tmux"})
    r.values[LEAD] = "architect"
    assert exit_code(r, ["coop-grant", "scout"], monkeypatch) == 1
    assert exit_code(r, ["coop-grant", "api"], monkeypatch) == 1
    assert not r.hashes[PARTICIPANTS]
