"""Installed DSH lifecycle against isolated Buddy2api. Never production config."""
import asyncio
from contextlib import contextmanager
import hashlib
import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading

import httpx
import pytest

import server
import session_identity
from test_session_affinity_integration import wire, post, rows, REAL_CLIENT, KEY_HEADERS
from test_session_affinity import state
from offline_runner import allow_loopback

DSH = Path('/opt/homebrew/lib/node_modules/@deepseek-ai/dsh/lib/bin.js')
NODE = Path('/opt/homebrew/bin/node')


def fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest()[:16]


@contextmanager
def buddy_http_bridge():
    records = []
    original = session_identity.from_request
    parsed = []
    def observe(*args):
        try:
            result = original(*args)
        except session_identity.IdentityError:
            parsed.append('invalid')
            raise
        parsed.append(result.digest[:16] if result is not None else None)
        return result
    session_identity.from_request = observe
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            data = self.rfile.read(int(self.headers['Content-Length']))
            raw = [(k.encode('latin1'), v.encode('latin1')) for k, v in self.headers.raw_items()]
            async def call():
                async with REAL_CLIENT(transport=httpx.ASGITransport(app=server.app), base_url='http://synthetic') as client:
                    return await client.post(self.path, content=data, headers=raw)
            response = asyncio.run(call())
            identity_values = {}
            for name, value in self.headers.raw_items():
                if name.lower() in {'session_id','session-id','thread-id','x-session-id','x-session-affinity','x-buddy2api-session-id','x-buddy2api-session-namespace'}:
                    identity_values.setdefault(name.lower(),[]).append(fingerprint(value))
            candidate_ids=[value for name,value in self.headers.raw_items()
                if name.lower() in {'session_id','session-id','thread-id','x-buddy2api-session-id'} and value]
            assert all(value.encode() not in response.content for value in candidate_ids)
            event_types=[]
            for line in response.content.decode(errors='replace').splitlines():
                if line.startswith('event: '):event_types.append(line[7:])
            records.append({'response_identity_absent':True,'path':self.path,'event_types':event_types,'header_values': identity_values, 'headers': {k.lower(): fingerprint(v) for k, v in self.headers.raw_items()
                                        if k.lower() in {'session_id','session-id','thread-id','x-session-id','x-session-affinity','x-buddy2api-session-id','x-buddy2api-session-namespace'}},
                            'status': response.status_code, 'parsed_identity': parsed[-1] if parsed else None})
            self.send_response(response.status_code)
            self.send_header('Content-Type', response.headers.get('Content-Type', 'application/json'))
            self.send_header('Content-Length', str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)
    with allow_loopback(0, bind=True):
        service = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=service.serve_forever, daemon=True)
    worker.start()
    try:
        yield service.server_port, records
    finally:
        service.shutdown()
        service.server_close()
        worker.join(timeout=3)
        session_identity.from_request = original
        import sys
        if sys.exc_info()[0] is not None and os.environ.get('BUDDY_AFFINITY_EVIDENCE_DIR'):
            Path(os.environ['BUDDY_AFFINITY_EVIDENCE_DIR'],'client-failure-http.json').write_text(json.dumps(records,indent=2))
        assert not worker.is_alive()


def run_dsh(root, port, session=None, protocol='openai-responses', retention='short', model='auto'):
    patch = root / 'probe.patch.yml'
    patch.write_text(f'''- id: agent-default-model
  config:
    provider: affinity-probe
    model: {model}
- id: llm-pi-ai
  config:
    providers:
      affinity-probe:
        api: {protocol}
        baseURL: http://127.0.0.1:{port}/v1
        apiKeyEnv: SYNTHETIC_BUDDY_API_KEY
        cacheRetention: {retention}
        retryPolicy:
          mode: normal
          maxRetries: 0
        models:
          - id: {model}
            contextWindow: 32768
            maxTokens: 128
- id: session-title-llm
  disabled: true
''')
    policy = root / 'client.sb'
    policy.write_text(f'''(version 1)(allow default)(deny network*)
(allow network-outbound (remote ip "localhost:{port}"))
(deny file-read* (subpath "{os.environ['HOME']}"))
(deny file-write*)(allow file-write* (subpath "{root}"))
(allow file-read* (subpath "{root}"))
''')
    env = {'PATH':'/opt/homebrew/bin:/usr/bin:/bin', 'HOME':os.environ['HOME'], 'LANG':'en_US.UTF-8',
           'DSH_HOME':str(root / 'dsh-home'), 'SYNTHETIC_BUDDY_API_KEY':'synthetic-caller',
           'GIT_CONFIG_GLOBAL':'/dev/null', 'GIT_CONFIG_NOSYSTEM':'1'}
    command = ['/usr/bin/sandbox-exec','-f',str(policy),str(NODE),str(DSH),'--profile','headless','--patch',str(patch),'--json']
    if session is not None:
        command += ['--session-id',session]
    command += ['synthetic local verification']
    result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=35)
    if result.returncode:
        # Diagnostic contains only this synthetic isolated process; redact generated session IDs.
        import re
        diagnostic = re.sub(r'session-[a-zA-Z0-9-]+', '<session>', result.stderr[-1800:])
        pytest.fail('Isolated DSH startup/request failed: ' + diagnostic, pytrace=False)
    events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
    session_event = next(e for e in events if e.get('type') == 'session')
    # Do not publish the generated identity or session log; callers compare in memory.
    return session_event, events


