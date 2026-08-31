"""Prove tmux session control mode and WebSocket service against an isolated tmux server."""

import asyncio
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from modules.session.app import SessionSettings, create_app
from modules.session.control import ControlModeClient, Subscriber
from modules.tmux import run_tmux


async def main_async() -> None:
    tmpdir = tempfile.mkdtemp()
    socket = os.path.join(tmpdir, "tmux_session_smoke.sock")
    session_name = f"smoke-sess-{os.urandom(4).hex()}"
    agent_name = "test-viewer-agent"

    def cleanup() -> None:
        try:
            run_tmux("kill-server", socket=socket)
        except Exception:
            pass
        shutil.rmtree(tmpdir, ignore_errors=True)

    try:
        # Create session with window named agent_name
        code, stdout, stderr = run_tmux(
            "new-session", "-d", "-s", session_name, "-n", agent_name, socket=socket
        )
        if code != 0:
            raise AssertionError(f"failed to start isolated tmux: {stderr}")

        # Test 1: Direct ControlModeClient
        controller = ControlModeClient(session_name=session_name, socket=socket)
        await controller.start()
        try:
            subscriber = Subscriber()
            unknown = await controller.update_subscription(subscriber, {agent_name})
            if unknown:
                raise AssertionError(f"unexpected unknown agents: {unknown}")

            # Verify snapshot
            snapshot_event = await asyncio.wait_for(subscriber.queue.get(), timeout=2.0)
            if snapshot_event.get("agent") != agent_name:
                raise AssertionError(f"bad snapshot agent: {snapshot_event}")
            if not snapshot_event.get("data", "").startswith("\x1b[2J\x1b[H"):
                raise AssertionError(f"snapshot missing clear-and-home: {snapshot_event}")

            # Send keystrokes
            await controller.send_keys(agent_name, "echo session_smoke_token\n")

            # Collect output
            matched = False
            for _ in range(20):
                event = await asyncio.wait_for(subscriber.queue.get(), timeout=2.0)
                if "session_smoke_token" in event.get("data", ""):
                    matched = True
                    break

            if not matched:
                raise AssertionError("did not receive echoed keystrokes from control stream")

            controller.unsubscribe(subscriber)
        finally:
            await controller.stop()

        # Test 2: Full ASGI WebSocket app exchange
        app = create_app(
            settings=SessionSettings(
                tenant="smoke",
                api_token="smoke-token",
                session_name=session_name,
                tmux_socket=socket,
            ),
        )

        incoming = asyncio.Queue()
        incoming.put_nowait({"type": "websocket.connect"})
        incoming.put_nowait(
            {
                "type": "websocket.receive",
                "text": '{"subscribe": ["test-viewer-agent"], "mode": "read-write"}',
            }
        )
        incoming.put_nowait(
            {
                "type": "websocket.receive",
                "text": '{"agent": "test-viewer-agent", "data": "echo ws_smoke_token\\n"}',
            }
        )

        sent = []

        async def receive():
            if incoming.empty():
                await asyncio.sleep(0.5)
                return {"type": "websocket.disconnect", "code": 1000}
            return await incoming.get()

        async def send(message):
            sent.append(message)

        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "scheme": "ws",
            "path": "/session",
            "raw_path": b"/session",
            "query_string": b"",
            "headers": [(b"authorization", b"Bearer smoke-token")],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 8081),
            "subprotocols": [],
            "root_path": "",
        }

        # Run with lifespan
        async with app.router.lifespan_context(app):
            await app(scope, receive, send)

        accepted = any(msg.get("type") == "websocket.accept" for msg in sent)
        if not accepted:
            raise AssertionError("websocket was not accepted")

        print("real session control-mode and websocket smoke passed")
    finally:
        cleanup()


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
