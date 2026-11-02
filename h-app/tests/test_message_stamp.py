"""The send time and short id at the front of a pasted message line (#20)."""

import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from modules.tmux.port import message_opener, message_stamp

SID = "4f3a9c21" + "0" * 24
TS = "2026-11-02T03:42:11.123Z"


def _env(**over):
    env = {"stream_id": SID, "ts": TS, "l2": {"source": "alice"}, "payload": {"text": "hello"}}
    env.update(over)
    return env


def test_stamp_is_utc_seconds_and_a_short_id():
    assert message_stamp(_env()) == "2026-11-02T03:42:11Z #4f3a9c21 "


def test_stamp_accepts_a_ts_without_fraction():
    assert message_stamp(_env(ts="2026-11-02T03:42:11Z")) == "2026-11-02T03:42:11Z #4f3a9c21 "


@pytest.mark.parametrize("ts", [
    pytest.param("2026-11-02T03:42:11+02:00", id="offset"),
    pytest.param("yesterday", id="words"),
    pytest.param("2026-11-02T03:42:11Z\nrm -rf", id="newline"),
    pytest.param("2026-11-02T03:42:11.123Z\x1b[2J", id="escape"),
    pytest.param("", id="empty"),
    pytest.param(None, id="none"),
    pytest.param(20261102, id="int"),
])
def test_unusable_ts_is_left_out_and_the_id_stays(ts):
    assert message_stamp(_env(ts=ts)) == "#4f3a9c21 "


@pytest.mark.parametrize("stream_id", [
    pytest.param("4F3A9C21" + "0" * 24, id="uppercase"),
    pytest.param("4f3a9c21", id="short"),
    pytest.param(SID + "\n", id="newline"),
    pytest.param(None, id="none"),
])
def test_unusable_id_is_left_out_and_the_time_stays(stream_id):
    assert message_stamp(_env(stream_id=stream_id)) == "2026-11-02T03:42:11Z "


def test_nothing_usable_gives_the_old_plain_header():
    assert message_stamp({}) == ""
    assert message_stamp(_env(ts="x", stream_id="y")) == ""


def test_the_pasted_line_is_stamp_then_the_unchanged_header():
    r = MagicMock()
    with (
        patch("modules.tmux.port.list_windows", return_value={"bob"}),
        patch("modules.tmux.port.port_type", return_value="tmux"),
        patch("modules.tmux.port.submit_text") as submit,
        patch("modules.tmux.port.mark_delivery_pending"),
        patch("modules.tmux.port.record_delivered"),
    ):
        message_opener(r, "p", "t", "bob", _env(), "sess")
    pasted = submit.call_args.args[2]
    assert pasted == "2026-11-02T03:42:11Z #4f3a9c21 [message from alice] hello\n"
    assert re.fullmatch(r"\S+ #[0-9a-f]{8} \[message from alice\] hello\n", pasted)
