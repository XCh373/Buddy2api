import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

import auth_manager
import control_plane
import credential_crypto
import database as db
import server
from providers.workbuddy import oauth


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "gateway.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "pytest-master-key")
    monkeypatch.delenv("CB_GATEWAY_KEEPALIVE", raising=False)
    monkeypatch.delenv("CB_GATEWAY_KEEPALIVE_HOUR", raising=False)
    credential_crypto.reset_cache()
    db.init_db()
    auth_manager.reset_session_dead_counts()
    oauth.reset_sessions()
    control_plane._keepalive_running = False
    yield path
    auth_manager.reset_session_dead_counts()
    oauth.reset_sessions()
    control_plane._keepalive_running = False
    credential_crypto.reset_cache()


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.content = json.dumps(payload).encode()

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class QueueClient:
    queue = []
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def _next(self, method, url, headers=None, content=None):
        QueueClient.calls.append({"method": method, "url": url, "headers": headers or {}, "content": content})
        item = QueueClient.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        status, payload = item
        return FakeResponse(status, payload)

    async def request(self, method, url, headers=None, content=None):
        return self._next(method, url, headers, content)

    async def post(self, url, headers=None, json=None):
        return self._next("POST", url, headers, json)


def _install_queue(monkeypatch, target):
    QueueClient.queue = []
    QueueClient.calls = []
    monkeypatch.setattr(target, "AsyncClient", QueueClient)


def _add_account(**extra):
    payload = {
        "name": "Ada",
        "nickname": "Ada",
        "uid": "uid-1",
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "status": "active",
        "provider": "workbuddy",
        "domain": "www.codebuddy.cn",
        "expires_at": int(time.time() * 1000) + 3_600_000,
        "refresh_expires_at": int(time.time() * 1000) + 86_400_000,
    }
    payload.update(extra)
    return db.get_account(db.add_account(payload))


def test_transient_refresh_does_not_expire(isolated_db, monkeypatch):
    _install_queue(monkeypatch, auth_manager.httpx)
    account = _add_account()
    QueueClient.queue = [(500, {"code": 500, "msg": "upstream busy"})]

    result = asyncio.run(auth_manager.refresh_token_result(account))

    fresh = db.get_account(account["id"])
    assert result["ok"] is False
    assert result["reason"] == "transient"
    assert fresh["status"] == "active"
    assert fresh["access_token"] == "old-access"


def test_session_dead_expires_only_after_three_failures(isolated_db, monkeypatch):
    _install_queue(monkeypatch, auth_manager.httpx)
    account = _add_account()
    dead = (401, {"code": 12153, "msg": "Offline user session not found"})

    QueueClient.queue = [dead]
    first = asyncio.run(auth_manager.refresh_token_result(account))
    assert first["reason"] == "session_dead"
    assert first["session_dead_count"] == 1
    assert db.get_account(account["id"])["status"] == "active"

    QueueClient.queue = [dead, dead]
    asyncio.run(auth_manager.refresh_token_result(db.get_account(account["id"])))
    third = asyncio.run(auth_manager.refresh_token_result(db.get_account(account["id"])))
    assert third["session_dead_count"] == 3
    assert db.get_account(account["id"])["status"] == "expired"


def test_successful_refresh_keeps_old_refresh_token_and_revives(isolated_db, monkeypatch):
    _install_queue(monkeypatch, auth_manager.httpx)
    account = _add_account(status="expired")
    auth_manager._note_session_dead(account["id"])
    QueueClient.queue = [(200, {"code": 0, "data": {"accessToken": "new-access", "expiresIn": 120}})]

    result = asyncio.run(auth_manager.refresh_token_result(account))

    fresh = db.get_account(account["id"])
    assert result["ok"] is True
    assert fresh["status"] == "active"
    assert fresh["access_token"] == "new-access"
    assert fresh["refresh_token"] == "old-refresh"
    assert fresh["expires_at"] > int(time.time() * 1000)
    assert auth_manager.session_dead_count(account["id"]) == 0
    assert "old-refresh" not in json.dumps(result)
    assert "new-access" not in json.dumps(result)


def test_network_error_keeps_status(isolated_db, monkeypatch):
    _install_queue(monkeypatch, auth_manager.httpx)
    account = _add_account()
    QueueClient.queue = [httpx.ConnectError("down")]

    result = asyncio.run(auth_manager.refresh_token_result(account))

    assert result["reason"] == "transient"
    assert db.get_account(account["id"])["status"] == "active"


