import io
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import MagicMock

import redis


H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from core.config import state_dir, state_path
from core.registry import is_member, member_types, members, port_type
from core.service import Switch, _forward_port_custody, main, transmission
from core.windowlog import WindowLogTailer


LEGACY_BANNED_NAMES = (
    "f" + "lock",
    "f" + "lock_",
    "f" + "lockclient",
    "h" + "f" + "lock_",
    "h" + "f" + "lock_session",
)
# An allowance is deliberately attached to the violating line and names the
# exact legacy identifier it permits. Position changes cannot transfer it.
LEGACY_ALLOW_MARKER = "# legacy-name-" + "allow:"


def _identifier_spans(text: str, identifier: str) -> list[tuple[int, int]]:
    """Find exact identifier occurrences using Python continuation semantics."""
    spans = []
    start = 0
    while (found := text.find(identifier, start)) != -1:
        end = found + len(identifier)
        preceding_continues = found > 0 and ("a" + text[found - 1]).isidentifier()
        following_continues = end < len(text) and ("a" + text[end]).isidentifier()
        if not preceding_continues and not following_continues:
            spans.append((found, end))
        start = found + 1
    return spans


def _remove_exact_identifiers(text: str, identifiers: list[str]) -> str:
    # Resolve every span against the original text. Sequential substitution
    # must not create a new occurrence eligible for a later allowance.
    spans = sorted(
        {
            span
            for identifier in identifiers
            for span in _identifier_spans(text, identifier)
        }
    )
    pieces = []
    cursor = 0
    for start, end in spans:
        pieces.append(text[cursor:start])
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def legacy_name_violations(root: Path) -> tuple[int, list[str]]:
    checked = 0
    violations = []
    excluded_dirs = {".git", ".pytest_cache", "__pycache__"}

    for path in root.rglob("*"):
        if (
            not path.is_file()
            or any(part in excluded_dirs or part.endswith(".egg-info") for part in path.parts)
        ):
            continue
        relative = path.relative_to(root)
        checked += 1
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            code, marker, allowance_text = line.partition(LEGACY_ALLOW_MARKER)
            folded = code.casefold()
            if marker:
                allowance_values = [
                    value.strip()
                    for value in allowance_text.split(",")
                    if value.strip()
                ]
                allowed_literals = [value.casefold() for value in allowance_values]
                invalid = [
                    value
                    for value in allowance_values
                    if not value.isidentifier()
                    or value != value.upper()
                    or not _identifier_spans(code, value)
                ]
                if not allowed_literals or invalid:
                    violations.append(
                        f"{relative}:{line_number}: invalid legacy allowance"
                    )
                folded = _remove_exact_identifiers(
                    code,
                    [value for value in allowance_values if value not in invalid],
                ).casefold()
            matches = [name for name in LEGACY_BANNED_NAMES if name in folded]
            if matches:
                violations.append(f"{relative}:{line_number}: {', '.join(matches)}")

    return checked, violations


def test_legacy_name_allowance_follows_content_not_line_position(tmp_path: Path):
    session_test = tmp_path / "tests" / "test_session.py"
    session_test.parent.mkdir(parents=True)
    new_violation = "F" + "LOCK_NEW_REFERENCE"
    legitimate_legacy_name = "F" + "LOCK_ALLOW_PLAINTEXT"
    lines = ["# filler"] * 370
    lines.extend(
        (
            f"NEW_SETTING = {new_violation!r}",  # old exempt line 371
            "# inserted line one",
            "# inserted line two",
            f"with patch.dict(os.environ, {{{legitimate_legacy_name!r}: '1'}}): "
            f"{LEGACY_ALLOW_MARKER} {legitimate_legacy_name}",
        )
    )
    session_test.write_text("\n".join(lines) + "\n")

    _, violations = legacy_name_violations(tmp_path)

    assert any("tests/test_session.py:371:" in item for item in violations), (
        "a new legacy reference must fail even when it lands on an old exempt line; "
        f"observed {violations}"
    )
    assert not any("tests/test_session.py:374:" in item for item in violations), (
        "shifting the legitimate legacy reference must not change its verdict; "
        f"observed {violations}"
    )


