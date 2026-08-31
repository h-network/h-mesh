import base64
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

import redis

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from core.channels import send
from core.keys import prefix
from modules.tmux import (
    AmbientTmuxError,
    deliver_tmux,
    list_windows,
    require_isolated_tmux,
    run_tmux,
)
from modules.tmux.reconciler import TmuxReconciler


class RealTmuxIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.socket = os.path.join(self.tmpdir, "isolated_tmux.sock")
        self.redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
        self.r = redis.Redis.from_url(self.redis_url)
        self.r.ping()
        self.pod = "real-tmux-test"
        self.tenant = f"tenant-{os.urandom(4).hex()}"
        self.session_name = f"sess-{os.urandom(4).hex()}"

        self.registry = prefix(self.pod, self.tenant, resource="registry")

    def tearDown(self):
        try:
            run_tmux("kill-server", socket=self.socket)
        except Exception:
            pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        # Clean Redis keys
        keys = self.r.keys(f"{self.pod}.{self.tenant}.*")
        if keys:
            self.r.delete(*keys)

    def test_ambient_tmux_refusal(self):
        # With neither TMUX_SOCKET nor TMUX_TMPDIR nor explicit socket, require_isolated_tmux raises
        old_socket = os.environ.pop("TMUX_SOCKET", None)
        old_tmpdir = os.environ.pop("TMUX_TMPDIR", None)
        try:
            with self.assertRaises(AmbientTmuxError):
                require_isolated_tmux()
        finally:
            if old_socket:
                os.environ["TMUX_SOCKET"] = old_socket
            if old_tmpdir:
                os.environ["TMUX_TMPDIR"] = old_tmpdir

    def test_real_tmux_reconcile_and_delivery_lifecycle(self):
        reconciler = TmuxReconciler(
            pod=self.pod,
            tenant=self.tenant,
            redis_url=self.redis_url,
            session_name=self.session_name,
            socket=self.socket,
        )

        # 1. Register agents: alice and bob
        self.r.hset(self.registry, mapping={"alice": "tmux", "bob": "tmux"})

        # Run reconcile_once
        reconciler.reconcile_once(self.r)

        # Verify windows created in real tmux session
        windows = list_windows(self.session_name, socket=self.socket)
        self.assertIn("alice", windows)
        self.assertIn("bob", windows)

        # 2. Test Message delivery
        payload = {"text": "hello alice from bob"}
        stream_id = send(
            self.r,
            pod=self.pod,
            tenant=self.tenant,
            source="bob",
            destination="alice",
            payload=payload,
        )
        # Move from bob egress to alice ingress
        raw = self.r.lpop(prefix(self.pod, self.tenant, "bob", "egress"))
        self.assertIsNotNone(raw)
        self.r.rpush(prefix(self.pod, self.tenant, "alice", "ingress"), raw)

        # Deliver to alice
        deliver_tmux(
            self.r,
            pod=self.pod,
            tenant=self.tenant,
            agent="alice",
            session_name=self.session_name,
            socket=self.socket,
        )

        # Capture pane text from alice window
        code, stdout, stderr = run_tmux("capture-pane", "-p", "-t", f"{self.session_name}:alice", socket=self.socket)
        self.assertEqual(code, 0)
        self.assertIn("[message from bob] hello alice from bob", stdout)

        # 3. Test Attachment delivery
        content = b"Binary attachment payload data"
        b64 = base64.b64encode(content).decode("ascii")
        att_stream_id = send(
            self.r,
            pod=self.pod,
            tenant=self.tenant,
            source="bob",
            destination="alice",
            payload={
                "filename": "report.pdf",
                "mime_type": "application/pdf",
                "content_base64": b64,
                "caption": "Quarterly report",
            },
            kind="Attachment",
        )
        att_raw = self.r.lpop(prefix(self.pod, self.tenant, "bob", "egress"))
        self.r.rpush(prefix(self.pod, self.tenant, "alice", "ingress"), att_raw)

        # We deliver with workdir_root inside our tmpdir
        from modules.tmux import attachment_opener
        with unittest.mock.patch("modules.tmux.port.attachment_opener", side_effect=lambda **kw: attachment_opener(workdir_root=self.tmpdir, **kw)):
            deliver_tmux(
                self.r,
                pod=self.pod,
                tenant=self.tenant,
                agent="alice",
                session_name=self.session_name,
                socket=self.socket,
            )

        saved_file = Path(self.tmpdir) / "alice" / "attachments" / att_stream_id / "report.pdf"
        self.assertTrue(saved_file.is_file())
        self.assertEqual(saved_file.read_bytes(), content)

        code, stdout, stderr = run_tmux("capture-pane", "-J", "-p", "-t", f"{self.session_name}:alice", socket=self.socket)
        self.assertEqual(code, 0)
        self.assertIn(f"[attachment from bob] saved to {saved_file} (application/pdf, {len(content)} bytes)", stdout)
        self.assertIn("[attachment caption] Quarterly report", stdout)

        # 4. Test removing alice from roster
        self.r.hdel(self.registry, "alice")
        reconciler.reconcile_once(self.r)

        windows_after = list_windows(self.session_name, socket=self.socket)
        self.assertNotIn("alice", windows_after)
        self.assertIn("bob", windows_after)

        # 5. Test removing all agents -> placeholder __init__
        self.r.hdel(self.registry, "bob")
        reconciler.reconcile_once(self.r)

        windows_final = list_windows(self.session_name, socket=self.socket)
        self.assertNotIn("bob", windows_final)
        self.assertIn("__init__", windows_final)

    def test_real_tmux_message_burst_sequential_delivery(self):
        reconciler = TmuxReconciler(
            pod=self.pod,
            tenant=self.tenant,
            redis_url=self.redis_url,
            session_name=self.session_name,
            socket=self.socket,
        )
        self.r.hset(self.registry, mapping={"charlie": "tmux", "dave": "tmux"})
        reconciler.reconcile_once(self.r)

        # Send a burst of 3 messages
        stream_ids = []
        for i in range(3):
            sid = send(
                self.r,
                pod=self.pod,
                tenant=self.tenant,
                source="dave",
                destination="charlie",
                payload={"text": f"burst message #{i+1}"},
            )
            stream_ids.append(sid)
            raw = self.r.lpop(prefix(self.pod, self.tenant, "dave", "egress"))
            self.r.rpush(prefix(self.pod, self.tenant, "charlie", "ingress"), raw)

        # Deliver all 3 messages sequentially (each deliver_tmux pops 1 message)
        for i in range(3):
            deliver_tmux(
                self.r,
                pod=self.pod,
                tenant=self.tenant,
                agent="charlie",
                session_name=self.session_name,
                socket=self.socket,
            )

        # Verify ingress is drained
        self.assertIsNone(self.r.lpop(prefix(self.pod, self.tenant, "charlie", "ingress")))

        # Check pane output contains all 3 messages
        code, stdout, stderr = run_tmux("capture-pane", "-J", "-p", "-t", f"{self.session_name}:charlie", socket=self.socket)
        self.assertEqual(code, 0)
        for i in range(3):
            self.assertIn(f"[message from dave] burst message #{i+1}", stdout)

    def test_real_tmux_module_subprocess_invocation(self):
        import signal
        import subprocess

        reconciler = TmuxReconciler(
            pod=self.pod,
            tenant=self.tenant,
            redis_url=self.redis_url,
            session_name=self.session_name,
            socket=self.socket,
        )
        self.r.hset(self.registry, mapping={"frank": "tmux", "grace": "tmux"})
        reconciler.reconcile_once(self.r)

        sid = send(
            self.r,
            pod=self.pod,
            tenant=self.tenant,
            source="frank",
            destination="grace",
            payload={"text": "invoked via python -m modules.tmux.port"},
        )
        raw = self.r.lpop(prefix(self.pod, self.tenant, "frank", "egress"))
        self.r.rpush(prefix(self.pod, self.tenant, "grace", "ingress"), raw)

        # Set up environment for subprocess
        env = dict(os.environ)
        env["PYTHONPATH"] = str(H_APP)
        env["POD"] = self.pod
        env["TENANT"] = self.tenant
        env["REDIS_URL"] = self.redis_url
        env["TMUX_SESSION"] = self.session_name
        env["TMUX_SOCKET"] = self.socket

        # Simulate parent having SIGCHLD = SIG_IGN
        old_handler = signal.signal(signal.SIGCHLD, signal.SIG_IGN)
        try:
            res = subprocess.run(
                [sys.executable, "-m", "modules.tmux.port", "grace"],
                env=env,
                capture_output=True,
                text=True,
            )
        finally:
            signal.signal(signal.SIGCHLD, old_handler)

        self.assertEqual(res.returncode, 0, f"port main failed: {res.stderr}")

        # Ingress drained
        self.assertIsNone(self.r.lpop(prefix(self.pod, self.tenant, "grace", "ingress")))

        # Check pane output
        code, stdout, stderr = run_tmux("capture-pane", "-J", "-p", "-t", f"{self.session_name}:grace", socket=self.socket)
        self.assertEqual(code, 0)
        self.assertIn("[message from frank] invoked via python -m modules.tmux.port", stdout)

        # Delivering lock released
        delivering_key = prefix(self.pod, self.tenant, resource="delivering")
        self.assertIsNone(self.r.hget(delivering_key, "grace"))


if __name__ == "__main__":
    unittest.main()
