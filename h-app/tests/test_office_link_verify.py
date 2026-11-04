"""office-add / offices --check / send --office: verifying the link.

The remote is a real local http.server speaking the GET /whoami contract:
200 {office, participant, kinds, api:1}; 401 {detail, reason} with reason one
of revoked | unknown_token | participant_not_enrolled.
"""

import io
import json
import types
import urllib.error
from unittest.mock import patch

import pytest

from core.keys import prefix
from lib.coop_tokens import grant, revoke
from modules.api.server import ApiSettings, create_app
from modules.office import cli as office_cli
from test_api import FakeRedis as ApiRedis
from test_api import request as asgi_request
from test_office_cli import POD, TENANT, _env
from test_office_interoffice import (  # noqa: F401 -- fixtures + helpers
    LEAD, OFFICES, OFFICE_TOKENS, SECRET, TOKEN, Redis, book, exit_code, remote, reply, run,
)

WHO = {"office": "beta-office", "participant": "alpha-office", "kinds": ["Message"], "api": 1}


def lead_redis(**kw):
    r = Redis(registry={"relay": "api"}, **kw)
    r.values[LEAD] = "architect"
    return r


def add(r, url, monkeypatch, *extra, participant="alpha-office"):
    argv = ["office-add", "beta", url, "--token-stdin", *extra]
    if participant:
        argv += ["--participant", participant]
    with patch("modules.office.cli.send"):
        return exit_code(r, argv, monkeypatch, TOKEN)


# ---------------------------------------------------------------- office-add

def test_add_probes_whoami_with_the_token_then_saves(monkeypatch, capsys, remote):
    _env(monkeypatch)
    rem = remote(reply(200, WHO))
    r = lead_redis()
    with patch("modules.office.cli.send"):
        run(r, ["office-add", "beta", rem.url, "--participant", "alpha-office", "--token-stdin"],
            monkeypatch, TOKEN)
    assert rem.requests == [{"method": "GET", "path": "/whoami", "auth": f"Bearer {TOKEN}",
                             "body": None}]
    assert json.loads(r.hashes[OFFICES]["beta"])["participant"] == "alpha-office"
    assert r.hashes[OFFICE_TOKENS]["beta"] == TOKEN
    out = capsys.readouterr()
    assert "verified with the remote office (beta-office)" in out.out
    assert TOKEN not in out.out + out.err


def test_add_defaults_the_participant_from_whoami(monkeypatch, capsys, remote):
    _env(monkeypatch)
    rem = remote(reply(200, WHO))
    r = lead_redis()
    with patch("modules.office.cli.send"):
        run(r, ["office-add", "beta", rem.url, "--token-stdin"], monkeypatch, TOKEN)
    assert json.loads(r.hashes[OFFICES]["beta"])["participant"] == "alpha-office"
    assert "(as alpha-office)" in capsys.readouterr().out


def test_add_participant_mismatch_names_the_right_one_and_saves_nothing(monkeypatch, capsys, remote):
    _env(monkeypatch)
    rem = remote(reply(200, WHO))
    r = lead_redis()
    assert add(r, rem.url, monkeypatch, participant="someone-else") == 1
    err = capsys.readouterr().err
    assert "belongs to participant 'alpha-office'" in err and "'someone-else'" in err
    assert "--participant alpha-office" in err and "nothing saved" in err
    assert not r.hashes[OFFICES] and not r.hashes[OFFICE_TOKENS]


@pytest.mark.parametrize("reason, fragment", [
    ("revoked", "revoked"),
    ("unknown_token", "does not recognise this token"),
    ("participant_not_enrolled", "no longer enrolled"),
    (None, "did not accept the cooperation token"),
    ("something-new", "did not accept the cooperation token"),
], ids=["revoked", "unknown_token", "not_enrolled", "no_reason", "new_reason"])
def test_add_names_the_401_reason_and_saves_nothing(reason, fragment, monkeypatch, capsys, remote):
    _env(monkeypatch)
    body = {"detail": f"Unauthorized {TOKEN}"}
    if reason:
        body["reason"] = reason
    rem = remote(reply(401, body))
    r = lead_redis()
    assert add(r, rem.url, monkeypatch) == 1
    err = capsys.readouterr().err
    assert fragment in err and "nothing saved" in err and "coop-grant" in err
    assert TOKEN not in err and SECRET not in err
    assert not r.hashes[OFFICES] and not r.hashes[OFFICE_TOKENS]


