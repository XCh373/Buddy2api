import asyncio
import copy
import json
from pathlib import Path

import pytest

import context_budget


def _long_chat(count=7, size=200):
    messages = [{"role": "system", "content": "keep all constraints"}]
    for index in range(count):
        messages.append({"role": "user", "content": "旧" * size, "turn": index})
    messages.append({"role": "user", "content": "最新问题"})
    return {"model": "GLM-5.3-Flash", "messages": messages}


@pytest.mark.parametrize("channel", ["workbuddy", "loomy"])
@pytest.mark.parametrize("stream", [False, True])
def test_repeated_context_rejection_preserves_all_history_without_retry(channel, stream, monkeypatch):
    monkeypatch.setenv("CB_LOOMY_CONTEXT_BUDGET", "10")
    payload = _long_chat()
    payload["messages"][2:2] = [
        {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "must retain result"},
    ]
    original = copy.deepcopy(payload)
    calls, closed = [], []

    async def source():
        try:
            yield b'data: {"error":{"code":11115,"message":"synthetic"}}\n\n'
        finally:
            closed.append(True)

    async def chat(body, info):
        calls.append(copy.deepcopy(body))
        return ("stream", source()) if stream else ("error", (400, {"error": {"code": 11115}}))

    async def run():
        for _ in range(2):
            result = await context_budget.dispatch_with_context_policy(
                type("P", (), {"chat_completions": staticmethod(chat)})(), channel, payload, {},
            )
            if stream:
                wire = b"".join([chunk async for chunk in result[1]])
                error = json.loads(wire.decode().removeprefix("data: ").strip())["error"]
            else:
                assert result[1][0] == 400
                error = result[1][1]["error"]
            assert error["code"] == "context_length_exceeded"
            assert "网关未删减历史" in error["message"]
    asyncio.run(run())
    assert calls == [original, original]
    assert payload == original
    assert len(closed) == (2 if stream else 0)


@pytest.mark.parametrize("channel", ["workbuddy", "loomy"])
def test_success_forwards_intact_history_and_usage(channel, monkeypatch):
    monkeypatch.setenv("CB_LOOMY_CONTEXT_BUDGET", "10")
    payload = _long_chat()
    original = copy.deepcopy(payload)
    async def chat(body, info):
        assert body == original
        return ("json", {"usage": {"prompt_tokens": 12345}})
    result = asyncio.run(context_budget.dispatch_with_context_policy(
        type("P", (), {"chat_completions": staticmethod(chat)})(), channel, payload, {},
    ))
    assert result == ("json", {"usage": {"prompt_tokens": 12345}})
    assert payload == original


@pytest.mark.parametrize("detail", [
    {"code": 11133, "message": "Invalid request parameters"},
    {"error": {"message": "<html>502 Bad Gateway</html>"}},
    {"error": {"message": "tool name too long"}},
    {"error": {"message": "请求超出速率限制"}},
    {"error": {"message": "读取上下文时发生服务器故障"}},
    {"error": {"code": "cancelled", "message": "cancelled"}},
])
def test_server_faults_and_generic_limits_are_not_context_errors(detail):
    assert not context_budget.is_context_rejection(502, detail, prompt_tokens=1000000)


@pytest.mark.parametrize("detail", [
    {"code": 11115},
    {"error": {"code": "context_length_exceeded"}},
    {"error": {"code": "CONTEXT_WINDOW_EXCEEDED"}},
    {"error": {"message": "maximum context length is 1000 tokens"}},
    {"error": {"message": "上下文超过上游限制"}},
])
def test_explicit_context_rejections_are_recognized(detail):
    assert context_budget.is_context_rejection(400, detail, prompt_tokens=1)


def test_large_html_error_is_rewritten_without_retry():
    calls = []
    async def chat(payload, info):
        calls.append(payload)
        return ("error", (502, {"error": {"message": "<html>gateway</html>"}}))
    result = asyncio.run(context_budget.dispatch_with_context_policy(
        type("P", (), {"chat_completions": staticmethod(chat)})(), "workbuddy", _long_chat(7, 150000), {},
    ))
    assert len(calls) == 1
    assert result[1][0] == 502
    assert result[1][1]["error"]["code"] == "upstream_html"
    assert "<html>" not in result[1][1]["error"]["message"]


def test_settings_explains_legacy_budget_does_not_trim_or_reject():
    text = Path("web/index.html").read_text(encoding="utf-8")
    budget = text.split('<div class="card-h">上下文预算', 1)[1].split('<div class="card-h">后端参数', 1)[0]
    assert 'v-model.number="s.loomy_context_budget"' in budget
    assert "网关不再按此值裁剪或拒绝请求" in budget
