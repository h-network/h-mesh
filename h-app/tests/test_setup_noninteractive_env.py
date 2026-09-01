"""setup.sh --non-interactive used to only read pod/tenant from env/flags --
every other wizard setting (agent roster, CLI/account choices, provider
config, TELEGRAM_*, API_TOKEN, per-profile OAuth tokens) was read via
TENANT_ENV_GET, which only ever looks at the persisted tenant config file,
never the live environment. That made non-interactive runs half-work by
accident: services.daemons.merged_daemon_env() layers live env on top of the
persisted file for a *running* daemon, so exporting e.g. TELEGRAM_BOT_TOKEN
+TELEGRAM_CHAT_ID before a non-interactive run did start the bot for that
one run -- but nothing was ever written to the tenant config, so a later
clean-shell run silently came up without it.

These tests exercise setup.sh's real --non-interactive path end to end
(subprocess, no pty needed -- unlike test_setup_wizard.py's interactive
tests) and assert on the persisted tenant config file, not just this run's
own daemon env, so a regression here is caught even if a single run happens
to "work by accident" again.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import redis

from services.daemons import enabled_daemon_modules
from services.tenant_config import read_tenant_env

REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP_SH = REPO_ROOT / "setup.sh"


@pytest.fixture
def noninteractive_env():
    redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    r = redis.Redis.from_url(redis_url)
    try:
        r.ping()
    except Exception:
        pytest.skip("Redis server not available at REDIS_URL")

    tmpdir = tempfile.mkdtemp(prefix="h_mesh_test_setup_noninteractive_")
    home_dir = os.path.join(tmpdir, "home")
    os.makedirs(home_dir, exist_ok=True)
    pod = f"testpod-{os.urandom(4).hex()}"
    tenant = f"testtenant-{os.urandom(4).hex()}"

    env = dict(os.environ)
    env["HOME"] = home_dir
    env["H_MESH_STATE_DIR"] = os.path.join(home_dir, ".h-mesh")
    env["PYTHONPATH"] = str(REPO_ROOT / "h-app")
    env["POD"] = pod
    env["TENANT"] = tenant
    env["REDIS_URL"] = redis_url

    # Scrub ambient tokens/telegram/api config so only what each test sets
    # explicitly is visible -- this office's own real env must not leak in.
    for k in list(env.keys()):
        if (k.startswith("CLAUDE_OAUTH_TOKEN_") or k == "CLAUDE_CODE_OAUTH_TOKEN"
                or k.startswith("TELEGRAM_") or k in ("API_TOKEN", "AGENTS", "DEFAULT_CLI",
                "ACCOUNTS", "DEFAULT_ACCOUNT", "AGENT_CLIS", "AGENT_PROFILES", "AGENT_PROVIDERS",
                "PROVIDER_LOCAL_URL", "PROVIDER_LOCAL_MODEL", "PROVIDER_LOCAL_KIND")):
            del env[k]

    ctx = {"tmpdir": tmpdir, "home_dir": home_dir, "pod": pod, "tenant": tenant, "env": env}
    yield ctx

    try:
        keys = r.keys(f"pod:{pod}:tenant:{tenant}:*") or []
        if keys:
            r.delete(*keys)
    except Exception:
        pass
    shutil.rmtree(tmpdir, ignore_errors=True)


def _run_setup(ctx: dict, extra_env: dict | None = None, timeout: float = 30) -> tuple[str, int]:
    env = dict(ctx["env"])
    env.update(extra_env or {})
    res = subprocess.run(
        [str(SETUP_SH), "--venv", sys.prefix, "--skip-install", "--skip-deps",
         "--no-daemons", "--non-interactive"],
        env=env, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
    )
    return res.stdout + res.stderr, res.returncode


def _read_persisted(ctx: dict, monkeypatch) -> dict:
    monkeypatch.setenv("H_MESH_STATE_DIR", ctx["env"]["H_MESH_STATE_DIR"])
    return read_tenant_env(ctx["tenant"])


def test_non_interactive_reads_the_full_wizard_setting_set_from_env_and_persists_it(noninteractive_env, monkeypatch):
    ctx = noninteractive_env
    output, code = _run_setup(ctx, {
        "AGENTS": "alice,bob",
        "DEFAULT_CLI": "codex",
        "ACCOUNTS": "default,work",
        "DEFAULT_ACCOUNT": "work",
        "AGENT_CLIS": "alice=claude",
        # ⚠ must genuinely differ from DEFAULT_ACCOUNT=work -- "only
        # exceptions travel" (see setup.sh) correctly drops an entry that
        # matches the default, same as the interactive wizard would.
        "AGENT_PROFILES": "bob=default",
        "CLAUDE_OAUTH_TOKEN_DEFAULT": "test-tok-default",
        "CLAUDE_OAUTH_TOKEN_WORK": "test-tok-work",
        "PROVIDER_LOCAL_URL": "http://10.0.0.5:8000",
        "PROVIDER_LOCAL_MODEL": "some-model",
        "PROVIDER_LOCAL_KIND": "vllm",
        "AGENT_PROVIDERS": "alice=local",
        "TELEGRAM_BOT_TOKEN": "fake-bot-token-env",
        "TELEGRAM_CHAT_ID": "555444333",
        "TELEGRAM_VOICE": "1",
    })
    assert code == 0, f"setup.sh exited {code}:\n{output}"
    # Never printed even for this env-sourced, entirely-fake test token.
    assert "test-tok-default" not in output
    assert "test-tok-work" not in output

    persisted = _read_persisted(ctx, monkeypatch)
    assert persisted.get("AGENTS") == "alice,bob"
    assert persisted.get("DEFAULT_CLI") == "codex"
    assert persisted.get("ACCOUNTS") == "default,work"
    assert persisted.get("DEFAULT_ACCOUNT") == "work"
    assert persisted.get("AGENT_CLIS") == "alice=claude"
    assert persisted.get("AGENT_PROFILES") == "bob=default"
    assert persisted.get("CLAUDE_OAUTH_TOKEN_DEFAULT") == "test-tok-default"
    assert persisted.get("CLAUDE_OAUTH_TOKEN_WORK") == "test-tok-work"
    assert persisted.get("AGENT_PROVIDERS") == "alice=local"
    assert persisted.get("PROVIDER_LOCAL_URL") == "http://10.0.0.5:8000"
    assert persisted.get("PROVIDER_LOCAL_MODEL") == "some-model"
    assert persisted.get("PROVIDER_LOCAL_KIND") == "vllm"
    assert persisted.get("TELEGRAM_BOT_TOKEN") == "fake-bot-token-env"
    assert persisted.get("TELEGRAM_CHAT_ID") == "555444333"
    assert persisted.get("TELEGRAM_VOICE") == "1"
    assert persisted.get("API_TOKEN"), "an API_TOKEN should have been auto-generated"

    modules = enabled_daemon_modules(persisted)
    assert "api" in modules and "telegram_bot" in modules


def test_non_interactive_explicit_api_token_env_is_persisted_verbatim_not_regenerated(noninteractive_env, monkeypatch):
    ctx = noninteractive_env
    output, code = _run_setup(ctx, {
        "TELEGRAM_BOT_TOKEN": "fake-bot-token",
        "TELEGRAM_CHAT_ID": "555444333",
        "API_TOKEN": "caller-supplied-api-token",
    })
    assert code == 0, f"setup.sh exited {code}:\n{output}"
    persisted = _read_persisted(ctx, monkeypatch)
    assert persisted.get("API_TOKEN") == "caller-supplied-api-token"


def test_non_interactive_this_run_converges_with_a_later_clean_shell_run(noninteractive_env, monkeypatch):
    # The exact bug the ticket described: run #1 exports Telegram config,
    # run #2 (a later "clean shell") exports nothing at all -- it must still
    # see the bot as configured, because run #1 persisted it, not just
    # handed it to its own daemon's live env.
    ctx = noninteractive_env
    output1, code1 = _run_setup(ctx, {
        "TELEGRAM_BOT_TOKEN": "fake-bot-token-run1",
        "TELEGRAM_CHAT_ID": "555444333",
    })
    assert code1 == 0, output1

    output2, code2 = _run_setup(ctx, {})  # a later, genuinely clean shell
    assert code2 == 0, output2
    assert "Telegram bot: enabled, chat id 555444333" in output2

    persisted = _read_persisted(ctx, monkeypatch)
    assert persisted.get("TELEGRAM_BOT_TOKEN") == "fake-bot-token-run1"
    assert persisted.get("TELEGRAM_CHAT_ID") == "555444333"


def test_non_interactive_precedence_env_wins_blank_keeps_persisted(noninteractive_env, monkeypatch):
    ctx = noninteractive_env
    # Run 1: persist DEFAULT_CLI=claude (the hardcoded default, with nothing
    # set in env yet).
    output1, code1 = _run_setup(ctx, {})
    assert code1 == 0, output1
    assert _read_persisted(ctx, monkeypatch).get("DEFAULT_CLI") == "claude"

    # Run 2: an explicit env var overrides the persisted value.
    output2, code2 = _run_setup(ctx, {"DEFAULT_CLI": "codex"})
    assert code2 == 0, output2
    assert _read_persisted(ctx, monkeypatch).get("DEFAULT_CLI") == "codex"

    # Run 3: env var unset again -- keeps what's now persisted ("codex"),
    # does not revert to the "claude" hardcoded default.
    output3, code3 = _run_setup(ctx, {})
    assert code3 == 0, output3
    assert _read_persisted(ctx, monkeypatch).get("DEFAULT_CLI") == "codex"


def test_non_interactive_partial_telegram_env_warns_and_persists_neither(noninteractive_env, monkeypatch):
    ctx = noninteractive_env
    output, code = _run_setup(ctx, {"TELEGRAM_BOT_TOKEN": "fake-bot-token-only"})
    assert code == 0, f"setup.sh exited {code}:\n{output}"
    assert "Telegram Bot Token and Chat ID are required" in output

    persisted = _read_persisted(ctx, monkeypatch)
    assert "TELEGRAM_BOT_TOKEN" not in persisted
    assert "TELEGRAM_CHAT_ID" not in persisted
    assert "API_TOKEN" not in persisted


def test_non_interactive_agy_with_non_default_account_env_falls_back_to_default(noninteractive_env, monkeypatch):
    ctx = noninteractive_env
    output, code = _run_setup(ctx, {
        "AGENTS": "worker1",
        "ACCOUNTS": "default,other",
        "AGENT_CLIS": "worker1=agy",
        "AGENT_PROFILES": "worker1=other",
    })
    assert code == 0, f"setup.sh exited {code}:\n{output}"
    assert "worker1 runs agy" in output
    assert "supports only one account" in output

    persisted = _read_persisted(ctx, monkeypatch)
    assert persisted.get("AGENT_CLIS") == "worker1=agy"
    # worker1's account exception must have been reset to default, not kept.
    assert "AGENT_PROFILES" not in persisted or "worker1" not in persisted.get("AGENT_PROFILES", "")


def test_non_interactive_names_which_settings_came_from_live_env(noninteractive_env, monkeypatch):
    # Not a guard -- purely informational, so a human running this by hand
    # can see what got pulled from their live shell. Names only, never a
    # value (including for the fake tokens this test itself sets).
    ctx = noninteractive_env
    output, code = _run_setup(ctx, {
        "DEFAULT_CLI": "codex",
        "API_TOKEN": "test-api-token-for-env-line",
    })
    assert code == 0, f"setup.sh exited {code}:\n{output}"
    assert "Persisting from live environment:" in output
    line = next(l for l in output.splitlines() if l.startswith("Persisting from live environment:"))
    assert "DEFAULT_CLI" in line
    assert "API_TOKEN" in line
    assert "test-api-token-for-env-line" not in output


def test_non_interactive_says_nothing_when_nothing_came_from_env(noninteractive_env, monkeypatch):
    ctx = noninteractive_env
    output, code = _run_setup(ctx, {})
    assert code == 0, f"setup.sh exited {code}:\n{output}"
    assert "Persisting from live environment:" not in output


def test_non_interactive_env_line_omits_a_value_a_second_run_reuses_from_persisted_config(noninteractive_env, monkeypatch):
    # A value that's already persisted and simply gets re-read back (no live
    # env override this run) must not be reported as "from live environment"
    # -- only a genuine env override counts.
    ctx = noninteractive_env
    output1, code1 = _run_setup(ctx, {"DEFAULT_CLI": "codex"})
    assert code1 == 0, output1
    assert "DEFAULT_CLI" in next(
        l for l in output1.splitlines() if l.startswith("Persisting from live environment:")
    )

    output2, code2 = _run_setup(ctx, {})
    assert code2 == 0, output2
    assert "Persisting from live environment:" not in output2
    assert _read_persisted(ctx, monkeypatch).get("DEFAULT_CLI") == "codex"
