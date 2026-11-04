import hashlib
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import redis

from services import upgrade
from services.daemons import DaemonConfig, DaemonError, enabled_daemon_modules
from services.tenant_config import ensure_api_token, read_tenant_env, tenant_env_path, write_tenant_env

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('initial,supplied', [
    (None, None), ('', None), ('stored-secret', 'different-ambient-secret'),
    (None, 'supplied-secret'),
], ids=['missing', 'empty', 'stored-wins', 'supplied'])
def test_upgrade_token_is_private_stable_and_preserves_other_settings(tmp_path, monkeypatch, initial, supplied):
    monkeypatch.setenv('H_MESH_STATE_DIR', str(tmp_path))
    values = {'AGENTS': 'architect', 'CUSTOM_SETTING': 'keep'}
    if initial is not None:
        values['API_TOKEN'] = initial
    write_tenant_env('proof', values)
    token = ensure_api_token('proof', supplied)
    assert token == (initial or supplied or token)
    if not initial and not supplied:
        assert re.fullmatch('[0-9a-f]{64}', token)
    assert ensure_api_token('proof', 'another-ambient-secret') == token
    assert read_tenant_env('proof') == {**values, 'API_TOKEN': token}
    assert tenant_env_path('proof').stat().st_mode & 0o777 == 0o600
    assert (tmp_path / 'proof' / 'env.lock').stat().st_mode & 0o777 == 0o600


def test_concurrent_upgrade_token_creation_keeps_one_identity(tmp_path, monkeypatch):
    monkeypatch.setenv('H_MESH_STATE_DIR', str(tmp_path))
    with ThreadPoolExecutor(max_workers=8) as executor:
        tokens = list(executor.map(lambda _: ensure_api_token('proof'), range(16)))
    assert len(set(tokens)) == 1
    assert read_tenant_env('proof')['API_TOKEN'] == tokens[0]
    # POSIX record locks share ownership inside a process: exercise the
    # mutex above and the independent-process file lock separately.
    code = ("import hashlib; from services.tenant_config import ensure_api_token; "
            "print(hashlib.sha256(ensure_api_token('processproof').encode()).hexdigest())")
    def create_in_process(_):
        result = subprocess.run([sys.executable, '-c', code], env=dict(os.environ),
                                text=True, capture_output=True, timeout=10, check=True)
        return result.stdout.strip()
    with ThreadPoolExecutor(max_workers=8) as executor:
        digests = list(executor.map(create_in_process, range(8)))
    assert len(set(digests)) == 1
    persisted = read_tenant_env('processproof')['API_TOKEN']
    assert hashlib.sha256(persisted.encode()).hexdigest() == digests[0]


def test_upgrade_rejects_multiline_token_without_writing_it(tmp_path, monkeypatch):
    monkeypatch.setenv('H_MESH_STATE_DIR', str(tmp_path))
    with pytest.raises(ValueError, match='single line'):
        ensure_api_token('proof', 'secret\nINJECTED=value')
    assert not tenant_env_path('proof').exists()


