"""Optional isolated Magpie process, forwarding into the actual Buddy2api app."""
from contextlib import contextmanager
import json
import http.server
import os
from pathlib import Path
import plistlib
import shutil
import socket
import subprocess
import time
import threading

import httpx
import pytest

import database as db
import session_identity
from offline_runner import allow_loopback, isolated_port
from test_session_affinity import state
from test_session_affinity_integration import wire, post, rows, KEY_HEADERS
from test_session_affinity_client_path import buddy_http_bridge, run_dsh, fingerprint, NODE, DSH

REAL_CONNECT = socket.socket.connect
REAL_CONNECT_EX = socket.socket.connect_ex
BINARY = Path(os.environ.get('BUDDY_AFFINITY_MAGPIE_BINARY','/nonexistent/optional-magpie-not-configured'))


@contextmanager
def isolated_magpie(root, destination, style, enabled):
    port = isolated_port()
    config = root / 'config' / 'magpie'
    config.mkdir(parents=True)
    protocol = 'chat' if style=='chat' else 'responses'
    config.joinpath('providers.json').write_text(json.dumps({'providers':[{
        'id':'affinity-probe','name':'Synthetic Buddy2api','key':'synthetic-caller',
        protocol:f'http://127.0.0.1:{destination}/v1','baseAPI':protocol,'models':['auto'],
        'proxy':'direct','buddy2apiIdentity':enabled}]}))
    config.joinpath('settings.json').write_text(json.dumps({'port':port,'proxy':'direct','noAccountDiscovery':True,
        'noAutoUpdate':True,'gatewayMode':'on','workbuddyCheckin':False,'traeCheckin':False,
        'qoderCheckin':False,'minimaxCheckin':False}))
    config.joinpath('plugins.yml').write_text('preset: essentials\n')
    for name in ('data','cache','tmp'): root.joinpath(name).mkdir()
    policy = root / 'proxy.sb'
    # The compiled binary is a task artifact; installed binary is outside HOME.
    allowed_binary = f'(allow file-read* (literal "{BINARY.resolve()}"))' if str(BINARY.resolve()).startswith(os.environ['HOME']) else ''
    policy.write_text(f'''(version 1)(allow default)(deny network*)
(allow network-inbound (local ip "localhost:{port}"))
(allow network-outbound (remote ip "localhost:{destination}"))
(deny file-read* (subpath "{os.environ['HOME']}"))
(allow file-read* (subpath "{root}")){allowed_binary}
(deny file-write*)(allow file-write* (subpath "{root}"))
''')
    env = {'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','HOME':os.environ['HOME'],'LANG':'en_US.UTF-8',
        'TMPDIR':str(root/'tmp'),'XDG_CONFIG_HOME':str(root/'config'),'XDG_DATA_HOME':str(root/'data'),
        'XDG_CACHE_HOME':str(root/'cache'),'MAGPIE_ADDR':f'127.0.0.1:{port}','MAGPIE_NO_ACCOUNTS':'1','NO_PROXY':'127.0.0.1'}
    with (root/'proxy.log').open('w') as log:
        process = subprocess.Popen(['/usr/bin/sandbox-exec','-f',str(policy),str(BINARY),'serve'],
            cwd=root,env=env,stdout=log,stderr=log)
        try:
            with allow_loopback(port):
                deadline = time.monotonic()+12
                while time.monotonic()<deadline:
                    if process.poll() is not None: pytest.fail('Isolated Magpie exited',pytrace=False)
                    try:
                        with socket.create_connection(('127.0.0.1',port),timeout=.1): break
                    except OSError: time.sleep(.05)
                else: pytest.fail('Isolated Magpie readiness timed out',pytrace=False)
                yield port
        finally:
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=6)
                except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=3)
            assert process.poll() is not None