def test_add_tolerates_a_reason_nested_in_detail(monkeypatch, capsys, remote):
    _env(monkeypatch)
    rem = remote(reply(401, {"detail": {"reason": "revoked"}}))
    assert add(lead_redis(), rem.url, monkeypatch) == 1
    assert "revoked" in capsys.readouterr().err


def test_add_unreachable_remote_saves_nothing(monkeypatch, capsys):
    _env(monkeypatch)
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    r = lead_redis()
    assert add(r, f"http://127.0.0.1:{port}", monkeypatch) == 1
    err = capsys.readouterr().err
    assert "cannot reach the remote office" in err and "nothing saved" in err
    assert not r.hashes[OFFICES]


def test_add_old_remote_without_whoami_says_so(monkeypatch, capsys, remote):
    _env(monkeypatch)
    rem = remote(reply(404, {"detail": "Not Found"}))
    r = lead_redis()
    assert add(r, rem.url, monkeypatch) == 1
    err = capsys.readouterr().err
    assert "no /whoami" in err and "--no-verify" in err
    assert not r.hashes[OFFICES]


@pytest.mark.parametrize("handler, fragment", [
    (reply(200, raw=b"<html>hi</html>"), "did not answer /whoami like an h-mesh api door"),
    (reply(200, {"office": "x"}), "did not answer /whoami like an h-mesh api door"),
    (reply(200, dict(WHO, api=2)), "api version 2"),
    (reply(500, {"detail": "boom"}), "HTTP 500"),
    (reply(302, {}, headers=[("Location", "http://evil.example/")]), "redirect"),
], ids=["html", "partial_json", "api_v2", "http_500", "redirect"])
def test_add_odd_answers_are_refused(handler, fragment, monkeypatch, capsys, remote):
    _env(monkeypatch)
    rem = remote(handler)
    r = lead_redis()
    assert add(r, rem.url, monkeypatch) == 1
    assert fragment in capsys.readouterr().err
    assert not r.hashes[OFFICES]


def test_add_refuses_a_reserved_participant_reported_by_the_remote(monkeypatch, capsys, remote):
    _env(monkeypatch)
    rem = remote(reply(200, dict(WHO, participant="host")))
    r = lead_redis()
    assert add(r, rem.url, monkeypatch, participant=None) == 1
    assert "reserved" in capsys.readouterr().err
    assert not r.hashes[OFFICES]


def test_add_no_verify_makes_no_request_and_needs_a_participant(monkeypatch, capsys, remote):
    _env(monkeypatch)
    rem = remote(reply(401, {"reason": "revoked"}))
    r = lead_redis()
    assert add(r, rem.url, monkeypatch, "--no-verify", participant=None) == 1
    assert "--no-verify needs --participant" in capsys.readouterr().err
    with patch("modules.office.cli.send"):
        run(r, ["office-add", "beta", rem.url, "--token-stdin", "--no-verify",
                "--participant", "alpha-office"], monkeypatch, TOKEN)
    assert rem.requests == []
    assert "beta" in r.hashes[OFFICES]
    assert "NOT verified" in capsys.readouterr().out


def test_add_probe_comes_after_the_lead_check_and_local_validation(monkeypatch, remote):
    _env(monkeypatch)
    rem = remote(reply(200, WHO))
    r = Redis(registry={"relay": "api"})            # no lead
    assert add(r, rem.url, monkeypatch) == 1
    r.values[LEAD] = "architect"
    r.hashes[prefix(POD, TENANT, resource="registry")]["relay"] = "tmux"   # relay taken by a non-api agent
    assert add(r, rem.url, monkeypatch) == 1
    assert rem.requests == []


# ------------------------------------------------------------ offices --check

def test_offices_without_check_makes_no_request(monkeypatch, capsys, remote):
    _env(monkeypatch)
    rem = remote(reply(200, WHO))
    r = Redis()
    book(r, rem.url)
    run(r, ["offices"], monkeypatch)
    assert rem.requests == [] and "link:" not in capsys.readouterr().out


