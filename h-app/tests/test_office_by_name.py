"""office-add NAME (no url): an office reachable by name on a shared network.

The name lookup is a fake resolver (socket.getaddrinfo is patched, so the same
table drives the by-name check AND urllib's own connect); the remote is a real
local http(s) server speaking GET /whoami and the envelope POST.
"""

import json
import socket
from unittest.mock import patch

import pytest

from modules.office import cli as office_cli
from test_office_cli import _env
from test_office_interoffice import (  # noqa: F401 -- fixtures + helpers
    LEAD, OFFICES, OFFICE_TOKENS, TOKEN, Redis, exit_code, remote, run,
)
from test_office_tls import _issue, _make_ca

WHO = {"office": "peer-office", "participant": "alpha-office", "kinds": ["Message"], "api": 1}
STREAM_ID = "0123456789abcdef0123456789abcdef"


def serve(handler):
    """whoami on GET, an accepted envelope on POST."""
    def respond(h):
        if h.command == "GET":
            payload, code = WHO, 200
        else:
            payload, code = {"stream_id": STREAM_ID}, 202
        data = json.dumps(payload).encode()
        h.send_response(code)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)
    return respond


class Dns:
    """A fake resolver table; counts lookups of names (not IP literals)."""

    def __init__(self, monkeypatch, table):
        self.table, self.lookups = dict(table), []
        real = socket.getaddrinfo

        def fake(host, port, *args, **kwargs):
            if isinstance(host, str) and not host[:1].isdigit():
                self.lookups.append(host)
                if host not in self.table:
                    raise socket.gaierror(-2, "Name or service not known")
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (self.table[host], port or 0))]
            return real(host, port, *args, **kwargs)
        monkeypatch.setattr(socket, "getaddrinfo", fake)


def lead_redis():
    r = Redis(registry={"relay": "api"})
    r.values[LEAD] = "architect"
    return r


def add(r, monkeypatch, *argv, stdin=TOKEN):
    with patch("modules.office.cli.send"):
        return exit_code(r, ["office-add", *argv, "--token-stdin"], monkeypatch, stdin)


def add_ok(r, monkeypatch, *argv):
    with patch("modules.office.cli.send"):
        run(r, ["office-add", *argv, "--token-stdin"], monkeypatch, TOKEN)


def port_of(rem):
    return rem.url.rsplit(":", 1)[1]


# ------------------------------------------------------------------- success

def test_add_by_name_derives_the_url_probes_whoami_and_saves(monkeypatch, capsys, remote):
    _env(monkeypatch)
    rem = remote(serve(None))
    dns = Dns(monkeypatch, {"peer-b": "127.0.0.1"})
    r = lead_redis()
    with patch("modules.office.cli.send"):
        run(r, ["office-add", "peer-b", "--port", port_of(rem), "--token-stdin"], monkeypatch, TOKEN)
    record = json.loads(r.hashes[OFFICES]["peer-b"])
    assert record["url"] == f"http://peer-b:{port_of(rem)}"
    assert record["participant"] == "alpha-office"             # read from /whoami
    assert r.hashes[OFFICE_TOKENS]["peer-b"] == TOKEN
    assert rem.requests[0]["path"] == "/whoami" and rem.requests[0]["auth"] == f"Bearer {TOKEN}"
    captured = capsys.readouterr()
    assert "plaintext http" in captured.err                     # the existing warning
    assert "verified with the remote office (peer-office)" in captured.out
    assert TOKEN not in captured.out + captured.err
    assert dns.lookups.count("peer-b") >= 1


def test_default_port_is_the_container_api_port(monkeypatch, remote):
    _env(monkeypatch)
    Dns(monkeypatch, {"peer-b": "172.20.0.5"})
    r = lead_redis()
    add_ok(r, monkeypatch, "peer-b", "--participant", "alpha-office", "--no-verify")
    assert json.loads(r.hashes[OFFICES]["peer-b"])["url"] == "http://peer-b:8080"


def test_by_name_office_works_with_offices_check_and_send(monkeypatch, capsys, remote):
    _env(monkeypatch)
    rem = remote(serve(None))
    Dns(monkeypatch, {"peer-b": "127.0.0.1"})
    r = lead_redis()
    with patch("modules.office.cli.send"):
        run(r, ["office-add", "peer-b", "--port", port_of(rem), "--token-stdin"], monkeypatch, TOKEN)
    capsys.readouterr()
    run(r, ["offices", "--check"], monkeypatch)
    assert "link: ok" in capsys.readouterr().out
    run(r, ["send", "--office", "peer-b", "-a", "scout", "hello"], monkeypatch)
    assert f"sent to peer-b/scout: 5 bytes ({STREAM_ID})" in capsys.readouterr().out
    assert rem.requests[-1]["path"] == "/agents/scout/envelopes"
    assert rem.requests[-1]["body"] == {"text": "hello", "as": "alpha-office"}


def test_ca_cert_by_name_uses_https_and_verifies_the_name(tmp_path_factory, monkeypatch, capsys, remote):
    _env(monkeypatch)
    ca = _make_ca(tmp_path_factory.mktemp("byname") / "ca", "byname-ca")
    cert, key = _issue(ca, "peer", "DNS:peer-b")
    rem = remote(serve(None), tls=(cert, key))
    Dns(monkeypatch, {"peer-b": "127.0.0.1"})
    r = lead_redis()
    with patch("modules.office.cli.send"):
        run(r, ["office-add", "peer-b", "--port", port_of(rem), "--ca-cert", str(ca / "ca.pem"),
                "--token-stdin"], monkeypatch, TOKEN)
    record = json.loads(r.hashes[OFFICES]["peer-b"])
    assert record["url"] == f"https://peer-b:{port_of(rem)}" and record["ca_cert"] == str(ca / "ca.pem")
    assert "plaintext" not in capsys.readouterr().err


