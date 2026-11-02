"""Optional vendor-model settings, from setup persistence to launch argv."""

import json
import sys

import pytest

from _setup_test_env import ambient_setup_env
from modules.tmux import ops
from modules.tmux import reconciler as reconciler_module
from modules.tmux.reconciler import TmuxReconciler
from services.daemons import merged_daemon_env
from test_setup_noninteractive_env import noninteractive_env, _read_persisted, _run_setup
from test_setup_wizard import DEFAULT_PATH_ANSWERS, REPO_ROOT, _run_wizard, wizard_env


@pytest.mark.parametrize("cli,resume,expected", [
    ("claude", False, ["h-agent", "claude", "--model", "chosen-model"]),
    ("claude", True, ["h-agent", "claude", "--continue", "--model", "chosen-model"]),
    ("codex", False, ["h-agent", "codex", "-c", 'model="chosen-model"']),
    ("codex", True, ["h-agent", "codex", "resume", "--last", "-c", 'model="chosen-model"']),
    ("agy", False, ["h-agent", "agy", "--model", "chosen-model"]),
    ("agy", True, ["h-agent", "agy", "--continue", "--model", "chosen-model"]),
], ids=["claude-fresh", "claude-resume", "codex-fresh", "codex-resume", "agy-fresh", "agy-resume"])
def test_vendor_model_argv(cli, resume, expected):
    assert ops.start_agent_command(cli, resume=resume, model="chosen-model") == expected


def test_codex_model_override_is_a_quoted_literal():
    model = 'vendor/odd"model\\name;$(false)'
    command = ops.start_agent_command("codex", model=model)
    assert command[:3] == ["h-agent", "codex", "-c"]
    assert json.loads(command[3].removeprefix("model=")) == model


@pytest.mark.parametrize("default,overrides,provider,expected", [
    ("tenant-model", "", None, "tenant-model"),
    ("tenant-model", "bob=other,alice=agent-model", None, "agent-model"),
    ("", "bob=other", None, None),
    ("tenant-model", "alice=agent-model", {"model": "local-model"}, None),
], ids=["default", "override", "absent", "provider"])
def test_model_selection(monkeypatch, default, overrides, provider, expected):
    monkeypatch.setenv("DEFAULT_MODEL", default)
    monkeypatch.setenv("AGENT_MODELS", overrides)
    reconciler = TmuxReconciler("p", "t", "unused")
    assert reconciler.get_agent_model("alice", provider) == expected


@pytest.mark.parametrize("initial", [True, False], ids=["session", "window"])
@pytest.mark.parametrize("local_provider", [False, True], ids=["vendor", "provider"])
def test_persisted_model_reaches_both_launch_paths(
    noninteractive_env, monkeypatch, tmp_path, initial, local_provider,
):
    ctx = noninteractive_env
    output, code = _run_setup(ctx, {
        "AGENTS": "alice", "DEFAULT_MODEL": "tenant-model", "AGENT_MODELS": "alice=agent-model",
    })
    assert code == 0, output
    _read_persisted(ctx, monkeypatch)
    for key, value in merged_daemon_env(ctx["tenant"], {}).items():
        monkeypatch.setenv(key, value)

    calls = []
    def fake_tmux(*args, **kwargs):
        if args[0] == "has-session":
            return 1, "", ""
        if args[0] == "new-session":
            calls.append(list(args))
        return 0, "", ""

    def fake_window(*args, command, **kwargs):
        calls.append(command)
        return 0, "", ""

    monkeypatch.setattr(ops, "run_tmux", fake_tmux)
    monkeypatch.setattr(ops, "create_window", fake_window)
    monkeypatch.setattr(reconciler_module, "members", lambda *a, **k: {})
    monkeypatch.setattr(reconciler_module, "window_env", lambda *a, **k: [])
    monkeypatch.setattr(reconciler_module, "write_agent_guide", lambda *a, **k: None)
    monkeypatch.setattr(reconciler_module, "get_agent_workdir", lambda *a: str(tmp_path / "alice"))
    reconciler = TmuxReconciler("p", ctx["tenant"], "unused")
    monkeypatch.setattr(reconciler, "log_window_created", lambda *a: None)
    provider = {"url": "http://127.0.0.1:1", "model": "local-model"} if local_provider else None
    if initial:
        reconciler.ensure_server_and_session(object(), initial_window="alice", cli="claude",
                                             provider=provider, resume=False)
    else:
        assert reconciler.create_window(object(), "alice", cli="claude", provider=provider, resume=False)
    assert len(calls) == 1
    command = calls[0][calls[0].index("h-agent"):]
    expected = ["h-agent", "claude"]
    if not local_provider:
        expected += ["--model", "agent-model"]
    assert command == expected


