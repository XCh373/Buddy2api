"""Context budget for long chats.

History belongs to the client: forward it intact and surface explicit upstream
context errors without retrying a reduced request.
"""

from __future__ import annotations

import json
import re

_CONTEXT_CODES = {"11115", "context_length_exceeded", "context_window_exceeded"}
_CONTEXT_WORDS = (
    "context_length_exceeded",
    "context_window_exceeded",
    "maximum context length",
    "context length exceeded",
    "context window exceeded",
    "上下文超限",
    "上下文超过",
    "上下文长度超",
)


def _walk_codes(value, found: set[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"code", "error_code", "errCode"} and item not in (None, ""):
                found.add(str(item).lower())
            _walk_codes(item, found)
    elif isinstance(value, list):
        for item in value:
            _walk_codes(item, found)


def _texts(value, found: list[str]) -> None:
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            _texts(item, found)
    elif isinstance(value, list):
        for item in value:
            _texts(item, found)


def looks_html(text: str) -> bool:
    sample = (text or "").lstrip()[:200].lower()
    return sample.startswith("<") or "<html" in sample or "<!doctype" in sample


def is_context_rejection(status: int, detail, *, prompt_tokens: int) -> bool:
    codes: set[str] = set()
    texts: list[str] = []
    _walk_codes(detail, codes)
    _texts(detail, texts)
    blob = "\n".join(texts)
    lowered = blob.lower()
    if codes & _CONTEXT_CODES:
        return True
    if any(word in lowered for word in _CONTEXT_WORDS):
        return True
    return False


def readable_error(channel: str, *, kind: str, status: int) -> tuple:
    if kind == "context":
        message = (
            f"context_length_exceeded: {channel} 上下文超过上游限制。"
            + "网关未删减历史。请由客户端压缩或精简上下文后重试。"
        )
        return (
            "error",
            (
                400,
                {
                    "error": {
                        "message": message,
                        "type": "invalid_request_error",
                        "code": "context_length_exceeded",
                    }
                },
            ),
        )
    if kind == "html":
        return (
            "error",
            (
                status or 502,
                {
                    "error": {
                        "message": f"上游返回了非 JSON 错误页（HTTP {status or 502}），没有原样转发。",
                        "type": "upstream_error",
                        "code": "upstream_html",
                    }
                },
            ),
        )
    return ("error", (status or 502, {"error": {"message": "上游拒绝了请求", "type": "upstream_error"}}))


def _detail_html(detail) -> bool:
    texts: list[str] = []
    _texts(detail, texts)
    return any(looks_html(item) for item in texts)


def _error_sse(detail: dict) -> bytes:
    return f"data: {json.dumps(detail, ensure_ascii=False)}\n\n".encode("utf-8")


async def dispatch_with_context_policy(provider, channel: str, payload: dict, info: dict) -> tuple:
    # Never make the client's history and upstream usage diverge by silently
    # dropping messages, either before dispatch or after a context rejection.
    result = await provider.chat_completions(payload, info)
    if result[0] == "stream":
        return ("stream", _guard_stream(channel, result[1]))
    return _finalize(channel, result)


def _finalize(channel: str, result: tuple) -> tuple:
    if result[0] == "json" and isinstance(result[1], dict) and result[1].get("error"):
        if is_context_rejection(200, result[1], prompt_tokens=0):
            return readable_error(channel, kind="context", status=400)
        return result
    if result[0] != "error":
        return result
    status, detail = result[1]
    if is_context_rejection(status, detail, prompt_tokens=0):
        return readable_error(channel, kind="context", status=status)
    if _detail_html(detail):
        return readable_error(channel, kind="html", status=status)
    return result


def _guard_event(channel: str, frame: bytes) -> tuple[bytes, bool]:
    text = frame.decode("utf-8", "replace")
    data = "\n".join(line[5:].lstrip(" ") for line in text.splitlines() if line.startswith("data:"))
    try:
        detail = json.loads(data)
    except (ValueError, TypeError):
        detail = {"error": {"message": data or text}} if looks_html(data or text) else None
    if isinstance(detail, dict) and detail.get("error"):
        if is_context_rejection(502, detail, prompt_tokens=0):
            return _error_sse(readable_error(channel, kind="context", status=400)[1][1]), True
        if _detail_html(detail):
            return _error_sse(readable_error(channel, kind="html", status=502)[1][1]), True
    return frame, False


async def _guard_stream(channel, stream):
    # Inspect every complete SSE event, including errors after role/usage/text.
    # Very large events pass through intact; malformed streams cannot cause
    # unbounded buffering. The Responses parser still handles their terminal state.
    limit = 1024 * 1024
    pending = b""
    passthrough = False
    try:
        async for chunk in stream:
            pending += chunk.encode("utf-8") if isinstance(chunk, str) else bytes(chunk)
            while match := re.search(br"(?:\r\n|\r(?!\n)|(?<!\r)\n){2}", pending):
                frame, pending = pending[:match.end()], pending[match.end():]
                if passthrough:
                    passthrough = False
                    yield frame
                    continue
                guarded, terminal = _guard_event(channel, frame)
                yield guarded
                if terminal:
                    return
            if len(pending) > limit:
                # Keep the delimiter suffix so CRLF/blank lines may span chunks.
                yield pending[:-3]
                pending = pending[-3:]
                passthrough = True
        if pending:
            guarded, _ = (pending, False) if passthrough else _guard_event(channel, pending)
            yield guarded
    finally:
        close = getattr(stream, "aclose", None)
        if close is not None:
            await close()
