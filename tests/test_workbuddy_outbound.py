import asyncio

import auth_manager
import proxy
import workbuddy_outbound


def _body(messages, **extra):
    payload = {"model": "glm-5.2", "messages": messages}
    payload.update(extra)
    return proxy.build_backend_body(payload)


def test_tool_results_move_next_to_their_call(monkeypatch):
    monkeypatch.setattr(proxy, "resolve_model_alias", lambda value: value)
    original = [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "read", "arguments": "{}"}},
        ]},
        {"role": "user", "content": "also this"},
        {"role": "tool", "tool_call_id": "a", "content": "file"},
    ]
    body = _body(original)
    roles = [(item["role"], item.get("tool_call_id")) for item in body["messages"]]
    assert roles == [
        ("system", None),
        ("assistant", None),
        ("tool", "a"),
        ("user", None),
    ]
    assert original[1]["role"] == "user"


def test_orphan_tool_parts_are_removed(monkeypatch):
    monkeypatch.setattr(proxy, "resolve_model_alias", lambda value: value)
    body = _body([
        {"role": "assistant", "content": "x", "tool_calls": [
            {"id": "keep", "type": "function", "function": {"name": "read", "arguments": "{}"}},
            {"id": "drop", "type": "function", "function": {"name": "write", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "keep", "content": "ok"},
        {"role": "tool", "tool_call_id": "missing", "content": "no"},
    ])
    assistant = next(item for item in body["messages"] if item["role"] == "assistant")
    assert [item["id"] for item in assistant["tool_calls"]] == ["keep"]
    assert [item.get("tool_call_id") for item in body["messages"] if item["role"] == "tool"] == ["keep"]


def test_tool_choice_object_becomes_string_and_keeps_tools(monkeypatch):
    monkeypatch.setattr(proxy, "resolve_model_alias", lambda value: value)
    tools = [{"type": "function", "function": {"name": "read"}}]
    body = _body(
        [{"role": "user", "content": "hi"}],
        tools=tools,
        tool_choice={"type": "function", "function": {"name": "read"}},
    )
    assert body["tool_choice"] == "read"
    assert body["tools"] == tools

    none_body = _body(
        [{"role": "user", "content": "hi"}],
        tools=tools,
        tool_choice={"type": "none"},
    )
    assert none_body["tool_choice"] == "none"
    assert none_body["tools"] == tools


def test_deepseek_backfill_only_when_thinking_or_trace(monkeypatch):
    monkeypatch.setattr(proxy, "resolve_model_alias", lambda value: value)
    monkeypatch.setenv("CB_GATEWAY_DEFAULT_REASONING_EFFORT", "off")
    def assistant(body):
        return next(item for item in body["messages"] if item["role"] == "assistant")

    quiet = _body(
        [{"role": "assistant", "content": "plain"}],
        model="deepseek-v4-flash",
    )
    assert "reasoning_content" not in assistant(quiet)

    traced = _body(
        [{"role": "assistant", "content": "a", "reasoning": "thought"}],
        model="deepseek-v4-flash",
    )
    assert assistant(traced)["reasoning_content"] == "thought"

    enabled = _body(
        [{"role": "assistant", "content": "a"}],
        model="deepseek-v4-flash",
        reasoning_effort="high",
    )
    assert assistant(enabled)["reasoning_content"] == ""

    other = _body(
        [{"role": "assistant", "content": "a", "reasoning": "thought"}],
        model="glm-5.2",
    )
    assert "reasoning_content" not in assistant(other)


def test_fingerprint_rewrite_can_be_disabled(monkeypatch):
    monkeypatch.setattr(proxy, "resolve_model_alias", lambda value: value)
    text = "You are Claude Code, Anthropic's official CLI for Claude."
    changed = _body([{"role": "system", "content": text}])
    assert "CLI tool for Claude" in changed["messages"][0]["content"]
    assert "11128" not in _body([{"role": "user", "content": "code 11128"}])["messages"][0]["content"]

    monkeypatch.setenv("CB_GATEWAY_SANITIZE_FINGERPRINTS", "off")
    kept = _body([{"role": "system", "content": text}])
    assert kept["messages"][0]["content"] == text


def test_terminal_code_does_not_penalize_or_rotate(monkeypatch):
    monkeypatch.setattr(proxy, "resolve_model_alias", lambda value: value)
    marks = []
    picks = []

    async def pick(_excluded, **_kwargs):
        picks.append(set(_excluded))
        return {"id": 7, "name": "acct"}

    async def headers(_account):
        return {"Authorization": "Bearer x"}

    async def collect(*_args, **_kwargs):
        return ("error", (400, {"code": 11128, "msg": "Illegal API invocation"}))

    monkeypatch.setattr(auth_manager, "pick_account_with_fallback", pick)
    monkeypatch.setattr(auth_manager, "get_valid_headers", headers)
    monkeypatch.setattr(auth_manager, "backend_url", lambda account=None: "https://copilot.tencent.com")
    monkeypatch.setattr(proxy, "_collect_stream", collect)
    monkeypatch.setattr(auth_manager, "mark_account_failure", lambda *args: marks.append(args))
    monkeypatch.setattr(proxy, "_log_request", lambda *args, **kwargs: None)

    result = asyncio.run(proxy.proxy_chat_completions(
        {"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
        None,
    ))

    assert result[0] == "error"
    assert result[1][0] == 400
    assert result[1][1]["code"] == 11128
    assert result[1][1]["error"]["code"] == "upstream_channel"
    assert marks == []
    assert picks == [set()]


def test_auth_code_expires_and_tries_the_next_account(monkeypatch):
    monkeypatch.setattr(proxy, "resolve_model_alias", lambda value: value)
    marks = []
    seen = []

    async def pick(excluded, **_kwargs):
        if 3 in excluded:
            return None
        return {"id": 3, "name": "dead"}

    async def headers(_account):
        return {"Authorization": "Bearer x"}

    async def collect(*_args, **_kwargs):
        seen.append(True)
        return ("error", (403, {"code": 12153, "msg": "Offline user session not found"}))

    monkeypatch.setattr(auth_manager, "pick_account_with_fallback", pick)
    monkeypatch.setattr(auth_manager, "get_valid_headers", headers)
    monkeypatch.setattr(auth_manager, "backend_url", lambda account=None: "https://copilot.tencent.com")
    monkeypatch.setattr(proxy, "_collect_stream", collect)
    monkeypatch.setattr(auth_manager, "mark_account_failure", lambda *args: marks.append(args))
    monkeypatch.setattr(proxy, "_log_request", lambda *args, **kwargs: None)
    monkeypatch.setattr(proxy, "_retry_delay", lambda _attempt: _async_none())

    result = asyncio.run(proxy.proxy_chat_completions(
        {"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
        None,
    ))

    assert result[0] == "error"
    assert marks == [(3, 401)]
    assert seen == [True]


def test_rate_limit_wording_does_not_expire_the_account():
    action, status, _detail, _failure = workbuddy_outbound.account_rule(
        200,
        {"code": 11140, "msg": "The model provider is rate-limiting requests."},
    )
    assert action == "rotate"
    assert status == 200
    illegal = workbuddy_outbound.account_rule(
        403,
        {"error": {"data": {"code": 11140, "msg": "request illegal"}}},
    )
    assert illegal[0] == "expire"


async def _async_none():
    return None
