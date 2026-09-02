import base64
import json
import os
import shutil
import subprocess
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
    EmptyRosterError,
    deliver_tmux,
    list_windows,
    require_isolated_tmux,
    run_tmux,
)
from modules.tmux.reconciler import TmuxReconciler


class RealTmuxIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.state_dir = os.path.join(self.tmpdir, "state")
        os.makedirs(self.state_dir, exist_ok=True)
        self.workdir_root = os.path.join(self.tmpdir, "workdir")
        os.makedirs(self.workdir_root, exist_ok=True)
        self.socket = os.path.join(self.tmpdir, "isolated_tmux.sock")
        self.redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
        self.r = redis.Redis.from_url(self.redis_url)
        self.r.ping()
        self.pod = "real-tmux-test"
        self.tenant = f"tenant-{os.urandom(4).hex()}"
        self.session_name = f"sess-{os.urandom(4).hex()}"

        self.registry = prefix(self.pod, self.tenant, resource="registry")

        # ⚠ Isolate environment: state dir, workdir, socket, session, and strip any ambient credentials.
        # This prevents test processes from inheriting production tokens or writing into the real office log.
        env_patch = {
            "H_MESH_STATE_DIR": self.state_dir,
            "H_MESH_WORKDIR": self.workdir_root,
            "TMUX_SOCKET": self.socket,
            "TMUX_SESSION": self.session_name,
        }
        for k in list(os.environ.keys()):
            if k.startswith("CLAUDE_OAUTH_TOKEN_") or k == "CLAUDE_CODE_OAUTH_TOKEN":
                env_patch[k] = ""
        if "H_MESH_LOG_FILE" in os.environ:
            env_patch["H_MESH_LOG_FILE"] = ""

        self.env_patcher = unittest.mock.patch.dict(os.environ, env_patch, clear=False)
        self.env_patcher.start()

    def tearDown(self):
        try:
            self.env_patcher.stop()
        except Exception:
            pass
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
        code, stdout, stderr = run_tmux("capture-pane", "-J", "-p", "-t", f"{self.session_name}:alice", socket=self.socket)
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

        # 5. Test removing all agents -> EmptyRosterError without override
        self.r.hdel(self.registry, "bob")
        with self.assertRaises(EmptyRosterError):
            reconciler.reconcile_once(self.r)
        self.assertIn("bob", list_windows(self.session_name, socket=self.socket))

        # With explicit override allow_empty_roster=True, placeholder __init__ is created and bob is reaped
        reconciler.allow_empty_roster = True
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
        delivering_key = prefix(self.pod, self.tenant, agent="grace", resource="delivering")
        self.assertIsNone(self.r.get(delivering_key))

    def test_reconciler_empty_registry_settles_without_churn(self):
        reconciler = TmuxReconciler(
            pod=self.pod,
            tenant=self.tenant,
            redis_url=self.redis_url,
            session_name=self.session_name,
            socket=self.socket,
        )

        # 1. Empty registry: should create __init__ once and settle
        for _ in range(3):
            reconciler.reconcile_once(self.r)
            self.assertEqual(list_windows(self.session_name, socket=self.socket), {"__init__"})

        # 2. Add an agent: should create agent window and remove __init__
        self.r.hset(self.registry, "helen", "tmux")
        reconciler.reconcile_once(self.r)
        self.assertEqual(list_windows(self.session_name, socket=self.socket), {"helen"})

        for _ in range(2):
            reconciler.reconcile_once(self.r)
            self.assertEqual(list_windows(self.session_name, socket=self.socket), {"helen"})

        # 3. Remove agent: should recreate __init__ and remove agent window without churn (requires allow_empty_roster=True)
        self.r.hdel(self.registry, "helen")
        with self.assertRaises(EmptyRosterError):
            reconciler.reconcile_once(self.r)
        self.assertEqual(list_windows(self.session_name, socket=self.socket), {"helen"})

        reconciler.allow_empty_roster = True
        reconciler.reconcile_once(self.r)
        self.assertEqual(list_windows(self.session_name, socket=self.socket), {"__init__"})

        for _ in range(3):
            reconciler.reconcile_once(self.r)
            self.assertEqual(list_windows(self.session_name, socket=self.socket), {"__init__"})

    def test_reconciler_host_workdir_custom_path_creation(self):
        custom_workdir = os.path.join(self.tmpdir, "custom_workdir_root")
        with unittest.mock.patch.dict(os.environ, {"H_MESH_WORKDIR": custom_workdir}):
            reconciler = TmuxReconciler(
                pod=self.pod,
                tenant=self.tenant,
                redis_url=self.redis_url,
                session_name=self.session_name,
                socket=self.socket,
            )
            self.r.hset(self.registry, "ian", "tmux")
            reconciler.reconcile_once(self.r)

            self.assertIn("ian", list_windows(self.session_name, socket=self.socket))
            ian_workdir = os.path.join(custom_workdir, "ian")
            self.assertTrue(os.path.isdir(ian_workdir))
            self.assertTrue(os.path.isfile(os.path.join(ian_workdir, "AGENTS.md")))

    def test_pane_office_send_logs_to_window_log_file(self):
        from core.windowlog import WindowLogTailer

        state_dir = os.path.join(self.tmpdir, "state")
        window_log_path = os.path.join(state_dir, "window.log.jsonl")

        with unittest.mock.patch.dict(os.environ, {"H_MESH_STATE_DIR": state_dir}):
            reconciler = TmuxReconciler(
                pod=self.pod,
                tenant=self.tenant,
                redis_url=self.redis_url,
                session_name=self.session_name,
                socket=self.socket,
            )
            self.r.hset(self.registry, mapping={"jack": "tmux", "kate": "tmux"})
            reconciler.reconcile_once(self.r)

            # Send a python send() command into jack's pane
            cmd = (
                f"python3 -c 'import os, redis; from core.channels import send; "
                f"r = redis.Redis.from_url(\"{self.redis_url}\"); "
                f"os.environ[\"H_MESH_LOG_QUIET\"] = \"1\"; "
                f"send(r, pod=\"{self.pod}\", tenant=\"{self.tenant}\", source=\"jack\", "
                f"destination=\"kate\", payload={{\"text\": \"durable sent test\"}})'"
            )
            run_tmux("send-keys", "-t", f"{self.session_name}:jack", f"PYTHONPATH={H_APP} {cmd}", "Enter", socket=self.socket)

            # Wait for execution and verify log file was created and written
            for _ in range(20):
                if os.path.exists(window_log_path) and os.path.getsize(window_log_path) > 0:
                    break
                time.sleep(0.1)

            self.assertTrue(os.path.exists(window_log_path), "window.log.jsonl was not created")
            content = Path(window_log_path).read_text()
            self.assertIn('"event":"sent"', content)
            self.assertIn('"source":"jack"', content)
            self.assertIn('"destination":"kate"', content)

            # Verify WindowLogTailer validates and publishes the sent record.
            # publish() is the single stdout-plus-durable-mirror seam.
            tailer = WindowLogTailer(self.r, pod=self.pod, tenant=self.tenant, path=window_log_path)
            published = []
            with unittest.mock.patch(
                "core.windowlog.publish",
                side_effect=lambda line: published.append(json.loads(line)),
            ):
                tailer.poll()

            self.assertEqual(len(published), 1)
            self.assertEqual(published[0]["event"], "sent")
            self.assertEqual(published[0]["source"], "jack")
            self.assertEqual(published[0]["destination"], "kate")

    def test_stop_agent_lifecycle_logs_window_killed(self):
        from core.envelope import build, encode
        from modules.office.port import deliver_office

        reconciler = TmuxReconciler(
            pod=self.pod,
            tenant=self.tenant,
            redis_url=self.redis_url,
            session_name=self.session_name,
            socket=self.socket,
        )
        self.r.hset(self.registry, mapping={"liam": "tmux", "noah": "tmux"})
        reconciler.reconcile_once(self.r)

        self.assertIn("liam", list_windows(self.session_name, socket=self.socket))
        self.assertIn("noah", list_windows(self.session_name, socket=self.socket))

        # Push StopAgent frame to office ingress
        frame = build(
            kind="StopAgent",
            source="architect",
            destination="office",
            payload={"agent": "liam"},
            pod=self.pod,
            tenant=self.tenant,
        )
        ingress_key = prefix(self.pod, self.tenant, agent="office", resource="ingress")
        self.r.rpush(ingress_key, encode(frame))

        logged = []
        with unittest.mock.patch("core.logging.mirror", side_effect=lambda line: logged.append(json.loads(line))):
            deliver_office(
                self.r,
                pod=self.pod,
                tenant=self.tenant,
                agent="office",
                session_name=self.session_name,
                socket=self.socket,
            )

        # Confirm liam was killed and noah remains
        windows = list_windows(self.session_name, socket=self.socket)
        self.assertNotIn("liam", windows)
        self.assertIn("noah", windows)

        # Confirm window_killed was logged
        killed_events = [rec for rec in logged if rec.get("event") == "window_killed"]
        self.assertEqual(len(killed_events), 1)
        self.assertEqual(killed_events[0]["destination"], "liam")

    def test_start_agent_command_flags(self):
        from modules.tmux.ops import start_agent_command

        self.assertEqual(start_agent_command("claude", resume=False), ["h-agent", "claude"])
        self.assertEqual(start_agent_command("claude", resume=True), ["h-agent", "claude", "--continue"])

        self.assertEqual(start_agent_command("codex", resume=False), ["h-agent", "codex"])
        self.assertEqual(start_agent_command("codex", resume=True), ["h-agent", "codex", "resume", "--last"])

        self.assertEqual(start_agent_command("agy", resume=False), ["h-agent", "agy"])
        self.assertEqual(start_agent_command("agy", resume=True), ["h-agent", "agy", "--continue"])

        self.assertEqual(start_agent_command("custom", resume=False), ["h-agent", "custom"])
        self.assertEqual(start_agent_command("custom", resume=True), ["h-agent", "custom", "--continue"])

    def test_generate_agents_md_command_derivation(self):
        from modules.tmux.ops import generate_agents_md, window_env

        # Default is h-mesh-office
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OFFICE_TOOLS", None)
            lead_guide = generate_agents_md("architect", lead="architect")
            self.assertIn("h-mesh-office status", lead_guide)
            self.assertIn("h-mesh-office clone-to-all", lead_guide)
            self.assertIn("h-mesh-office peers", lead_guide)
            self.assertIn("h-mesh-office send", lead_guide)
            self.assertIn("h-mesh-office list", lead_guide)
            self.assertIn("h-mesh-office take", lead_guide)
            self.assertIn("h-mesh-office done --outcome completed", lead_guide)
            self.assertIn("h-mesh-office return", lead_guide)
            self.assertIn("h-mesh-office show ID", lead_guide)
            self.assertIn("h-mesh-office retitle --title TEXT [ID]", lead_guide)
            self.assertNotIn("`office ", lead_guide)

            agent_guide = generate_agents_md("worker", lead="architect")
            self.assertIn("architect is the lead of this office", agent_guide)
            self.assertIn("h-mesh-office peers", agent_guide)
            self.assertIn("the operator's external entrance", agent_guide)
            self.assertIn("Treat where a message arrives (an external door) apart from who is speaking", agent_guide)
            self.assertIn("authenticated operator instruction outranks lead direction", agent_guide)
            self.assertIn("immediately notify the lead", agent_guide)
            self.assertIn("h-mesh-office send", agent_guide)
            self.assertIn("Interface entries may only", agent_guide)
            self.assertIn("not arbitrary messages", agent_guide)
            self.assertIn("python -m tools.run_tests", agent_guide)
            self.assertIn("Report both the collected count and the passed count", agent_guide)
            self.assertIn("old branch can still display a green result", agent_guide)
            self.assertNotIn("an app client", agent_guide)
            self.assertNotIn("an app client", lead_guide)
            self.assertNotIn("still a valid", agent_guide)
            self.assertNotIn("still a valid", lead_guide)
            self.assertNotIn("`office ", agent_guide)

            env = window_env("worker")
            self.assertIn("OFFICE_TOOLS=h-mesh-office", env)

        # Explicit override via OFFICE_TOOLS env var
        with unittest.mock.patch.dict(os.environ, {"OFFICE_TOOLS": "custom-office"}):
            guide = generate_agents_md("worker")
            self.assertIn("custom-office peers", guide)
            self.assertIn("custom-office send", guide)
            env = window_env("worker")
            self.assertIn("OFFICE_TOOLS=custom-office", env)

        # Explicit override via argument
        guide_arg = generate_agents_md("worker", office_cmd="arg-office")
        self.assertIn("arg-office peers", guide_arg)
        self.assertIn("arg-office send", guide_arg)

    def test_window_env_resolves_log_path_via_state_path_not_ambient_env(self):
        from modules.tmux.ops import window_env

        # Ambient H_MESH_LOG_FILE must not override state_path
        with unittest.mock.patch.dict(os.environ, {"H_MESH_LOG_FILE": "/real/office/window.log.jsonl"}):
            env = window_env("worker", tenant="custom-tenant")
            expected_log = os.path.join(self.state_dir, "window.log.jsonl")
            self.assertIn(f"H_MESH_LOG_FILE={expected_log}", env)
            self.assertNotIn("H_MESH_LOG_FILE=/real/office/window.log.jsonl", env)

        # Explicit log_file argument overrides state_path
        custom_log = "/custom/isolated/path/window.log.jsonl"
        env_explicit = window_env("worker", tenant="custom-tenant", log_file=custom_log)
        self.assertIn(f"H_MESH_LOG_FILE={custom_log}", env_explicit)

    def test_window_env_token_handling(self):
        from modules.tmux.ops import window_env

        # When tokens are unset or empty, CLAUDE_CODE_OAUTH_TOKEN is not in env_vars
        with unittest.mock.patch.dict(os.environ, {"CLAUDE_OAUTH_TOKEN_DEFAULT": "", "CLAUDE_OAUTH_TOKEN_WORK": ""}):
            env_default = window_env("worker")
            self.assertFalse(any(var.startswith("CLAUDE_CODE_OAUTH_TOKEN=") for var in env_default))

            env_work = window_env("worker", profile="work")
            self.assertFalse(any(var.startswith("CLAUDE_CODE_OAUTH_TOKEN=") for var in env_work))

        # When a test double token is explicitly provided, it is injected for that profile
        with unittest.mock.patch.dict(os.environ, {"CLAUDE_OAUTH_TOKEN_WORK": "test-double-work-token"}):
            env_work = window_env("worker", profile="work")
            self.assertIn("CLAUDE_CODE_OAUTH_TOKEN=test-double-work-token", env_work)

            env_default = window_env("worker")
            self.assertFalse(any(var.startswith("CLAUDE_CODE_OAUTH_TOKEN=") for var in env_default))

    def test_window_env_path_injection(self):
        from modules.tmux.ops import window_env
        from lib.paths import resolve_venv_bin

        # Default resolution prepends venv bin, user bin dirs, and system paths
        with unittest.mock.patch.dict(os.environ, {"HOME": "/home/worker", "PATH": "/usr/local/bin:/usr/bin", "VIRTUAL_ENV": "/custom/myvenv"}):
            env = window_env("worker")
            path_var = next(var for var in env if var.startswith("PATH="))
            entries = path_var[len("PATH="):].split(":")
            self.assertEqual(entries[0], "/custom/myvenv/bin")
            self.assertIn("/home/worker/.local/bin", entries)
            self.assertIn("/usr/local/bin", entries)
            self.assertIn("/usr/bin", entries)

        # Explicit venv_bin override
        with unittest.mock.patch.dict(os.environ, {"HOME": "/home/worker", "PATH": "/usr/local/bin:/usr/bin"}):
            env_explicit = window_env("worker", venv_bin="/explicit/bin")
            path_var = next(var for var in env_explicit if var.startswith("PATH="))
            entries = path_var[len("PATH="):].split(":")
            self.assertEqual(entries[0], "/explicit/bin")
            self.assertIn("/home/worker/.local/bin", entries)

        # Deduplicates/moves to front if venv bin is already in PATH
        with unittest.mock.patch.dict(os.environ, {"HOME": "/home/worker", "PATH": "/usr/local/bin:/explicit/bin:/usr/bin"}):
            env_dedup = window_env("worker", venv_bin="/explicit/bin")
            path_var = next(var for var in env_dedup if var.startswith("PATH="))
            entries = path_var[len("PATH="):].split(":")
            self.assertEqual(entries[0], "/explicit/bin")
            self.assertEqual(len(entries), len(set(entries)))

        # Clean/scrubbed environment (acceptance-agent repro case) guarantees ~/.local/bin and venv bin
        with unittest.mock.patch.dict(os.environ, {"HOME": "/home/worker", "PATH": "/usr/bin:/bin"}, clear=True):
            env_clean = window_env("worker", venv_bin="/opt/venv/bin")
            path_var = next(var for var in env_clean if var.startswith("PATH="))
            entries = path_var[len("PATH="):].split(":")
            self.assertEqual(entries[0], "/opt/venv/bin")
            self.assertIn("/home/worker/.local/bin", entries)
            self.assertIn("/usr/bin", entries)

    def test_hired_agent_pane_process_has_venv_bin_on_path_in_proc_environ(self):
        from lib.paths import resolve_venv_bin

        reconciler = TmuxReconciler(
            pod=self.pod,
            tenant=self.tenant,
            redis_url=self.redis_url,
            session_name=self.session_name,
            socket=self.socket,
        )

        # Register agent
        self.r.hset(self.registry, mapping={"testagent": "tmux"})
        reconciler.reconcile_once(self.r)

        # Get the PID of the pane process in real tmux
        code, stdout, stderr = run_tmux(
            "list-panes", "-t", f"{self.session_name}:testagent", "-F", "#{pane_pid}", socket=self.socket
        )
        self.assertEqual(code, 0, f"list-panes failed: {stderr}")
        pane_pid = stdout.strip()
        self.assertTrue(pane_pid.isdigit(), f"Expected integer PID, got {pane_pid}")

        # Read the real /proc/{pid}/environ of the hired agent's pane process
        environ_path = Path(f"/proc/{pane_pid}/environ")
        self.assertTrue(environ_path.exists(), f"{environ_path} does not exist")

        raw_environ = environ_path.read_bytes()
        env_dict = dict(
            item.decode("utf-8", errors="replace").split("=", 1)
            for item in raw_environ.split(b"\x00")
            if b"=" in item
        )

        self.assertIn("PATH", env_dict)
        expected_venv_bin = resolve_venv_bin()
        path_val = env_dict["PATH"]
        self.assertTrue(
            path_val == expected_venv_bin or path_val.startswith(f"{expected_venv_bin}:"),
            f"Expected PATH to start with {expected_venv_bin}, got: {path_val}",
        )

    def test_reconciler_dying_window_detection_and_backoff(self):
        reconciler = TmuxReconciler(
            pod=self.pod,
            tenant=self.tenant,
            redis_url=self.redis_url,
            poll_seconds=5.0,
            session_name=self.session_name,
            socket=self.socket,
        )

        # Register an agent that fails immediately upon execution (e.g. invalid command)
        self.r.hset(self.registry, mapping={"crashy": "tmux"})
        launch_key = prefix(self.pod, self.tenant, agent="crashy", resource="launch")
        self.r.set(launch_key, "invalid_cli")

        logged = []
        with unittest.mock.patch("core.logging.mirror", side_effect=lambda line: logged.append(json.loads(line))), \
             unittest.mock.patch("modules.tmux.ops.start_agent_command", return_value=["false"]):

            # Pass 1: Spawns crashy, which dies immediately. tmux removes an
            # exited window asynchronously, so reconcile until the observable
            # window_died behavior appears instead of assuming reaping happens
            # inside the first call on every host.
            deadline = time.monotonic() + 2.0
            while True:
                reconciler.reconcile_once(self.r)
                died_events = [
                    r for r in logged
                    if r.get("event") == "window_died" and r.get("destination") == "crashy"
                ]
                if died_events or time.monotonic() >= deadline:
                    break
                time.sleep(0.01)

            created_events = [r for r in logged if r.get("event") == "window_created" and r.get("destination") == "crashy"]
            died_events = [r for r in logged if r.get("event") == "window_died" and r.get("destination") == "crashy"]

            self.assertEqual(len(created_events), 1)
            self.assertEqual(len(died_events), 1)
            self.assertEqual(died_events[0]["count"], 1)
            self.assertEqual(died_events[0]["waited"], 5.0)

            # Pass 2: Immediately run again (within the 5s backoff). Should NOT retry spawning.
            reconciler.reconcile_once(self.r)

            created_events_p2 = [r for r in logged if r.get("event") == "window_created" and r.get("destination") == "crashy"]
            died_events_p2 = [r for r in logged if r.get("event") == "window_died" and r.get("destination") == "crashy"]

            # No new creations or dead window logs emitted while in backoff
            self.assertEqual(len(created_events_p2), 1)
            self.assertEqual(len(died_events_p2), 1)

            # Fast-forward time past the backoff interval (5s)
            reconciler._next_retry["crashy"] = time.monotonic() - 1.0

            # Pass 3: Backoff expired. Retries spawn, dies again -> count=2, waited=10s.
            reconciler.reconcile_once(self.r)

            created_events_p3 = [r for r in logged if r.get("event") == "window_created" and r.get("destination") == "crashy"]
            died_events_p3 = [r for r in logged if r.get("event") == "window_died" and r.get("destination") == "crashy"]

            self.assertEqual(len(created_events_p3), 2)
            self.assertEqual(len(died_events_p3), 2)
            self.assertEqual(died_events_p3[1]["count"], 2)
            self.assertEqual(died_events_p3[1]["waited"], 10.0)

    def test_reconciler_running_window_death_not_double_counted(self):
        reconciler = TmuxReconciler(
            pod=self.pod,
            tenant=self.tenant,
            redis_url=self.redis_url,
            poll_seconds=5.0,
            session_name=self.session_name,
            socket=self.socket,
        )

        self.r.hset(self.registry, mapping={"runner": "tmux"})

        logged = []
        with unittest.mock.patch("core.logging.mirror", side_effect=lambda line: logged.append(json.loads(line))):
            # Pass 1: Spawns runner (healthy bash window)
            reconciler.reconcile_once(self.r)
            self.assertIn("runner", reconciler.get_windows())

            # Mark runner as matured/healthy past poll_seconds
            reconciler._spawned_agents["runner"] = time.monotonic() - 10.0
            reconciler.reconcile_once(self.r)
            self.assertIn("runner", reconciler._known_windows)
            self.assertNotIn("runner", reconciler._spawned_agents)

            # Kill window directly via tmux (simulating an agent process crash)
            run_tmux("kill-window", "-t", f"{self.session_name}:runner", socket=self.socket)

            # Pass 3: Detects running window death
            reconciler.reconcile_once(self.r)

            died_events = [r for r in logged if r.get("event") == "window_died" and r.get("destination") == "runner"]
            self.assertEqual(len(died_events), 1)
            self.assertEqual(died_events[0]["count"], 1)
            self.assertEqual(reconciler._failure_counts.get("runner"), 1)

    def test_empty_roster_refusal_and_override(self):
        reconciler = TmuxReconciler(
            pod=self.pod,
            tenant=self.tenant,
            redis_url=self.redis_url,
            session_name=self.session_name,
            socket=self.socket,
        )
        self.r.hset(self.registry, mapping={"w1": "tmux", "w2": "tmux"})
        reconciler.reconcile_once(self.r)

        self.assertIn("w1", list_windows(self.session_name, socket=self.socket))
        self.assertIn("w2", list_windows(self.session_name, socket=self.socket))

        # Empty the Redis roster completely
        self.r.delete(self.registry)

        logged = []
        with unittest.mock.patch("core.logging.mirror", side_effect=lambda line: logged.append(json.loads(line))):
            # 1. reconcile_once with empty roster and allow_empty_roster=False (default) raises EmptyRosterError
            with self.assertRaises(EmptyRosterError):
                reconciler.reconcile_once(self.r)

            # Confirm no windows were killed
            windows = list_windows(self.session_name, socket=self.socket)
            self.assertIn("w1", windows)
            self.assertIn("w2", windows)

            # Confirm error was logged
            errors = [rec for rec in logged if rec.get("event") == "error" and "Refusing to reap" in rec.get("reason", "")]
            self.assertEqual(len(errors), 1)

            # 2. With allow_empty_roster=True, the reap succeeds down to placeholder __init__
            reconciler.allow_empty_roster = True
            reconciler.reconcile_once(self.r)

            windows_after = list_windows(self.session_name, socket=self.socket)
            self.assertNotIn("w1", windows_after)
            self.assertNotIn("w2", windows_after)
            self.assertIn("__init__", windows_after)

    def test_interop_with_tmux_tmpdir_server_when_ambient_tmux_set(self):
        # 1. Setup an ambient tmux server to simulate running inside a tmux session
        ambient_socket = os.path.join(self.tmpdir, "ambient_server.sock")
        ambient_sess = "ambient-office-sess"
        subprocess.run(["tmux", "-S", ambient_socket, "kill-server"], capture_output=True)
        subprocess.run(["tmux", "-S", ambient_socket, "new-session", "-d", "-s", ambient_sess, "-n", "ambient-pane", "bash"], check=True)

        # 2. Start a server the ORDINARY TMUX WAY (native tmux without -S, using TMUX_TMPDIR)
        isolated_tmpdir = os.path.join(self.tmpdir, "isolated_tmpdir")
        os.makedirs(isolated_tmpdir, exist_ok=True)
        server_env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE", "TMUX_SOCKET")}
        server_env["TMUX_TMPDIR"] = isolated_tmpdir
        subprocess.run(["tmux", "new-session", "-d", "-s", "shared-tenant-sess", "-n", "pre-existing-window", "bash"], env=server_env, check=True)

        # 3. Simulate calling run_tmux/list_windows from inside an ambient session with TMUX_TMPDIR set
        with unittest.mock.patch.dict(os.environ, {
            "TMUX": f"{ambient_socket},9999,0",
            "TMUX_PANE": "%0",
            "TMUX_TMPDIR": isolated_tmpdir,
        }, clear=False):
            os.environ.pop("TMUX_SOCKET", None)

            # list_windows without explicit socket argument must find the pre-existing server's windows
            windows = list_windows("shared-tenant-sess")
            self.assertEqual(windows, {"pre-existing-window"})

            # Creating a new window on this session interacts with the same pre-existing server
            ret, _, err = run_tmux("new-window", "-t", "shared-tenant-sess:", "-n", "second-window")
            self.assertEqual(ret, 0, f"run_tmux new-window failed: {err}")
            windows_after = list_windows("shared-tenant-sess")
            self.assertEqual(windows_after, {"pre-existing-window", "second-window"})

            # Ambient server must remain untouched
            res_ambient = subprocess.run(["tmux", "-S", ambient_socket, "list-windows", "-t", ambient_sess, "-F", "#{window_name}"], capture_output=True, text=True)
            self.assertEqual(set(res_ambient.stdout.splitlines()), {"ambient-pane"})

            # Explicitly attempting to target ambient_socket while TMUX=ambient_socket raises AmbientTmuxError
            with self.assertRaises(AmbientTmuxError):
                run_tmux("list-windows", "-t", ambient_sess, socket=ambient_socket)

        # Clean up servers
        try:
            subprocess.run(["tmux", "kill-server"], env=server_env, capture_output=True)
            subprocess.run(["tmux", "-S", ambient_socket, "kill-server"], capture_output=True)
        except Exception:
            pass

    def test_concurrent_submit_text_with_shared_stream_id_does_not_collide_paste_buffer(self):
        import concurrent.futures
        from modules.tmux.ops import run_tmux, submit_text

        run_tmux("new-session", "-d", "-s", self.session_name, "-n", "agent1", "bash", socket=self.socket)
        run_tmux("new-window", "-t", f"{self.session_name}:", "-n", "agent2", "bash", socket=self.socket)

        stream_id = "82605b8b1234567890abcdef12345678"

        def assert_pane_contains(agent: str, marker: str, timeout: float = 3.0) -> None:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                code, stdout, _ = run_tmux(
                    "capture-pane", "-J", "-p", "-t", f"{self.session_name}:{agent}", socket=self.socket
                )
                if code == 0 and marker in stdout:
                    return
                time.sleep(0.05)
            code, stdout, _ = run_tmux(
                "capture-pane", "-J", "-p", "-t", f"{self.session_name}:{agent}", socket=self.socket
            )
            self.assertEqual(code, 0)
            self.assertIn(marker, stdout, f"Marker {marker!r} missing from {agent} pane output: {stdout!r}")

        # 20 trials of concurrent submissions with the same stream_id across different agents
        for trial in range(20):
            marker1 = f"MARKER_A1_{trial}_{os.urandom(4).hex()}"
            marker2 = f"MARKER_A2_{trial}_{os.urandom(4).hex()}"

            def deliver(agent: str, text: str) -> None:
                submit_text(self.session_name, agent, text, stream_id=stream_id, socket=self.socket)

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                f1 = executor.submit(deliver, "agent1", f"echo {marker1}")
                f2 = executor.submit(deliver, "agent2", f"echo {marker2}")
                f1.result()
                f2.result()

            assert_pane_contains("agent1", marker1)
            assert_pane_contains("agent2", marker2)


if __name__ == "__main__":
    unittest.main()
