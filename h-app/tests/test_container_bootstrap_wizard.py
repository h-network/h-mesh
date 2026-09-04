"""container/bootstrap.sh's interactive wizard -- the OAuth-token/Telegram
prompts and banner added in ticket b87f9f0a, driven through a real pty the
same way tests/test_setup_wizard.py drives setup.sh's own host wizard (a
piped/redirected stdin is never a tty, so `[ -t 0 ]` would never see this
code path at all otherwise). No real docker or Redis needed: this exercises
only the prompt/upsert-into-env-file logic, which runs entirely before
bootstrap.sh ever calls `docker compose up` -- a fake `docker` on PATH (same
technique tests/test_container_bootstrap.py already uses) stands in for it.
"""

import os
import pty
import select
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO_ROOT / "container" / "bootstrap.sh"


def _fake_docker(bin_dir: Path) -> None:
    docker = bin_dir / "docker"
    docker.write_text("#!/usr/bin/env bash\nexit 0\n")
    docker.chmod(0o755)


def _run_wizard(args: list[str], env: dict, answers: list[str], timeout: float = 15) -> tuple[str, int]:
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        ["bash", str(BOOTSTRAP), *args],
        stdin=slave, stdout=slave, stderr=slave, env=env,
    )
    os.close(slave)

    output = b""
    answer_idx = 0
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            ready, _, _ = select.select([master], [], [], 1)
            if master not in ready:
                continue
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
            if output.rstrip().endswith(b":") and answer_idx < len(answers):
                os.write(master, (answers[answer_idx] + "\n").encode())
                answer_idx += 1

        if proc.poll() is None:
            proc.wait(timeout=max(1, deadline - time.monotonic()))
    finally:
        os.close(master)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    return output.decode(errors="replace"), proc.returncode


def _env(bin_dir: Path) -> dict:
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    return env


def _env_file_text(path: Path) -> str:
    return path.read_text() if path.exists() else ""


def test_banner_prints_at_a_real_terminal(tmp_path):
    _fake_docker(tmp_path)
    env_file = tmp_path / "office.env"
    output, code = _run_wizard(
        ["--pod", "testpod", "--tenant", "testtenant", "--env-file", str(env_file)],
        env=_env(tmp_path),
        # agents, cli, oauth, telegram(no)
        answers=["", "", "", "n"],
    )
    assert code == 0, output
    assert "H-MESH" in output
    assert "agentic office framework" in output


def test_oauth_token_answer_is_persisted(tmp_path):
    _fake_docker(tmp_path)
    env_file = tmp_path / "office.env"
    output, code = _run_wizard(
        ["--pod", "testpod", "--tenant", "testtenant", "--env-file", str(env_file)],
        env=_env(tmp_path),
        answers=["", "", "tok-abc123", "n"],
    )
    assert code == 0, output
    assert "CLAUDE_OAUTH_TOKEN_DEFAULT=tok-abc123" in _env_file_text(env_file)


def test_blank_oauth_token_answer_is_not_persisted_at_all(tmp_path):
    """'blank to log in interactively later' must not write an empty
    CLAUDE_OAUTH_TOKEN_DEFAULT= line -- absent means unconfigured, not
    configured-as-empty (same distinction write_tenant_env's own docstring
    draws for the host wizard's persisted config)."""
    _fake_docker(tmp_path)
    env_file = tmp_path / "office.env"
    output, code = _run_wizard(
        ["--pod", "testpod", "--tenant", "testtenant", "--env-file", str(env_file)],
        env=_env(tmp_path),
        answers=["", "", "", "n"],
    )
    assert code == 0, output
    assert "CLAUDE_OAUTH_TOKEN_DEFAULT" not in _env_file_text(env_file)


def test_rerun_keeps_existing_oauth_token_when_answer_is_blank(tmp_path):
    _fake_docker(tmp_path)
    env_file = tmp_path / "office.env"
    env_file.write_text("POD=testpod\nTENANT=testtenant\nCLAUDE_OAUTH_TOKEN_DEFAULT=already-set\n")

    output, code = _run_wizard(
        ["--pod", "testpod", "--tenant", "testtenant", "--env-file", str(env_file)],
        env=_env(tmp_path),
        answers=["", "", "", "n"],
    )
    assert code == 0, output
    assert "CLAUDE_OAUTH_TOKEN_DEFAULT=already-set" in _env_file_text(env_file)
    assert "[keep existing]" in output


def test_telegram_yes_with_both_values_persists_all_three(tmp_path):
    _fake_docker(tmp_path)
    env_file = tmp_path / "office.env"
    output, code = _run_wizard(
        ["--pod", "testpod", "--tenant", "testtenant", "--env-file", str(env_file)],
        env=_env(tmp_path),
        # ..., voice=n, TLS cert path=blank (accept plaintext)
        answers=["", "", "", "y", "tg-token-xyz", "12345", "n", ""],
    )
    assert code == 0, output
    text = _env_file_text(env_file)
    assert "TELEGRAM_BOT_TOKEN=tg-token-xyz" in text
    assert "TELEGRAM_CHAT_ID=12345" in text
    assert "TELEGRAM_VOICE=0" in text


