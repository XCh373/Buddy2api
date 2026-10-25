"""Actual server.main lifecycle with synthetic state and a fail-closed Mock upstream."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from offline_runner import guard, allow_loopback

sys.addaudithook(guard)
import httpx
import server

def mock(request):
    if (request.method != 'POST' or request.url.scheme != 'https' or
            request.url.host != 'synthetic-upstream.invalid' or request.url.path != '/v2/chat/completions'):
        raise AssertionError('Unregistered outbound HTTP in isolated service')
    body = json.loads(request.content)
    headers = {name.lower() for name in request.headers}
    assert 'x-buddy2api-session-id' not in headers and 'x-buddy2api-session-namespace' not in headers
    assert '_session_affinity' not in body
    data = {'model':'auto','choices':[{'index':0,'delta':{'role':'assistant','content':'mock-ok'},'finish_reason':None}]}
    done = {'model':'auto','choices':[{'index':0,'delta':{},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}}
    return httpx.Response(200, content=('data: '+json.dumps(data)+'\n\ndata: '+json.dumps(done)+'\n\ndata: [DONE]\n\n').encode())

OriginalClient = httpx.AsyncClient
httpx.AsyncClient = lambda **kw: OriginalClient(transport=httpx.MockTransport(mock), **kw)
port = int(sys.argv[1])
sys.argv = [server.__file__, '--host','127.0.0.1','--port',str(port),'--no-browser']
with allow_loopback(port, bind=True), allow_loopback(port):
    server.main()
