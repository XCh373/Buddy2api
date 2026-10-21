import asyncio
import json
import sqlite3
import time

import httpx
import pytest

import account_routing as routing
import auth_manager as auth
import credential_crypto
import database as db
import providers
import routing_refresh
import server

NOW = 2_000_000_000


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "routing.db")
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "synthetic-routing-test-key")
    monkeypatch.setattr(time, "time", lambda: NOW)
    monkeypatch.setattr(auth, "_sticky_account_id", {})
    monkeypatch.setattr(auth, "_account_failures", {})
    monkeypatch.setattr(server, "LOCAL_MODE", True)
    monkeypatch.setattr(server, "ADMIN_TOKEN", "")
    credential_crypto.reset_cache()
    db.init_db()
    yield
    credential_crypto.reset_cache()


def config(**fields):
    return routing.normalize_config({"mode": "expiry_first", **fields})


def package(days=1, remaining=100, **fields):
    return {"expire_ts": NOW + days * 86400, "remaining_precise": remaining,
            "capacity_unit": "credit", **fields}


def snapshot(packages=None, **fields):
    return {"ok": True, "updated_at": NOW, "route_usage_baseline": 0,
            "route_last_attempt_ok": True, "route_last_attempt_at": NOW,
            "packages": [package()] if packages is None else packages, **fields}


def add_account(name="a", *, days=1, remaining=100, provider="workbuddy", **fields):
    aid = db.add_account({"name": name, "uid": name, "provider": provider,
                          "status": "active", "access_token": "synthetic-token",
                          "expires_at": 9_999_999_999_999, **fields})
    db.upsert_account_resource_cache(aid, snapshot([package(days, remaining)]))
    return aid


def enable(**fields):
    db.set_setting(routing.SETTING, config(**fields))


