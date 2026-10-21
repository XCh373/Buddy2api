import asyncio

import pytest

import credential_crypto
import database as db
from providers.loomy.headers import model_headers
from providers.loomy.models import parse_supplier_models
from providers.loomy.quota import parse_quota
from providers.loomy.signer import sign, string_to_sign
from providers.loomy.store import session_to_account


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "gateway.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "pytest-master-key")
    credential_crypto.reset_cache()
    db.init_db()
    yield path
    credential_crypto.reset_cache()


def test_signer_matches_client_vector():
    body = '{"base":{"appid":"GM3LOOMY"}}'
    got = string_to_sign(
        method="post",
        path="/login/account/getPuKey",
        body=body,
        date="Sat, 26 Sep 2026 12:00:00 GMT",
        nonce="11111111-2222-3333-4444-555555555555",
    )
    assert got == (
        "POST\n"
        "/login/account/getPuKey\n"
        "\n"
        "ZXFsDbtKIeoeelrBB+sO7g==\n"
        "application/json\n"
        "Sat, 26 Sep 2026 12:00:00 GMT\n"
        "11111111-2222-3333-4444-555555555555\n"
        "\n"
    )
    assert (
        sign(
            "TEST_SECRET",
            method="POST",
            path="/login/account/getPuKey",
            body=body,
            date="Sat, 26 Sep 2026 12:00:00 GMT",
            nonce="11111111-2222-3333-4444-555555555555",
        )
        == "hu4CwF8SGwW9+0aCwfU7idd7gho="
    )


def test_session_import_and_model_headers():
    parsed = session_to_account(
        {"session": "sess-1", "userid": "u1", "phone": "13800000000", "updatedAt": 1_700_000_000_000}
    )
    assert parsed["provider"] == "loomy"
    assert parsed["access_token"] == "sess-1"
    assert parsed["uid"] == "u1"
    headers = model_headers("sess-1")
    assert headers["Authorization"] == "Bearer sess-1"
    assert headers["token"] == "sess-1"
    assert headers["traceparent"].startswith("00-")
    assert headers["loomy-version"]
    assert "devid" not in headers


def test_models_and_daily_balance_parse():
    models = parse_supplier_models(
        {"data": [{"id": "provider/spark-x", "name": "Spark", "context_length": 1024}]}
    )
    assert models[0]["id"] == "spark-x"
    assert models[0]["context_window"] == 1024
    quota = parse_quota({"data": {"balance": 10, "dailyBalance": 5000, "availableBalance": 5010}})
    assert quota["daily_balance"] == 5000
    assert quota["available"] == 5010


def test_status_read_keeps_today_claim_when_upstream_flakes(isolated_db, monkeypatch):
    from providers.loomy import quota

    account_id = db.add_account(
        {
            "name": "loomy",
            "uid": "u-flake",
            "provider": "loomy",
            "access_token": "sess",
            "expires_at": 9_999_999_999_999,
        }
    )
    account = db.get_account(account_id)
    calls = {"n": 0}

    async def fake_points(_account):
        calls["n"] += 1
        if calls["n"] == 1:
            return 200, {"data": {"balance": 1, "dailyBalance": 80, "availableBalance": 81}}
        return 503, {}

    monkeypatch.setattr(quota, "_points", fake_points)
    first = asyncio.run(quota.fetch_checkin(account))
    second = asyncio.run(quota.fetch_checkin(account))
    assert first["ok"] is True
    assert first["already_claimed"] is True
    assert second["ok"] is True
    assert second["already_claimed"] is True
    assert second["stale"] is True
    assert second["credit"] == 80
    assert "HTTP 503" in second["message"]


