"""Run pytest with synthetic defaults and fail-closed network/credential access.

Usage: python tests/offline_runner.py -q tests/test_session_identity.py ...
Does not change HOME. Only registered isolated loopback endpoints are allowed;
the default production database and real credential stores are forbidden.
"""
import os
from pathlib import Path
import sys
import tempfile
from contextlib import contextmanager
from collections import Counter
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEMP_ROOT = str(Path(tempfile.gettempdir()).resolve()) + "/"
_network = Counter()
_network_lock = threading.Lock()


@contextmanager
def allow_loopback(*ports, bind=False):
    """Explicit per-test local endpoints only; never production gateway ports."""
    if any(type(port) is not int or port in {3425, 8787} or not 0 <= port <= 65535 for port in ports):
        raise ValueError("Invalid isolated endpoint")
    entries = [(operation, port) for port in ports for operation in
               (("socket.bind",) if bind else ("socket.connect", "socket.getaddrinfo"))]
    with _network_lock:
        _network.update(entries)
    try:
        yield
    finally:
        with _network_lock:
            _network.subtract(entries)


def isolated_port():
    import socket
    with allow_loopback(0, bind=True), socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def guard(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.bind"}:
        address = args[:2] if event == "socket.getaddrinfo" else args[1]
        host, port = address[:2]
        with _network_lock:
            allowed = host == "127.0.0.1" and _network[(event, port)] > 0
        if not allowed:
            raise AssertionError("Unregistered network operation in offline tests")
    if event not in {"open", "sqlite3.connect", "os.scandir", "os.listdir"} or not args:
        return
    raw = args[0]
    if not isinstance(raw, (str, bytes, os.PathLike)):
        return
    path = Path(os.fsdecode(raw)).resolve()
    text = str(path)
    temporary = text.startswith(("/tmp/", "/private/tmp/", TEMP_ROOT))
    production = str(ROOT / "codebuddy_gateway.db")
    if text.startswith(production) or (not temporary and (
        path.suffix == ".info" or "CodeBuddyExtension/Data/Public/auth" in text or
        "/.config/magpie/" in text or "/.codex/auth.json" in text or
        text.endswith((".credentials.key", "/.admin_token", "/.client_key"))
    )):
        raise AssertionError("Real runtime/credential access forbidden in offline tests")


if __name__ == "__main__":
    sys.modules["offline_runner"] = sys.modules[__name__]
    with tempfile.TemporaryDirectory(prefix="buddy-affinity-offline-") as folder:
        os.environ["CB_GATEWAY_DB_PATH"] = str(Path(folder) / "default-synthetic.db")
        os.environ["CB_GATEWAY_MASTER_KEY"] = "synthetic-offline-tests-only"
        os.environ["CB_AUTH_DIR"] = str(Path(folder) / "empty-auth")
        os.environ["CB_DEBUG_DUMP"] = "0"
        sys.addaudithook(guard)
        import database
        database.init_db()
        import pytest
        arguments = sys.argv[1:]
        # Client transcripts and synthetic runtime configs belong to this run,
        # not pytest's retained global temporary directories.
        if not any(arg == "--basetemp" or arg.startswith("--basetemp=") for arg in arguments):
            arguments = ["--basetemp", str(Path(folder) / "pytest"), *arguments]
        raise SystemExit(pytest.main(arguments))