def request(method, path, **kwargs):
    async def run():
        transport = httpx.ASGITransport(app=server.app, client=("127.0.0.1", 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8788") as client:
            return await client.request(method, path, **kwargs)
    return asyncio.run(run())


@pytest.mark.parametrize("value", [False, [], "expiry_first", {"mode": "unknown"},
    {"expiry_window_days": True}, {"expiry_window_days": 0}, {"expiry_window_days": 31},
    {"cache_max_age_seconds": 59}, {"cache_max_age_seconds": 1801},
    {"refresh_interval_seconds": 29}, {"refresh_interval_seconds": 601},
    {"cache_max_age_seconds": 120}, {"refresh_interval_seconds": "120"}, {"unexpected": 1}])
def test_configuration_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        routing.normalize_config(value)


def test_manual_priority_precedes_expiry_and_expiry_precedes_old_sticky(isolated_db):
    early = add_account("early", days=1)
    later = add_account("later", days=10, weight=100)
    auth._set_sticky_account(later)
    enable()
    assert auth.pick_account()["id"] == early
    db.update_account(later, {"priority": 10})
    assert auth.pick_account()["id"] == later


def test_equal_expiry_preserves_sticky_and_consumption_advances_to_next_package(isolated_db):
    a = add_account("a", days=1)
    b = add_account("b", days=1)
    enable()
    auth._set_sticky_account(b)
    assert auth.pick_account()["id"] == b
    db.upsert_account_resource_cache(b, snapshot([package(1, 5), package(20, 100)]))
    db.account_increment_usage(b, 10, 5)
    assert auth.pick_account()["id"] == a
    db.update_account(a, {"status": "inactive"})
    assert auth.pick_account()["id"] == b
    preview = auth.routing_preview()
    assert preview["accounts"][1]["expire_ts"] == NOW + 20 * 86400


@pytest.mark.parametrize("provider", ["qclaw", "qwenwork", "traework", "workbuddy-i"])
def test_other_providers_never_read_expiry_cache(isolated_db, monkeypatch, provider):
    early = add_account("early", provider=provider)
    later = add_account("later", provider=provider, days=10)
    enable()
    auth._set_sticky_account(later, provider)
    monkeypatch.setattr(routing, "read_snapshots", lambda _: pytest.fail("cross-provider cache access"))
    assert auth.pick_account(provider=provider)["id"] == later
    assert auth.pick_account({later}, provider=provider)["id"] == early


def test_legacy_and_empty_cache_preserve_original_order(isolated_db, monkeypatch):
    early = add_account("early")
    later = add_account("later", days=10, weight=5)
    assert auth.pick_account()["id"] == later
    enable()
    monkeypatch.setattr(db, "get_account_resource_caches", lambda _: {})
    assert auth.pick_account()["id"] == later
    assert auth.pick_account({later})["id"] == early


def test_cooling_disabled_excluded_accounts_are_never_prioritized(isolated_db):
    early = add_account("early")
    later = add_account("later", days=10)
    enable()
    auth.mark_account_failure(early, 429)
    assert auth.pick_account()["id"] == later
    assert auth.pick_account({later}) is None
    auth.mark_account_success(early)
    db.update_account(early, {"status": "inactive"})
    assert auth.pick_account()["id"] == later


@pytest.mark.parametrize("changes,reason", [
    ({"updated_at": NOW - 301}, "cache_expired"),
    ({"updated_at": NOW + 1}, "invalid_snapshot"),
    ({"updated_at": False}, "invalid_snapshot"),
    ({"packages": {}}, "invalid_snapshot"),
    ({"ok": False}, "invalid_snapshot"),
    ({"stale": True}, "refresh_failed"),
    ({"route_last_attempt_ok": False}, "refresh_failed"),
    ({"route_last_attempt_ok": "yes"}, "invalid_snapshot"),
    ({"route_usage_baseline": float("nan")}, "invalid_snapshot"),
    ({"route_usage_baseline": -1}, "invalid_snapshot"),
    ({"route_usage_baseline": 1}, "invalid_snapshot"),
])
def test_untrusted_snapshots_have_no_expiry_advantage(changes, reason):
    result = routing.credit_summary({"total_credits": 0}, snapshot(**changes), config(), NOW)
    assert result["reason"] == reason
    assert result["expire_ts"] is None


@pytest.mark.parametrize("pkg", [
    package(expire_ts=NOW), package(expire_ts=NOW - 1), package(days=31),
    package(expire_ts=(NOW + 100) * 1000), package(expire_ts=None), package(remaining=0),
    package(remaining=-1), package(remaining=float("nan")), package(remaining=float("inf")),
    package(remaining=10**1000), package(remaining=True), package(capacity_unit="token"),
    package(cycle_start_ts=NOW + 1), package(deduction_start_ts=NOW + 1),
    package(cycle_end_ts=NOW), package(deduction_end_ts=NOW - 1),
    package(cycle_end_ts="bad"),
])
def test_unusable_package_boundaries_have_no_expiry_advantage(pkg):
    result = routing.credit_summary({"total_credits": 0}, snapshot([pkg]), config(), NOW)
    assert result["expire_ts"] is None


def test_window_end_and_cache_max_age_are_inclusive():
    result = routing.credit_summary({"total_credits": 0},
        snapshot([package(days=30)], updated_at=NOW - 300), config(), NOW)
    assert result["reason"] == "expiry_candidate"
    assert result["age_seconds"] == 300


def test_legacy_snapshot_waits_for_refresh_and_baseline_is_conservative():
    old = snapshot()
    del old["route_usage_baseline"]
    assert routing.credit_summary({"total_credits": 0}, old, config(), NOW)["reason"] == "legacy_snapshot"
    result = routing.credit_summary({"total_credits": 30},
        snapshot([package(1, 20), package(2, 50), package(2, 40)]), config(), NOW)
    assert result["expire_ts"] == NOW + 2 * 86400
    assert result["effective_remaining"] == 30
    assert routing.credit_summary({"total_credits": 100}, snapshot(), config(), NOW)["reason"] == "local_usage_exhausted"


def test_cache_failure_revokes_trust_without_advancing_success_time(isolated_db):
    a = add_account()
    original = db.get_account_resource_cache(a)
    db.mark_resource_refresh_failed(a)
    failed = db.get_account_resource_cache(a)
    assert failed["cache_updated_at"] == original["cache_updated_at"]
    assert failed["ok"] is True
    assert routing.credit_summary(db.get_account(a), failed, config(), NOW)["reason"] == "refresh_failed"
    db.upsert_account_resource_cache(a, snapshot())
    assert routing.credit_summary(db.get_account(a), db.get_account_resource_cache(a), config(), NOW)["trusted"]


def test_cache_read_fault_and_corrupt_config_fall_back(isolated_db, monkeypatch):
    a = add_account()
    enable()
    def broken(_):
        raise sqlite3.OperationalError("synthetic error")
    monkeypatch.setattr(db, "get_account_resource_caches", broken)
    assert auth.pick_account()["id"] == a
    db.set_setting(routing.SETTING, {"mode": "bad"})
    assert routing.load_config()["mode"] == "legacy"
    with db.connection() as conn:
        conn.execute("UPDATE account_resource_cache SET payload='[]' WHERE account_id=?", (a,))
        conn.commit()
    assert db.get_account_resource_cache(a) is None


def test_batch_cache_read_uses_one_connection(isolated_db, monkeypatch):
    a, b = add_account("a"), add_account("b")
    real = db.get_conn
    calls = []
    def connection():
        calls.append(1)
        return real()
    monkeypatch.setattr(db, "get_conn", connection)
    assert set(db.get_account_resource_caches([a, b])) == {a, b}
    assert len(calls) == 1


def test_settings_validation_is_atomic_and_preview_is_read_only(isolated_db):
    a = add_account("a")
    b = add_account("b", days=10)
    auth._set_sticky_account(b)
    db.set_setting("timeout", 300)
    rejected = request("PUT", "/admin/settings", json={"timeout": 50, routing.SETTING: {"expiry_window_days": True}})
    assert rejected.status_code == 400
    assert db.get_setting("timeout") == 300
    accepted = request("PUT", "/admin/settings", json={routing.SETTING: {"mode": "expiry_first"}})
    assert accepted.status_code == 200
    assert request("GET", "/admin/settings").json()[routing.SETTING] == config()
    preview = request("GET", "/admin/routing/preview").json()
    assert preview["selected_account_id"] == a
    assert auth._sticky_account_id["workbuddy"] == b
    assert "synthetic-token" not in json.dumps(preview)
    assert request("GET", "/admin/routing/preview?channel=qclaw").status_code == 400
    assert request("PUT", "/admin/settings", json={routing.SETTING: None}).status_code == 400


def test_preview_requires_admin_auth(isolated_db, monkeypatch):
    monkeypatch.setattr(server, "LOCAL_MODE", False)
    monkeypatch.setattr(server, "ADMIN_TOKEN", "synthetic-management-key")
    assert request("GET", "/admin/routing/preview").status_code == 401
    assert request("GET", "/admin/routing/preview", headers={"Authorization": "Bearer synthetic-management-key"}).status_code == 200


def test_resource_refresh_serializes_and_records_baseline_before_network(isolated_db, monkeypatch):
    a = add_account()
    concurrent = 0
    peak = 0
    calls = 0
    async def headers(_):
        return {"Authorization": "synthetic"}
    async def post(_request):
        nonlocal concurrent, peak, calls
        concurrent += 1
        peak = max(peak, concurrent)
        calls += 1
        await asyncio.sleep(0)
        db.account_increment_usage(a, 1, 10)
        concurrent -= 1
        return httpx.Response(200, json={"Response": {"Data": {"Accounts": [
            {"CapacityRemainPrecise": 100, "ExpiredTime": NOW + 86400}
        ]}}})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(auth, "get_billing_headers", headers)
    monkeypatch.setattr(auth.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(post), **kwargs))
    async def run():
        await asyncio.gather(*(auth.fetch_account_resources(db.get_account(a), force=True) for _ in range(3)))
    asyncio.run(run())
    assert peak == 1 and calls == 3
    cached = db.get_account_resource_cache(a)
    assert cached["route_usage_baseline"] == 20
    assert db.get_account(a)["total_credits"] == 30
    assert routing.credit_summary(db.get_account(a), cached, config(), NOW)["effective_remaining"] == 90


def test_resource_failure_retains_stale_display_and_next_query_recovers(isolated_db, monkeypatch):
    a = add_account()
    responses = iter([httpx.Response(503, json={"error": "synthetic failure"}),
                      httpx.Response(200, json={"Response": {"Data": {"Accounts": [
                          {"CapacityRemainPrecise": 10, "ExpiredTime": NOW + 86400}
                      ]}}})])
    async def headers(_):
        return {}
    async def good_headers(_):
        return {"Authorization": "synthetic"}
    monkeypatch.setattr(auth, "get_billing_headers", headers)
    assert asyncio.run(auth.fetch_account_resources(db.get_account(a), force=True))["stale"]
    assert not db.get_account_resource_cache(a)["route_last_attempt_ok"]
    monkeypatch.setattr(auth, "get_billing_headers", good_headers)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(auth.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda _: next(responses)), **kwargs))
    failed = asyncio.run(auth.fetch_account_resources(db.get_account(a)))
    assert failed["ok"] and failed["stale"]
    recovered = asyncio.run(auth.fetch_account_resources(db.get_account(a)))
    assert recovered["route_last_attempt_ok"] and not recovered["stale"]