@pytest.mark.skipif(not DSH.exists() or not NODE.exists() or not shutil.which('sandbox-exec'), reason='installed macOS DSH probe unavailable')
@pytest.mark.parametrize('protocol', ['openai-responses', 'openai-completions'])
@pytest.mark.parametrize('retention', ['short', 'none'])
def test_installed_dsh_direct_multiturn_resume_and_distinct_session(wire, tmp_path, protocol, retention):
    import database as db
    cfg, caller, aids, captures, failures = wire
    db.set_setting(session_identity.SETTING, {**cfg, 'profiles': {str(caller): {'native': session_identity.NATIVE}}})
    root = tmp_path.resolve()
    with buddy_http_bridge() as (port, records):
        first, _ = run_dsh(root, port, protocol=protocol, retention=retention)
        session = first.get('sessionId', first.get('id'))
        assert session is not None, 'DSH session event has no documented identity'
        second, _ = run_dsh(root, port, session, protocol, retention)
        third, _ = run_dsh(root, port, protocol=protocol, retention=retention)
    assert first == second
    assert first != third
    assert len(records) == 3 and all(r['status'] == 200 for r in records)
    native = protocol == 'openai-responses' and retention == 'short'
    if native:
        assert records[0]['headers']['session_id'] == records[1]['headers']['session_id']
        assert records[0]['headers']['session_id'] != records[2]['headers']['session_id']
        assert records[0]['parsed_identity'] == records[1]['parsed_identity'] != records[2]['parsed_identity']
    else:
        assert all(record['parsed_identity'] is None for record in records)
    before = len(rows())
    assert before == (2 if native else 0)
    # Explicit-contract control uses the actual logical ID obtained from the real client.
    headers = {**KEY_HEADERS, 'X-Buddy2api-Session-Id':session}
    for api in ('chat','responses'):
        assert asyncio.run(post(api, headers=headers)).status_code == 200
    assert len(rows()) == before + 1  # explicit domain is intentionally separate from native
    assert session not in json.dumps(captures)
    output = os.environ.get('BUDDY_AFFINITY_EVIDENCE_DIR')
    if output:
        Path(output, f'dsh-lifecycle-{protocol}-{retention}.json').write_text(json.dumps({
            'client':'DSH 0.2.0-rc.2 headless', 'protocol':protocol, 'cache':retention,
            'native_affinity':native, 'same_session_resume_verified':True,
            'different_session_verified':True, 'explicit_control_verified':True,
            'records':records, 'session_digests':[fingerprint(session),fingerprint(third.get('sessionId',third.get('id')))],
            'outbound_identity_absent':True}, indent=2))


@pytest.mark.skipif(not DSH.exists() or not NODE.exists() or not shutil.which('sandbox-exec'), reason='installed macOS DSH probe unavailable')
def test_installed_dsh_subagent_has_distinct_real_session(wire, tmp_path):
    import database as db
    cfg, caller, aids, captures, failures = wire
    db.set_setting(session_identity.SETTING, {**cfg, 'profiles': {str(caller): {'native': session_identity.NATIVE}}})
    def delegate(request):
        body = json.loads(request.content)
        available = {tool.get('function',{}).get('name') for tool in body.get('tools',[])}
        assert 'subagent' in available, 'DSH native subagent tool not available in this profile'
        failures.clear()
        arguments = json.dumps({'description':'Synthetic local child','prompt':'synthetic local child verification','run_in_background':False})
        delta = {'model':'auto','choices':[{'index':0,'delta':{'role':'assistant','tool_calls':[
            {'index':0,'id':'synthetic-call','type':'function','function':{'name':'subagent','arguments':arguments}}]},'finish_reason':None}]}
        done = {'model':'auto','choices':[{'index':0,'delta':{},'finish_reason':'tool_calls'}],
                'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}}
        return httpx.Response(200, content=('data: '+json.dumps(delta)+'\n\ndata: '+json.dumps(done)+'\n\ndata: [DONE]\n\n').encode())
    failures[aids[0]] = delegate
    with buddy_http_bridge() as (port, records):
        first, events = run_dsh(tmp_path.resolve(), port)
    assert any(event.get('type')=='tool_result' for event in events)
    assert len(records)==3 and all(record['status']==200 for record in records)
    identities = [record['parsed_identity'] for record in records]
    assert identities[0] == identities[2] and identities[0] != identities[1]
    assert all(identities) and len(rows())==2
    output = os.environ.get('BUDDY_AFFINITY_EVIDENCE_DIR')
    if output:
        Path(output,'dsh-subagent.json').write_text(json.dumps({'actual_client':'DSH 0.2.0-rc.2 headless',
            'real_spawn_tool_executed':True,'parent_child_parent_identity_verified':True,'records':records},indent=2))