def test_legacy_name_allowance_cannot_hide_an_unlisted_reference(tmp_path: Path):
    source = tmp_path / "module.py"
    allowed_name = "F" + "LOCK_ALLOW_PLAINTEXT"
    new_violation = "F" + "LOCK_NEW_REFERENCE"
    source.write_text(
        f"VALUES = ({allowed_name!r}, {new_violation!r})  "
        f"{LEGACY_ALLOW_MARKER} {allowed_name}\n"
    )

    _, violations = legacy_name_violations(tmp_path)

    banned_fragment = "f" + "lock"
    assert any(
        "module.py:1:" in item and banned_fragment in item for item in violations
    ), (
        "an explicit allowance must remove only the literal it names; "
        f"observed {violations}"
    )


def test_legacy_name_allowance_does_not_apply_inside_longer_identifier(tmp_path: Path):
    source = tmp_path / "module.py"
    allowed_name = "F" + "LOCK"
    longer_violation = allowed_name + "_NEW_REFERENCE"
    source.write_text(
        f'{allowed_name} = "{longer_violation}"  '
        f"{LEGACY_ALLOW_MARKER} {allowed_name}\n"
    )

    _, violations = legacy_name_violations(tmp_path)

    banned_match = "f" + "lock"
    assert violations == [f"module.py:1: {banned_match}, {banned_match}_"], (
        "an allowance for one identifier must not erase the same substring "
        f"inside an unlisted longer identifier; observed {violations}"
    )


def test_legacy_name_allowance_respects_unicode_identifier_boundaries(tmp_path: Path):
    source = tmp_path / "module.py"
    allowed_name = "F" + "LOCK"
    unicode_identifier = f"α{allowed_name}β"
    source.write_text(
        f"{allowed_name} = 1; {unicode_identifier} = 2  "
        f"{LEGACY_ALLOW_MARKER} {allowed_name}\n"
    )

    _, violations = legacy_name_violations(tmp_path)

    banned_match = "f" + "lock"
    assert violations == [f"module.py:1: {banned_match}"], (
        "an allowance must not erase its substring from a Unicode identifier; "
        f"observed {violations}"
    )


def test_legacy_name_allowance_must_name_a_literal_on_its_line(tmp_path: Path):
    source = tmp_path / "module.py"
    absent_name = "F" + "LOCK_ALLOW_PLAINTEXT"
    overly_broad_name = "F" + "LOCK"
    source.write_text(
        f"VALUE = 'safe'  {LEGACY_ALLOW_MARKER} {absent_name}\n"
        f"VALUE = {absent_name!r}  {LEGACY_ALLOW_MARKER} {overly_broad_name}\n"
        f"VALUE = {absent_name!r}  {LEGACY_ALLOW_MARKER} F, LOCK_ALLOW_PLAINTEXT\n"
    )

    _, violations = legacy_name_violations(tmp_path)

    banned_match = "f" + "lock"
    assert violations == [
        "module.py:1: invalid legacy allowance",
        "module.py:2: invalid legacy allowance",
        f"module.py:2: {banned_match}, {banned_match}_",
        "module.py:3: invalid legacy allowance",
        f"module.py:3: {banned_match}, {banned_match}_",
    ]


class RegistryRedis:
    def __init__(self):
        self.calls = []

    def hkeys(self, key):
        self.calls.append(("hkeys", key))
        return [b"alice"]

    def hexists(self, key, agent):
        self.calls.append(("hexists", key, agent))
        return 1

    def hget(self, key, agent):
        self.calls.append(("hget", key, agent))
        return b"tmux"

    def hgetall(self, key):
        self.calls.append(("hgetall", key))
        return {b"alice": b"tmux"}