@pytest.mark.skipif(not BINARY.exists() or not shutil.which('sandbox-exec'),reason='optional isolated Magpie binary unavailable')
@pytest.mark.parametrize('style',['chat','responses','chat-to-responses'])
@pytest.mark.parametrize('enabled',[False,True])
def test_magpie_explicit_identity_chain(wire,tmp_path,monkeypatch,style,enabled):
    cfg,caller,aids,captures,failures = wire
    monkeypatch.setattr(socket.socket,'connect',REAL_CONNECT)
    monkeypatch.setattr(socket.socket,'connect_ex',REAL_CONNECT_EX)
    certificates = []
    with buddy_http_bridge() as (destination,records):
        with isolated_magpie(tmp_path.resolve(),destination,style,enabled) as port:
            with httpx.Client(base_url=f'http://127.0.0.1:{port}',trust_env=False,timeout=8) as client:
                path='/v1/responses' if style=='responses' else '/v1/chat/completions'
                body={'model':'affinity-probe/auto',**({'input':'synthetic'} if style=='responses' else {'messages':[{'role':'user','content':'synthetic'}]})}
                for label,values in [('A1',['synthetic-A']),('A2',['synthetic-A']),('B',['synthetic-B']),
                                     ('namespace',['synthetic-A']),('missing',[]),('duplicate',['synthetic-A','synthetic-B']),('invalid',['bad,value']),('namespace-only',[]),('duplicate-namespace',['synthetic-A'])]:
                    headers=[('Authorization','Bearer magpie'),('X-Unrelated','must-not-pass')]
                    headers += [('X-Buddy2api-Session-Id',v) for v in values]
                    if label in {'namespace','namespace-only','duplicate-namespace'}:
                        headers.append(('X-Buddy2api-Session-Namespace','default'))
                    if label=='duplicate-namespace':
                        headers.append(('X-Buddy2api-Session-Namespace','default'))
                    before=rows()
                    response=client.post(path,headers=headers,json=body)
                    record=records[-1]
                    certificates.append({'case':label,'client_identity_hashes':[fingerprint(v) for v in values],
                                         'forwarded':record,'http_status':response.status_code})
                    if enabled and label in {'duplicate','invalid','namespace-only','duplicate-namespace'}:
                        assert response.status_code==400
                        assert record['parsed_identity']=='invalid'
                        assert rows()==before
                    else:
                        assert response.status_code==200
                        if enabled and values:
                            assert record['headers'].get('x-buddy2api-session-id') is not None
                            assert record['parsed_identity'] is not None
                            if label=='namespace':
                                assert record['headers'].get('x-buddy2api-session-namespace') is not None
                        else:
                            assert record['parsed_identity'] is None
                            assert rows()==before
                if enabled:
                    assert certificates[0]['forwarded']['parsed_identity']==certificates[1]['forwarded']['parsed_identity']
                    assert certificates[0]['forwarded']['parsed_identity']!=certificates[2]['forwarded']['parsed_identity']
                    assert len(rows())==2
                else: assert not rows()
    for _,headers,body in captures:
        assert 'x-unrelated' not in headers
        assert 'x-buddy2api-session-id' not in headers
        assert '_session_affinity' not in body
    output=os.environ.get('BUDDY_AFFINITY_EVIDENCE_DIR')
    if output:
        Path(output,f'magpie-chain-{style}-{enabled}.json').write_text(json.dumps({'style':style,'enabled':enabled,
            'binary_label':BINARY.name,'cases':certificates,'upstream_identity_absent':True,'child_stopped':True},indent=2))


@pytest.mark.skipif(not BINARY.exists() or not DSH.exists() or not shutil.which('sandbox-exec'),reason='optional installed client/proxy unavailable')
def test_stock_dsh_proxy_lifecycle_records_explicit_identity_blocker(wire,tmp_path,monkeypatch):
    cfg,caller,aids,captures,failures=wire
    monkeypatch.setattr(socket.socket,'connect',REAL_CONNECT)
    monkeypatch.setattr(socket.socket,'connect_ex',REAL_CONNECT_EX)
    db.set_setting(session_identity.SETTING,{**cfg,'profiles':{str(caller):{'native':session_identity.NATIVE}}})
    client_records=[]
    with buddy_http_bridge() as (destination,forwarded):
        proxy_root=tmp_path/'proxy';proxy_root.mkdir()
        with isolated_magpie(proxy_root.resolve(),destination,'responses',True) as port:
            class Tap(http.server.BaseHTTPRequestHandler):
                def log_message(self,*args):pass
                def do_POST(self):
                    raw=self.rfile.read(int(self.headers['Content-Length']))
                    client_records.append({k.lower():fingerprint(v) for k,v in self.headers.raw_items()
                        if k.lower() in {'session_id','x-buddy2api-session-id'}})
                    with httpx.Client(base_url=f'http://127.0.0.1:{port}',trust_env=False,timeout=8) as client:
                        response=client.post(self.path,content=raw,headers=list(self.headers.raw_items()))
                    self.send_response(response.status_code)
                    self.send_header('Content-Type',response.headers.get('Content-Type','application/json'))
                    self.send_header('Content-Length',str(len(response.content)));self.end_headers()
                    self.wfile.write(response.content)
            with allow_loopback(0,bind=True):tap=http.server.ThreadingHTTPServer(('127.0.0.1',0),Tap)
            worker=threading.Thread(target=tap.serve_forever,daemon=True);worker.start()
            client_root=tmp_path/'client';client_root.mkdir()
            try:
                first,_=run_dsh(client_root.resolve(),tap.server_port,model='affinity-probe/auto')
                sid=first['sessionId']
                second,_=run_dsh(client_root.resolve(),tap.server_port,sid,model='affinity-probe/auto')
                third,_=run_dsh(client_root.resolve(),tap.server_port,model='affinity-probe/auto')
            finally:
                tap.shutdown();tap.server_close();worker.join(timeout=3)
    assert len(client_records)==len(forwarded)==3
    assert client_records[0]['session_id']==client_records[1]['session_id']!=client_records[2]['session_id']
    assert all('x-buddy2api-session-id' not in r for r in client_records)
    assert all(r['parsed_identity'] is None for r in forwarded)
    assert not rows()
    output=os.environ.get('BUDDY_AFFINITY_EVIDENCE_DIR')
    if output:
        Path(output,'stock-dsh-magpie-blocker.json').write_text(json.dumps({'client':'DSH 0.2.0-rc.2 headless',
            'lifecycle_stable_native_identity':True,'client_inputs':client_records,'buddy_received':forwarded,
            'binding_count':0,'target_path_status':'PARTIAL_WITH_BLOCKERS',
            'reason':'stock DSH sends native session_id only; proxy contract forwards only explicit Buddy2api fields; client adapter change not authorized'},indent=2))