def test_keepalive_skips_inactive_and_reports_running(isolated_db, monkeypatch):
    monkeypatch.setattr(control_plane, "checkin_gap_seconds", lambda: 0)
    active = _add_account(uid="active-user", name="active")
    inactive = _add_account(uid="inactive-user", name="inactive", status="inactive")
    called = []

    async def fake_refresh(account):
        called.append(account["id"])
        return {
            "ok": True,
            "account_id": account["id"],
            "account_name": account["name"],
            "provider": "workbuddy",
            "reason": "refreshed",
            "message": "已续期",
            "status": "active",
            "skipped": False,
        }

    monkeypatch.setattr(auth_manager, "refresh_token_result", fake_refresh)
    summary = asyncio.run(control_plane.keepalive_all())
    assert called == [active["id"]]
    assert summary["refreshed"] == 1
    assert summary["skipped"] == 1
    assert any(row["account_id"] == inactive["id"] and row["skipped"] for row in summary["results"])

    control_plane._keepalive_running = True
    busy = asyncio.run(control_plane.keepalive_all())
    assert busy["running"] is True
    control_plane._keepalive_running = False


def test_seconds_until_hour_rolls_to_next_day():
    assert control_plane.seconds_until_hour(22, datetime(2026, 10, 13, 21, 30, 0)) == 1800
    assert control_plane.seconds_until_hour(22, datetime(2026, 10, 13, 22, 0, 0)) == 86400


def test_browser_login_stores_tokens_without_returning_them(isolated_db, monkeypatch):
    _install_queue(monkeypatch, oauth.httpx)
    state = "state-token-1"
    QueueClient.queue = [
        (200, {"code": 0, "data": {"state": state, "authUrl": "https://www.codebuddy.cn/login?state=" + state}})
    ]
    started = asyncio.run(oauth.start_login("cn"))
    assert started["url"].startswith("https://www.codebuddy.cn/")
    assert started["realm"] == "cn"
    assert QueueClient.calls[0]["url"].startswith("https://copilot.tencent.com/v2/plugin/auth/state")

    QueueClient.queue = [(200, {"code": 9, "msg": "logging in"})]
    pending = asyncio.run(oauth.poll_login(started["state"]))
    assert pending == {"done": False, "message": "logging in"}

    QueueClient.queue = [
        (
            200,
            {
                "code": 0,
                "data": {
                    "accessToken": "access-secret",
                    "refreshToken": "refresh-secret",
                    "expiresIn": 3600,
                    "domain": "www.codebuddy.cn",
                },
            },
        ),
        (
            200,
            {
                "code": 0,
                "data": {
                    "uid": "452a1489-03b5-4af3-a64c-5688c03f1e43",
                    "nickname": "Ada",
                    "enterpriseId": "ent-1",
                },
            },
        ),
    ]
    done = asyncio.run(oauth.poll_login(started["state"]))
    encoded = json.dumps(done)
    assert done["done"] is True
    assert done["updated"] is False
    assert "access-secret" not in encoded
    assert "refresh-secret" not in encoded
    saved = db.get_account(done["id"])
    assert saved["access_token"] == "access-secret"
    assert saved["refresh_token"] == "refresh-secret"
    assert saved["nickname"] == "Ada"
    assert saved["enterprise_id"] == "ent-1"
    assert saved["provider"] == "workbuddy"

    QueueClient.queue = []
    again = asyncio.run(oauth.poll_login(started["state"]))
    assert again["id"] == done["id"]
    assert QueueClient.calls[-1]["url"].endswith("/login/account?state=" + state)


def test_browser_login_rejects_unexpected_host_and_unknown_state(isolated_db, monkeypatch):
    _install_queue(monkeypatch, oauth.httpx)
    QueueClient.queue = [
        (200, {"code": 0, "data": {"state": "abcdefghij", "authUrl": "https://evil.example/login"}})
    ]
    with pytest.raises(ValueError, match="rejected"):
        asyncio.run(oauth.start_login("cn"))

    QueueClient.queue = [
        (200, {"code": 0, "data": {"state": "globalstate1", "authUrl": "https://www.workbuddy.ai/login"}})
    ]
    started = asyncio.run(oauth.start_login("global"))
    assert started["realm"] == "global"
    assert QueueClient.calls[-1]["url"].startswith("https://www.workbuddy.ai/v2/plugin/auth/state")
    with pytest.raises(LookupError):
        asyncio.run(oauth.poll_login("missing-state"))