def test_refresh_worker_limits_concurrency_and_continues_after_one_failure(isolated_db, monkeypatch):
    for name in ("a", "b", "c", "d"):
        add_account(name)
    enable()
    concurrent = 0
    peak = 0
    visited = []
    async def fetch(account, **kwargs):
        nonlocal concurrent, peak
        visited.append(account["id"])
        concurrent += 1
        peak = max(peak, concurrent)
        try:
            await asyncio.sleep(0)
            if account["name"] == "a":
                raise RuntimeError("synthetic failure")
        finally:
            concurrent -= 1
    monkeypatch.setattr(auth, "fetch_account_resources", fetch)
    asyncio.run(routing_refresh.RoutingRefresher().refresh_round(config(), set()))
    assert len(visited) == 4 and peak == 2


def test_lifespan_has_no_legacy_network_then_warms_and_cancels(isolated_db, monkeypatch):
    a = add_account()
    started = asyncio.Event()
    stopped = asyncio.Event()
    async def fetch(account, **kwargs):
        assert account["id"] == a
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    monkeypatch.setattr(auth, "fetch_account_resources", fetch)
    async def run():
        async with server.app.router.lifespan_context(server.app):
            await asyncio.sleep(0)
            assert not started.is_set()
            enable()
            assert routing_refresh.notify()
            await asyncio.wait_for(started.wait(), 1)
        assert stopped.is_set()
        assert server.app.state.routing_refresher.task is None
        assert not routing_refresh.notify(a)
    asyncio.run(run())