def test_noninteractive_models_round_trip_and_env_precedence(noninteractive_env, monkeypatch):
    ctx = noninteractive_env
    output, code = _run_setup(ctx, {
        "AGENTS": "alice,bob", "DEFAULT_MODEL": "tenant-model", "AGENT_MODELS": "bob=other-model",
    })
    assert code == 0, output
    for extra in ({}, {"DEFAULT_MODEL": "new-model"}, {"DEFAULT_MODEL": ""}):
        output, code = _run_setup(ctx, extra)
        assert code == 0, output
    persisted = _read_persisted(ctx, monkeypatch)
    assert persisted["DEFAULT_MODEL"] == "new-model"
    assert persisted["AGENT_MODELS"] == "bob=other-model"


def test_noninteractive_no_model_keeps_cli_defaults(noninteractive_env, monkeypatch):
    ctx = noninteractive_env
    output, code = _run_setup(ctx, {})
    assert code == 0, output
    persisted = _read_persisted(ctx, monkeypatch)
    assert "DEFAULT_MODEL" not in persisted
    assert "AGENT_MODELS" not in persisted
    for cli in ("claude", "codex", "agy"):
        assert ops.start_agent_command(cli) == ["h-agent", cli]


@pytest.mark.parametrize("value", ["alice:model", "=model", "alice="],
                         ids=["separator", "empty-agent", "empty-model"])
def test_noninteractive_model_map_refuses_malformed_entries(noninteractive_env, value):
    output, code = _run_setup(noninteractive_env, {"AGENTS": "alice", "AGENT_MODELS": value})
    assert code != 0, output
    assert "AGENT_MODELS" in output and "malformed" in output


@pytest.mark.parametrize("settings", [
    {"DEFAULT_MODEL": "model\nAPI_TOKEN=injected"},
    {"DEFAULT_MODEL": "model,other"},
    {"AGENT_MODELS": "alice=model name"},
], ids=["line", "comma", "space"])
def test_model_ids_refuse_config_delimiters(noninteractive_env, settings):
    output, code = _run_setup(noninteractive_env, {"AGENTS": "alice", **settings})
    assert code != 0, output


def test_wizard_models_keep_exceptions_when_default_changes(wizard_env, monkeypatch):
    ctx = wizard_env
    args = ["--venv", sys.prefix, "--skip-install", "--skip-deps", "--no-daemons"]
    answers = [
        "", "", "2", "", "observer",  # roster
        "", "", "", "observer", "codex",  # accounts / CLI exception
        "tenant-model", "observer", "agent-model",  # vendor models
        "", "",  # provider / Telegram
    ]
    output, code = _run_wizard(str(REPO_ROOT), args, ctx["env"], answers)
    assert code == 0, output
    assert "Model examples for claude" in output
    assert "Model example for codex" in output
    rerun = list(DEFAULT_PATH_ANSWERS)
    rerun[7] = "new-model"
    output, code = _run_wizard(str(REPO_ROOT), args, ctx["env"], rerun)
    assert code == 0, output
    output, code = _run_wizard(str(REPO_ROOT), args, ctx["env"], DEFAULT_PATH_ANSWERS)
    assert code == 0, output
    monkeypatch.setenv("H_MESH_STATE_DIR", ctx["env"]["H_MESH_STATE_DIR"])
    persisted = merged_daemon_env(ctx["tenant"], {})
    assert persisted["DEFAULT_MODEL"] == "new-model"
    assert persisted["AGENT_MODELS"] == "observer=agent-model"
