"""Actual installed CLI lifecycle; no production profile, token or model service."""
import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess

import pytest

import database as db
import session_identity

from test_session_affinity import state, rows
from test_session_affinity_integration import wire, post, KEY_HEADERS
from test_session_affinity_client_path import buddy_http_bridge, fingerprint, NODE

CODEX=Path('/opt/homebrew/Caskroom/codex/0.162.1/bin/codex')
PI=Path('/opt/homebrew/lib/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js')


def execute_client(root,port,command,extra_env):
    policy=root/'client.sb'
    (root/'tmp').mkdir(exist_ok=True)
    policy.write_text(f'''(version 1)(allow default)(deny network*)
(allow network-outbound (remote ip "localhost:{port}"))
(allow network* (local unix-socket (path-prefix "{root}")))
(allow network-outbound (remote unix-socket (path-prefix "{root}")))
(deny file-read* (subpath "{os.environ['HOME']}"))
(deny file-write*)(allow file-write* (subpath "{root}"))
(allow file-read* (subpath "{root}"))
''')
    env={'NO_PROXY':'127.0.0.1,localhost','no_proxy':'127.0.0.1,localhost','PATH':'/opt/homebrew/bin:/usr/bin:/bin','HOME':os.environ['HOME'],'LANG':'en_US.UTF-8',
         'TMPDIR':str(root/'tmp'),'SYNTHETIC_BUDDY_API_KEY':'synthetic-caller','GIT_CONFIG_GLOBAL':'/dev/null','GIT_CONFIG_NOSYSTEM':'1',**extra_env}
    process=subprocess.Popen(['/usr/bin/sandbox-exec','-f',str(policy),*command],cwd=root,env=env,
                             stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
    failure=None
    try:
        stdout,stderr=process.communicate(timeout=35)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid,signal.SIGKILL);stdout,stderr=process.communicate(timeout=3)
        failure='Isolated client timed out'
    finally:
        try:os.killpg(process.pid,signal.SIGTERM)
        except ProcessLookupError:pass
    if failure or process.returncode:
        stderr='\n'.join(line for line in stderr.splitlines() if any(word in line.lower() for word in ['error','warn','denied','failed','retry','stream','socket']))[-1400:]
        stderr=re.sub(r'[0-9a-f]{8}-[0-9a-f-]{27,}', '<session>', stderr[-1600:])
        types=[json.loads(line).get('type') for line in stdout.splitlines() if line.startswith('{')]
        errors=[json.loads(line).get('message','') for line in stdout.splitlines() if line.startswith('{') and json.loads(line).get('type')=='error']
        errors=re.sub(r'[0-9a-f]{8}-[0-9a-f-]{27,}', '<session>',str(errors))
        pytest.fail((failure or 'Isolated client failed')+': '+stderr+'; event types='+str(types)+'; errors='+errors,pytrace=False)
    return [json.loads(line) for line in stdout.splitlines() if line.startswith('{')]


def run_codex(root,port,session=None,fork=False,model="auto"):
    client_state_path=root/'codex-state';client_state_path.mkdir(exist_ok=True)
    client_state_path.joinpath('config.toml').write_text(f'''model_provider="synthetic-buddy"
model="{model}"
[model_providers.synthetic-buddy]
name="synthetic"
base_url="http://127.0.0.1:{port}/v1"
env_key="SYNTHETIC_BUDDY_API_KEY"
wire_api="responses"
requires_openai_auth=false
''')
    command=[str(CODEX),'--no-daemon','exec']
    if session:command += ['fork' if fork else 'resume',session]
    command += ['--skip-git-repo-check','--json','synthetic local verification']
    events=execute_client(root,port,command,{'CODEX_HOME':str(client_state_path)})
    return next(e['thread_id'] for e in events if e.get('type')=='thread.started'),events


def run_pi(root,port,session=None,fork=False,protocol='openai-responses',model='auto'):
    client_state_path=root/'pi-state';client_state_path.mkdir(exist_ok=True)
    client_state_path.joinpath('models.json').write_text(json.dumps({'providers':{'affinity-probe':{
        'baseUrl':f'http://127.0.0.1:{port}/v1','api':protocol,'apiKey':'synthetic-caller',
        'models':[{'id':model,'contextWindow':32768,'maxTokens':128,'reasoning':False,'input':['text']} ]}}}))
    client_state_path.joinpath('settings.json').write_text(json.dumps({'defaultProvider':'affinity-probe',
        'defaultModel':'auto','extensions':[],'skills':[],'packages':[]}))
    command=[str(NODE),str(PI),'--print','--mode','json','--provider','affinity-probe','--model',model,
             '--no-extensions','--no-skills','--no-prompt-templates','--no-tools',
             '--session-dir',str(root/'sessions')]
    if session:command += ['--fork' if fork else '--session',session]
    command += ['synthetic local verification']
    events=execute_client(root,port,command,{'PI_CODING_AGENT_DIR':str(client_state_path)})
    files=list((root/'sessions').glob('*.jsonl'))
    assert files,'Pi did not persist a session'
    latest=max(files,key=lambda p:p.stat().st_mtime_ns)
    header=json.loads(latest.open().readline())
    return header['id'],events