def test_worker_rechecks_disabled_accounts_and_policy(isolated_db, monkeypatch):
    a = add_account()
    enable()
    db.update_account(a, {"status": "inactive"})
    async def fetch(*args, **kwargs):
        pytest.fail("disabled account queried")
    monkeypatch.setattr(auth, "fetch_account_resources", fetch)
    asyncio.run(routing_refresh.RoutingRefresher().refresh_round(config(), {a}))
    db.update_account(a, {"status": "active"})
    db.set_setting(routing.SETTING, {"mode": "legacy"})
    asyncio.run(routing_refresh.RoutingRefresher().refresh_round(config(), {a}))


def test_cancelled_resource_query_revokes_trust_and_releases_lock(isolated_db, monkeypatch):
    a = add_account()
    started = asyncio.Event()
    async def headers(_):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(auth, "get_billing_headers", headers)
    async def run():
        task = asyncio.create_task(auth.fetch_account_resources(db.get_account(a), force=True))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not db.get_account_resource_cache(a)["route_last_attempt_ok"]
        async def unavailable(_):
            return None
        monkeypatch.setattr(auth, "get_billing_headers", unavailable)
        await asyncio.wait_for(auth.fetch_account_resources(db.get_account(a), force=True), 1)
    asyncio.run(run())


def test_background_query_rechecks_policy_after_waiting_for_manual_refresh(isolated_db, monkeypatch):
    a = add_account()
    enable()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []
    async def headers(_):
        calls.append(1)
        entered.set()
        await release.wait()
        return None
    monkeypatch.setattr(auth, "get_billing_headers", headers)
    async def run():
        manual = asyncio.create_task(auth.fetch_account_resources(db.get_account(a), force=True))
        await entered.wait()
        worker = asyncio.create_task(auth.fetch_account_resources(db.get_account(a), force=True, routing_only=True))
        await asyncio.sleep(0)
        db.set_setting(routing.SETTING, {"mode": "legacy"})
        release.set()
        await manual
        result = await worker
        assert result["message"] == "routing refresh is disabled"
    asyncio.run(run())
    assert len(calls) == 1