def test_offices_check_ok(monkeypatch, capsys, remote):
    _env(monkeypatch)
    rem = remote(reply(200, WHO))
    r = Redis()
    book(r, rem.url)
    run(r, ["offices", "--check"], monkeypatch)
    out = capsys.readouterr()
    assert "link: ok" in out.out and TOKEN not in out.out + out.err
    assert rem.requests[0]["auth"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize("handler, label", [
    (reply(401, {"reason": "revoked"}), "link: token problem -- the cooperation token was revoked"),
    (reply(200, dict(WHO, participant="other")), "link: participant mismatch"),
    (reply(404, {}), "link: cannot verify"),
], ids=["revoked", "mismatch", "no_whoami"])
def test_offices_check_reports_each_state_and_exits_1(handler, label, monkeypatch, capsys, remote):
    _env(monkeypatch)
    rem = remote(handler)
    r = Redis()
    book(r, rem.url)
    assert exit_code(r, ["offices", "--check"], monkeypatch) == 1
    out = capsys.readouterr()
    assert label in out.out and "1 office link(s) not ok" in out.err
    assert TOKEN not in out.out + out.err


def test_offices_check_unreachable_and_missing_token_dont_stop_the_rest(monkeypatch, capsys, remote):
    _env(monkeypatch)
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    good = remote(reply(200, WHO))
    r = Redis()
    book(r, good.url, name="aaa-good")
    book(r, f"http://127.0.0.1:{port}", name="bbb-down")
    book(r, good.url, name="ccc-notoken")
    del r.hashes[OFFICE_TOKENS]["ccc-notoken"]
    assert exit_code(r, ["offices", "--check"], monkeypatch) == 1
    out = capsys.readouterr()
    lines = {ln.split("\t")[0]: ln for ln in out.out.splitlines()}
    assert "link: ok" in lines["aaa-good"]
    assert "link: unreachable" in lines["bbb-down"]
    assert "no token saved" in lines["ccc-notoken"]
    assert "2 office link(s) not ok" in out.err


# ---------------------------------------------------------------------- send

@pytest.mark.parametrize("code, payload, fragment", [
    (401, {"reason": "revoked"}, "was revoked by the remote office"),
    (401, {}, "did not accept our cooperation token"),
    (403, {"detail": "no"}, "does not allow this token to send to 'scout'"),
    (404, {"detail": "unknown agent"}, "has no agent 'scout'"),
], ids=["401_revoked", "401_plain", "403", "404"])
def test_send_office_failures_name_the_cause(code, payload, fragment, monkeypatch, capsys, remote):
    _env(monkeypatch)
    rem = remote(reply(code, payload))
    r = Redis()
    book(r, rem.url)
    assert exit_code(r, ["send", "--office", "beta", "-a", "scout", "x"], monkeypatch) == 1
    err = capsys.readouterr().err
    assert f"HTTP {code}" in err and fragment in err and TOKEN not in err


# ------------------------------------------- against the REAL api app (ASGI)
# The office CLI is driven against modules.api.server.create_app in-process:
# urllib's opener is replaced by one that runs the request through the real
# app, so the 401 reasons, the /whoami body and the coop routing are the
# server's own, not a re-statement of the contract.

class _Resp:
    def __init__(self, status, raw):
        self.status, self._raw = status, raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        return self._raw


class _AppOpener:
    def __init__(self, app):
        self.app = app

    def open(self, req, timeout=None):
        from urllib.parse import urlsplit
        auth = req.get_header("Authorization") or ""
        status, payload = asgi_request(
            self.app, req.get_method(), urlsplit(req.full_url).path,
            token=auth.removeprefix("Bearer ") or None,
            body=json.loads(req.data) if req.data else None,
        )
        raw = json.dumps(payload).encode()
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "err", {}, io.BytesIO(raw))
        return _Resp(status, raw)


@pytest.fixture
def real_office(monkeypatch):
    server_redis = ApiRedis()
    server_redis.registry.update({"coop-a": "api", "alice": "tmux", "telegram": "api"})
    app = create_app(settings=ApiSettings(pod="test", tenant="office", api_token="secret"),
                     redis_client=server_redis)
    monkeypatch.setattr(office_cli.urllib.request, "build_opener",
                        lambda *handlers: _AppOpener(app))
    token_id, token = grant(server_redis, "test", "office", "coop-a")
    return types.SimpleNamespace(redis=server_redis, token_id=token_id, token=token)



