"""Account profile -> Claude/Codex config-dir and credential env resolution.

A hired tmux agent's window environment (``modules/tmux/ops.py::window_env``)
and a one-shot SDK port invocation (``modules/sdk/port.py``) both need the
same mapping from an account profile to ``CLAUDE_CONFIG_DIR``/``CODEX_HOME``
and a profile-scoped OAuth token. This is the one place that owns it, so a
third caller never has to re-derive it a third way.
"""

import os


def resolve_claude_profile_env(
    profile: str | None, *, home_dir: str | None = None
) -> dict[str, str]:
    """Resolve the env vars an account profile contributes for claude/codex.

    ``CLAUDE_CONFIG_DIR``/``CODEX_HOME`` only appear when a profile is named:
    an unprofiled/default agent reads ``~/.claude``/``~/.codex`` directly, so
    no override belongs in its env.

    ``CLAUDE_CODE_OAUTH_TOKEN`` is looked up under
    ``CLAUDE_OAUTH_TOKEN_<PROFILE-OR-DEFAULT>`` in this process's own
    environment regardless of whether a profile was named -- an unprofiled
    caller still resolves against ``CLAUDE_OAUTH_TOKEN_DEFAULT``, the token
    for the "default" account every hire without an explicit ``--profile``
    already uses implicitly.

    Absent is not empty: a missing token means the key is omitted from the
    returned dict entirely, never set to ``""`` -- an empty string would look
    to the CLI/SDK like a credential that fails, not like no credential.
    """
    home_dir = home_dir or os.environ.get("HOME", os.path.expanduser("~"))
    env: dict[str, str] = {}
    if profile:
        env["CLAUDE_CONFIG_DIR"] = f"{home_dir}/.claude-{profile}"
        env["CODEX_HOME"] = f"{home_dir}/.codex-{profile}"
    token = os.environ.get(
        f"CLAUDE_OAUTH_TOKEN_{(profile or 'default').upper().replace('-', '_')}"
    )
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return env
