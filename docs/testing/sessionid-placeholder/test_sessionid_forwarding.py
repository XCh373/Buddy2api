"""Standalone reproduction: magpie ${sessionId} -> X-Buddy2api-Session-Id forwarding.

Self-contained: depends only on stdlib + httpx + a locally installed magpie
binary. It does NOT import the production tests/ fixtures. It launches an
isolated magpie (no auto-update, isolated XDG dirs) whose synthetic provider
sets ``headers: {"X-Buddy2api-Session-Id": "${sessionId}"}``, then POSTs
synthetic requests to it, capturing what magpie actually forwards to a raw tap.

Run (macOS; nested sandbox-exec must be permitted by the outer environment):
    BUDDY_AFFINITY_MAGPIE_BINARY=/Applications/magpie.app/Contents/MacOS/magpie \
    python docs/testing/sessionid-placeholder/test_sessionid_forwarding.py

See README.md and ../../verification/sessionid-placeholder/conclusion.md.
"""
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx

BINARY = Path(os.environ.get("BUDDY_AFFINITY_MAGPIE_BINARY", "/Applications/magpie.app/Contents/MacOS/magpie"))
HEADERS = {"X-Buddy2api-Session-Id": "${sessionId}"}


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Tap(http.server.BaseHTTPRequestHandler):
    records = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
        self.records.append(self.headers.get("x-buddy2api-session-id"))
        body = b'{"id":"r","object":"response","status":"completed","output":[],"usage":{"input_tokens":1,"output_tokens":1}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _start_magpie(root, destination):
    port = _free_port()
    cfg = root / "config" / "magpie"
    cfg.mkdir(parents=True)
    (cfg / "providers.json").write_text(json.dumps({"providers": [{
        "id": "affinity-probe", "name": "Synthetic", "key": "k",
        "responses": f"http://127.0.0.1:{destination}/v1", "baseAPI": "responses",
        "models": ["auto"], "proxy": "direct", "headers": HEADERS}]}))
    (cfg / "settings.json").write_text(json.dumps({
        "port": port, "proxy": "direct", "noAccountDiscovery": True, "noAutoUpdate": True,
        "gatewayMode": "on", "workbuddyCheckin": False, "traeCheckin": False,
        "qoderCheckin": False, "minimaxCheckin": False}))
    (cfg / "plugins.yml").write_text("preset: essentials\n")
    for n in ("data", "cache", "tmp"):
        (root / n).mkdir()
    policy = root / "proxy.sb"
    policy.write_text(f'''(version 1)(allow default)(deny network*)
(allow network-inbound (local ip "localhost:{port}"))
(allow network-outbound (remote ip "localhost:{destination}"))
(deny file-read* (subpath "{os.environ['HOME']}"))
(allow file-read* (subpath "{root}"))
(deny file-write*)(allow file-write* (subpath "{root}"))
''')
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": os.environ["HOME"], "LANG": "en_US.UTF-8",
           "TMPDIR": str(root / "tmp"), "XDG_CONFIG_HOME": str(root / "config"),
           "XDG_DATA_HOME": str(root / "data"), "XDG_CACHE_HOME": str(root / "cache"),
           "MAGPIE_ADDR": f"127.0.0.1:{port}", "MAGPIE_NO_ACCOUNTS": "1", "NO_PROXY": "127.0.0.1"}
    log = (root / "proxy.log").open("w")
    proc = subprocess.Popen(["/usr/bin/sandbox-exec", "-f", str(policy), str(BINARY), "serve"],
                            cwd=root, env=env, stdout=log, stderr=log)
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("isolated magpie exited; see " + str(root / "proxy.log"))
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.05)
    else:
        proc.kill()
        raise RuntimeError("magpie readiness timed out")
    return proc, port


def main():
    if not BINARY.exists() or not shutil.which("sandbox-exec"):
        print(f"SKIP: magpie binary or sandbox-exec unavailable ({BINARY})")
        return 0
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dest = s.getsockname()[1]
    tap = http.server.ThreadingHTTPServer(("127.0.0.1", dest), _Tap)
    threading.Thread(target=tap.serve_forever, daemon=True).start()
    root = Path(tempfile.mkdtemp())
    proc, port = _start_magpie(root, dest)
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=8) as client:
            def send(native):
                h = [("Authorization", "Bearer magpie")]
                if native is not None:
                    h.append(("session_id", native))
                client.post("/v1/responses", headers=h, json={"model": "affinity-probe/auto", "input": "x"})
                return _Tap.records[-1]

            a1, a2, b = send("synthetic-A"), send("synthetic-A"), send("synthetic-B")
            m1, m2 = send(None), send(None)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            proc.kill()
        tap.shutdown()

    ok = True
    print("=== results ===")
    print(f"native A  -> {a1!r}")
    print(f"native A' -> {a2!r}")
    print(f"native B  -> {b!r}")
    print(f"no-native -> {m1!r}")
    print(f"no-native -> {m2!r}")
    def check(name, cond):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name)
        ok = ok and cond
    check("same native session -> same forwarded id", a1 == a2 and a1 == "synthetic-A")
    check("distinct native session -> distinct id", b == "synthetic-B" and b != a1)
    check("no native session -> CONSTANT fallback (collision hazard)", m1 == m2 and m1 is not None and m1.startswith("magpie-"))
    print("=== " + ("ALL PASS" if ok else "FAILURES") + " ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
