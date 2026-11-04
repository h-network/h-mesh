"""Trust and server TLS settings survive whole-config writes and upgrades."""

import pytest

from services import upgrade
from services.daemons import merged_daemon_env
from services.tenant_config import read_tenant_env, write_tenant_env
from test_setup_noninteractive_env import noninteractive_env, _read_persisted, _run_setup
from test_upgrade_config import post_update_env


@pytest.mark.parametrize("source", ["environment", "persisted"])
def test_setup_keeps_tls_and_ca_through_clean_shell_reruns(noninteractive_env, monkeypatch, source):
    ctx = noninteractive_env
    monkeypatch.setenv("H_MESH_STATE_DIR", ctx["env"]["H_MESH_STATE_DIR"])
    settings = {"API_TLS_CERT": "/run/h-mesh/tls/api.crt", "API_TLS_KEY": "/run/h-mesh/tls/api.key",
                "SESSION_TLS_CERT": "/run/h-mesh/tls/session.crt", "SESSION_TLS_KEY": "/run/h-mesh/tls/session.key",
                "H_MESH_CA_CERT": "/run/h-mesh/tls/ca.crt", "H_MESH_ALLOW_PLAINTEXT": "1"}
    for key in settings:
        ctx["env"].pop(key, None)
    if source == "persisted":
        write_tenant_env(ctx["tenant"], settings)
    output, code = _run_setup(ctx, settings if source == "environment" else {})
    assert code == 0, output
    output, code = _run_setup(ctx)
    assert code == 0, output
    persisted = _read_persisted(ctx, monkeypatch)
    assert all(persisted.get(key) == value for key, value in settings.items())
    daemon_env = merged_daemon_env(ctx["tenant"], {})
    assert all(daemon_env[key] == value for key, value in settings.items())


def test_upgrade_preserves_ca_and_passes_it_to_restarted_daemons(post_update_env):
    config, calls = post_update_env
    settings = {"H_MESH_CA_CERT": "/run/h-mesh/tls/ca.crt",
                "API_TLS_CERT": "/run/h-mesh/tls/api.crt", "API_TLS_KEY": "/run/h-mesh/tls/api.key"}
    write_tenant_env(config.tenant, settings)
    config.env.update(merged_daemon_env(config.tenant, {}))
    upgrade._post_update(config)
    assert all(read_tenant_env(config.tenant)[key] == value for key, value in settings.items())
    assert calls[1]["env"]["H_MESH_CA_CERT"] == settings["H_MESH_CA_CERT"]
