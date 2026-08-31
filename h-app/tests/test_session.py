import asyncio
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from modules.session.app import (
    SessionSettings,
    _authorized,
    _connection_log,
    create_app,
)
from modules.session.control import ControlModeClient, ControlModeError, Subscriber
from modules.tmux import AmbientTmuxError


class FakeHeaders(dict):
    def get(self, key, default=None):
        return super().get(key.lower(), default)


class FakeWebSocket:
    def __init__(self, authorization="", query_params=None, query_string=b""):
        self.headers = FakeHeaders(authorization=authorization)
        self.query_params = query_params or {}
        self.scope = {"query_string": query_string}


class FakeController:
    def __init__(self):
        self.sent = []
        self.subscribers = []

    async def start(self):
        pass

    async def stop(self):
        pass

    async def update_subscription(self, subscriber, agents, *, refresh=False):
        self.subscribers.append((subscriber, set(agents), refresh))
        subscriber.agents = set(agents)
        for agent in sorted(agents):
            subscriber.queue.put_nowait({"agent": agent, "data": f"snapshot:{agent}"})
        return []

    def unsubscribe(self, subscriber):
        subscriber.agents.clear()

    async def send_keys(self, agent, data):
        self.sent.append((agent, data))


def _run_websocket_exchange(messages, *, token="secret", query_string=b"", headers=None):
    async def scenario():
        controller = FakeController()
        app = create_app(
            settings=SessionSettings(
                tenant="hq", api_token="secret", session_name="hq"
            ),
            controller=controller,
        )
        incoming = asyncio.Queue()
        incoming.put_nowait({"type": "websocket.connect"})
        for message in messages:
            if isinstance(message, str):
                incoming.put_nowait({"type": "websocket.receive", "text": message})
            else:
                incoming.put_nowait(
                    {"type": "websocket.receive", "text": json.dumps(message)}
                )
        sent = []

        async def receive():
            if incoming.empty():
                await asyncio.sleep(0.01)
                return {"type": "websocket.disconnect", "code": 1000}
            return await incoming.get()

        async def send(message):
            sent.append(message)

        default_headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
        hdrs = headers if headers is not None else default_headers

        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "scheme": "ws",
            "path": "/session",
            "raw_path": b"/session",
            "query_string": query_string,
            "headers": hdrs,
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 8081),
            "subprotocols": [],
            "root_path": "",
        }
        await app(scope, receive, send)
        payloads = [
            json.loads(message["text"])
            for message in sent
            if message["type"] == "websocket.send" and "text" in message
        ]
        return sent, payloads, controller

    return asyncio.run(scenario())


