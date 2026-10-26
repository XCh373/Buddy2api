import asyncio
import json
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


class _Response:
    def __init__(self, status, body="", lines=None, payload=None):
        self.status_code = status
        self.text = body
        self._body = body.encode("utf-8")
        self._lines = lines or []
        self._payload = payload

    async def aread(self):
        return self._body

    def json(self):
        if self._payload is not None:
            return self._payload
        return json.loads(self.text)

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


def _patch_upstream(monkeypatch, responses):
    import httpx

    import auth_manager
    from providers.xiaohuanxiong import chat

    queue = list(responses)
    logs = []

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def _next(self):
            return queue.pop(0)

        async def post(self, *_args, **_kwargs):
            return self._next()

        def stream(self, *_args, **_kwargs):
            return self._next()

    async def ready(account):
        return account

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    monkeypatch.setattr(chat, "_ready", ready)
    monkeypatch.setattr(
        auth_manager,
        "pick_account",
        lambda excluded, provider="": {"id": 9, "name": "raccoon", "access_token": "token"},
    )
    monkeypatch.setattr(auth_manager, "mark_account_failure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(auth_manager, "mark_account_success", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(db, "record_request", lambda data: logs.append(dict(data)))
    return chat, logs


def _brief(logs):
    return [
        (
            item["status_code"],
            item["finish_reason"],
            item["account_id"],
            item["increment_usage"],
            item["total_tokens"],
            item["error_msg"],
        )
        for item in logs
    ]


def _run(chat, stream):
    payload = {"model": "auto", "messages": [{"role": "user", "content": "ping"}], "stream": stream}
    key = {"id": 4, "name": "Key"}
    if not stream:
        return asyncio.run(chat.chat_completions(payload, key))

    kind, generator = asyncio.run(chat.chat_completions(payload, key))

    async def collect():
        return [chunk async for chunk in generator]

    return kind, asyncio.run(collect())


def test_stream_records_final_usage_and_forwards_events(monkeypatch):
    lines = [
        'data: {"choices":[{"delta":{"content":"pong"}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":1,"total_tokens":4}}',
        "data: [DONE]",
    ]
    chat, logs = _patch_upstream(monkeypatch, [_Response(200, lines=lines)])
    kind, chunks = _run(chat, True)
    forwarded = b"".join(chunks).decode("utf-8")
    assert kind == "stream"
    assert "total_tokens" in forwarded
    assert "pong" in forwarded
    assert _brief(logs) == [(200, "stop", 9, True, 4, "")]


def test_stream_without_usage_keeps_zero_tokens(monkeypatch):
    chat, logs = _patch_upstream(monkeypatch, [
        _Response(200, lines=['data: {"choices":[{"delta":{"content":"pong"}}]}']),
    ])
    kind, _chunks = _run(chat, True)
    assert kind == "stream"
    assert _brief(logs) == [(200, "stop", 9, True, 0, "")]


def test_stream_retries_do_not_count_until_the_final_failure(monkeypatch):
    chat, logs = _patch_upstream(monkeypatch, [
        _Response(502, "bad gateway"),
        _Response(502, "bad gateway"),
        _Response(502, "bad gateway"),
    ])
    kind, chunks = _run(chat, True)
    assert kind == "stream"
    assert len(chunks) == 1
    assert b"bad gateway" in chunks[0]
    assert _brief(logs) == [
        (502, "retry", 9, False, 0, "bad gateway"),
        (502, "retry", 9, False, 0, "bad gateway"),
        (502, "error", 9, True, 0, "bad gateway"),
    ]


def test_nonstream_retry_keeps_raw_error_then_records_success(monkeypatch):
    chat, logs = _patch_upstream(monkeypatch, [
        _Response(502, "bad gateway"),
        _Response(200, payload={"choices": [{"finish_reason": "stop"}], "usage": {"total_tokens": 7}}),
    ])
    kind, data = _run(chat, False)
    assert kind == "json"
    assert data["usage"]["total_tokens"] == 7
    assert _brief(logs) == [
        (502, "retry", 9, False, 0, "bad gateway"),
        (200, "stop", 9, True, 7, ""),
    ]
