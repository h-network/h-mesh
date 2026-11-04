"""Inter-office TLS: private-CA trust and correct TLS failure exit codes.

Real throwaway certificates (the openssl CLI, generated once per module) and a
real local https server, so verification, host-name checking, expiry and the
"nothing was sent" classification are the actual ssl/urllib behaviour.

  good      issued by CA1, valid for 127.0.0.1 / localhost
  wronghost issued by CA1, valid only for other.example
  expired   issued by CA1, validity ended in January 2020
  untrusted issued by CA2, valid for 127.0.0.1 (CA1 is what the client trusts)
"""

import json
import ssl
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from lib import tls_context
from lib.tls_context import TlsConfigError, build_context, check_ca_file, resolve_ca_file
from modules.office import cli as office_cli
from test_office_cli import _env
from test_office_interoffice import (  # noqa: F401 -- fixtures + helpers
    LEAD, OFFICES, OFFICE_TOKENS, TOKEN, Redis, book, exit_code, remote, reply, run,
)

WHO = {"office": "beta-office", "participant": "alpha-office", "kinds": ["Message"], "api": 1}
EC = ["-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1"]
STREAM_ID = "0123456789abcdef0123456789abcdef"
OK_202 = {"stream_id": STREAM_ID}


def _openssl(cwd, *args):
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


def _make_ca(directory: Path, cn: str):
    directory.mkdir()
    _openssl(directory, "req", "-x509", *EC, "-nodes", "-keyout", "ca.key", "-out", "ca.pem",
             "-days", "30", "-subj", f"/CN={cn}",
             "-addext", "basicConstraints=critical,CA:TRUE",
             "-addext", "keyUsage=critical,keyCertSign,cRLSign",
             "-addext", "subjectKeyIdentifier=hash")
    (directory / "ca.cnf").write_text(
        "[ca]\ndefault_ca = d\n[d]\ndatabase = index.txt\nnew_certs_dir = .\nserial = serial.txt\n"
        "default_md = sha256\npolicy = pol\ncertificate = ca.pem\nprivate_key = ca.key\n"
        "unique_subject = no\n[pol]\ncommonName = supplied\n")
    (directory / "index.txt").write_text("")
    (directory / "serial.txt").write_text("01\n")
    _openssl(directory, "req", "-new", *EC, "-nodes", "-keyout", "s.key", "-out", "s.csr",
             "-subj", "/CN=server")
    return directory


def _issue(ca: Path, name: str, san: str, expired=False):
    (ca / f"{name}.ext").write_text(
        f"subjectAltName={san}\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n")
    if expired:
        _openssl(ca, "ca", "-config", "ca.cnf", "-batch", "-notext",
                 "-startdate", "20200101000000Z", "-enddate", "20200102000000Z",
                 "-extfile", f"{name}.ext", "-in", "s.csr", "-out", f"{name}.pem")
    else:
        _openssl(ca, "x509", "-req", "-in", "s.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
                 "-CAcreateserial", "-days", "30", "-extfile", f"{name}.ext",
                 "-out", f"{name}.pem")
    return str(ca / f"{name}.pem"), str(ca / "s.key")


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    base = tmp_path_factory.mktemp("pki")
    ca1, ca2 = _make_ca(base / "ca1", "test-ca-1"), _make_ca(base / "ca2", "test-ca-2")
    local = "IP:127.0.0.1,DNS:localhost"
    return {
        "ca1": str(ca1 / "ca.pem"), "ca2": str(ca2 / "ca.pem"),
        "good": _issue(ca1, "good", local),
        "wronghost": _issue(ca1, "wronghost", "DNS:other.example"),
        "expired": _issue(ca1, "expired", local, expired=True),
        "untrusted": _issue(ca2, "untrusted", local),
    }


@pytest.fixture(autouse=True)
def _clean_tls_env(monkeypatch):
    for name in ("H_MESH_CA_CERT", "SSL_CERT_FILE", "SSL_CERT_DIR", "H_MESH_ALLOW_PLAINTEXT"):
        monkeypatch.delenv(name, raising=False)


def lead_redis():
    r = Redis(registry={"relay": "api"})
    r.values[LEAD] = "architect"
    return r


