import asyncio

import context_budget
import router


def _long_chat(count: int, size: int = 200) -> dict:
    messages = [{"role": "system", "content": "keep"}]
    for index in range(count):
        messages.append({"role": "user", "content": "旧" * size, "turn": index})
    messages.append({"role": "user", "content": "最新问题"})
    return {"model": "GLM-5.3-Flash", "messages": messages}


def test_loomy_pretrim_drops_old_history_and_keeps_latest(monkeypatch):
    monkeypatch.setenv("CB_LOOMY_CONTEXT_BUDGET", "500")
    payload = _long_chat(30, 40)
    trimmed, changed = context_budget.prepare_dispatch("loomy", payload)
    assert changed is True
    assert trimmed["messages"][-1]["content"] == "最新问题"
    assert trimmed["messages"][0]["content"] == "keep"
    assert len(trimmed["messages"]) < len(payload["messages"])
    assert router.estimate_prompt_tokens(trimmed) <= context_budget.input_limit(payload, 500)


def test_workbuddy_is_not_pretrimmed(monkeypatch):
    monkeypatch.setenv("CB_LOOMY_CONTEXT_BUDGET", "500")
    payload = _long_chat(10, 40)
    trimmed, changed = context_budget.prepare_dispatch("workbuddy", payload)
    assert changed is False
    assert trimmed is payload


def test_zero_budget_disables_pretrim(monkeypatch):
    monkeypatch.setenv("CB_LOOMY_CONTEXT_BUDGET", "0")
    payload = _long_chat(10, 40)
    trimmed, changed = context_budget.prepare_dispatch("loomy", payload)
    assert changed is False
    assert trimmed is payload


def test_context_rejection_codes_and_html():
    assert context_budget.is_context_rejection(400, {"code": 11115, "msg": "context"}, prompt_tokens=10)
    assert context_budget.is_context_rejection(
        400, {"code": 11133, "msg": "Invalid request parameters"}, prompt_tokens=150_000
    )
    assert not context_budget.is_context_rejection(
        400, {"code": 11133, "msg": "Invalid request parameters"}, prompt_tokens=20
    )
    html = {"error": {"message": "<html><body>502 Bad Gateway</body></html>"}}
    assert context_budget.is_context_rejection(502, html, prompt_tokens=192_000)
    assert not context_budget.is_context_rejection(502, html, prompt_tokens=100)


def test_rejection_trims_once_then_returns_readable_error():
    calls = []

    async def chat(payload, info):
        calls.append((len(payload["messages"]), info.get("_context_retried")))
        return (
            "error",
            (502, {"error": {"message": "<html>bad gateway</html>", "code": 11115}}),
        )

    payload = _long_chat(12, 80)
    result = asyncio.run(
        context_budget.dispatch_with_context_policy(
            type("P", (), {"chat_completions": staticmethod(chat)})(),
            "loomy",
            payload,
            {},
        )
    )
    assert calls[0][1] is None
    assert calls[1][1] is True
    assert calls[1][0] < calls[0][0]
    assert len(calls) == 2
    assert result[0] == "error"
    assert result[1][0] == 400
    assert result[1][1]["error"]["code"] == "context_length_exceeded"
    assert "<html>" not in result[1][1]["error"]["message"]


def test_small_html_error_is_rewritten_without_retry():
    calls = []

    async def chat(payload, info):
        calls.append(payload)
        return ("error", (502, {"error": {"message": "<html>gateway</html>"}}))

    result = asyncio.run(
        context_budget.dispatch_with_context_policy(
            type("P", (), {"chat_completions": staticmethod(chat)})(),
            "workbuddy",
            {"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
            {},
        )
    )
    assert len(calls) == 1
    assert result[1][0] == 502
    assert result[1][1]["error"]["code"] == "upstream_html"
    assert "<html>" not in result[1][1]["error"]["message"]