@pytest.fixture
def post_update_env(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('H_MESH_STATE_DIR', str(tmp_path / 'state'))
    config = DaemonConfig('testpod', 'proof', 'redis://127.0.0.1:1/0', 'proof',
                          tmp_path / 'tmux', str(tmp_path / 'tmux.sock'),
                          Path(sys.executable), tmp_path / 'run',
                          {'POD': 'testpod', 'TENANT': 'proof',
                           'API_BIND': '0.0.0.0', 'SESSION_BIND': '0.0.0.0'})
    calls = []
    monkeypatch.setattr(upgrade, 'persist_venv_on_path', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(upgrade, 'install_tmux_conf', lambda **_kwargs: None)
    monkeypatch.setattr(upgrade, 'install_statusline', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(upgrade, 'stop_daemons', lambda *_args, **_kwargs: calls.append('stop'))
    monkeypatch.setattr(upgrade, 'start_daemons', lambda **kwargs: calls.append(kwargs))
    return config, calls


@pytest.mark.parametrize('telegram', [False, True], ids=['api-only', 'telegram'])
@pytest.mark.parametrize('transport', ['loopback', 'plaintext', 'api-tls', 'session-tls'])
def test_post_update_uses_complete_new_daemon_config_and_safe_transport(post_update_env, capsys, telegram, transport):
    config, calls = post_update_env
    if telegram:
        config.env.update(TELEGRAM_BOT_TOKEN='fake-bot', TELEGRAM_CHAT_ID='123')
    if transport == 'plaintext':
        config.env['H_MESH_ALLOW_PLAINTEXT'] = '1'
    elif transport == 'api-tls':
        config.env.update(API_TLS_CERT='/cert', API_TLS_KEY='/key')
    elif transport == 'session-tls':
        config.env.update(SESSION_TLS_CERT='/cert', SESSION_TLS_KEY='/key')
    upgrade._post_update(config)
    assert calls[0] == 'stop'
    started = calls[1]
    assert started['daemon_modules'] == enabled_daemon_modules(config.env)
    assert {'api', 'interoffice_relay'} <= started['daemon_modules'].keys()
    assert ('session' in started['daemon_modules']) == telegram
    assert ('telegram_bot' in started['daemon_modules']) == telegram
    assert config.env['API_TOKEN'] == read_tenant_env('proof')['API_TOKEN']
    output = capsys.readouterr().out
    assert config.env['API_TOKEN'] not in output
    api_loopback = transport in ('loopback', 'session-tls')
    assert config.env['API_BIND'] == ('127.0.0.1' if api_loopback else '0.0.0.0')
    assert config.env['SESSION_BIND'] == ('127.0.0.1' if transport == 'loopback' else '0.0.0.0')
    assert ('API is configured for loopback only' in output) == api_loopback
    if api_loopback:
        assert 'API_TLS_CERT and API_TLS_KEY' in output
        assert 'H_MESH_ALLOW_PLAINTEXT=1' in output


def test_post_update_token_failure_never_stops_working_daemons(post_update_env, monkeypatch, capsys):
    config, calls = post_update_env
    def fail(*_args):
        raise OSError('must not echo a secret-bearing error')
    monkeypatch.setattr(upgrade, 'ensure_api_token', fail)
    with pytest.raises(SystemExit) as exc:
        upgrade._post_update(config)
    assert exc.value.code == 1
    assert calls == []
    output = capsys.readouterr().err
    assert 'cannot prepare private API configuration' in output
    assert 'secret-bearing' not in output


def test_post_update_start_failure_repeats_transport_enablement(post_update_env, monkeypatch, capsys):
    config, calls = post_update_env
    def fail(**_kwargs):
        raise DaemonError('api failed to start; check its log')
    monkeypatch.setattr(upgrade, 'start_daemons', fail)
    with pytest.raises(SystemExit) as exc:
        upgrade._post_update(config)
    assert exc.value.code == 1
    assert calls == ['stop']
    error = capsys.readouterr().err
    assert 'api failed to start' in error
    assert 'API_TLS_CERT and API_TLS_KEY' in error
    assert 'H_MESH_ALLOW_PLAINTEXT=1' in error
    assert config.env['API_TOKEN'] not in error


@pytest.mark.parametrize('failure', ['pull', 'install'])
def test_failed_update_never_enters_restart_or_exec(post_update_env, monkeypatch, failure):
    config, calls = post_update_env
    monkeypatch.setattr(upgrade, 'resolve_config', lambda _args: config)
    def fail(command):
        raise subprocess.CalledProcessError(7, command)
    monkeypatch.setattr(upgrade, '_run', fail)
    monkeypatch.setattr(upgrade.os, 'execve', lambda *_args: pytest.fail('failed update reexecuted'))
    args = ['--skip-install'] if failure == 'pull' else ['--skip-pull']
    with pytest.raises(SystemExit) as exc:
        upgrade.main(args)
    assert exc.value.code == 1
    assert calls == []
    assert not read_tenant_env('proof')


def test_upgrade_fresh_interpreter_ignores_stale_inventory_and_is_repeatable(managed_tmpdir):
    redis_url = os.environ.get('REDIS_URL', 'redis://127.0.0.1:6379/0')
    r = redis.Redis.from_url(redis_url)
    try:
        r.ping()
    except Exception:
        pytest.skip('Redis server not available at REDIS_URL')
    root = Path(managed_tmpdir('h_mesh_test_upgrade_fresh_'))
    (root / 'home').mkdir()
    pod = 'testpod-' + os.urandom(4).hex()
    tenant = 'testtenant-' + os.urandom(4).hex()
    env = dict(os.environ)
    for name in list(env):
        if name.startswith(('TELEGRAM_', 'API_TLS_', 'SESSION_TLS_')) or name in ('API_TOKEN', 'H_MESH_ALLOW_PLAINTEXT'):
            env.pop(name)
    env.update(HOME=str(root / 'home'), H_MESH_STATE_DIR=str(root / 'state'),
               H_MESH_RUN_DIR=str(root / 'run'), H_MESH_WORKDIR=str(root / 'workdir'),
               POD=pod, TENANT=tenant, REDIS_URL=redis_url,
               TMUX_SOCKET=str(root / 'tmux.sock'), TMUX_TMPDIR=str(root / 'tmux'),
               TMUX_SESSION=tenant, API_BIND='0.0.0.0', API_PORT='0',
               PYTHONPATH=str(REPO_ROOT / 'h-app'))
    args = ['--pod', pod, '--tenant', tenant, '--venv', sys.prefix,
            '--skip-pull', '--skip-install']
    wrapper = '''
import sys
from services import upgrade
# Simulate objects imported before an update: a stale parent must never
# choose the daemon set or execute its own restart phase afterward.
upgrade.enabled_daemon_modules = lambda env: {"switch": "core.service"}
upgrade._post_update = lambda config: sys.exit(91)
upgrade.main(sys.argv[1:])
'''
    config_path = root / 'state' / tenant / 'env'
    def run(command, run_env):
        res = subprocess.run(command, env=run_env, capture_output=True, text=True, timeout=30)
        assert res.returncode == 0, res.stdout + res.stderr
        return res.stdout + res.stderr
    def values():
        return dict(line.split('=', 1) for line in config_path.read_text().splitlines() if '=' in line)
    def running():
        result = {}
        for p in (root / 'run').glob('*.pid'):
            pid = int(p.read_text())
            os.kill(pid, 0)
            result[p.stem] = pid
        return result
    try:
        output = run([sys.executable, '-c', wrapper, *args], env)
        token = values()['API_TOKEN']
        assert token not in output
        assert config_path.stat().st_mode & 0o777 == 0o600
        assert 'Starting fresh post-update interpreter' in output
        assert 'API is configured for loopback only' in output
        first = running()
        expected = {'switch', 'tmux_reconciler', 'watchdog', 'api', 'interoffice_relay'}
        assert set(first) == expected
        api_env = Path(f'/proc/{first["api"]}/environ').read_bytes().split(b'\0')
        assert b'API_BIND=127.0.0.1' in api_env
        # An ambient token from another office cannot rotate this identity.
        output = run([sys.executable, '-m', 'services.upgrade', *args],
                     {**env, 'API_TOKEN': 'unrelated-ambient-test-token'})
        assert values()['API_TOKEN'] == token
        assert token not in output
        second = running()
        assert set(second) == expected
        assert all(first[name] != second[name] for name in expected)
        for pid in first.values():
            stat = Path(f'/proc/{pid}/stat')
            if stat.exists():
                assert stat.read_text().rsplit(') ', 1)[1].split()[0] == 'Z'
    finally:
        keys = list(r.scan_iter(match=f'pod:{pod}:tenant:{tenant}:*'))
        if keys:
            r.delete(*keys)
