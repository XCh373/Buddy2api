"""Offline regression for BUD-001/003 protocol and dispatch boundaries."""
import asyncio
import json

import httpx
import pytest

import context_budget
import responses
import router
import server


IMAGE = {"type": "input_image", "image_url": "data:image/png;base64,c3ludGhldGlj"}


USER_IMAGE_CONTENTS = [
    [IMAGE],
    [{"role": "user", "content": [{"type": "input_text", "text": "look"}, IMAGE, IMAGE]}],
    [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://synthetic.invalid/image.png"}}]}],
]

TOOL_IMAGE_CONTENTS = [
    [{"type": "function_call_output", "call_id": "c", "output": [IMAGE]}],
]


@pytest.mark.parametrize("content", TOOL_IMAGE_CONTENTS)
@pytest.mark.parametrize("stream", [False, True])
def test_tool_result_images_rejected_before_quota_or_provider(monkeypatch, content, stream):
    """Tool-result images cannot ride a Chat tool message; refuse before dispatch."""
    calls = []
    monkeypatch.setattr(server, "_check_client_auth", lambda *a, **k: {"id": 1})
    def forbidden(*a, **k):
        calls.append(True)
        raise AssertionError("Rejected images must fail before dispatch/quota")
    monkeypatch.setattr(server, "_reserve_client_quota", forbidden)
    monkeypatch.setattr(router, "bind_http", forbidden)
    monkeypatch.setattr(router, "ensure_usable", forbidden)
    monkeypatch.setattr(router, "responses_after_bind", forbidden)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://synthetic") as client:
            return await client.post("/v1/responses", json={"model": "auto", "input": content, "stream": stream})
    result = asyncio.run(run())
    assert result.status_code == 400
    assert result.json()["error"]["code"] == "unsupported_image_input"
    assert not calls


@pytest.mark.parametrize("content", USER_IMAGE_CONTENTS)
def test_user_images_pass_through_as_structured_parts(content):
    """User-message images must survive the bridge as structured image_url parts."""
    converted = responses.responses_to_chat({"input": content})
    blob = json.dumps(converted["messages"], ensure_ascii=False)
    assert '"type": "image_url"' in blob
    # No [image: ...] text flattening for user images.
    assert "[image:" not in blob


def test_image_guard_does_not_parse_text_literals():
    text = 'data:image/png;base64,c3ludGhldGlj and {"type":"input_image"}'
    body = {"input": [{"role": "user", "content": [{"type": "input_text", "text": text}]},
                      {"type": "function_call_output", "call_id": "c", "output": text}]}
    converted = responses.responses_to_chat(body)
    assert converted["messages"][0]["content"] == text
    assert converted["messages"][1]["content"] == text


def test_direct_bridge_tool_image_rejection_never_calls_handler():
    async def forbidden(*a):
        raise AssertionError("Provider must not run")
    body = {"input": [{"type": "function_call_output", "call_id": "c", "output": [IMAGE]}]}
    result = asyncio.run(responses.proxy_responses(body, chat_handler=forbidden))
    assert result[0] == "error" and result[1][0] == 400
    assert result[1][1]["error"]["code"] == "unsupported_image_input"


def _sse(value):
    return ("data: " + json.dumps(value, ensure_ascii=False) + "\n\n").encode()


@pytest.mark.parametrize("prefix", [
    b"",
    _sse({"choices": [{"delta": {"role": "assistant"}}]}),
    _sse({"choices": [], "usage": {"prompt_tokens": 200}}),
    _sse({"choices": [{"delta": {"content": "partial text 中文"}}]}),
    b": keepalive\r\n\r\n",
])
@pytest.mark.parametrize("fragmented", [False, True])
def test_context_error_after_any_prefix_stays_context_and_closes(prefix, fragmented):
    error = b'data: {"error":\r\ndata: {"code":11115,"message":"synthetic"}}\r\n\r\n'
    wire = prefix + error
    closed = []
    async def source():
        try:
            for chunk in ([wire[i:i+1] for i in range(len(wire))] if fragmented else [wire]):
                yield chunk
        finally:
            closed.append(True)
    async def run():
        guarded = context_budget._guard_stream("workbuddy", source())
        return "".join([event async for event in responses.chat_stream_to_responses_stream(guarded, "auto")])
    result = asyncio.run(run())
    events = [json.loads(line[6:]) for line in result.splitlines() if line.startswith("data: ")]
    errors = [e for e in events if e["type"] == "error"]
    failed = [e for e in events if e["type"] == "response.failed"]
    assert len(errors) == len(failed) == 1
    assert errors[0]["code"] == "context_length_exceeded"
    assert failed[0]["response"]["error"]["code"] == "invalid_prompt"
    assert "context_length_exceeded" in failed[0]["response"]["error"]["message"]
    assert all(e["type"] != "response.completed" for e in events)
    assert [e["type"] for e in events].index("error") < [e["type"] for e in events].index("response.failed")
    if b"partial text" in prefix:
        assert "partial text 中文" in result
    assert closed == [True]


def test_noncontext_stream_unchanged_and_unterminated_error_normalized():
    async def collect(wire):
        async def source():
            yield wire
        return b"".join([chunk async for chunk in context_budget._guard_stream("workbuddy", source())])
    for wire in [b': keepalive\n\ndata: [DONE]\n\n', _sse({"error": {"code": "cancelled", "message": "cancelled"}}),
                 _sse({"choices": [{"delta": {"content": "context_length_exceeded"}}]})]:
        assert asyncio.run(collect(wire)) == wire
    error = b'data: {"error":{"code":11115}}'
    normalized = asyncio.run(collect(error))
    assert json.loads(normalized.decode().removeprefix("data: "))["error"]["code"] == "context_length_exceeded"


def test_json_embedded_context_error_is_not_a_success():
    result = context_budget._finalize("workbuddy", ("json", {"error": {"code": 11115}}))
    assert result[0] == "error" and result[1][0] == 400
    assert result[1][1]["error"]["code"] == "context_length_exceeded"
    converted = responses.chat_response_to_responses({"error": {"code": "context_length_exceeded", "message": "synthetic"}}, "auto")
    assert converted["status"] == "failed"
    assert converted["error"] == {"code": "invalid_prompt", "message": "context_length_exceeded: synthetic"}


@pytest.mark.parametrize("separator", [b"\r\r", b"\n\n", b"\r\n\r\n", b"\n\r", b"\r\r\n", b"\r\n\r"])
def test_sse_line_endings_forward_without_waiting_for_next_chunk(separator):
    frame = b'data: {"choices":[{"delta":{"role":"assistant"}}]}' + separator
    async def run():
        async def source():
            yield frame
            await asyncio.Event().wait()
        guard = context_budget._guard_stream("workbuddy", source())
        try:
            assert await asyncio.wait_for(anext(guard), timeout=1) == frame
        finally:
            await guard.aclose()
    asyncio.run(run())


def test_image_without_url_still_rejected():
    bad = {"type": "input_image", "image_url": {"url": ""}}
    with pytest.raises(responses.UnsupportedImageInput):
        responses.responses_to_chat({"input": [{"type": "message", "role": "user", "content": [bad]}]})

@pytest.mark.parametrize("detail,expected", [
    ("original", "auto"),
    ("auto", "auto"),
    ("low", "low"),
    ("high", "high"),
    ("bogus", "auto"),
])
def test_image_detail_normalized_to_chat_enum(detail, expected):
    """Responses allows 'original'; Chat only allows auto/low/high."""
    body = {"input": [{"type": "message", "role": "user", "content": [
        {"type": "input_image", "image_url": {"url": "https://synthetic.invalid/x.png"}, "detail": detail}
    ]}]}
    converted = responses.responses_to_chat(body)
    part = converted["messages"][0]["content"][0]
    assert part["type"] == "image_url"
    assert part["image_url"]["detail"] == expected


def test_image_detail_inside_image_url_object_normalized():
    body = {"input": [{"type": "message", "role": "user", "content": [
        {"type": "input_image", "image_url": {"url": "https://synthetic.invalid/x.png", "detail": "original"}}
    ]}]}
    converted = responses.responses_to_chat(body)
    assert converted["messages"][0]["content"][0]["image_url"]["detail"] == "auto"