def book_tls(r, url, ca_cert=None, name="beta"):
    book(r, url)
    if ca_cert:
        record = json.loads(r.hashes[OFFICES][name])
        record["ca_cert"] = ca_cert
        r.hashes[OFFICES][name] = json.dumps(record)


def send_argv(text="hi"):
    return ["send", "--office", "beta", "-a", "scout", text]


def sent_nothing(r, rem):
    assert rem.requests == []
    assert not any("xsent" in k for k in r.values)


# ------------------------------------------------------------ lib.tls_context

def test_check_ca_file_accepts_a_real_ca_and_refuses_the_rest(pki, tmp_path):
    assert check_ca_file(pki["ca1"]) == pki["ca1"]
    with pytest.raises(TlsConfigError, match="does not exist"):
        check_ca_file(str(tmp_path / "nope.pem"))
    with pytest.raises(TlsConfigError, match="directory"):
        check_ca_file(str(tmp_path))
    plain = tmp_path / "plain.pem"
    plain.write_text("hello")
    with pytest.raises(TlsConfigError, match="not PEM"):
        check_ca_file(str(plain))
    bogus = tmp_path / "bogus.pem"
    bogus.write_text("-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n")
    with pytest.raises(TlsConfigError, match="no usable certificate"):
        check_ca_file(str(bogus))
    with pytest.raises(TlsConfigError, match="empty"):
        check_ca_file("  ")


def test_build_context_always_verifies_and_a_ca_file_is_the_only_trust(pki):
    default = build_context()
    private = build_context(pki["ca1"])
    for context in (default, private):
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True
    assert private.cert_store_stats()["x509_ca"] == 1          # CA1 only, not the system store


def test_resolve_ca_file_per_office_wins_over_the_environment():
    assert resolve_ca_file("/a.pem", {"H_MESH_CA_CERT": "/b.pem"}) == "/a.pem"
    assert resolve_ca_file(None, {"H_MESH_CA_CERT": "/b.pem"}) == "/b.pem"
    assert resolve_ca_file("", {"H_MESH_CA_CERT": "  "}) is None
    assert resolve_ca_file(None, {}) is None


def test_verification_is_never_switched_off_anywhere():
    for module in (tls_context, office_cli):
        source = Path(module.__file__).read_text()
        assert "CERT_NONE" not in source and "check_hostname = False" not in source
        assert "_create_unverified_context" not in source


# ---------------------------------------------------------- send --office TLS

def test_send_trusts_a_private_ca_from_the_address_book(pki, monkeypatch, capsys, remote):
    _env(monkeypatch)
    rem = remote(reply(202, OK_202), tls=pki["good"])
    r = Redis()
    book_tls(r, rem.url, pki["ca1"])
    run(r, send_argv(), monkeypatch)
    assert "sent to beta/scout" in capsys.readouterr().out
    assert rem.requests[0]["auth"] == f"Bearer {TOKEN}"


def test_send_trusts_a_private_ca_from_the_environment(pki, monkeypatch, capsys, remote):
    _env(monkeypatch)
    monkeypatch.setenv("H_MESH_CA_CERT", pki["ca1"])
    rem = remote(reply(202, OK_202), tls=pki["good"])
    r = Redis()
    book_tls(r, rem.url)
    run(r, send_argv(), monkeypatch)
    assert "sent to beta/scout" in capsys.readouterr().out


def test_per_office_ca_wins_over_the_environment_in_both_directions(pki, monkeypatch, capsys, remote):
    _env(monkeypatch)
    rem = remote(reply(202, OK_202), tls=pki["good"])
    r = Redis()
    monkeypatch.setenv("H_MESH_CA_CERT", pki["ca2"])           # wrong, but overridden
    book_tls(r, rem.url, pki["ca1"])
    run(r, send_argv(), monkeypatch)
    assert "sent to beta/scout" in capsys.readouterr().out
    monkeypatch.setenv("H_MESH_CA_CERT", pki["ca1"])           # right, but overridden
    book_tls(r, rem.url, pki["ca2"])
    assert exit_code(r, send_argv(), monkeypatch) == 1
    assert "TLS verification failed" in capsys.readouterr().err