@contextmanager
def identity_tap(destination):
    """Observe only redacted candidate fields before the optional proxy."""
    inputs=[]
    class Tap(http.server.BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            body=self.rfile.read(int(self.headers['Content-Length']))
            inputs.append({k.lower():fingerprint(v) for k,v in self.headers.raw_items()
                if k.lower() in {'session_id','session-id','thread-id','x-buddy2api-session-id'}})
            with httpx.Client(base_url=f'http://127.0.0.1:{destination}',trust_env=False,timeout=8) as client:
                result=client.post(self.path,content=body,headers=list(self.headers.raw_items()))
            self.send_response(result.status_code)
            self.send_header('Content-Type',result.headers.get('Content-Type','application/json'))
            self.send_header('Content-Length',str(len(result.content)));self.end_headers()
            self.wfile.write(result.content)
    with allow_loopback(0,bind=True):tap=http.server.ThreadingHTTPServer(('127.0.0.1',0),Tap)
    worker=threading.Thread(target=tap.serve_forever,daemon=True);worker.start()
    try:yield tap.server_port,inputs
    finally:
        tap.shutdown();tap.server_close();worker.join(timeout=3)
        assert not worker.is_alive()


@pytest.mark.skipif(not BINARY.exists() or not shutil.which('sandbox-exec'),reason='optional isolated Magpie unavailable')
@pytest.mark.parametrize('client_name',['codex','pi'])
def test_actual_other_client_proxy_identity_lifecycle(wire,tmp_path,monkeypatch,client_name,caplog):
    from test_session_affinity_other_clients import run_codex,run_pi,CODEX,PI
    if not (CODEX if client_name=='codex' else PI).exists():pytest.skip('optional installed CLI unavailable')
    cfg,caller,*_=wire
    monkeypatch.setattr(socket.socket,'connect',REAL_CONNECT)
    monkeypatch.setattr(socket.socket,'connect_ex',REAL_CONNECT_EX)
    native=session_identity.CODEX_NATIVE if client_name=='codex' else session_identity.NATIVE
    db.set_setting(session_identity.SETTING,{**cfg,'profiles':{str(caller):{'native':native}}})
    with buddy_http_bridge() as (destination,received):
        proxy_root=tmp_path/'proxy';proxy_root.mkdir()
        with isolated_magpie(proxy_root.resolve(),destination,'responses',True) as proxy_port:
            with identity_tap(proxy_port) as (port,inputs):
                root=tmp_path/'client';root.mkdir()
                run=run_codex if client_name=='codex' else run_pi
                a,_=run(root.resolve(),port,model='affinity-probe/auto')
                same,_=run(root.resolve(),port,a,model='affinity-probe/auto')
                b,_=run(root.resolve(),port,model='affinity-probe/auto')
                child,_=run(root.resolve(),port,a,fork=True,model='affinity-probe/auto')
    assert a==same and len({a,b,child})==3
    assert len(inputs)==len(received)==4
    field='session-id' if client_name=='codex' else 'session_id'
    assert inputs[0][field]==inputs[1][field]!=inputs[2][field]
    assert inputs[0][field]==fingerprint(a)
    assert all('x-buddy2api-session-id' not in r for r in inputs)
    if client_name=='codex':
        assert received[0]['parsed_identity']==received[1]['parsed_identity']
        assert len({r['parsed_identity'] for r in received})==3
        assert all(r['parsed_identity'] is not None for r in received)
        assert len(rows())==3
    else:
        assert all(r['parsed_identity'] is None for r in received)
        assert not rows()
    assert all(value not in json.dumps(wire[3])+caplog.text for value in [a,b,child])
    output=os.environ.get('BUDDY_AFFINITY_EVIDENCE_DIR')
    if output:Path(output,f'{client_name}-magpie-lifecycle.json').write_text(json.dumps({
        'client':client_name,'client_inputs':inputs,'buddy_received':received,
        'native_affinity':client_name=='codex','binding_count':len(rows()),
        'lifecycle':'fresh, resume, fresh, fork; fork is not a spawned subagent',
        'explicit_header_sent':False,'child_stopped':True,'outbound_and_logs_identity_absent':True},indent=2))
