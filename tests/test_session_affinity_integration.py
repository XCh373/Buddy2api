import asyncio
import json
import sqlite3
from pathlib import Path
import os
import subprocess
import sys
import threading

import httpx
import pytest

import account_routing
import auth_manager as auth
import catalog
import context_budget
import database as db
import providers
import proxy
import router
import server
import session_affinity as affinity
import session_identity as identity
from test_session_affinity import state, ident, rows


REAL_CLIENT = httpx.AsyncClient
KEY_HEADERS = {"Authorization": "Bearer synthetic-caller", "X-Buddy2api-Session-Id": "private-conversation-A"}


@pytest.fixture
def wire(state, monkeypatch):
    cfg, caller, aids = state
    db.set_setting(identity.SETTING, cfg)
    monkeypatch.setattr(auth, "_sticky_account_id", {})
    monkeypatch.setattr(auth, "_account_failures", {})
    captures = []
    failures = {}
    async def headers(account):
        return {"Authorization": "synthetic-account-" + str(account["id"])}
    async def no_delay(_): pass
    def upstream(request):
        assert (request.method == "POST" and request.url.scheme == "https" and
                request.url.host == "synthetic-upstream.invalid" and
                request.url.path == "/v2/chat/completions"), "Unregistered mock target"
        aid = int(request.headers["Authorization"].rsplit("-", 1)[1])
        captures.append((aid, dict(request.headers), json.loads(request.content)))
        failure = failures.get(aid)
        if callable(failure):
            return failure(request)
        if failure:
            return httpx.Response(failure, json={"error": {"message": "synthetic failure"}})
        events = [
            {"id": "mock", "object": "chat.completion.chunk", "model": "auto", "choices": [
                {"index": 0, "delta": {"role": "assistant", "content": "ok"}, "finish_reason": None}]},
            {"id": "mock", "object": "chat.completion.chunk", "model": "auto", "choices": [
                {"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
        ]
        return httpx.Response(200, content=("".join("data: " + json.dumps(e) + "\n\n" for e in events) + "data: [DONE]\n\n").encode())
    monkeypatch.setattr(auth, "get_valid_headers", headers)
    monkeypatch.setattr(auth, "backend_url", lambda account: "https://synthetic-upstream.invalid")
    monkeypatch.setattr(proxy, "_retry_delay", no_delay)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(transport=httpx.MockTransport(upstream), **kwargs))
    return cfg, caller, aids, captures, failures


async def post(path="chat", stream=False, headers=None, app=None, **extra):
    body = {"model": "auto", "stream": stream, **extra}
    body.update({"input": "hello"} if path == "responses" else {"messages": [{"role": "user", "content": "hello"}]})
    route = "/v1/responses" if path == "responses" else "/v1/chat/completions"
    async with REAL_CLIENT(transport=httpx.ASGITransport(app=app or server.app), base_url="http://synthetic") as client:
        return await client.post(route, json=body, headers=KEY_HEADERS if headers is None else headers)


@pytest.mark.parametrize("path", ["chat", "responses"])
@pytest.mark.parametrize("stream", [False, True])
def test_direct_actual_chain_preserves_binding_and_no_leaks(wire, path, stream, caplog):
    cfg, caller, aids, captures, failures = wire
    async def run():
        first = await post(path, stream, _session_affinity={"digest": "evil-injected"}, prompt_cache_key="untrusted-cache-key")
        assert first.status_code == 200
        bound = rows()[0]["account_id"]
        other = next(a for a in aids if a != bound)
        auth._set_sticky_account(other)
        now = __import__("time").time()
        db.set_setting(account_routing.SETTING, {"mode": "expiry_first"})
        db.upsert_account_resource_cache(other, {"ok": True, "updated_at": now, "route_usage_baseline": 0,
            "route_last_attempt_ok": True, "packages": [{"expire_ts": now + 86400, "remaining_precise": 100, "capacity_unit": "credit"}]})
        assert (await post(path, stream)).status_code == 200
        assert captures[-1][0] == bound
        b_headers = {**KEY_HEADERS, "X-Buddy2api-Session-Id": "private-conversation-B"}
        assert (await post(path, stream, headers=b_headers)).status_code == 200
        assert len(rows()) == 2
        affinity.reset_runtime()
        assert (await post(path, stream)).status_code == 200
        assert captures[-1][0] == bound
        assert "private-conversation" not in first.text
        assert "evil-injected" not in first.text
    asyncio.run(run())
    serialized = json.dumps(captures)
    for forbidden in ("private-conversation", "buddy2api-session", "_session_affinity", "evil-injected", "untrusted-cache-key"):
        assert forbidden not in serialized
        assert forbidden not in caplog.text
    store = affinity.get_store(cfg)
    assert not store.active
    assert store.secret.decode() not in serialized
    evidence_dir = os.environ.get("BUDDY_AFFINITY_EVIDENCE_DIR")
    if evidence_dir:
        certificate = {"protocol": path, "stream": stream, "requests": len(captures),
                       "outbound_header_names": sorted({k for _, h, _ in captures for k in h}),
                       "outbound_body_keys": sorted({k for _, _, b in captures for k in b}),
                       "raw_identity_absent": True, "identity_headers_absent": True,
                       "private_info_absent": True, "secret_absent": True,
                       "response_and_logs_checked": True}
        Path(evidence_dir, f"outbound-{path}-{stream}.json").write_text(json.dumps(certificate, indent=2))


def test_highest_priority_cooldown_and_no_return_to_recovered_account(wire):
    cfg, caller, aids, captures, failures = wire
    async def run():
        await post()
        a = captures[-1][0]
        b = next(x for x in aids if x != a)
        db.update_account(b, {"priority": 10})
        await post()
        assert captures[-1][0] == b
        auth.mark_account_failure(b, 429)
        await post()
        assert captures[-1][0] != b
        # Recovered higher priority must reclaim; equal priority recovery must not.
        db.update_account(b, {"priority": 0})
        auth.mark_account_success(b)
        current = rows()[0]["account_id"]
        await post()
        assert captures[-1][0] == current
        db.update_account(current, {"status": "disabled"})
        await post()
        assert captures[-1][0] != current
    asyncio.run(run())


@pytest.mark.parametrize("stream", [False, True])
def test_storage_error_before_headers_and_during_migration(wire, stream):
    cfg, caller, aids, captures, failures = wire
    affinity.get_store(cfg)
    async def initial():
        with db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            result = await proxy.proxy_chat_completions({"model": "auto", "messages": [], "stream": stream},
                {"id": caller, "name": "synthetic", "_session_affinity": ident(caller)})
        assert result[0] == "error" and result[1][0] == 503
        assert not captures
    asyncio.run(initial())
    # Open a write lock only AFTER first upstream request. Next account must not be submitted.
    lock = db.get_conn()
    first = aids[0]
    def fail(request):
        lock.execute("BEGIN IMMEDIATE")
        return httpx.Response(500, json={"error": {"message": "synthetic"}})
    failures[first] = fail
    # Avoid unrelated account usage writes blocking on this deliberately held lock.
    async def run():
        result = await proxy.proxy_chat_completions({"model": "auto", "messages": [], "stream": stream},
            {"id": caller, "name": "synthetic", "_session_affinity": ident(caller)})
        if stream:
            assert result[0] == "stream"
            output = b"".join([chunk async for chunk in result[1]])
            assert b"affinity_state_unavailable" in output
        else:
            assert result[0] == "error" and result[1][0] == 503
    # Logging uses another SQLite connection; don't test its existing 5s busy timeout here.
    original_log = proxy._log_request
    proxy._log_request = lambda *args, **kwargs: None
    try:
        asyncio.run(run())
    finally:
        proxy._log_request = original_log
        lock.close()
    assert len(captures) == 1


def test_invalid_identity_before_precheck_or_quota(wire, monkeypatch):
    async def forbidden(_): raise AssertionError("precheck must not run")
    monkeypatch.setattr(router, "ensure_usable", forbidden)
    result = asyncio.run(post(headers=[("Authorization", "Bearer synthetic-caller"),
                                      ("X-Buddy2api-Session-Id", "A"), ("X-Buddy2api-Session-Id", "B")]))
    assert result.status_code == 400
    assert not rows()
    assert db.get_api_key_daily_requests(wire[1]) == 0


def test_noninference_never_touches_binding(wire, monkeypatch):
    cfg, caller, aids, captures, failures = wire
    asyncio.run(post())
    before = rows()
    store = affinity.get_store(cfg)
    writes = store.writes
    async def fetch(_): return [{"id": "synthetic-model"}]
    monkeypatch.setitem(catalog.LIVE_FETCHERS, "workbuddy", fetch)
    async def run():
        await router.ensure_usable("workbuddy")
        await providers.get_provider("workbuddy").has_usable_account()
        await catalog.refresh_one("workbuddy")
        auth.routing_preview()
    asyncio.run(run())
    assert rows() == before and store.writes == writes


@pytest.mark.parametrize("stream", [False, True])
def test_expired_token_fallback_and_stop(wire, monkeypatch, stream):
    cfg, caller, aids, captures, failures = wire
    for aid in aids:
        db.update_account(aid, {"status": "expired"})
    async def refresh(account):
        db.update_account(account["id"], {"status": "active"})
        return True
    monkeypatch.setattr(auth, "refresh_token", refresh)
    assert asyncio.run(post(stream=stream)).status_code == 200
    assert len(rows()) == 1
    bound = rows()[0]["account_id"]
    failures[bound] = lambda _: httpx.Response(400, json={"code": 11128, "msg": "synthetic"})
    before = len(captures)
    result = asyncio.run(post(stream=stream))
    assert len(captures) == before + 1 and rows()[0]["account_id"] == bound
    assert ("11128" in result.text)


def test_budget_retry_and_responses_trim_keep_identity(wire, monkeypatch):
    cfg, caller, aids, captures, failures = wire
    seen = []
    provider = providers.get_provider("workbuddy")
    real = provider.chat_completions
    async def chat(body, info):
        seen.append(info["_session_affinity"])
        if len(seen) == 1:
            return ("error", (400, {"code": 11115}))
        return await real(body, info)
    monkeypatch.setattr(provider, "chat_completions", chat)
    monkeypatch.setattr(context_budget, "recovery_payload", lambda body: (dict(body), True))
    result = asyncio.run(post("responses"))
    assert result.status_code == 200 and seen[0] == seen[1]
    # Codex trimming executes after identity parsing, before conversion.
    db.update_api_key(caller, {"client_type": "codex"})
    monkeypatch.setattr(router, "trim_codex_prompt", lambda body, *args: {"model": "auto", "input": "trimmed"})
    assert asyncio.run(post("responses")).status_code == 200
    assert seen[-1] == seen[0]


def test_plain_proxy_transparent_or_dropping_identity(wire):
    def proxy_app(drop=False):
        async def app(scope, receive, send):
            copied = dict(scope)
            copied["headers"] = [(k, v) for k, v in scope["headers"] if not (drop and k.lower().startswith(b"x-buddy2api-session"))]
            await server.app(copied, receive, send)
        return app
    async def run():
        assert (await post(app=proxy_app())).status_code == 200
        before = rows()
        assert (await post(app=proxy_app(True))).status_code == 200
        assert rows() == before
    asyncio.run(run())


def test_cancelled_or_unconsumed_stream_releases_pin(wire):
    cfg, caller, aids, captures, failures = wire
    async def run():
        result = await proxy.proxy_chat_completions({"model": "auto", "messages": [], "stream": True},
            {"id": caller, "name": "synthetic", "_session_affinity": ident(caller)})
        assert result[0] == "stream"
        await result[1].aclose()
    asyncio.run(run())
    assert not affinity.get_store(cfg).active


@pytest.mark.parametrize("late_failure", [False, True])
def test_real_concurrent_migration_late_result_cannot_restore_old_binding(wire, monkeypatch, late_failure):
    cfg, caller, aids, captures, failures = wire
    async def run():
        release = asyncio.Event()
        a = aids[0]
        def chunk(text):
            return ("data: " + json.dumps({"model": "auto", "choices": [{"index": 0,
                    "delta": {"role": "assistant", "content": text}, "finish_reason": None}]}) + "\n\n").encode()
        terminal = b'data: {"model":"auto","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
        class Paused(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield chunk("first")
                await release.wait()
                if late_failure:
                    raise httpx.ReadError("synthetic late failure")
                yield terminal
        def upstream(req):
            aid = int(req.headers["Authorization"].rsplit("-", 1)[1])
            captures.append((aid, {}, {}))
            return httpx.Response(200, stream=Paused()) if aid == a else httpx.Response(200, content=chunk("other") + terminal)
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(transport=httpx.MockTransport(upstream), **kwargs))
        result = await proxy.proxy_chat_completions({"model": "auto", "messages": [], "stream": True},
            {"id": caller, "name": "synthetic", "_session_affinity": ident(caller, "private-conversation-A")})
        old_stream = result[1]
        assert b"first" in await anext(old_stream)
        auth.mark_account_failure(a, 429)
        assert (await post()).status_code == 200
        b = captures[-1][0]
        assert b != a and rows()[0]["generation"] == 2
        release.set()
        output = b"".join([chunk async for chunk in old_stream])
        assert (await post()).status_code == 200
        assert captures[-1][0] == b and rows()[0]["account_id"] == b
        assert [x[0] for x in captures] == [a, b, b]
        assert not affinity.get_store(cfg).active
    asyncio.run(run())


def test_concurrent_asgi_first_requests(wire):
    async def run():
        gate = asyncio.Event()
        async def request():
            await gate.wait()
            return await post()
        tasks = [asyncio.create_task(request()) for _ in range(12)]
        gate.set()
        results = await asyncio.gather(*tasks)
        assert all(r.status_code == 200 for r in results)
    asyncio.run(run())
    assert len(rows()) == 1 and rows()[0]["generation"] == 1
    assert len({r[0] for r in wire[3]}) == 1


def test_real_process_restart_restores_binding_without_network(wire):
    cfg, caller, aids, captures, failures = wire
    asyncio.run(post())
    first = rows()[0]
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ["HOME"], "LANG": "en_US.UTF-8",
           "CB_GATEWAY_DB_PATH": str(db.DB_PATH), "CB_GATEWAY_MASTER_KEY": "synthetic-affinity-only",
           "CB_AUTH_DIR": str(db.DB_PATH.parent / "empty-auth")}
    script = """
import sys
sys.path.insert(0, 'tests')
from offline_runner import guard
sys.addaudithook(guard)
import auth_manager, session_identity
key = {'id': int(sys.argv[1])}
i = session_identity.parse([(b'x-buddy2api-session-id', b'private-conversation-A')], key, 'workbuddy', 'chat', session_identity.load_config())
print(auth_manager.pick_account(affinity=i)['id'])
"""
    result = subprocess.run([sys.executable, "-c", script, str(caller)], env=env, capture_output=True, text=True, check=True, timeout=20)
    assert int(result.stdout) == first["account_id"]
    assert rows()[0]["generation"] == first["generation"]


@pytest.mark.parametrize("mode", ["legacy", "expiry_first"])
def test_disabled_and_missing_identity_differential_reference(wire, monkeypatch, mode):
    cfg, caller, aids, captures, failures = wire
    db.set_setting(account_routing.SETTING, {"mode": mode})
    def sequence(reference):
        for aid in aids:
            db.update_account(aid, {"status": "active", "priority": 0, "weight": 1, "total_requests": 0})
        monkeypatch.setattr(auth, "_sticky_account_id", {})
        monkeypatch.setattr(auth, "_account_failures", {})
        scope = dict(vars(auth))
        exec(compile(Path("tests/legacy_selection_reference.py").read_text(), "legacy_selection_reference.py", "exec"), scope)
        picker = scope["pick_account"] if reference else auth.pick_account
        output = [picker()["id"]]
        db.update_account(aids[-1], {"weight": 100})
        output.append(picker()["id"])
        auth._set_sticky_account(aids[1])
        output.append(picker()["id"])
        db.update_account(aids[2], {"priority": 10})
        output.append(picker()["id"])
        auth.mark_account_failure(aids[2], 429)
        output.append(picker()["id"])
        output.append(picker({output[-1]})["id"])
        for aid in aids:
            db.update_account(aid, {"status": "disabled"})
        output.append(picker())
        return output
    expected = sequence(True)
    db.set_setting(identity.SETTING, {**cfg, "enabled": False})
    assert sequence(False) == expected
    db.set_setting(identity.SETTING, cfg)
    assert sequence(False) == expected
    assert not rows()


@pytest.mark.parametrize("stream", [False, True])
def test_missing_identity_and_disabled_endpoint_quota_error_equivalence(wire, stream):
    cfg, caller, aids, captures, failures = wire
    async def scenario(enabled):
        db.set_setting(identity.SETTING, {**cfg, "enabled": enabled})
        auth._sticky_account_id.clear()
        auth._account_failures.clear()
        for aid in aids:
            db.update_account(aid, {"status": "active", "priority": 0, "total_requests": 0})
        with db.connection() as conn:
            conn.execute("UPDATE accounts SET total_requests=0, total_tokens=0, total_credits=0")
            conn.execute("DELETE FROM api_key_daily_usage")
            conn.execute("DELETE FROM logs")
            conn.commit()
        captures.clear()
        failures[aids[0]] = 429
        h = {"Authorization": "Bearer synthetic-caller"}
        if not enabled:
            h["X-Buddy2api-Session-Id"] = "invalid,ignored-when-off"
        response = await post(stream=stream, headers=h)
        result = (response.status_code, [c[0] for c in captures], db.get_api_key_daily_requests(caller))
        assert not rows()
        return result
    assert asyncio.run(scenario(False)) == asyncio.run(scenario(True))


def test_native_direct_contract_and_missing_identity_degradation(wire):
    cfg, caller, aids, captures, failures = wire
    db.set_setting(identity.SETTING, {**cfg, "profiles": {str(caller): {"native": identity.NATIVE}}})
    async def run():
        for value in ("native-A", "native-A", "native-B"):
            assert (await post("responses", headers={"Authorization": "Bearer synthetic-caller", "session_id": value})).status_code == 200
        before = rows()
        assert len(before) == 2
        assert (await post("responses", headers={"Authorization": "Bearer synthetic-caller"})).status_code == 200
        assert rows() == before
        assert (await post("chat", headers={"Authorization": "Bearer synthetic-caller", "session_id": "native-C"})).status_code == 200
        assert rows() == before
    asyncio.run(run())
    assert "native-A" not in json.dumps(captures)


@pytest.mark.parametrize("stream", [False, True])
def test_asgi_initial_storage_failure_is_http_503(wire, monkeypatch, stream):
    def fail(*args, **kwargs): raise identity.StateUnavailable()
    monkeypatch.setattr(affinity.Store, "select", fail)
    result = asyncio.run(post("responses", stream=stream))
    assert result.status_code == 503
    assert "affinity_state_unavailable" in result.text
    assert not wire[3]


def test_weight_usage_and_invalid_snapshot_never_steal_binding(wire):
    cfg, caller, aids, captures, failures = wire
    asyncio.run(post())
    bound = rows()[0]["account_id"]
    for aid in aids:
        db.update_account(aid, {"weight": 1000 if aid != bound else 1})
    with db.connection() as conn:
        conn.execute("UPDATE accounts SET total_requests=100000 WHERE id=?", (bound,))
        conn.commit()
    db.set_setting(account_routing.SETTING, {"mode": "expiry_first"})
    db.upsert_account_resource_cache(bound, {"ok": False, "stale": True})
    assert asyncio.run(post()).status_code == 200
    assert captures[-1][0] == bound


def test_provider_pool_and_shared_key_isolation(wire):
    cfg, caller, aids, captures, failures = wire
    asyncio.run(post())
    intl = db.add_account({"name": "synthetic-intl", "uid": "intl", "provider": "workbuddy-i", "access_token": "synthetic", "expires_at": 9999999999999})
    db.update_api_key(caller, {"default_channel": "workbuddy-i"})
    assert asyncio.run(post()).status_code == 200
    assert captures[-1][0] == intl
    assert {r["provider_channel"] for r in rows()} == {"workbuddy", "workbuddy-i"}
    other = db.add_api_key("synthetic-other", "other")
    h = {**KEY_HEADERS, "Authorization": "Bearer synthetic-other"}
    assert asyncio.run(post(headers=h)).status_code == 200
    assert {r["api_key_id"] for r in rows()} == {caller, other}


def test_anonymous_ignores_identity_and_keeps_old_request_behavior(wire, monkeypatch):
    db.delete_api_key(wire[1])
    monkeypatch.setattr(server, "ALLOW_UNAUTHENTICATED_API", True)
    result = asyncio.run(post(headers={"X-Buddy2api-Session-Id": "bad,anonymous-ignored"}))
    assert result.status_code == 200
    assert not rows()


def test_secret_hidden_and_minimal_browser_cors(wire, monkeypatch):
    affinity.get_store(wire[0])
    monkeypatch.setattr(server, "LOCAL_MODE", False)
    monkeypatch.setattr(server, "ALLOW_NO_ADMIN_AUTH", True)
    settings = asyncio.run(server.admin_get_settings())
    assert identity.SECRET_SETTING not in settings
    async def run():
        async with REAL_CLIENT(transport=httpx.ASGITransport(app=server.app), base_url="http://synthetic") as client:
            response = await client.options("/v1/responses", headers={"Origin": "http://localhost:8787", "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,x-buddy2api-session-id"})
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "http://localhost:8787"
    asyncio.run(run())


def test_debug_redaction_including_native_cache_hints():
    import responses
    redacted = responses._redact_debug_value({"prompt_cache_key": "private-session", "_session_affinity": "private-internal", "metadata": {"session_id": "private-session"}}, include_content=True)
    assert "private-" not in json.dumps(redacted)


def test_cancellation_during_lease_acquisition_does_not_leak_pin(wire, monkeypatch):
    cfg, caller, aids, captures, failures = wire
    store = affinity.get_store(cfg)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    real_pin = store.pin
    def paused(identity):
        lease = real_pin(identity)
        entered.set()
        assert release.wait(2)
        finished.set()
        return lease
    monkeypatch.setattr(store, "pin", paused)
    async def run():
        task = asyncio.create_task(proxy.proxy_chat_completions({"model": "auto", "messages": []},
            {"id": caller, "name": "synthetic", "_session_affinity": ident(caller)}))
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert await asyncio.to_thread(finished.wait, 2)
        await asyncio.sleep(.02)  # let the worker discard its cancelled result and finalize the lease
    asyncio.run(run())
    assert not store.active and not captures
