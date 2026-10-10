import asyncio
import json

import httpx
import pytest

import auth_manager
import credential_crypto
import database as db
import server
import wb_atrest


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "gateway.db")
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "pytest-master-key")
    credential_crypto.reset_cache()
    db.init_db()
    yield
    credential_crypto.reset_cache()


@pytest.fixture
def workbuddy_cipher(monkeypatch):
    monkeypatch.setenv("WORKBUDDY_AT_REST_SECRET", "synthetic-workbuddy-test-secret")
    monkeypatch.setattr(wb_atrest, "_cached_key", None)
    yield
    monkeypatch.setattr(wb_atrest, "_cached_key", None)


def _encrypted_auth():
    return {
        "account": {
            "uid": wb_atrest.encrypt_auth_field("uid-test-123"),
            "nickname": wb_atrest.encrypt_auth_field("WorkBuddy Test"),
            "phoneNumber": wb_atrest.encrypt_auth_field("13800000000"),
        },
        "auth": {
            "accessToken": wb_atrest.encrypt_auth_field("access-token-one"),
            "refreshToken": wb_atrest.encrypt_auth_field("refresh-token-one"),
            "expiresAt": 1_900_000_000_000,
            "refreshExpiresAt": 2_000_000_000_000,
            "domain": "www.codebuddy.cn",
        },
    }


def test_encrypted_workbuddy_snapshot_is_discoverable_and_importable(tmp_path, workbuddy_cipher):
    path = tmp_path / "encrypted.info"
    path.write_text(json.dumps(_encrypted_auth()), encoding="utf-8")

    parsed = auth_manager.parse_auth_file(path)
    meta = auth_manager._safe_auth_file_meta(path, set())

    assert parsed["uid"] == "uid-test-123"
    assert parsed["access_token"] == "access-token-one"
    assert parsed["refresh_token"] == "refresh-token-one"
    assert meta["valid"] is True
    assert meta["account_name"] == "WorkBuddy Test"
    assert meta["uid_masked"] != "uid-test-123"


def test_manual_add_decrypts_and_updates_existing_workbuddy_by_uid(
    isolated_db, workbuddy_cipher
):
    payload = _encrypted_auth()
    parsed = auth_manager.parse_account_payload(payload)
    first_id, first_action = auth_manager.upsert_account(parsed, "workbuddy")

    payload["auth"]["accessToken"] = wb_atrest.encrypt_auth_field("access-token-two")
    parsed_again = auth_manager.parse_account_payload(payload)
    second_id, second_action = auth_manager.upsert_account(parsed_again, "workbuddy")

    rows = db.list_accounts(provider="workbuddy")
    assert first_id == second_id
    assert first_action == "imported"
    assert second_action == "updated"
    assert len(rows) == 1
    assert rows[0]["access_token"] == "access-token-two"


def test_manual_add_endpoint_decrypts_and_returns_update_status(
    isolated_db, workbuddy_cipher, monkeypatch
):
    monkeypatch.setattr(server, "_check_admin", lambda _authorization: None)

    async def post(payload):
        transport = httpx.ASGITransport(app=server.app, client=("127.0.0.1", 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8787") as client:
            return await client.post("/admin/accounts", json=payload)

    payload = {**_encrypted_auth(), "provider": "workbuddy"}
    first = asyncio.run(post(payload))
    payload["auth"]["accessToken"] = wb_atrest.encrypt_auth_field("access-token-two")
    second = asyncio.run(post(payload))

    assert first.status_code == second.status_code == 200
    assert first.json()["action"] == "imported"
    assert second.json()["action"] == "updated"
    assert first.json()["id"] == second.json()["id"]
    assert second.headers["cache-control"] == "no-store"
    assert len(db.list_accounts(provider="workbuddy")) == 1


def test_expiry_days_round_up_to_match_reminder_window():
    now = 1_700_000_000
    item = auth_manager._safe_resource_item(
        {"ExpiredTime": now + 30 * 86400 + 1}, now
    )
    assert item["days_to_expire"] == 31
