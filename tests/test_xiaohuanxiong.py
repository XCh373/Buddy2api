import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import credential_crypto
import database as db
from providers.xiaohuanxiong.headers import client_headers
from providers.xiaohuanxiong.models import parse_supplier_models
from providers.xiaohuanxiong.quota import _today_points, parse_balance, parse_bills
from providers.xiaohuanxiong.store import session_to_account


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "gateway.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "pytest-master-key")
    credential_crypto.reset_cache()
    db.init_db()
    yield path
    credential_crypto.reset_cache()


def test_auth_and_request_headers():
    parsed = session_to_account(
        {"access_token": "access-1", "refresh_token": "refresh-1", "user_id": "42", "name": "raccoon"}
    )
    assert parsed["provider"] == "xiaohuanxiong"
    assert parsed["uid"] == "42"
    headers = client_headers("access-1", json_body=True)
    assert headers["Authorization"] == "Bearer access-1"
    assert headers["X-Client-Platform"] == "desktop-windows"
    assert headers["X-Client-Version"] == "v1.0.0"
    assert headers["X-Raccoon-Language"] == "zh"
    assert headers["User-Agent"]


def test_catalog_and_balance_parse():
    models = parse_supplier_models(
        {
            "data": {
                "categories": [
                    {
                        "models": [
                            {"name": "sn-glm-5-3-flash", "context_window": 128000, "visible": True},
                            {"name": "hidden", "visible": False},
                        ]
                    }
                ]
            }
        }
    )
    assert [item["id"] for item in models] == ["sn-glm-5-3-flash"]
    assert models[0]["context_window"] == 128000
    balance = parse_balance({"code": 0, "data": {"available_points": 6000, "daily_points": 300}})
    assert balance["available"] == 6000
    assert balance["daily"] == 300


def test_today_bill_counts_as_claimed():
    bills = parse_bills(
        {"data": [{"points": 300, "created_at": "2026-10-15T01:02:03+08:00", "event_name": "daily"}]}
    )
    now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone(timedelta(hours=8)))
    assert _today_points(bills, now=now) == 300


def test_claim_reports_existing_daily_bill(isolated_db, monkeypatch):
    from providers.xiaohuanxiong import quota

    account_id = db.add_account(
        {
            "name": "xhx",
            "uid": "42",
            "provider": "xiaohuanxiong",
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_at": 9_999_999_999_999,
        }
    )
    account = db.get_account(account_id)

    async def fake_refresh(row):
        return row

    async def fake_get(url, token):
        assert token == "access"
        if url.endswith("/setting_info"):
            return 200, {"code": 0, "data": {}}
        return 200, {"data": [{"points": 20, "created_at": datetime.now().astimezone().isoformat(), "event_name": "daily"}]}

    monkeypatch.setattr(quota, "refresh_account", fake_refresh)
    monkeypatch.setattr(quota, "_get", fake_get)
    result = asyncio.run(quota.claim_checkin(account))
    assert result["ok"] is True
    assert result["already_claimed"] is True
    assert result["credit"] == 20