@pytest.mark.skipif(not CODEX.exists() or not shutil.which('sandbox-exec'),reason='isolated Codex CLI unavailable')
def test_codex_cli_real_session_resume_and_fork(wire,tmp_path,caplog):
    cfg,caller,*_=wire
    db.set_setting(session_identity.SETTING,{**cfg,'profiles':{str(caller):{'native':session_identity.CODEX_NATIVE}}})
    root=tmp_path.resolve()
    with buddy_http_bridge() as (port,records):
        a,_=run_codex(root,port)
        same,_=run_codex(root,port,a)
        b,_=run_codex(root,port)
        child,_=run_codex(root,port,a,fork=True)
    assert a==same and a!=b and child not in {a,b}
    assert len(records)==4
    assert records[0]['headers'].get('session-id')==fingerprint(a)
    assert records[0]['headers'].get('session-id')==records[1]['headers'].get('session-id')
    assert records[0]['headers'].get('session-id')!=records[2]['headers'].get('session-id')
    assert len(rows())==3
    assert len({r['parsed_identity'] for r in records})==3
    h={**KEY_HEADERS,'X-Buddy2api-Session-Id':a}
    assert asyncio.run(post(headers=h)).status_code==200
    assert asyncio.run(post('responses',headers=h)).status_code==200
    assert len(rows())==4
    serialized=json.dumps(wire[3])+caplog.text
    assert all(value not in serialized for value in [a,b,child])
    output=os.environ.get('BUDDY_AFFINITY_EVIDENCE_DIR')
    if output:Path(output,'codex-cli-lifecycle.json').write_text(json.dumps({'client':'Codex CLI 0.162.1',
        'real_fresh_resume_fork_verified':True,'records':records,'explicit_control_verified':True,'outbound_and_logs_identity_absent':True,
        'native_registered':True,'native_affinity':True,'session_digests':[fingerprint(v) for v in [a,same,b,child]]},indent=2))


@pytest.mark.skipif(not PI.exists() or not shutil.which('sandbox-exec'),reason='isolated Pi CLI unavailable')
@pytest.mark.parametrize('protocol',['openai-responses','openai-completions'])
def test_pi_cli_real_session_resume_and_fork(wire,tmp_path,protocol,caplog):
    cfg,caller,*_=wire
    db.set_setting(session_identity.SETTING,{**cfg,'profiles':{str(caller):{'native':session_identity.NATIVE}}})
    root=tmp_path.resolve()
    with buddy_http_bridge() as (port,records):
        a,_=run_pi(root,port,protocol=protocol)
        same,_=run_pi(root,port,a,protocol=protocol)
        b,_=run_pi(root,port,protocol=protocol)
        child,_=run_pi(root,port,a,fork=True,protocol=protocol)
    assert a==same and a!=b and child not in {a,b}
    assert len(records)==4
    native=protocol=='openai-responses'
    assert len(rows())==(3 if native else 0)
    if native:
        assert records[0]['headers']['session_id']==records[1]['headers']['session_id']
        assert records[0]['headers']['session_id']==fingerprint(a)
        assert len({r['parsed_identity'] for r in records})==3
    else:
        assert all(r['parsed_identity'] is None for r in records)
    before=len(rows())
    h={**KEY_HEADERS,'X-Buddy2api-Session-Id':a}
    assert asyncio.run(post(headers=h)).status_code==200
    assert asyncio.run(post('responses',headers=h)).status_code==200
    assert len(rows())==before+1
    serialized=json.dumps(wire[3])+caplog.text
    assert all(value not in serialized for value in [a,b,child])
    output=os.environ.get('BUDDY_AFFINITY_EVIDENCE_DIR')
    if output:Path(output,f'pi-cli-lifecycle-{protocol}.json').write_text(json.dumps({'client':'Pi 1.2.0',
        'protocol':protocol,'default_cache':'short','native_affinity':native,
        'real_fresh_resume_fork_verified':True,'records':records,'explicit_control_verified':True,'outbound_and_logs_identity_absent':True,
        'session_digests':[fingerprint(v) for v in [a,same,b,child]]},indent=2))