def add_real(r, monkeypatch, token, *extra, participant="coop-a"):
    argv = ["office-add", "beta", "https://beta.example", "--token-stdin", *extra]
    if participant:
        argv += ["--participant", participant]
    with patch("modules.office.cli.send"):
        return exit_code(r, argv, monkeypatch, token)


def test_real_app_add_succeeds_and_defaults_the_participant(real_office, monkeypatch, capsys):
    _env(monkeypatch)
    r = lead_redis()
    with patch("modules.office.cli.send"):
        run(r, ["office-add", "beta", "https://beta.example", "--token-stdin"],
            monkeypatch, real_office.token)
    assert json.loads(r.hashes[OFFICES]["beta"])["participant"] == "coop-a"
    assert "verified with the remote office (test/office)" in capsys.readouterr().out


def test_real_app_add_wrong_participant_names_the_right_one(real_office, monkeypatch, capsys):
    _env(monkeypatch)
    r = lead_redis()
    assert add_real(r, monkeypatch, real_office.token, participant="someone-else") == 1
    err = capsys.readouterr().err
    assert "belongs to participant 'coop-a'" in err and "--participant coop-a" in err
    assert not r.hashes[OFFICES]


def test_real_app_add_every_refusal_reason(real_office, monkeypatch, capsys):
    _env(monkeypatch)
    secret = real_office.token.rsplit(".", 1)[1]
    wrong_secret = f"coop.{real_office.token_id}.{'B' * 43}"
    r = lead_redis()
    assert add_real(r, monkeypatch, wrong_secret) == 1
    assert "does not recognise this token" in capsys.readouterr().err
    real_office.redis.registry["coop-a"] = "tmux"
    assert add_real(r, monkeypatch, real_office.token) == 1
    assert "no longer enrolled" in capsys.readouterr().err
    real_office.redis.registry["coop-a"] = "api"
    assert revoke(real_office.redis, "test", "office", real_office.token_id)
    assert add_real(r, monkeypatch, real_office.token) == 1
    err = capsys.readouterr().err
    assert "revoked" in err and secret not in err
    assert not r.hashes[OFFICES] and not r.hashes[OFFICE_TOKENS]


def test_real_app_offices_check_tracks_the_token_lifecycle(real_office, monkeypatch, capsys):
    _env(monkeypatch)
    r = Redis()
    book(r, "https://beta.example", participant="coop-a", token=real_office.token)
    run(r, ["offices", "--check"], monkeypatch)
    assert "link: ok" in capsys.readouterr().out
    book(r, "https://beta.example", participant="other", token=real_office.token)
    assert exit_code(r, ["offices", "--check"], monkeypatch) == 1
    assert "link: participant mismatch" in capsys.readouterr().out
    revoke(real_office.redis, "test", "office", real_office.token_id)
    book(r, "https://beta.example", participant="coop-a", token=real_office.token)
    assert exit_code(r, ["offices", "--check"], monkeypatch) == 1
    assert "link: token problem -- the cooperation token was revoked" in capsys.readouterr().out


def test_real_app_send_names_403_and_404_causes(real_office, monkeypatch, capsys):
    _env(monkeypatch)
    r = Redis()
    book(r, "https://beta.example", participant="coop-a", token=real_office.token)
    assert exit_code(r, ["send", "--office", "beta", "-a", "ghost", "x"], monkeypatch) == 1
    assert "has no agent 'ghost'" in capsys.readouterr().err
    assert exit_code(r, ["send", "--office", "beta", "-a", "telegram", "x"], monkeypatch) == 1
    assert "does not allow this token to send to 'telegram'" in capsys.readouterr().err
    revoke(real_office.redis, "test", "office", real_office.token_id)
    assert exit_code(r, ["send", "--office", "beta", "-a", "alice", "x"], monkeypatch) == 1
    assert "HTTP 401" in capsys.readouterr().err


def test_offices_check_counts_an_unreadable_entry_but_plain_offices_does_not(monkeypatch, capsys):
    _env(monkeypatch)
    r = Redis()
    r.hashes[OFFICES]["broken"] = "{not json"
    run(r, ["offices"], monkeypatch)                          # plain listing: exit 0
    assert "broken\t(unreadable entry)" in capsys.readouterr().out
    assert exit_code(r, ["offices", "--check"], monkeypatch) == 1
    assert "unreadable entry; re-add it" in capsys.readouterr().out
