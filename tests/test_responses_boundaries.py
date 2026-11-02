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
def test_tool_result_images_hoisted_into_user_message(content):
    """Tool-result images are hoisted into a following user message, not rejected.

    Chat's ``role:"tool"`` content is text-only, so an image there cannot ride
    the tool message. ``responses_to_chat`` moves it into a ``user`` message
    right after the tool turn (see ``_split_tool_output_images``). The tool
    message keeps a text placeholder and no base64 blob.
    """
    converted = responses.responses_to_chat({"input": content})
    msgs = converted["messages"]
    tool_idx = next(i for i, m in enumerate(msgs) if m["role"] == "tool")
    tool_msg = msgs[tool_idx]
    assert isinstance(tool_msg["content"], str)
    assert tool_msg["content"] == "[image]"
    assert "base64" not in tool_msg["content"]
    follow = msgs[tool_idx + 1]
    assert follow["role"] == "user"
    blob = json.dumps(follow["content"], ensure_ascii=False)
    assert '"type": "image_url"' in blob


@pytest.mark.parametrize("content", TOOL_IMAGE_CONTENTS)
@pytest.mark.parametrize("stream", [False, True])
def test_tool_result_images_reach_provider(content, stream):
    """A tool-result image no longer short-circuits with a 400; it is dispatched."""
    seen = {}

    async def handler(chat_payload, *a, **k):
        seen["messages"] = chat_payload["messages"]
        return ("error", (599, {"error": {"message": "captured"}}))

    result = asyncio.run(responses.proxy_responses({"model": "auto", "input": content, "stream": stream},
                                                   chat_handler=handler))
    # Reached the handler (no pre-dispatch rejection) with hoisted structure.
    assert "messages" in seen
    roles = [m["role"] for m in seen["messages"]]
    assert "tool" in roles and "user" in roles
    assert result[0] == "error"  # handler's sentinel, not a 400 image rejection


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


def test_parallel_tool_images_hoisted_after_tool_run():
    """Images from a batch of parallel tool results hoist into one user message
    that lands *after* the whole tool run (Chat needs consecutive tool msgs)."""
    img2 = {"type": "input_image", "image_url": "data:image/png;base64,BBB"}
    body = {"model": "auto", "input": [
        {"role": "user", "content": "do two things"},
        {"type": "function_call", "name": "a", "call_id": "c1", "arguments": "{}"},
        {"type": "function_call", "name": "b", "call_id": "c2", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": [IMAGE, {"type": "input_text", "text": "r1"}]},
        {"type": "function_call_output", "call_id": "c2", "output": [{"type": "input_text", "text": "r2"}, img2]},
        {"role": "user", "content": "thanks"},
    ]}
    msgs = responses.responses_to_chat(body)["messages"]
    roles = [m["role"] for m in msgs]
    # assistant(tool_calls) -> tool c1 -> tool c2 -> user(images) -> user
    assert roles == ["user", "assistant", "tool", "tool", "user", "user"]
    assert [m["tool_call_id"] for m in msgs if m["role"] == "tool"] == ["c1", "c2"]
    imgs = msgs[4]["content"]
    assert [p["type"] for p in imgs] == ["image_url", "image_url"]
    assert imgs[0]["image_url"]["url"] == IMAGE["image_url"]
    assert imgs[1]["image_url"]["url"] == img2["image_url"]
    # Tool messages kept their own text, no base64 repr.
    assert msgs[2]["content"] == "r1" and msgs[3]["content"] == "r2"


def test_tool_placeholder_when_image_only():
    """A tool result that is only an image keeps a text placeholder."""
    IMAGE2 = {"type": "input_image", "image_url": "data:image/png;base64,CCC"}
    body = {"input": [{"type": "function_call_output", "call_id": "c", "output": [IMAGE2]}]}
    msgs = responses.responses_to_chat(body)["messages"]
    assert msgs[0]["role"] == "tool" and msgs[0]["content"] == "[image]"
    assert msgs[1]["role"] == "user"
    assert msgs[1]["content"][0]["type"] == "image_url"


def test_plain_text_tool_result_unchanged():
    """A tool result with no image keeps the old string content and adds no user."""
    body = {"input": [
        {"type": "function_call", "name": "echo", "call_id": "c", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c", "output": "hello"},
    ]}
    msgs = responses.responses_to_chat(body)["messages"]
    assert msgs[1]["role"] == "tool" and msgs[1]["content"] == "hello"
    assert all(m["role"] != "user" or m.get("role") == "user" for m in msgs)
    assert len(msgs) == 2  # assistant(tool_calls) + tool; no spurious user


def test_direct_bridge_tool_image_reaches_handler_hoisted():
    """The bridge calls the provider with the tool image hoisted into a user msg."""
    captured = {}

    async def handler(chat_payload, *a):
        captured["messages"] = chat_payload["messages"]
        return ("error", (599, {"error": {"message": "captured"}}))

    body = {"input": [{"type": "function_call_output", "call_id": "c", "output": [IMAGE]}]}
    result = asyncio.run(responses.proxy_responses(body, chat_handler=handler))
    assert "messages" in captured
    msgs = captured["messages"]
    assert msgs[0]["role"] == "tool" and msgs[0]["content"] == "[image]"
    assert msgs[1]["role"] == "user"
    assert '"type": "image_url"' in json.dumps(msgs[1]["content"], ensure_ascii=False)
    assert result[0] == "error" and result[1][0] == 599  # handler ran, no 400 gate


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

