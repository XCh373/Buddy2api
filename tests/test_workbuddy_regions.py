import pytest

import credential_crypto
import database as db
from auth_manager import workbuddy_channel_for_domain


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "gateway.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "pytest-master-key")
    credential_crypto.reset_cache()
    db.init_db()
    yield path
    credential_crypto.reset_cache()


def test_international_domain_uses_its_own_channel():
    assert workbuddy_channel_for_domain("www.workbuddy.ai") == "workbuddy-i"
    assert workbuddy_channel_for_domain("www.codebuddy.cn") == "workbuddy"


def test_intl_refresh_uses_workbuddy_token_refresh(isolated_db, monkeypatch):
    import asyncio

    import control_plane

    called = {}

    async def fake_refresh(account):
        called["provider"] = account["provider"]
        return {"ok": True, "provider": account["provider"], "status": "active"}

    monkeypatch.setattr(control_plane.auth_manager, "refresh_token_result", fake_refresh)
    account = {
        "id": 3,
        "provider": "workbuddy-i",
        "name": "intl",
        "status": "active",
        "refresh_token": "rt",
    }
    result = asyncio.run(control_plane.refresh_one(account))
    assert called["provider"] == "workbuddy-i"
    assert result["ok"] is True
    assert result.get("reason") != "unsupported"


def test_intl_only_key_moves_to_workbuddy_i(isolated_db):
    db.add_account({
        "name": "intl",
        "uid": "intl-user",
        "provider": "workbuddy",
        "domain": "www.workbuddy.ai",
        "access_token": "a",
        "status": "active",
    })
    raw = "sk-cb-intl-filter"
    db.add_api_key(
        raw,
        "intl",
        default_channel="workbuddy",
        account_domain_filter=["*.workbuddy.ai"],
    )
    db.init_db()
    account = db.list_accounts(provider="workbuddy-i")[0]
    assert account["uid"] == "intl-user"
    saved = db.get_api_key_by_key(raw)
    assert saved["default_channel"] == "workbuddy-i"
    assert saved["account_domain_filter"] is None
