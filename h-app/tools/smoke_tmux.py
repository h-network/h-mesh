"""Prove tmux reconciliation and delivery against real Redis and an isolated tmux server."""

import os
import shutil
import sys
import tempfile

import redis

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.channels import send  # noqa: E402
from core.dispatch import register_type, dispatch_ingress  # noqa: E402
from core.keys import prefix  # noqa: E402
from core.service import Switch  # noqa: E402
from modules.tmux import deliver_tmux, list_windows, run_tmux  # noqa: E402
from modules.tmux.reconciler import TmuxReconciler  # noqa: E402


def main() -> None:
    url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    pod = os.environ.get("POD", "ci-tmux-smoke")
    tenant = os.environ.get("TENANT", "ci-tmux-smoke")
    session_name = f"smoke-tmux-{os.urandom(4).hex()}"
    sender = "tmux-sender"
    recipient = "tmux-recipient"

    tmpdir = tempfile.mkdtemp()
    socket = os.path.join(tmpdir, "tmux_smoke.sock")
    r = redis.Redis.from_url(url)
    r.ping()

    registry = prefix(pod, tenant, resource="registry")
    owned_keys = [
        prefix(pod, tenant, agent, resource)
        for agent in (sender, recipient)
        for resource in ("egress", "ingress", "dead", "unreplied", "launch")
    ]

    def cleanup() -> None:
        try:
            run_tmux("kill-server", socket=socket)
        except Exception:
            pass
        shutil.rmtree(tmpdir, ignore_errors=True)
        r.hdel(registry, sender, recipient)
        r.delete(*owned_keys)

    # Initial reset without deleting tmpdir
    r.hdel(registry, sender, recipient)
    r.delete(*owned_keys)
    try:
        # Register sender and recipient as tmux agents
        r.hset(registry, mapping={sender: "tmux", recipient: "tmux"})

        # Step 1: Reconcile creates windows
        reconciler = TmuxReconciler(
            pod=pod,
            tenant=tenant,
            redis_url=url,
            session_name=session_name,
            socket=socket,
        )
        reconciler.reconcile_once(r)

        windows = list_windows(session_name, socket=socket)
        if sender not in windows or recipient not in windows:
            raise AssertionError(f"reconciler failed to create windows: {windows!r}")

        # Step 2: Send message and route via switch
        payload = {"text": "hello through real tmux port", "smoke": True}
        stream_id = send(
            r,
            pod=pod,
            tenant=tenant,
            source=sender,
            destination=recipient,
            payload=payload,
        )

        kicked = []
        switch = Switch(
            r,
            pod=pod,
            tenant=tenant,
            kick=lambda agent, port_type, envelope: kicked.append((agent, port_type)),
        )
        if not switch.step(timeout=1):
            raise AssertionError("switch did not forward the envelope")

        if kicked != [(recipient, "tmux")]:
            raise AssertionError(f"unexpected kick: {kicked!r}")

        # Register tmux delivery handler with dispatch
        register_type("tmux", lambda r, pod, tenant, agent: deliver_tmux(
            r, pod=pod, tenant=tenant, agent=agent, session_name=session_name, socket=socket,
        ))

        # Step 3: Dispatch ingress
        dispatch_ingress(r, pod=pod, tenant=tenant, agent=recipient)

        # Step 4: Verify text in recipient pane
        code, stdout, stderr = run_tmux(
            "capture-pane", "-J", "-p", "-t", f"{session_name}:{recipient}", socket=socket,
        )
        if code != 0:
            raise AssertionError(f"capture-pane failed ({code}): {stderr}")

        expected_text = f"[message from {sender}] {payload['text']}"
        if expected_text not in stdout:
            raise AssertionError(f"expected text {expected_text!r} not in pane: {stdout!r}")

        # Step 5: Test cleanup / window retirement
        r.hdel(registry, sender)
        reconciler.reconcile_once(r)

        windows_after = list_windows(session_name, socket=socket)
        if sender in windows_after or recipient not in windows_after:
            raise AssertionError(f"window retirement failed: {windows_after!r}")

        print("real tmux reconciliation and delivery smoke passed")
    finally:
        cleanup()


if __name__ == "__main__":
    main()