@pytest.mark.parametrize("cert, ca, fragment, fix", [
    ("good", None, "unable to get local issuer certificate", "--ca-cert PATH"),
    ("untrusted", "ca1", "unable to get local issuer certificate", "--ca-cert PATH"),
    ("wronghost", "ca1", "mismatch", "host name the certificate was issued for"),
    ("expired", "ca1", "expired", "renew its certificate"),
], ids=["no_ca_configured", "untrusted_issuer", "wrong_host", "expired"])
def test_send_tls_verification_failure_is_exit_1_and_provably_not_sent(
        pki, cert, ca, fragment, fix, monkeypatch, capsys, remote):
    _env(monkeypatch)
    rem = remote(reply(202, OK_202), tls=pki[cert])
    r = Redis()
    book_tls(r, rem.url, pki[ca] if ca else None)
    assert exit_code(r, send_argv(), monkeypatch) == 1          # not 3: nothing was sent
    err = capsys.readouterr().err
    assert "TLS verification failed" in err and fragment in err and fix in err
    assert "the message was NOT sent" in err and "may have been accepted" not in err
    assert TOKEN not in err
    sent_nothing(r, rem)


def test_send_plain_http_server_behind_an_https_url_is_not_sent(monkeypatch, capsys, remote):
    _env(monkeypatch)
    rem = remote(reply(202, OK_202))
    r = Redis()
    book_tls(r, rem.url.replace("http://", "https://"))
    assert exit_code(r, send_argv(), monkeypatch) == 1
    err = capsys.readouterr().err
    assert "TLS handshake" in err and "NOT sent" in err and "plain http" in err
    sent_nothing(r, rem)


def test_send_unknown_outcome_after_a_good_handshake_is_still_exit_3(pki, monkeypatch, capsys, remote):
    _env(monkeypatch)
    rem = remote(lambda handler: None, tls=pki["good"])        # accepts, then hangs up
    r = Redis()
    book_tls(r, rem.url, pki["ca1"])
    assert exit_code(r, send_argv(), monkeypatch) == 3
    assert "may have been accepted" in capsys.readouterr().err


def test_send_unusable_ca_file_is_refused_before_any_io(pki, tmp_path, monkeypatch, capsys, remote):
    _env(monkeypatch)
    rem = remote(reply(202, OK_202), tls=pki["good"])
    r = Redis()
    book_tls(r, rem.url, str(tmp_path / "gone.pem"))
    assert exit_code(r, send_argv(), monkeypatch) == 1
    assert "does not exist" in capsys.readouterr().err
    sent_nothing(r, rem)


# ------------------------------------------------------------------ office-add

def add_argv(url, *extra):
    return ["office-add", "beta", url, "--participant", "alpha-office", "--token-stdin", *extra]


def test_add_with_ca_cert_verifies_stores_the_path_and_lists_it(pki, monkeypatch, capsys, remote):
    _env(monkeypatch)
    rem = remote(reply(200, WHO), tls=pki["good"])
    r = lead_redis()
    with patch("modules.office.cli.send"):
        run(r, add_argv(rem.url, "--ca-cert", pki["ca1"]), monkeypatch, TOKEN)
    assert json.loads(r.hashes[OFFICES]["beta"])["ca_cert"] == pki["ca1"]
    assert "verified with the remote office" in capsys.readouterr().out
    run(r, ["offices"], monkeypatch)
    line = capsys.readouterr().out
    assert f"\tCA {pki['ca1']}" in line and TOKEN not in line
    run(r, ["offices", "--check"], monkeypatch)                 # uses the stored CA
    assert "link: ok" in capsys.readouterr().out


def test_add_uses_the_environment_ca_but_does_not_store_it(pki, monkeypatch, capsys, remote):
    _env(monkeypatch)
    monkeypatch.setenv("H_MESH_CA_CERT", pki["ca1"])
    rem = remote(reply(200, WHO), tls=pki["good"])
    r = lead_redis()
    with patch("modules.office.cli.send"):
        run(r, add_argv(rem.url), monkeypatch, TOKEN)
    assert "ca_cert" not in json.loads(r.hashes[OFFICES]["beta"])


