"""Account profile -> CLI config-dir/credential env resolution, and the
registry read that names which profile an agent uses.

A hired tmux agent's window environment (``modules/tmux/ops.py::window_env``)
and the one-shot SDK ports (``modules/claude_sdk/port.py``,
``modules/codex_sdk/port.py``, ...) all need the same mapping from an
account profile to ``CLAUDE_CONFIG_DIR``/``CODEX_HOME`` and a profile-scoped
OAuth token. This is the one place that owns it, so a third (and fourth)
caller never has to re-derive it independently.
"""

import os

from core.keys import prefix


def resolve_cli_profile_env(
    profile: str | None, *, home_dir: str | None = None
) -> dict[str, str]:
    """Resolve the env vars an account profile contributes for claude/codex.

    ``CLAUDE_CONFIG_DIR``/``CODEX_HOME`` only appear when a profile is named:
    an unprofiled/default agent reads ``~/.claude``/``~/.codex`` directly, so
    no override belongs in its env. Both are returned together and it's
    harmless for a caller that only cares about one (an sdk port that only
    calls Claude, say) to pass the whole dict through -- the other vendor's
    key is simply never read by that process.

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


def read_agent_profile(r, *, pod: str, tenant: str, agent: str) -> str | None:
    """Read the account profile an agent was hired against, or None (default)."""
    raw = r.get(prefix(pod, tenant, agent=agent, resource="profile"))
    return raw.decode() if isinstance(raw, bytes) else raw