class CoreAdaptationTests(unittest.TestCase):
    def test_tree_contains_no_old_project_names(self):
        checked, violations = legacy_name_violations(H_APP)

        self.assertGreater(checked, 100, "Expected to scan the complete h-app tree")
        self.assertEqual(violations, [], "Old project names found:\n" + "\n".join(violations))

    def test_registry_uses_registry_wire_resource(self):
        client = RegistryRedis()
        self.assertEqual(members(client, pod="mesh", tenant="office"), {"alice"})
        self.assertEqual(member_types(client, pod="mesh", tenant="office"), {"alice": "tmux"})
        self.assertTrue(is_member(client, pod="mesh", tenant="office", agent="alice"))
        self.assertEqual(port_type(client, pod="mesh", tenant="office", agent="alice"), "tmux")
        self.assertTrue(
            all(call[1] == "pod:mesh:tenant:office:registry" for call in client.calls)
        )

    def test_switch_invokes_injected_kick(self):
        calls = []
        switch = Switch(object(), pod="mesh", tenant="office", kick=lambda agent, port_type, env: calls.append((agent, port_type, env)))
        envelope = {"stream_id": "stream", "l2": {"source": "alice"}}
        with patch("core.service._log_observation") as log:
            switch._kick("bob", "tmux", envelope)
        self.assertEqual(calls, [("bob", "tmux", envelope)])
        self.assertEqual(log.call_args.args, ("kick_started",))

    def test_switch_records_callback_failure_without_raising(self):
        def fail(agent, port_type, envelope):
            raise RuntimeError("edge unavailable")

        switch = Switch(object(), pod="mesh", tenant="office", kick=fail)
        envelope = {"stream_id": "stream", "l2": {"source": "alice"}}
        with patch("core.service._log_observation") as log:
            switch._kick("bob", "tmux", envelope)
        self.assertEqual(log.call_args.args, ("kick_unknown",))
        self.assertIn("edge unavailable", log.call_args.kwargs["reason"])

    def test_switch_without_kick_records_terminal_skip(self):
        switch = Switch(object(), pod="mesh", tenant="office")
        envelope = {"stream_id": "stream", "l2": {"source": "alice"}}
        with patch("core.service._log_observation") as log:
            switch._kick("bob", "tmux", envelope)
        self.assertEqual(log.call_args.args, ("kick_skipped",))
        self.assertEqual(log.call_args.kwargs["destination"], "bob")

    def test_switch_run_retries_after_redis_connection_error(self):
        switch = Switch(object(), pod="mesh", tenant="office", poll_seconds=5)
        switch._reconcile_ingress = MagicMock()
        switch.step = MagicMock(
            side_effect=[redis.exceptions.ConnectionError("redis restarting"), KeyboardInterrupt]
        )
        with (
            patch("core.service.time.monotonic", return_value=0),
            patch("core.service.time.sleep") as sleep,
            patch("core.service._emit_observation") as emit,
            self.assertRaises(KeyboardInterrupt),
        ):
            switch.run()

        self.assertEqual(switch.step.call_count, 2)
        switch._reconcile_ingress.assert_called_once_with()
        sleep.assert_called_once_with(2.0)
        emit.assert_called_once_with(
            "error", {},
            reason="forwarding pass failed: ConnectionError: redis restarting",
        )

    def test_broadcast_without_resolved_type_records_terminal_skip(self):
        kick = MagicMock()
        switch = Switch(object(), pod="mesh", tenant="office", kick=kick)
        envelope = {"stream_id": "stream", "l2": {"source": "alice"}}
        with patch("core.service._log_observation") as log:
            switch._kick("bob", None, envelope)
        kick.assert_not_called()
        self.assertEqual(log.call_args.args, ("kick_skipped",))
        self.assertIn("no delivery attempt started", log.call_args.kwargs["reason"])

    def test_transmission_spawns_module_port_without_envelope_in_argv(self):
        envelope = {"stream_id": "secret", "payload": {"text": "not argv"}}
        with patch("core.service.subprocess.Popen") as popen:
            transmission("bob", "tmux", envelope)
        popen.assert_called_once()
        args, kwargs = popen.call_args
        self.assertEqual(args[0], [sys.executable, "-m", "modules.tmux.port", "bob"])
        self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
        self.assertEqual(kwargs["pass_fds"], (int(kwargs["env"]["H_MESH_LOG_FILE"].rsplit("/", 1)[1]),))
        self.assertEqual(kwargs["env"]["H_MESH_LOG_QUIET"], "1")
        self.assertTrue(kwargs["stdout"].closed)
        self.assertNotIn("secret", popen.call_args.args[0])

    def test_transmission_rejects_unresolved_port_type(self):
        with patch("core.service.subprocess.Popen") as popen:
            with self.assertRaisesRegex(ValueError, "resolved port_type"):
                transmission("bob", None, {})
        popen.assert_not_called()

    def test_port_custody_pipe_publishes_only_valid_json_objects(self):
        raw = b'{"event":"opened","stream_id":"ok"}\nnot-json\n[]\n'
        with (
            patch("core.service.os.fdopen", return_value=io.BytesIO(raw)),
            patch("core.service.publish") as publish,
            patch("core.service.log_record") as record,
        ):
            _forward_port_custody(123, agent="bob")

        publish.assert_called_once_with('{"event":"opened","stream_id":"ok"}')
        self.assertEqual(record.call_count, 2)
        self.assertTrue(all(call.args[:2] == ("switch", "port_custody_parse_error") for call in record.call_args_list))

    def test_main_installs_auto_reap_and_wires_production_transmission(self):
        events = []
        switch = MagicMock(pod="mesh", tenant="office")
        switch.run.side_effect = lambda **kwargs: events.append("run")
        with (
            patch.dict(
                os.environ,
                {"REDIS_URL": "redis://example", "POD": "mesh", "TENANT": "office"},
                clear=True,
            ),
            patch("core.service.signal.signal", side_effect=lambda *args: events.append("signal")) as set_signal,
            patch("core.service.redis.Redis.from_url", return_value=object()),
            patch("core.service.Switch", return_value=switch) as switch_class,
            patch("core.service.WindowLogTailer", return_value=object()),
            patch("core.service.RetentionTrimmer", return_value=object()),
        ):
            main()
        set_signal.assert_called_once_with(signal.SIGCHLD, signal.SIG_IGN)
        self.assertIs(switch_class.call_args.kwargs["kick"], transmission)
        self.assertEqual(events, ["signal", "run"])

    def test_state_paths_follow_configured_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"H_MESH_STATE_DIR": directory}):
                self.assertEqual(state_dir(), Path(directory))
                self.assertEqual(state_path("tasks.jsonl"), Path(directory) / "tasks.jsonl")
                tailer = WindowLogTailer(object(), pod="mesh", tenant="office")
                self.assertEqual(tailer.path, Path(directory) / "window.log.jsonl")

    def test_window_tailer_reports_malformed_custody_instead_of_republishing_it(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "window.log.jsonl"
            path.write_text('not-json\n[]\n', encoding="utf-8")
            redis_client = MagicMock()
            redis_client.get.return_value = 0
            tailer = WindowLogTailer(
                redis_client, pod="mesh", tenant="office", path=path,
            )
            with (
                patch("core.windowlog.publish") as publish_record,
                patch("core.windowlog.log_record") as diagnostic,
            ):
                tailer.poll()

            publish_record.assert_not_called()
            self.assertEqual(diagnostic.call_count, 2)
            self.assertTrue(all(
                call.args[:2] == ("switch", "window_log_parse_error")
                for call in diagnostic.call_args_list
            ))
            redis_client.set.assert_called_with(tailer.offset_key, path.stat().st_size)


if __name__ == "__main__":
    unittest.main()