class SessionTests(unittest.TestCase):
    def test_bearer_auth_is_exact_and_constant_scheme_insensitive(self):
        self.assertTrue(_authorized(FakeWebSocket("Bearer secret"), "secret"))
        self.assertTrue(_authorized(FakeWebSocket("bearer secret"), "secret"))
        self.assertFalse(_authorized(FakeWebSocket("Bearer wrong"), "secret"))
        self.assertFalse(_authorized(FakeWebSocket(""), "secret"))

    def test_session_close_record_names_session_writer(self):
        output = io.StringIO()
        with patch("sys.stdout", output):
            _connection_log("c-1", "browser", {"architect"}, "read", "earlier")
        record = json.loads(output.getvalue().strip())
        self.assertEqual(record["writer"], "session")
        self.assertEqual(record["module"], "session")
        self.assertEqual(record["event"], "closed")
        self.assertEqual(record["connection_id"], "c-1")
        self.assertEqual(record["agents"], ["architect"])

    def test_snapshot_precedes_output_arriving_during_capture(self):
        async def scenario():
            controller = ControlModeClient("hq")
            controller.pane_to_agent = {"%1": "alice"}
            controller.agent_to_pane = {"alice": "%1"}

            async def command(*args):
                self.assertIn(args[0], ("capture-pane", "display-message"))
                if args[0] == "display-message":
                    return ["0 0"]
                controller._publish("%1", b"live")
                return ["snapshot"]

            controller.command = command
            subscriber = Subscriber()
            self.assertEqual(await controller.update_subscription(subscriber, {"alice"}), [])
            return [subscriber.queue.get_nowait(), subscriber.queue.get_nowait()]

        results = asyncio.run(scenario())
        self.assertEqual(
            results,
            [
                {"agent": "alice", "data": "\x1b[2J\x1b[Hsnapshot\x1b[1;1H"},
                {"agent": "alice", "data": "live"},
            ],
        )

    def test_refresh_resnapshots_an_already_subscribed_agent(self):
        async def scenario():
            controller = ControlModeClient("hq")
            controller.pane_to_agent = {"%1": "alice"}
            controller.agent_to_pane = {"alice": "%1"}

            captures = 0

            async def command(*args):
                nonlocal captures
                if args[0] == "display-message":
                    return ["0 0"]
                captures += 1
                return [f"frame-{captures}"]

            controller.command = command
            subscriber = Subscriber()
            await controller.update_subscription(subscriber, {"alice"})
            first = subscriber.queue.get_nowait()

            # Same set, no refresh: nothing new
            await controller.update_subscription(subscriber, {"alice"})
            self.assertTrue(subscriber.queue.empty())

            # Same set, refresh=True: fresh snapshot
            await controller.update_subscription(subscriber, {"alice"}, refresh=True)
            second = subscriber.queue.get_nowait()
            return first, second, captures

        first, second, captures = asyncio.run(scenario())
        self.assertEqual(first["data"], "\x1b[2J\x1b[Hframe-1\x1b[1;1H")
        self.assertEqual(second["data"], "\x1b[2J\x1b[Hframe-2\x1b[1;1H")
        self.assertEqual(captures, 2)

    def test_keystrokes_are_hex_encoded_for_control_protocol(self):
        async def scenario():
            controller = ControlModeClient("hq")
            controller.agent_to_pane = {"alice": "%1"}
            calls = []

            async def command(*args):
                calls.append(args)
                return []

            controller.command = command
            await controller.send_keys("alice", "A\n\x03")
            return calls

        calls = asyncio.run(scenario())
        self.assertEqual(
            calls,
            [("send-keys", "-t", "%1", "-H", "41", "0a", "03")],
        )

    def test_read_only_subscription_refuses_input_server_side(self):
        sent, payloads, controller = _run_websocket_exchange(
            [
                {"subscribe": ["alice"], "mode": "read-only"},
                {"agent": "alice", "data": "touch /tmp/no"},
            ]
        )
        self.assertTrue(any(message["type"] == "websocket.accept" for message in sent))
        self.assertIn({"error": "read-only"}, payloads)
        self.assertEqual(controller.sent, [])

    def test_read_write_subscription_sends_input(self):
        _, _, controller = _run_websocket_exchange(
            [
                {"subscribe": ["alice"], "mode": "read-write"},
                {"agent": "alice", "data": "echo yes\n"},
            ]
        )
        self.assertEqual(controller.sent, [("alice", "echo yes\n")])

    def test_subscribe_refresh_field_reaches_the_controller(self):
        _, _, controller = _run_websocket_exchange(
            [
                {"subscribe": ["alice"], "mode": "read-only"},
                {"subscribe": ["alice"], "mode": "read-only", "refresh": True},
            ]
        )
        self.assertEqual([refresh for _, _, refresh in controller.subscribers], [False, True])

    def test_bad_token_is_closed_before_accept(self):
        sent, _, controller = _run_websocket_exchange([], token="wrong")
        self.assertEqual(sent[0]["type"], "websocket.close")
        self.assertEqual(sent[0]["code"], 4401)
        self.assertFalse(any(message["type"] == "websocket.accept" for message in sent))
        self.assertEqual(controller.sent, [])

    def test_query_token_authentication(self):
        sent, _, controller = _run_websocket_exchange(
            [], token=None, query_string=b"token=secret", headers=[]
        )
        self.assertTrue(any(msg["type"] == "websocket.accept" for msg in sent))

    def test_unauthorized_query_token_sends_close_4401(self):
        sent, _, _ = _run_websocket_exchange(
            [], token=None, query_string=b"token=wrong_token", headers=[]
        )
        close_msg = next((msg for msg in sent if msg["type"] == "websocket.close"), None)
        self.assertIsNotNone(close_msg)
        self.assertEqual(close_msg["code"], 4401)
        self.assertEqual(close_msg.get("reason"), "unauthorized")

    def test_websocket_malformed_json_returns_error_frame(self):
        _, payloads, _ = _run_websocket_exchange(
            ["{invalid json format"]
        )
        self.assertIn({"error": "invalid json"}, payloads)

    def test_websocket_non_dict_json_returns_error_frame(self):
        _, payloads, _ = _run_websocket_exchange(
            [[1, 2, 3]]
        )
        self.assertIn({"error": "message must be an object"}, payloads)

    def test_session_has_one_websocket_route(self):
        app = create_app(
            settings=SessionSettings(tenant="hq", api_token="secret", session_name="hq"),
            controller=FakeController(),
        )
        routes = [route.path for route in app.routes if getattr(route, "path", None) == "/session"]
        self.assertEqual(routes, ["/session"])

    def test_start_requires_isolated_tmux(self):
        with patch.dict(os.environ, {}, clear=True):
            controller = ControlModeClient("hq", socket=None)
            with self.assertRaises(AmbientTmuxError):
                asyncio.run(controller.start())

    def test_refresh_panes_uses_session_scope_flag(self):
        async def scenario():
            controller = ControlModeClient("hq")
            calls = []

            async def command(*args):
                calls.append(args)
                return ["%0\talice", "%1\tbob"]

            controller.command = command
            await controller.refresh_panes()
            return calls, controller.agent_to_pane

        calls, mapping = asyncio.run(scenario())
        self.assertEqual(calls, [("list-panes", "-s", "-t", "hq", "-F", "#{pane_id}\t#{window_name}")])
        self.assertEqual(mapping, {"alice": "%0", "bob": "%1"})

    def test_refresh_panes_handles_hyphenated_names_with_digits(self):
        async def scenario():
            controller = ControlModeClient("hq")

            async def command(*args):
                return ["%0\tarchitect", "%1\tsme-2", "%2\tsme-3"]

            controller.command = command
            await controller.refresh_panes()
            return controller.agent_to_pane, controller.pane_to_agent

        agent_to_pane, pane_to_agent = asyncio.run(scenario())
        self.assertEqual(agent_to_pane, {"architect": "%0", "sme-2": "%1", "sme-3": "%2"})
        self.assertEqual(pane_to_agent, {"%0": "architect", "%1": "sme-2", "%2": "sme-3"})

    def test_session_non_loopback_bind_requires_tls(self):
        with patch.dict(os.environ, {"FLOCK_ALLOW_PLAINTEXT": "0", "H_MESH_ALLOW_PLAINTEXT": "0"}):
            settings = SessionSettings(
                tenant="office",
                api_token="secret",
                session_name="office",
                session_bind="0.0.0.0",
            )
            with self.assertRaises(RuntimeError) as ctx:
                settings.validate()
            self.assertIn("SESSION_TLS_CERT and SESSION_TLS_KEY are required", str(ctx.exception))

    def test_session_partial_tls_configuration_raises_error(self):
        with patch.dict(os.environ, {"FLOCK_ALLOW_PLAINTEXT": "0", "H_MESH_ALLOW_PLAINTEXT": "0"}):
            settings = SessionSettings(
                tenant="office",
                api_token="secret",
                session_name="office",
                session_tls_cert="/cert.pem",
            )
            with self.assertRaises(RuntimeError) as ctx:
                settings.validate()
            self.assertIn("Both SESSION_TLS_CERT and SESSION_TLS_KEY must be provided", str(ctx.exception))

    def test_session_non_loopback_bind_with_tls_succeeds(self):
        with patch.dict(os.environ, {"FLOCK_ALLOW_PLAINTEXT": "0", "H_MESH_ALLOW_PLAINTEXT": "0"}):
            settings = SessionSettings(
                tenant="office",
                api_token="secret",
                session_name="office",
                session_bind="0.0.0.0",
                session_tls_cert="/cert.pem",
                session_tls_key="/key.pem",
            )
            settings.validate()

    def test_session_non_loopback_bind_with_allow_plaintext(self):
        with patch.dict(os.environ, {"H_MESH_ALLOW_PLAINTEXT": "1"}):
            settings = SessionSettings(
                tenant="office",
                api_token="secret",
                session_name="office",
                session_bind="0.0.0.0",
            )
            settings.validate()

    def test_slow_viewer_queue_bounded(self):
        subscriber = Subscriber()
        self.assertEqual(subscriber.queue.maxsize, 1000)

    def test_session_control_recovers_after_stream_break(self):
        with patch.dict(os.environ, {"TMUX_TMPDIR": "/tmp"}):
            controller = ControlModeClient("test_session")
            controller.broken_reason = "tmux stream closed"

            with patch.object(controller, "start", new_callable=AsyncMock) as mock_start:
                asyncio.run(controller.ensure_connected())
                mock_start.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
