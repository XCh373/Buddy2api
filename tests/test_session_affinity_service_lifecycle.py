import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import httpx
import pytest

import database as db
import session_identity
import offline_runner
from offline_runner import allow_loopback, isolated_port
from test_session_affinity import state, rows

REAL_CONNECT = socket.socket.connect
REAL_CONNECT_EX = socket.socket.connect_ex


def start_service(root, port):
    policy = root / 'service.sb'
    repo = Path(__file__).resolve().parents[1]
    # The interpreter's own stdlib lives under .workbuddy/binaries, not its auth stores.
    python_library = Path(sys.base_prefix).resolve()
    policy.write_text(f'''(version 1)(allow default)(deny network*)
(allow network-inbound (local ip "localhost:{port}"))
(allow network-outbound (remote ip "localhost:{port}"))
(deny file-read* (subpath "{os.environ['HOME']}"))
(allow file-read* (subpath "{repo}"))
(allow file-read* (subpath "{python_library}"))
(allow file-read* (subpath "{root}"))
(deny file-read* (literal "{repo / 'codebuddy_gateway.db'}") (literal "{repo / 'codebuddy_gateway.db.credentials.key'}"))
(deny file-write*)(allow file-write* (subpath "{root}"))
''')
    env = {'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','HOME':os.environ['HOME'],'LANG':'en_US.UTF-8',
           'CB_GATEWAY_DB_PATH':str(db.DB_PATH),'CB_GATEWAY_MASTER_KEY':'synthetic-affinity-only',
           'CB_AUTH_DIR':str(root / 'empty-auth'),'CB_GATEWAY_AUTO_IMPORT':'0','CB_GATEWAY_PROVIDERS':'workbuddy',
           'PYTHONDONTWRITEBYTECODE':'1'}
    log = tempfile.NamedTemporaryFile(mode='w', prefix=f'service-{port}-', suffix='.log', dir=root, delete=False)
    command = [sys.executable, str(repo / 'tests/affinity_service_child.py'), str(port)]
    if shutil.which('sandbox-exec'):
        command = ['/usr/bin/sandbox-exec', '-f', str(policy), *command]
    # The child also installs the fail-closed Python audit guard before server import.
    process = subprocess.Popen(command,
                               cwd=root, env=env, stdout=log, stderr=log)
    return process, log


def stop_service(process, log):
    if process.poll() is None:
        process.terminate()
        try: process.wait(timeout=6)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    log.close()


def test_isolated_service_start_bind_stop_restart_and_lifecycle(state, tmp_path, monkeypatch):
    cfg, caller, aids = state
    # Restore socket methods; the runner audit hook still denies all unregistered targets.
    monkeypatch.setattr(socket.socket, 'connect', REAL_CONNECT)
    monkeypatch.setattr(socket.socket, 'connect_ex', REAL_CONNECT_EX)
    root = tmp_path.resolve()
    db.set_settings({'session_affinity':cfg,'backend_url':'https://synthetic-upstream.invalid',
                     'keepalive_enabled':False,'auto_checkin_enabled':False})
    identity_headers = {'Authorization':'Bearer synthetic-caller','X-Buddy2api-Session-Id':'synthetic-lifecycle-session'}
    def request(client):
        response = client.post('/v1/chat/completions', headers=identity_headers,
                               json={'model':'auto','messages':[{'role':'user','content':'synthetic'}]})
        assert response.status_code == 200
        assert response.json()['choices'][0]['message']['content'] == 'mock-ok'
        return rows()
    def running(action, *, check_startup=False):
        port = isolated_port()
        process, log = start_service(root, port)
        try:
            with allow_loopback(port), httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False, timeout=3) as client:
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        pytest.fail('Isolated service exited before ready', pytrace=False)
                    try:
                        if client.get('/health').status_code == 200: break
                    except httpx.HTTPError: pass
                    time.sleep(.05)
                else: pytest.fail('Isolated service readiness timed out', pytrace=False)
                if check_startup:
                    # Cover the old startup test's stale browser credential and
                    # duplicate/different-port behavior using the same isolated DB.
                    client.cookies.set('cb_gw_admin_token', 'synthetic-stale')
                    assert client.get('/admin/settings').status_code == 200
                    for duplicate_port, expected in ((port, 'already running'), (isolated_port(), 'already in use')):
                        duplicate, duplicate_log = start_service(root, duplicate_port)
                        try:
                            code = duplicate.wait(timeout=12)
                        finally:
                            stop_service(duplicate, duplicate_log)
                        text = Path(duplicate_log.name).read_text()
                        assert (code == 0) == (duplicate_port == port)
                        assert expected in text
                        assert process.poll() is None
                    assert client.get('/admin/settings').status_code == 200
                return action(client)
        finally:
            stop_service(process, log)
            assert process.poll() is not None
    first = running(request, check_startup=True)[0]
    second = running(request, check_startup=True)[0]
    assert first['binding_key'] == second['binding_key'] and first['account_id'] == second['account_id']
    assert first['generation'] == second['generation']
    # Delete the bound account with the service stopped: cascade + a legal new assignment.
    db.delete_account(second['account_id'])
    after_delete = running(request)[0]
    assert after_delete['account_id'] != second['account_id']
    # Explicit domain/namespace change creates a new isolated binding.
    changed = {**cfg,'profiles':{str(caller):{'namespace':'changed'}}}
    db.set_setting(session_identity.SETTING, changed)
    assert len(running(request)) == 2
    # Expire durable state, restart and reassign through existing initial rules.
    with db.connection() as conn:
        conn.execute('UPDATE session_bindings SET expires_at=0')
        conn.commit()
    assert running(request)[0]['expires_at'] > 0
    db.delete_api_key(caller)
    assert not rows()
    def deleted_key(client):
        result = client.post('/v1/chat/completions',headers=identity_headers,
                             json={'messages':[{'role':'user','content':'synthetic'}]})
        assert result.status_code in {401,503}
        assert not rows()
    running(deleted_key)
    output = os.environ.get('BUDDY_AFFINITY_EVIDENCE_DIR')
    if output:
        Path(output,'service-lifecycle.json').write_text(json.dumps({'actual_server_main':True,
            'isolated_service_starts':6,'startup_control_processes':4,
            'duplicate_start_and_conflicting_port_verified':True,'stale_browser_cookie_verified':True,
            'same_identity_restored_original_account':True,'account_delete_verified':True,
            'key_delete_verified':True,'namespace_change_verified':True,'ttl_boundary_verified':True,
            'network':'registered loopback startup/health only; upstream HTTP mocked; other connect/DNS denied',
            'all_children_stopped':True},indent=2))