def test_successful_claim_queues_resource_refresh_in_active_lifespan(isolated_db, monkeypatch):
    import control_plane
    a = add_account()
    enable()
    async def claim(_):
        return {"ok": True, "claimed": True}
    async def fetch(*args, **kwargs):
        return snapshot()
    monkeypatch.setattr(auth, "claim_daily_checkin", claim)
    monkeypatch.setattr(auth, "fetch_account_resources", fetch)
    async def run():
        async with server.app.router.lifespan_context(server.app):
            result = await control_plane.claim_one(db.get_account(a))
            assert result["resources_pending"] is True
            assert a in server.app.state.routing_refresher.pending
    asyncio.run(run())


def test_existing_expired_token_fallback_still_works(isolated_db, monkeypatch):
    a = add_account(status="expired")
    enable()
    async def refresh(account):
        db.update_account(account["id"], {"status": "active"})
        return True
    monkeypatch.setattr(auth, "refresh_token", refresh)
    assert asyncio.run(auth.pick_account_with_fallback())["id"] == a


def test_configuration_wakes_running_worker_via_settings_api(isolated_db, monkeypatch):
    a = add_account()
    visited = asyncio.Event()
    async def fetch(account, **kwargs):
        assert account["id"] == a
        visited.set()
        return snapshot()
    monkeypatch.setattr(auth, "fetch_account_resources", fetch)
    async def run():
        async with server.app.router.lifespan_context(server.app):
            await asyncio.sleep(0)
            assert not visited.is_set()
            transport = httpx.ASGITransport(app=server.app, client=("127.0.0.1", 12345))
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8788") as client:
                result = await client.put("/admin/settings", json={routing.SETTING: config()})
                assert result.status_code == 200
                await asyncio.wait_for(visited.wait(), 1)
    asyncio.run(run())


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/responses"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("fail_early", [False, True])
def test_real_endpoint_routes_and_retries_with_actual_account_logs(
    isolated_db, monkeypatch, path, stream, fail_early
):
    import proxy
    early = add_account("early", days=1)
    later = add_account("later", days=10)
    enable()
    key = "sk-cb-synthetic-routing-client"
    db.add_api_key(key, "routing-test")
    auth._set_sticky_account(later)
    attempts = []
    async def headers(account):
        return {"Authorization": str(account["id"])}
    async def no_delay(_):
        pass
    def upstream(req):
        aid = int(req.headers["Authorization"])
        attempts.append(aid)
        if fail_early and aid == early:
            return httpx.Response(429, json={"error": {"message": "synthetic exhausted account"}})
        chunks = [
            {"id": "chatcmpl-routing", "object": "chat.completion.chunk", "created": NOW,
             "model": "auto", "choices": [{"index": 0, "delta": {"role": "assistant", "content": f"account-{aid}"}, "finish_reason": None}]},
            {"id": "chatcmpl-routing", "object": "chat.completion.chunk", "created": NOW,
             "model": "auto", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "credit": 3}},
        ]
        data = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, content=data.encode(), headers={"Content-Type": "text/event-stream"})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(auth, "get_valid_headers", headers)
    monkeypatch.setattr(proxy, "_retry_delay", no_delay)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(upstream), **kwargs))
    async def run():
        transport = httpx.ASGITransport(app=server.app, client=("127.0.0.1", 12345))
        async with real_client(transport=transport, base_url="http://127.0.0.1:8788") as client:
            payload = {"model": "auto", "stream": stream}
            if path.endswith("responses"):
                payload["input"] = "hello"
            else:
                payload["messages"] = [{"role": "user", "content": "hello"}]
            return await client.post(path, json=payload, headers={"Authorization": "Bearer " + key})
    result = asyncio.run(run())
    expected = later if fail_early else early
    assert result.status_code == 200
    assert f"account-{expected}" in result.text
    assert attempts == ([early, later] if fail_early else [early])
    if stream:
        assert ("response.completed" if path.endswith("responses") else "[DONE]") in result.text
    with db.connection() as conn:
        success = conn.execute("SELECT account_id FROM logs WHERE status_code=200").fetchall()
    assert [row["account_id"] for row in success] == [expected]
    assert db.get_account(expected)["total_credits"] == 3


