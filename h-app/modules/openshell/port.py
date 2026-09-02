"""One-shot delivery against a long-lived OpenShell sandbox.

OpenShell exec starts a fresh process and returns when it exits; unlike tmux,
there is no persistent pane into which a message can be pasted. Conversation
continuity therefore comes from each CLI's resume flag and its session files
inside the sandbox.
"""

from __future__ import annotations

import base64
import os
import signal
import sys

import redis

from core.channels import DeadLetter, receive, send
from core.dispatch import delivery_lock
from core.keys import prefix
from core.logging import configure_logging
from lib.attachment_schema import validate_attachment_payload
from lib.board_interaction import add_ticket
from lib.reply_correlation import record_delivered

from .client import OpenShellClient, OpenShellUnavailable
from .headless import headless_command
from .naming import sandbox_name, workspace_name

_CODEX_AUTH_PATH = "/sandbox/.codex/auth.json"
_AGY_AUTH_PATH = "/sandbox/.gemini/antigravity-cli/antigravity-oauth-token"
_CREDENTIAL_FILE_PATHS = {"codex": _CODEX_AUTH_PATH, "agy": _AGY_AUTH_PATH}
_CREDENTIAL_ENV_VARS = {"codex": "CODEX_AUTH_JSON", "agy": "AGY_AUTH_JSON"}


def _agent_cli(r, pod: str, tenant: str, agent: str) -> str:
    raw = r.get(prefix(pod, tenant, agent=agent, resource="launch"))
    cli = raw.decode() if isinstance(raw, bytes) else raw
    return cli or "claude"


def _agent_profile(r, pod: str, tenant: str, agent: str) -> str | None:
    raw = r.get(prefix(pod, tenant, agent=agent, resource="profile"))
    return raw.decode() if isinstance(raw, bytes) else raw


def _profile_env_suffix(profile: str | None) -> str:
    return (profile or "default").upper().replace("-", "_")


def _sandbox_before_effect(client: OpenShellClient, sbx_name: str):
    """Resolve the sandbox before submission; failure here proves no effect began."""
    try:
        return client.get_sandbox(sbx_name)
    except OpenShellUnavailable as exc:
        raise DeadLetter(f"gateway_unavailable_before_submission: {exc}") from exc


def _write_credential_file(
    client: OpenShellClient, sandbox_id: str, path: str, content: bytes
) -> None:
    directory = path.rsplit("/", 1)[0]
    encoded = base64.b64encode(content)
    script = 'mkdir -p "$1" && base64 -d > "$2"'
    result = client.exec_sandbox(
        sandbox_id,
        ["/bin/sh", "-c", script, "sh", directory, path],
        stdin=encoded,
    )
    if result.exit_code != 0:
        raise OpenShellUnavailable(
            f"failed to write credential file {path!r}: {result.stderr or result.stdout}"
        )


def _wipe_credential_file(client: OpenShellClient, sandbox_id: str, path: str) -> None:
    """Best-effort removal, attempted even when the headless exec fails."""
    script = 'shred -u "$1" 2>/dev/null || rm -f "$1" 2>/dev/null || true'
    try:
        client.exec_sandbox(sandbox_id, ["/bin/sh", "-c", script, "sh", path])
    except OpenShellUnavailable:
        pass


def _exec_headless(
    client: OpenShellClient,
    sbx_name: str,
    cli: str,
    stdin_text: str,
    profile: str | None = None,
):
    """Run one resumed CLI invocation with credentials scoped to that exec.

    Claude's token exists only in the process environment. Codex and agy have
    no per-invocation environment auth path, so their credential file is
    written immediately before exec and wiped in a finally immediately after.
    Credentials must never be persisted in the sandbox creation environment.
    """
    try:
        command = headless_command(cli, resume=True)
    except ValueError as exc:
        raise DeadLetter(str(exc)) from exc
    ref = _sandbox_before_effect(client, sbx_name)
    stdin = stdin_text.encode("utf-8")

    if cli == "claude":
        token = os.environ.get(
            f"CLAUDE_OAUTH_TOKEN_{_profile_env_suffix(profile)}"
        )
        env = {"CLAUDE_CODE_OAUTH_TOKEN": token} if token else None
        return client.exec_sandbox(ref.id, command, stdin=stdin, env=env)

    path = _CREDENTIAL_FILE_PATHS.get(cli)
    env_name = _CREDENTIAL_ENV_VARS.get(cli)
    if path is None or env_name is None:
        return client.exec_sandbox(ref.id, command, stdin=stdin)

    credential = os.environ.get(f"{env_name}_{_profile_env_suffix(profile)}")
    if not credential:
        return client.exec_sandbox(ref.id, command, stdin=stdin)

    _write_credential_file(client, ref.id, path, credential.encode("utf-8"))
    try:
        return client.exec_sandbox(ref.id, command, stdin=stdin)
    finally:
        _wipe_credential_file(client, ref.id, path)