# ------------------------------------------------------------------- refusals

def test_a_mismatched_participant_refuses_and_saves_nothing(monkeypatch, capsys, remote):
    _env(monkeypatch)
    rem = remote(serve(None))
    Dns(monkeypatch, {"peer-b": "127.0.0.1"})
    r = lead_redis()
    assert add(r, monkeypatch, "peer-b", "--port", port_of(rem), "--participant", "someone-else") == 1
    err = capsys.readouterr().err
    assert "belongs to participant 'alpha-office'" in err and "nothing saved" in err
    assert not r.hashes[OFFICES] and not r.hashes[OFFICE_TOKENS]


def test_an_unresolvable_name_says_what_to_check_and_saves_nothing(monkeypatch, capsys, remote):
    _env(monkeypatch)
    rem = remote(serve(None))
    Dns(monkeypatch, {})
    r = lead_redis()
    assert add(r, monkeypatch, "ghost-office") == 1
    err = capsys.readouterr().err
    assert "cannot resolve office name 'ghost-office'" in err
    assert "same shared office network" in err and "SUMMARY.md" in err and "Nothing saved" in err
    assert not r.hashes[OFFICES] and not r.hashes[OFFICE_TOKENS] and rem.requests == []


@pytest.mark.parametrize("name", ["peer.example", "peer/evil", "peer:80", "user@host", "1234",
                                  "10.0.0.1", "Peer"])
def test_names_that_are_not_a_plain_dns_label_never_reach_the_resolver(name, monkeypatch, capsys):
    _env(monkeypatch)
    dns = Dns(monkeypatch, {name: "127.0.0.1"})
    r = lead_redis()
    assert add(r, monkeypatch, name) == 1
    assert dns.lookups == []
    assert not r.hashes[OFFICES]


def test_the_lookup_error_for_a_dotted_name_explains_itself(monkeypatch, capsys):
    _env(monkeypatch)
    with pytest.raises(office_cli.OfficeError, match="no dots, slashes, colons or '@'"):
        office_cli._url_by_name("a.b", 8080, https=False)


def test_a_name_that_resolves_to_a_public_address_is_refused(monkeypatch, capsys):
    _env(monkeypatch)
    Dns(monkeypatch, {"peer-b": "8.8.8.8"})
    r = lead_redis()
    assert add(r, monkeypatch, "peer-b") == 1
    err = capsys.readouterr().err
    assert "not a private address" in err and "8.8.8.8" in err
    assert not r.hashes[OFFICES]


def test_every_resolved_address_must_be_private(monkeypatch, capsys):
    _env(monkeypatch)
    real = socket.getaddrinfo
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("172.20.0.5", port)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
    ] if host == "peer-b" else real(host, port, *a, **k))
    r = lead_redis()
    assert add(r, monkeypatch, "peer-b") == 1
    assert "93.184.216.34" in capsys.readouterr().err


@pytest.mark.parametrize("port", ["0", "70000", "-1"])
def test_invalid_ports_are_refused(port, monkeypatch, capsys):
    _env(monkeypatch)
    Dns(monkeypatch, {"peer-b": "127.0.0.1"})
    r = lead_redis()
    assert add(r, monkeypatch, "peer-b", "--port", port) == 1
    assert not r.hashes[OFFICES]


def test_port_is_not_valid_with_an_explicit_url(monkeypatch, capsys):
    _env(monkeypatch)
    r = lead_redis()
    assert add(r, monkeypatch, "beta", "https://beta.example", "--port", "9") == 1
    assert "--port only applies when the url is omitted" in capsys.readouterr().err
    assert not r.hashes[OFFICES]


# ------------------------------------------------------- explicit url untouched

def test_an_explicit_url_never_triggers_a_name_lookup(monkeypatch, capsys):
    _env(monkeypatch)
    dns = Dns(monkeypatch, {})
    r = lead_redis()
    add_ok(r, monkeypatch, "beta", "https://beta.example", "--participant", "p", "--no-verify")
    assert json.loads(r.hashes[OFFICES]["beta"])["url"] == "https://beta.example"
    assert dns.lookups == []


def test_an_explicit_plaintext_url_still_needs_the_opt_in(monkeypatch, capsys):
    _env(monkeypatch)
    monkeypatch.delenv("H_MESH_ALLOW_PLAINTEXT", raising=False)
    r = lead_redis()
    assert add(r, monkeypatch, "beta", "http://10.0.0.5:8080", "--participant", "p") == 1
    assert "plaintext" in capsys.readouterr().err


# ----------------------------------------------------- after the fact: no resolve

def test_offices_check_and_send_explain_a_name_that_stopped_resolving(monkeypatch, capsys):
    _env(monkeypatch)
    Dns(monkeypatch, {})
    r = lead_redis()
    r.hashes[OFFICES]["peer-b"] = json.dumps({"url": "http://peer-b:8080", "participant": "alpha-office"})
    r.hashes[OFFICE_TOKENS]["peer-b"] = TOKEN
    assert exit_code(r, ["offices", "--check"], monkeypatch) == 1
    out = capsys.readouterr().out
    assert "link: unreachable" in out and "same shared office network" in out
    assert exit_code(r, ["send", "--office", "peer-b", "-a", "scout", "x"], monkeypatch) == 1
    err = capsys.readouterr().err
    assert "cannot reach the remote office at http://peer-b:8080" in err and "SUMMARY.md" in err
