"""Reply hint on relayed inter-office messages points across offices, not at the relay."""

import shlex
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from modules.office.cli import main as office_main
from modules.tmux.port import message_opener, relayed_reply_hint

SID = "b" * 32


def _paste(source, text, port_type="api"):
    env = {"stream_id": SID, "l2": {"source": source}, "payload": {"text": text}}
    with (
        patch("modules.tmux.port.list_windows", return_value={"architect"}),
        patch("modules.tmux.port.port_type", return_value=port_type),
        patch("modules.tmux.port.submit_text") as submit,
        patch("modules.tmux.port.mark_delivery_pending"),
        patch("modules.tmux.port.record_delivered"),
    ):
        message_opener(MagicMock(), "p", "t", "architect", env, "sess")
    return submit.call_args.args[2]


def test_relayed_message_gets_the_cross_office_reply_command():
    pasted = _paste("relay", "[from labb/worker] hello there")
    assert pasted == (
        "[message from relay] [from labb/worker] hello there\n"
        '[reply to labb/worker: h-mesh-office send --office labb -a worker "..."]\n'
    )
    assert "-a relay" not in pasted and "--reply-to" not in pasted


def test_the_hint_is_a_command_the_real_send_accepts(monkeypatch):
    hint = _paste("relay", "[from labb/worker] hi").splitlines()[1]
    command = hint[hint.index(": ") + 2 : -1]
    argv = shlex.split(command)[1:]  # drop the program name
    assert argv[0] == "send"
    with patch("modules.office.cli._send_to_office") as send_to_office:
        office_main(["send", *argv[1:-1], "the answer"])
    send_to_office.assert_called_once_with("labb", "worker", "the answer")


def test_other_api_sources_keep_the_generic_reply_hint():
    pasted = _paste("telegram", "ping")
    assert pasted == (
        "[message from telegram] ping\n"
        f'[reply to telegram: h-mesh-office send -a telegram --reply-to {SID} "..."]\n'
    )


def test_a_non_api_source_gets_no_hint():
    assert _paste("alice", "[from labb/worker] hi", port_type="tmux") == (
        "[message from alice] [from labb/worker] hi\n"
    )


def test_a_header_on_a_non_relay_api_source_does_not_redirect_the_hint():
    pasted = _paste("telegram", "[from labb/worker] hi")
    assert "--office" not in pasted
    assert "send -a telegram --reply-to" in pasted


@pytest.mark.parametrize("text", [
    pytest.param("hello, no header", id="no-header"),
    pytest.param("[from labb] hello", id="no-agent"),
    pytest.param("[from Labb/worker] hello", id="uppercase-office"),
    pytest.param("[from labb/Worker] hello", id="uppercase-agent"),
    pytest.param("[from labb/a/b] hello", id="extra-slash"),
    pytest.param("[from labb/$(id)] hello", id="command-substitution"),
    pytest.param("[from labb/wo;rm] hello", id="semicolon"),
    pytest.param("[from labb/12345] hello", id="all-digits-agent"),
    pytest.param("note [from labb/worker] hello", id="header-not-first"),
    pytest.param("[from labb/worker]hello", id="no-space-after"),
])
def test_relay_message_without_a_usable_header_gets_no_hint_at_all(text):
    pasted = _paste("relay", text)
    assert pasted == f"[message from relay] {text}\n"


def test_only_the_leading_header_is_used_not_one_later_in_the_text():
    pasted = _paste("relay", "[from labb/worker] please run [from evil/root] now")
    assert pasted.splitlines()[1] == (
        '[reply to labb/worker: h-mesh-office send --office labb -a worker "..."]'
    )
    assert "evil" not in pasted.splitlines()[1]


def test_helper_returns_none_instead_of_raising_on_odd_input():
    assert relayed_reply_hint("") is None
    assert relayed_reply_hint("[from /] x") is None