def _reply(
    r, pod: str, tenant: str, agent: str, destination: str, envelope: dict, result
) -> None:
    # Unlike a tmux agent, this reply is generated mechanically in the same
    # call that received the envelope -- no human or CLI chooses to
    # correlate it, so in_reply_to is set directly from what was just
    # delivered rather than exposed for an agent to name back. record_delivered
    # still runs first: deliver_api validates every in_reply_to against real
    # provenance regardless of how it was produced, and this path is no
    # exception.
    stream_id = envelope.get("stream_id")
    if stream_id:
        origin = envelope.get("l2", {}).get("source")
        record_delivered(r, pod=pod, tenant=tenant, agent=agent, stream_id=stream_id, source=origin)
    text = result.stdout if result.exit_code == 0 else result.stdout + result.stderr
    if not text.strip():
        return
    send(
        r,
        pod=pod,
        tenant=tenant,
        source=agent,
        destination=destination,
        payload={"text": text},
        kind="Message",
        correlation_id=envelope.get("stream_id"),
        module="openshell",
        in_reply_to=stream_id,
    )


def _deliver_text(
    r,
    pod: str,
    tenant: str,
    agent: str,
    envelope: dict,
    client: OpenShellClient,
    sbx_name: str,
    cli: str,
    profile: str | None,
    *,
    is_message: bool,
) -> None:
    source = envelope.get("l2", {}).get("source", "unknown")
    payload = envelope.get("payload", {})
    text = payload.get("text", "") if isinstance(payload, dict) else str(payload)
    prompt = f"[message from {source}] {text}" if is_message else text
    result = _exec_headless(client, sbx_name, cli, prompt, profile=profile)
    _reply(r, pod, tenant, agent, source, envelope, result)


def _write_attachment(
    client: OpenShellClient,
    sbx_name: str,
    stream_id: str,
    filename: str,
    content: bytes,
) -> str:
    """Write through a temporary path then atomically rename inside sandbox.

    Paths are shell positional arguments, never interpolated into script text.
    `/sandbox` is the sandbox home; h-mesh's host `/workdir` does not exist
    there and may not be writable.
    """
    ref = _sandbox_before_effect(client, sbx_name)
    target_dir = f"/sandbox/attachments/{stream_id}"
    final_path = f"{target_dir}/{filename}"
    temp_path = f"{target_dir}/.tmp.{os.urandom(8).hex()}"
    script = 'mkdir -p "$1" && base64 -d > "$2" && mv -f "$2" "$3"'
    result = client.exec_sandbox(
        ref.id,
        ["/bin/sh", "-c", script, "sh", target_dir, temp_path, final_path],
        stdin=base64.b64encode(content),
    )
    if result.exit_code != 0:
        raise DeadLetter(
            f"attachment write failed: {result.stderr or result.stdout}"
        )
    return final_path


def _deliver_attachment(
    r,
    pod: str,
    tenant: str,
    agent: str,
    envelope: dict,
    client: OpenShellClient,
    sbx_name: str,
    cli: str,
    profile: str | None,
) -> None:
    validated = validate_attachment_payload(envelope.get("payload"))
    stream_id = envelope.get("stream_id") or envelope.get("l2", {}).get("stream_id")
    if not stream_id or not isinstance(stream_id, str):
        raise DeadLetter("missing stream_id for attachment delivery")

    source = envelope.get("l2", {}).get("source", "unknown")
    final_path = _write_attachment(
        client, sbx_name, stream_id, validated.filename, validated.data
    )
    notice = (
        f"[attachment from {source}] saved to {final_path} "
        f"({validated.mime_type}, {len(validated.data)} bytes)"
    )
    if validated.caption:
        notice += f"\n[attachment caption] {validated.caption}"
    result = _exec_headless(client, sbx_name, cli, notice, profile=profile)
    _reply(r, pod, tenant, agent, source, envelope, result)


def deliver_openshell(
    r,
    pod: str,
    tenant: str,
    agent: str,
    timeout: int = 0,
    blocking: bool = False,
    client: OpenShellClient | None = None,
    **kwargs,
) -> None:
    """Deliver one queued envelope using an injected or real client."""
    cli = _agent_cli(r, pod, tenant, agent)
    profile = _agent_profile(r, pod, tenant, agent)
    sbx_name = sandbox_name(agent)
    owns_client = client is None
    client = client or OpenShellClient(workspace_name(pod, tenant))

    openers = {
        "Message": lambda env: _deliver_text(
                r, pod, tenant, agent, env, client, sbx_name, cli, profile,
                is_message=True,
            ),
        "Command": lambda env: _deliver_text(
                r, pod, tenant, agent, env, client, sbx_name, cli, profile,
                is_message=False,
            ),
        "AddTicket": lambda env: add_ticket(
            r=r, pod=pod, tenant=tenant, agent=agent, envelope=env
        ),
        "Attachment": lambda env: _deliver_attachment(
                r, pod, tenant, agent, env, client, sbx_name, cli, profile
            ),
    }
    try:
        receive(
            r,
            pod=pod,
            tenant=tenant,
            agent=agent,
            openers=openers,
            timeout=timeout,
            blocking=blocking,
            module="openshell",
        )
    finally:
        if owns_client:
            client.close()


def main(argv: list[str] | None = None) -> None:
    # First thing in the process, and only in the process: this is the entry
    # point, so it is the one place allowed to set the root logger's level.
    configure_logging()
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("usage: python -m modules.openshell.port <agent>", file=sys.stderr)
        sys.exit(1)
    agent = args[0]
    pod = os.environ["POD"]
    tenant = os.environ["TENANT"]
    redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")

    r = redis.Redis.from_url(redis_url)
    with delivery_lock(r, pod=pod, tenant=tenant, agent=agent):
        paused_key = prefix(pod, tenant, agent=agent, resource="paused")
        if r.get(paused_key):
            return
        deliver_openshell(r, pod=pod, tenant=tenant, agent=agent)


if __name__ == "__main__":
    main()