def test_status_read_refreshes_once_after_auth_failure(isolated_db, monkeypatch):
    from providers.loomy import quota

    account_id = db.add_account(
        {
            "name": "loomy",
            "uid": "u-auth",
            "provider": "loomy",
            "access_token": "old",
            "expires_at": 9_999_999_999_999,
        }
    )
    account = db.get_account(account_id)
    calls = {"n": 0, "refreshed": 0}

    async def fake_points(row):
        calls["n"] += 1
        if calls["n"] == 1:
            return 401, {}
        assert row["access_token"] == "fresh"
        return 200, {"data": {"dailyBalance": 40, "availableBalance": 40}}

    async def fake_refresh(row):
        calls["refreshed"] += 1
        return {**row, "access_token": "fresh"}

    monkeypatch.setattr(quota, "_points", fake_points)
    monkeypatch.setattr(quota, "refresh_account", fake_refresh)
    result = asyncio.run(quota.fetch_checkin(account))
    assert calls["refreshed"] == 1
    assert result["ok"] is True
    assert result["already_claimed"] is True
    assert result["credit"] == 40


def test_claim_reads_existing_daily_balance(isolated_db, monkeypatch):
    from providers.loomy import quota

    account_id = db.add_account(
        {
            "name": "loomy",
            "uid": "u1",
            "provider": "loomy",
            "access_token": "sess",
            "expires_at": 9_999_999_999_999,
        }
    )
    account = db.get_account(account_id)

    async def fake_points(_account):
        return 200, {"data": {"balance": 1, "dailyBalance": 80, "availableBalance": 81}}

    async def fake_refresh(row):
        return row

    monkeypatch.setattr(quota, "_points", fake_points)
    monkeypatch.setattr(quota, "refresh_account", fake_refresh)
    result = asyncio.run(quota.claim_checkin(account))
    assert result["ok"] is True
    assert result["already_claimed"] is True
    assert result["credit"] == 80


def test_account_test_builds_body_before_request(isolated_db, monkeypatch):
    from providers.loomy import chat

    account_id = db.add_account(
        {
            "name": "loomy",
            "uid": "u-test",
            "provider": "loomy",
            "access_token": "sess",
            "expires_at": 9_999_999_999_999,
        }
    )

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"choices": [{"message": {"content": "pong"}}]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            assert "chat/completions" in url
            assert "ping" in kwargs["content"]
            return FakeResponse()

    monkeypatch.setattr(chat.httpx, "AsyncClient", FakeClient)
    result = asyncio.run(chat.test_chat(db.get_account(account_id), "auto", "ping"))
    assert result["ok"] is True
    assert result["message"] == "pong"


def test_refresh_probes_valid_session_without_password(isolated_db, monkeypatch):
    from providers.loomy import token

    account_id = db.add_account(
        {
            "name": "loomy",
            "uid": "missing-local",
            "provider": "loomy",
            "access_token": "sess",
            "expires_at": 9_999_999_999_999,
        }
    )

    class FakeResponse:
        status_code = 200

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            assert "points/records" in url
            assert kwargs["headers"]["token"] == "sess"
            return FakeResponse()

    monkeypatch.setattr(token.httpx, "AsyncClient", FakeClient)
    fresh = asyncio.run(token.refresh_account(db.get_account(account_id)))
    assert fresh["access_token"] == "sess"


def test_refresh_names_empty_transport_error_and_retries(isolated_db, monkeypatch):
    from providers.loomy import token

    account_id = db.add_account(
        {
            "name": "loomy",
            "uid": "missing-local",
            "provider": "loomy",
            "access_token": "sess",
            "expires_at": 9_999_999_999_999,
        }
    )
    calls = {"n": 0}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise token.httpx.ConnectTimeout("")
            class FakeResponse:
                status_code = 200
            return FakeResponse()

    monkeypatch.setattr(token, "_PROBE_RETRY_SECONDS", 0)
    monkeypatch.setattr(token.httpx, "AsyncClient", FakeClient)
    fresh = asyncio.run(token.refresh_account(db.get_account(account_id)))
    assert calls["n"] == 2
    assert fresh["access_token"] == "sess"

    class AlwaysTimeout:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            raise token.httpx.ReadTimeout("")

    monkeypatch.setattr(token.httpx, "AsyncClient", AlwaysTimeout)
    with pytest.raises(token.LoomyAuthError) as caught:
        asyncio.run(token.refresh_account(db.get_account(account_id)))
    assert str(caught.value) == "Loomy 保活请求失败: ReadTimeout"