def test_repeat_browser_login_updates_same_uid(isolated_db, monkeypatch):
    _install_queue(monkeypatch, oauth.httpx)
    existing = _add_account(uid="452a1489-03b5-4af3-a64c-5688c03f1e43", weight=7, priority=3)
    state = "state-token-2"
    QueueClient.queue = [
        (200, {"code": 0, "data": {"state": state, "authUrl": "https://copilot.tencent.com/oauth"}}),
        (
            200,
            {"code": 0, "data": {"accessToken": "access-2", "refreshToken": "refresh-2", "expiresIn": 60}},
        ),
        (200, {"code": 0, "data": {"uid": existing["uid"], "nickname": "Ada 2"}}),
    ]
    asyncio.run(oauth.start_login("cn"))
    done = asyncio.run(oauth.poll_login(state))
    assert done["updated"] is True
    assert done["id"] == existing["id"]
    fresh = db.get_account(existing["id"])
    assert fresh["access_token"] == "access-2"
    assert fresh["weight"] == 7
    assert fresh["priority"] == 3
    assert fresh["status"] == "active"


def _settings_request(payload):
    body = json.dumps(payload).encode()
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {"type": "http", "method": "PUT", "path": "/admin/settings", "headers": [], "query_string": b""}
    return Request(scope, receive)


def test_automation_defaults_off(isolated_db):
    assert control_plane.keepalive_enabled() is False
    assert control_plane.auto_checkin_enabled() is False
    assert control_plane.automation_due(datetime(2026, 10, 13, 9, 5), 9) is True
    assert control_plane.automation_due(datetime(2026, 10, 13, 9, 10), 9) is False


def test_one_click_keepalive_also_claims(isolated_db, monkeypatch):
    monkeypatch.setattr(control_plane, "checkin_gap_seconds", lambda: 0)

    async def fake_refresh(account, allow_inactive=True):
        return {
            "ok": True,
            "account_id": account["id"],
            "account_name": account["name"],
            "provider": "workbuddy",
            "reason": "refreshed",
            "message": "已续期",
            "status": "active",
            "skipped": False,
        }

    async def fake_checkin(channel_filter=None):
        return {"total": 1, "claimed": 1, "already_claimed": 0, "failed": 0, "credit": 150, "results": [{"account_id": 1, "ok": True, "claimed": True}]}

    monkeypatch.setattr(control_plane, "refresh_one", fake_refresh)
    monkeypatch.setattr(control_plane, "checkin_all", fake_checkin)
    _add_account()
    summary = asyncio.run(control_plane.keepalive_all(claim=True))
    assert summary["refreshed"] == 1
    assert summary["claimed"] == 1
    assert summary["claim"]["claimed"] == 1


def test_settings_save_keepalive(isolated_db, monkeypatch):
    monkeypatch.setattr(server, "_check_admin", lambda _authorization: None)
    asyncio.run(
        server.admin_update_settings(
            _settings_request(
                {
                    "keepalive_enabled": True,
                    "keepalive_hour": 7,
                    "auto_checkin_enabled": True,
                    "auto_checkin_hour": 8,
                }
            )
        )
    )
    assert control_plane.keepalive_enabled() is True
    assert control_plane.keepalive_hour() == 7
    assert control_plane.auto_checkin_enabled() is True
    assert control_plane.auto_checkin_hour() == 8

    with pytest.raises(HTTPException) as err:
        asyncio.run(server.admin_update_settings(_settings_request({"keepalive_hour": 24})))
    assert err.value.status_code == 400


def test_account_page_has_keepalive_and_login_controls():
    text = Path("web/index.html").read_text(encoding="utf-8")
    assert "一键保活" in text
    assert "浏览器登录" in text
    assert "/admin/accounts/keepalive-all" in text
    assert "/admin/workbuddy/login/start" in text
    assert "refreshLife" in text
    assert "keepalive_enabled" in text
    assert "auto_checkin_enabled" in text
    assert "默认关闭" in text