def test_enabling_telegram_forces_a_plaintext_or_tls_decision(tmp_path):
    """The actual bug this closes: container/entrypoint.sh refuses to start
    at all once Telegram is on, without H_MESH_ALLOW_PLAINTEXT=1 or real TLS
    certs -- previously discoverable only as a live crash-loop. The wizard
    must never let Telegram get enabled without resolving this."""
    _fake_docker(tmp_path)
    env_file = tmp_path / "office.env"
    output, code = _run_wizard(
        ["--pod", "testpod", "--tenant", "testtenant", "--env-file", str(env_file)],
        env=_env(tmp_path),
        answers=["", "", "", "y", "tg-token-xyz", "12345", "n", ""],  # blank = accept plaintext
    )
    assert code == 0, output
    assert "H_MESH_ALLOW_PLAINTEXT=1" in _env_file_text(env_file)
    assert "unencrypted" in output


def test_telegram_with_a_real_cert_path_persists_cert_and_key_not_plaintext(tmp_path):
    _fake_docker(tmp_path)
    env_file = tmp_path / "office.env"
    output, code = _run_wizard(
        ["--pod", "testpod", "--tenant", "testtenant", "--env-file", str(env_file)],
        env=_env(tmp_path),
        answers=[
            "", "", "", "y", "tg-token-xyz", "12345", "n",
            "/home/ubuntu/tlscerts/tls.crt", "/home/ubuntu/tlscerts/tls.key",
        ],
    )
    assert code == 0, output
    text = _env_file_text(env_file)
    assert "API_TLS_CERT=/home/ubuntu/tlscerts/tls.crt" in text
    assert "API_TLS_KEY=/home/ubuntu/tlscerts/tls.key" in text
    assert "H_MESH_ALLOW_PLAINTEXT" not in text


def test_a_cert_path_with_no_key_is_a_hard_error_before_persisting_anything(tmp_path):
    _fake_docker(tmp_path)
    env_file = tmp_path / "office.env"
    output, code = _run_wizard(
        ["--pod", "testpod", "--tenant", "testtenant", "--env-file", str(env_file)],
        env=_env(tmp_path),
        answers=["", "", "", "y", "tg-token-xyz", "12345", "n", "/home/ubuntu/tlscerts/tls.crt", ""],
    )
    assert code != 0
    text = _env_file_text(env_file)
    assert "API_TLS_CERT" not in text
    assert "H_MESH_ALLOW_PLAINTEXT" not in text


def test_rerun_with_existing_plaintext_choice_does_not_reprompt(tmp_path):
    _fake_docker(tmp_path)
    env_file = tmp_path / "office.env"
    env_file.write_text(
        "POD=testpod\nTENANT=testtenant\n"
        "TELEGRAM_BOT_TOKEN=already-set\nTELEGRAM_CHAT_ID=99999\n"
        "H_MESH_ALLOW_PLAINTEXT=1\n"
    )
    output, code = _run_wizard(
        ["--pod", "testpod", "--tenant", "testtenant", "--env-file", str(env_file)],
        env=_env(tmp_path),
        # oauth blank, telegram=y, both prompts show "[keep existing]" so
        # blank keeps them, voice -- no TLS/plaintext answer supplied at all
        answers=["", "", "", "y", "", "", "n"],
    )
    assert code == 0, output
    assert "H_MESH_ALLOW_PLAINTEXT=1" in _env_file_text(env_file)
    assert "Telegram turns on the API and session doors" not in output


def test_telegram_partial_pair_is_not_persisted(tmp_path):
    _fake_docker(tmp_path)
    env_file = tmp_path / "office.env"
    output, code = _run_wizard(
        ["--pod", "testpod", "--tenant", "testtenant", "--env-file", str(env_file)],
        env=_env(tmp_path),
        # telegram=y, token given, chat id left blank
        answers=["", "", "", "y", "tg-token-xyz", ""],
    )
    assert code == 0, output
    text = _env_file_text(env_file)
    assert "TELEGRAM_BOT_TOKEN" not in text
    assert "TELEGRAM_CHAT_ID" not in text
    assert "required" in output


def test_telegram_declined_prompts_nothing_further(tmp_path):
    _fake_docker(tmp_path)
    env_file = tmp_path / "office.env"
    output, code = _run_wizard(
        ["--pod", "testpod", "--tenant", "testtenant", "--env-file", str(env_file)],
        env=_env(tmp_path),
        answers=["", "", "", ""],  # blank telegram answer = No, same as setup.sh's own default
    )
    assert code == 0, output
    text = _env_file_text(env_file)
    assert "TELEGRAM_BOT_TOKEN" not in text
    assert "TELEGRAM_CHAT_ID" not in text


def test_non_interactive_never_prompts_for_oauth_or_telegram(tmp_path):
    _fake_docker(tmp_path)
    env_file = tmp_path / "office.env"
    proc = subprocess.run(
        [
            "bash", str(BOOTSTRAP),
            "--pod", "testpod", "--tenant", "testtenant", "--env-file", str(env_file),
            "--non-interactive",
        ],
        env=_env(tmp_path),
        capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    text = _env_file_text(env_file)
    assert "CLAUDE_OAUTH_TOKEN_DEFAULT" not in text
    assert "TELEGRAM_BOT_TOKEN" not in text
