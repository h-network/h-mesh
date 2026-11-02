"""Synthetic caller configuration for setup subprocess isolation checks."""

import os

import pytest


@pytest.fixture
def ambient_setup_env(monkeypatch, tmp_path):
    # Exercise isolation even when the approved runner starts with a clean
    # shell. These values must never become the subprocess's prompt defaults,
    # install destination, credentials, or state paths.
    ambient_bin = tmp_path / "ambient-bin"
    ambient_bin.mkdir()
    h_agent = ambient_bin / "h-agent"
    h_agent.write_text("#!/bin/sh\nexit 97\n")
    h_agent.chmod(0o755)
    bash_env = tmp_path / "must-not-be-sourced"
    bash_env.write_text("exit 97\n")
    monkeypatch.setenv("PATH", f"{ambient_bin}:{os.environ.get('PATH', '')}")
    for name, value in {
        "AGENTS": "ambient-agent",
        "DEFAULT_CLI": "codex",
        "ACCOUNTS": "ambient-account",
        "DEFAULT_ACCOUNT": "ambient-account",
        "AGENT_CLIS": "ambient-agent=agy",
        "AGENT_PROFILES": "ambient-agent=ambient-account",
        "AGENT_PROVIDERS": "ambient-agent=ambient-provider",
        "PREFIX": str(tmp_path / "ambient-prefix"),
        "H_MESH_STATE_DIR": str(tmp_path / "ambient-state"),
        "BASH_ENV": str(bash_env),
        "CLAUDE_OAUTH_TOKEN_DEFAULT": "synthetic-ambient-token",
    }.items():
        monkeypatch.setenv(name, value)