@pytest.mark.parametrize("cert, ca, fragment", [
    ("good", None, "unable to get local issuer certificate"),
    ("untrusted", "ca1", "unable to get local issuer certificate"),
    ("wronghost", "ca1", "mismatch"),
    ("expired", "ca1", "expired"),
], ids=["no_ca_configured", "untrusted_issuer", "wrong_host", "expired"])
def test_add_tls_failure_says_what_to_do_and_saves_nothing(
        pki, cert, ca, fragment, monkeypatch, capsys, remote):
    _env(monkeypatch)
    rem = remote(reply(200, WHO), tls=pki[cert])
    r = lead_redis()
    extra = ["--ca-cert", pki[ca]] if ca else []
    with patch("modules.office.cli.send"):
        assert exit_code(r, add_argv(rem.url, *extra), monkeypatch, TOKEN) == 1
    err = capsys.readouterr().err
    assert "nothing saved" in err and "TLS verification failed" in err and fragment in err
    assert TOKEN not in err
    assert not r.hashes[OFFICES] and not r.hashes[OFFICE_TOKENS]
    assert rem.requests == []


@pytest.mark.parametrize("make, fragment", [
    (lambda tmp: str(tmp / "missing.pem"), "does not exist"),
    (lambda tmp: str(tmp), "directory"),
    (lambda tmp: (tmp / "x.txt").write_text("nope") and str(tmp / "x.txt"), "not PEM"),
], ids=["missing", "directory", "not_pem"])
def test_add_refuses_an_unusable_ca_file_before_the_network(make, fragment, tmp_path, monkeypatch, capsys, remote):
    _env(monkeypatch)
    rem = remote(reply(200, WHO))
    r = lead_redis()
    ca = make(tmp_path)
    assert exit_code(r, add_argv(rem.url.replace("http:", "https:"), "--ca-cert", ca),
                     monkeypatch, TOKEN) == 1
    assert fragment in capsys.readouterr().err
    assert not r.hashes[OFFICES] and rem.requests == []


def test_add_ca_cert_makes_no_sense_for_http(pki, monkeypatch, capsys, remote):
    _env(monkeypatch)
    rem = remote(reply(200, WHO))
    r = lead_redis()
    assert exit_code(r, add_argv(rem.url, "--ca-cert", pki["ca1"]), monkeypatch, TOKEN) == 1
    assert "only applies to an https:// url" in capsys.readouterr().err
    assert not r.hashes[OFFICES]


def test_add_no_verify_still_validates_and_stores_the_ca(pki, monkeypatch, capsys):
    _env(monkeypatch)
    r = lead_redis()
    with patch("modules.office.cli.send"):
        run(r, add_argv("https://beta.example", "--no-verify", "--ca-cert", pki["ca1"]),
            monkeypatch, TOKEN)
    assert json.loads(r.hashes[OFFICES]["beta"])["ca_cert"] == pki["ca1"]
    assert exit_code(r, add_argv("https://beta.example", "--no-verify", "--ca-cert", "/nope.pem"),
                     monkeypatch, TOKEN) == 1


def test_offices_check_reports_a_tls_problem_without_stopping(pki, monkeypatch, capsys, remote):
    _env(monkeypatch)
    rem = remote(reply(200, WHO), tls=pki["expired"])
    r = Redis()
    book_tls(r, rem.url, pki["ca1"])
    assert exit_code(r, ["offices", "--check"], monkeypatch) == 1
    out = capsys.readouterr()
    assert "link: TLS problem -- TLS verification failed: certificate has expired" in out.out
    assert "1 office link(s) not ok" in out.err


def test_a_malformed_ca_cert_field_makes_the_entry_unreadable(monkeypatch, capsys):
    _env(monkeypatch)
    r = Redis()
    book(r, "https://beta.example")
    record = json.loads(r.hashes[OFFICES]["beta"])
    record["ca_cert"] = 5
    r.hashes[OFFICES]["beta"] = json.dumps(record)
    assert exit_code(r, send_argv(), monkeypatch) == 1
    assert "unreadable" in capsys.readouterr().err
