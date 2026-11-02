"""BUD-010 / BUD-011 / BUD-012 的回归测试。

* BUD-010: `cached_tokens` 缺失必须是 None，不能填 0（否则真实 miss 与字段缺失无法区分）
* BUD-011: 非 function 工具被丢弃时必须可被调用方感知
* BUD-012: 日志带 request_id / attempt / failure_class，一次请求的多跳可关联
"""

import pathlib
import tempfile

import pytest

import credential_crypto
import database as db
import proxy
import responses


# ---------------------------------------------------------------- BUD-010
def test_responses_usage_missing_cached_tokens_is_null():
    usage = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
    details = responses._responses_usage(usage)["input_tokens_details"]
    assert details["cached_tokens"] is None, "缺失必须记 None，不能填 0"
    assert "cache_write_tokens" not in details


def test_responses_usage_real_zero_cached_tokens_stays_zero():
    usage = {
        "prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
        "prompt_tokens_details": {"cached_tokens": 0},
    }
    details = responses._responses_usage(usage)["input_tokens_details"]
    assert details["cached_tokens"] == 0, "上游明确报 0 时必须保留 0"


def test_responses_usage_hit_and_cache_write():
    usage = {
        "prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
        "prompt_tokens_details": {"cached_tokens": 80, "cache_write_tokens": 12},
    }
    details = responses._responses_usage(usage)["input_tokens_details"]
    assert details["cached_tokens"] == 80
    assert details["cache_write_tokens"] == 12


def test_proxy_cache_usage_distinguishes_missing_from_zero():
    assert proxy._cache_usage({"prompt_tokens": 1})["cached_tokens"] is None
    assert proxy._cache_usage(
        {"prompt_tokens_details": {"cached_tokens": 0}}
    )["cached_tokens"] == 0
    assert proxy._cache_usage(None)["cached_tokens"] is None


# ---------------------------------------------------------------- BUD-011
def test_dropped_tools_are_reported():
    payload = {
        "model": "auto",
        "input": [{"type": "message", "role": "user",
                   "content": [{"type": "input_text", "text": "hi"}]}],
        "tools": [
            {"type": "function", "name": "f", "parameters": {}},
            {"type": "web_search"},
            {"type": "file_search", "name": "fs"},
        ],
    }
    dropped = responses.dropped_tool_types(payload)
    assert [d["type"] for d in dropped] == ["web_search", "file_search"]

    chat = responses.responses_to_chat(payload)
    # function 工具保留，其它丢弃
    assert len(chat["tools"]) == 1
    # 丢弃信息挂在内部键上，供 HTTP 层读，不发给上游
    assert chat["_dropped_tools"] == dropped
    assert "_dropped_tools" not in proxy.build_backend_body(chat)


def test_no_dropped_tools_when_all_function():
    payload = {"model": "auto", "input": [], "tools": [{"type": "function", "name": "f"}]}
    assert responses.dropped_tool_types(payload) == []


# ---------------------------------------------------------------- BUD-012
@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "logs.db")
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "pytest-logs-key")
    credential_crypto.reset_cache()
    db.init_db()
    yield
    credential_crypto.reset_cache()


def test_logs_carry_request_id_attempt_and_failure_class(isolated_db):
    db.record_request({
        "api_key_name": "k", "account_name": "a", "model": "m",
        "status_code": 429, "finish_reason": "retry",
        "request_id": "req-abc", "attempt": 2, "failure_class": "upstream_http",
        "cached_tokens": None, "cache_write_tokens": None,
    })
    db.record_request({
        "api_key_name": "k", "account_name": "b", "model": "m",
        "status_code": 200, "finish_reason": "stop",
        "request_id": "req-abc", "attempt": 3, "failure_class": None,
        "cached_tokens": 80,
    })
    rows = db.get_conn().execute(
        "SELECT request_id, attempt, failure_class, cached_tokens, account_name "
        "FROM logs WHERE request_id='req-abc' ORDER BY attempt"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["attempt"] == 2 and rows[0]["failure_class"] == "upstream_http"
    assert rows[0]["cached_tokens"] is None, "未报告缓存应为 NULL"
    assert rows[1]["attempt"] == 3 and rows[1]["cached_tokens"] == 80
    # 同一 request_id 把两跳（换号前后）串起来
    assert {r["account_name"] for r in rows} == {"a", "b"}


def test_new_request_id_is_unique():
    ids = {proxy._new_request_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(len(i) == 16 for i in ids)