def test_targeted_refresh_notifications_do_not_starve_full_rounds(isolated_db, monkeypatch):
    a = add_account()
    enable()
    quick_config = {**config(), "refresh_interval_seconds": 0.02}
    monkeypatch.setattr(routing, "load_config", lambda: quick_config)
    rounds = []
    async def run():
        worker = routing_refresh.RoutingRefresher()
        async def refresh(_config, requested, *, only_requested=False):
            rounds.append(only_requested)
        monkeypatch.setattr(worker, "refresh_round", refresh)
        worker.start()
        try:
            for _ in range(6):
                await asyncio.sleep(0.01)
                routing_refresh.notify(a)
            assert rounds.count(False) >= 2
            assert True in rounds
        finally:
            await worker.stop()
    asyncio.run(run())


def test_inflight_stream_stays_on_account_when_next_request_switches(isolated_db, monkeypatch):
    import proxy
    early = add_account("early")
    later = add_account("later", days=10)
    enable()
    attempts = []
    async def headers(account):
        return {"Authorization": str(account["id"])}
    class UpstreamStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            first = {"id": "chatcmpl-inflight", "object": "chat.completion.chunk", "created": NOW,
                     "model": "auto", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "first"}, "finish_reason": None}]}
            yield ("data: " + json.dumps(first) + "\n\n").encode()
            db.account_increment_usage(early, 0, 100)
            assert auth.pick_account()["id"] == later
            last = {"id": "chatcmpl-inflight", "object": "chat.completion.chunk", "created": NOW,
                    "model": "auto", "choices": [{"index": 0, "delta": {"content": "last"}, "finish_reason": "stop"}]}
            yield ("data: " + json.dumps(last) + "\n\ndata: [DONE]\n\n").encode()
    def upstream(req):
        attempts.append(int(req.headers["Authorization"]))
        return httpx.Response(200, stream=UpstreamStream(), headers={"Content-Type": "text/event-stream"})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(auth, "get_valid_headers", headers)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(upstream), **kwargs))
    async def run():
        kind, chunks = await proxy.proxy_chat_completions({"model": "auto", "stream": True,
            "messages": [{"role": "user", "content": "hello"}]})
        assert kind == "stream"
        return b"".join([chunk async for chunk in chunks])
    output = asyncio.run(run())
    assert b'first' in output and b'last' in output and output.count(b'[DONE]') == 1
    assert attempts == [early]
    with db.connection() as conn:
        assert conn.execute("SELECT account_id FROM logs").fetchone()["account_id"] == early


def test_background_timeout_revokes_cache_trust_and_finishes_round(isolated_db, monkeypatch):
    a = add_account()
    enable()
    async def headers(_):
        await asyncio.Event().wait()
    real_wait_for = asyncio.wait_for
    async def short_timeout(awaitable, timeout):
        return await real_wait_for(awaitable, 0.01 if timeout == 25 else timeout)
    monkeypatch.setattr(auth, "get_billing_headers", headers)
    monkeypatch.setattr(routing_refresh.asyncio, "wait_for", short_timeout)
    asyncio.run(routing_refresh.RoutingRefresher().refresh_round(config(), {a}))
    assert db.get_account_resource_cache(a)["route_last_attempt_ok"] is False


def test_future_timestamp_cannot_suppress_official_refresh(isolated_db, monkeypatch):
    a = add_account()
    db.upsert_account_resource_cache(a, snapshot(updated_at=NOW + 86400))
    queried = []
    async def headers(account):
        queried.append(account["id"])
        return None
    monkeypatch.setattr(auth, "get_billing_headers", headers)
    result = asyncio.run(auth.fetch_account_resources(db.get_account(a)))
    assert queried == [a]
    assert result["route_last_attempt_ok"] is False
