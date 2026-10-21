"""Regression: non-stream requests must honour the key's bound channel.

v2.2.4 split WorkBuddy International into its own `workbuddy-i` channel, but
`proxy_chat_completions()` only forwarded `provider` on the streaming paths.
The non-streaming path fell back to the default ("workbuddy"), so an
international-only API key silently used domestic accounts.
"""

import asyncio

import proxy


def _run_non_stream(monkeypatch, provider):
    seen = {}

    async def fake_retry(body, api_key_info, model_name, provider="workbuddy"):
        seen["provider"] = provider
        return ("json", {"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(proxy, "_json_chat_with_retries", fake_retry)

    payload = {"model": "deepseek-v4.1-flash", "messages": [{"role": "user", "content": "hi"}]}
    result = asyncio.run(proxy.proxy_chat_completions(payload, {"default_channel": provider}, provider=provider))
    return seen, result


def test_non_stream_requests_use_the_key_channel(monkeypatch):
    seen, result = _run_non_stream(monkeypatch, "workbuddy-i")
    assert seen["provider"] == "workbuddy-i"
    assert result[0] == "json"


def test_non_stream_defaults_to_workbuddy(monkeypatch):
    seen, _ = _run_non_stream(monkeypatch, "workbuddy")
    assert seen["provider"] == "workbuddy"
