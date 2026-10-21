"""Context budget for long chats.

Loomy is trimmed before send. Every channel may trim once after the upstream
rejects the context. The advertised catalog window is left unchanged.
"""

from __future__ import annotations

import json
import os
import sys

DEFAULT_LOOMY_INPUT_BUDGET = 160_000
OUTPUT_RESERVE = 8192
MAX_OUTPUT_RESERVE = 32768
HUGE_PROMPT_TOKENS = 100_000

_CONTEXT_CODES = {"11115"}
_HUGE_BODY_CODES = {"11133"}
_CONTEXT_WORDS = (
    "context length",
    "context window",
    "maximum context",
    "too long",
    "token limit",
    "context_length_exceeded",
    "上下文",
    "超出",
    "过长",
)


def loomy_input_budget() -> int:
    """Input-token budget for Loomy. Zero disables the pre-send trim."""
    env = os.environ.get("CB_LOOMY_CONTEXT_BUDGET")
    if env is not None and env.strip() != "":
        return _parse_budget(env)
    try:
        import database as db

        stored = db.get_setting("loomy_context_budget", None)
    except Exception:
        stored = None
    if stored is None or stored == "":
        return DEFAULT_LOOMY_INPUT_BUDGET
    return _parse_budget(stored)


def _parse_budget(value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return DEFAULT_LOOMY_INPUT_BUDGET
    return max(0, min(number, 2_000_000))


def input_limit(payload: dict, budget: int) -> int:
    reserve = OUTPUT_RESERVE
    raw = payload.get("max_tokens")
    if raw is None:
        raw = payload.get("max_completion_tokens")
    try:
        asked = int(raw)
    except (TypeError, ValueError):
        asked = 0
    if asked > 0:
        reserve = min(max(asked, 1), MAX_OUTPUT_RESERVE)
    if budget <= reserve:
        return max(1, budget // 2)
    return budget - reserve


def trim_to_limit(payload: dict, limit: int) -> tuple[dict, bool]:
    """Drop oldest chat history until the estimate fits. Keeps the newest turn."""
    import router

    if not isinstance(payload.get("messages"), list) or limit <= 0:
        return payload, False
    if router.estimate_prompt_tokens(payload) <= limit:
        return payload, False
    trimmed = json.loads(json.dumps(payload))
    before = len(trimmed["messages"])
    router._trim_history(trimmed, "messages", limit)
    changed = len(trimmed["messages"]) < before
    return trimmed, changed


def still_over(payload: dict, limit: int) -> bool:
    import router

    return router.estimate_prompt_tokens(payload) > limit


def _walk_codes(value, found: set[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"code", "error_code", "errCode"} and item not in (None, ""):
                found.add(str(item))
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
    if codes & _HUGE_BODY_CODES and prompt_tokens >= HUGE_PROMPT_TOKENS:
        return True
    if status in {502, 503} and looks_html(blob) and prompt_tokens >= HUGE_PROMPT_TOKENS:
        return True
    return False


def readable_error(channel: str, *, kind: str, trimmed: bool, status: int) -> tuple:
    if kind == "context":
        message = (
            f"{channel} 上下文超过可用预算。"
            + ("已裁掉更早历史后仍然失败。" if trimmed else "没有更早的历史可以裁。")
            + "请新开一段对话。"
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


def _log(action: str, channel: str, before: int, after: int) -> None:
    sys.stderr.write(f"[context] action={action} channel={channel} before={before} after={after} trimmed\n")
    sys.stderr.flush()


def prepare_dispatch(channel: str, payload: dict) -> tuple[dict, bool]:
    if channel != "loomy":
        return payload, False
    budget = loomy_input_budget()
    if budget <= 0:
        return payload, False
    limit = input_limit(payload, budget)
    import router

    before = router.estimate_prompt_tokens(payload)
    if before <= limit:
        return payload, False
    trimmed, changed = trim_to_limit(payload, limit)
    if not changed:
        return payload, False
    after = router.estimate_prompt_tokens(trimmed)
    _log("pretrim", channel, before, after)
    return trimmed, True


def recovery_payload(payload: dict) -> tuple[dict, bool]:
    import router

    current = router.estimate_prompt_tokens(payload)
    target = max(1, current // 2)
    trimmed, changed = trim_to_limit(payload, target)
    return trimmed, changed


def _detail_html(detail) -> bool:
    texts: list[str] = []
    _texts(detail, texts)
    return any(looks_html(item) for item in texts)


def _error_sse(detail: dict) -> bytes:
    return f"data: {json.dumps(detail, ensure_ascii=False)}\n\n".encode("utf-8")


def _chunk_text(chunk) -> str:
    if isinstance(chunk, (bytes, bytearray)):
        return chunk.decode("utf-8", "replace")
    return str(chunk)


def _sse_payloads(text: str) -> list[dict]:
    found = []
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            if looks_html(data):
                found.append({"error": {"message": data[:400], "code": "upstream_html"}})
            continue
        if isinstance(parsed, dict):
            found.append(parsed)
    return found


def _chunk_has_content(text: str) -> bool:
    for parsed in _sse_payloads(text):
        if "error" in parsed and not parsed.get("choices"):
            continue
        return True
    if text and not text.lstrip().startswith("data:") and not looks_html(text):
        return True
    return False


async def dispatch_with_context_policy(provider, channel: str, payload: dict, info: dict) -> tuple:
    import router

    dispatch, pretrimmed = prepare_dispatch(channel, payload)
    if pretrimmed and still_over(dispatch, input_limit(payload, loomy_input_budget() or DEFAULT_LOOMY_INPUT_BUDGET)):
        return readable_error(channel, kind="context", trimmed=True, status=400)
    result = await provider.chat_completions(dispatch, info)
    return await _recover(provider, channel, dispatch, info, result, pretrimmed, router.estimate_prompt_tokens(dispatch))


async def _recover(provider, channel: str, payload: dict, info: dict, result: tuple, already_trimmed: bool, prompt_tokens: int) -> tuple:
    if info.get("_context_retried"):
        return _finalize(channel, result, already_trimmed, prompt_tokens)
    if result[0] == "error":
        status, detail = result[1]
        if is_context_rejection(status, detail, prompt_tokens=prompt_tokens):
            return await _retry(provider, channel, payload, info, already_trimmed)
        if _detail_html(detail):
            return readable_error(channel, kind="html", trimmed=False, status=status)
        return result
    if result[0] == "stream":
        return ("stream", _guard_stream(provider, channel, payload, info, result[1], already_trimmed, prompt_tokens))
    return result


async def _retry(provider, channel: str, payload: dict, info: dict, already_trimmed: bool) -> tuple:
    import router

    trimmed, changed = recovery_payload(payload)
    if not changed:
        return readable_error(channel, kind="context", trimmed=already_trimmed, status=400)
    before = router.estimate_prompt_tokens(payload)
    after = router.estimate_prompt_tokens(trimmed)
    _log("retry", channel, before, after)
    nxt = dict(info)
    nxt["_context_retried"] = True
    result = await provider.chat_completions(trimmed, nxt)
    return _finalize(channel, result, True, after)


def _finalize(channel: str, result: tuple, trimmed: bool, prompt_tokens: int) -> tuple:
    if result[0] != "error":
        return result
    status, detail = result[1]
    if is_context_rejection(status, detail, prompt_tokens=prompt_tokens):
        return readable_error(channel, kind="context", trimmed=trimmed, status=status)
    if _detail_html(detail):
        return readable_error(channel, kind="html", trimmed=False, status=status)
    return result


async def _guard_stream(provider, channel, payload, info, stream, already_trimmed, prompt_tokens):
    buffered = []
    text = ""
    async for chunk in stream:
        buffered.append(chunk)
        text += _chunk_text(chunk)
        if _chunk_has_content(text):
            for item in buffered:
                yield item
            async for item in stream:
                yield item
            return
        payloads = _sse_payloads(text)
        if payloads and all("error" in item for item in payloads):
            detail = payloads[-1]
            if is_context_rejection(502, detail, prompt_tokens=prompt_tokens):
                recovered = await _retry(provider, channel, payload, info, already_trimmed)
                async for item in _yield_result(recovered):
                    yield item
                return
            if _detail_html(detail):
                yield _error_sse(readable_error(channel, kind="html", trimmed=False, status=502)[1][1])
                return
            break
        if len(buffered) >= 4:
            break
    for item in buffered:
        yield item
    async for item in stream:
        yield item


async def _yield_result(result: tuple):
    if result[0] == "stream":
        async for chunk in result[1]:
            yield chunk
        return
    if result[0] == "error":
        yield _error_sse(result[1][1] if isinstance(result[1][1], dict) else {"error": {"message": "上下文超限"}})
        return
    if result[0] == "json":
        yield _error_sse({"error": {"message": "上游改为了非流式响应", "type": "upstream_error"}})
        return
    yield _error_sse({"error": {"message": "上下文重试失败", "type": "upstream_error"}})
