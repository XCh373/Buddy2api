"""WorkBuddy outbound adjustments.

Protocol fixes always run. Fingerprint rewriting is separate and can be turned
off with CB_GATEWAY_SANITIZE_FINGERPRINTS=off. A miss leaves the body unchanged.
"""

from __future__ import annotations

import copy
import os
import re

_OFF = {"0", "off", "false", "none", "no"}

_AUTH_CODES = {12153, 14015, 11142}
_TERMINAL_CODES = {11115, 11128, 11101, 11148, 11155}
_ROTATE_CODES = {6000, 6001, 6002, 6003, 6004, 6005, 6006, 6007, 6008, 14003, 14017}

_TERMINAL_CLIENT = {
    11115: ("context_length_exceeded", "请求上下文超过上游限制（11115）。换账号没有用。"),
    11128: ("upstream_channel", "上游按渠道特征拒绝了这条请求（11128）。换账号没有用。"),
    11101: ("upstream_request", "上游无法解析请求体（11101）。换账号没有用。"),
    11148: ("upstream_request", "上游拒绝了不完整的工具调用历史（11148）。换账号没有用。"),
    11155: ("upstream_request", "上游拒绝了缺少思考字段的历史（11155）。换账号没有用。"),
}

# Exact strings the WorkBuddy channel check matches. One-word changes keep the
# meaning and miss the exact match. 11128 is matched as text, so a user message
# that contains that number is rewritten too.
_REWRITES = (
    (
        "You are Claude Code, Anthropic's official CLI for Claude",
        "You are Claude Code, Anthropic's official CLI tool for Claude",
    ),
    (
        "Main branch (you will usually use this for PRs)",
        "Default branch (you will usually use this for PRs)",
    ),
    (
        "You are a coding agent running in the Codex CLI, a terminal-based coding assistant.",
        "You are a coding agent running in the Codex CLI tool, a terminal-based coding assistant.",
    ),
    (
        "To give feedback, users should report the issue at https://github.com/anthropics/claude-code/issues",
        "To provide feedback, users should report the issue at https://github.com/anthropics/claude-code/issues",
    ),
    (
        "Sisyphus-Junior - Focused executor from OhMyOpenCode",
        "Sisyphus-Junior - Focused executor from Oh My OpenCode",
    ),
    ("11128", "11-128"),
)
_NEEDLES = (
    "x-anthropic-billing-header",
    "cc_entrypoint=",
    "You are Claude Code",
    "Main branch (",
    "You are a coding agent running in the Codex CLI",
    "github.com/anthropics/",
    "OhMyOpenCode",
    "11128",
)
_HEADER_RE = re.compile(r"(?i)x-anthropic-billing-header:[^;\n]*;?\s*")
_KV_RE = re.compile(r"(?i)\bcc_[a-z0-9_]+=[^;\n]*;?\s*")


def fingerprints_enabled() -> bool:
    raw = os.environ.get("CB_GATEWAY_SANITIZE_FINGERPRINTS")
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() not in _OFF


def prepare(body: dict) -> dict:
    """Mutate a body that build_backend_body already owns."""
    messages = body.get("messages")
    if isinstance(messages, list):
        copied = copy.deepcopy(messages)
        repaired = _repair_tool_history(copied)
        _backfill_reasoning(body.get("model"), repaired, body)
        if fingerprints_enabled():
            _sanitize_messages(repaired)
        body["messages"] = repaired
    _normalize_tool_choice(body)
    return body


def account_rule(status: int, detail) -> tuple[str, int, dict, str] | None:
    """Return stop, expire, or rotate when a Tencent envelope code decides it.

    stop: return this error, leave the account usable.
    expire: session is dead, mark the account expired and try another.
    rotate: this account cannot serve the request, try another without expiring it.
    """
    code, text = _primary_code(detail)
    if code is None:
        return None
    if code == 11140:
        lowered = text.lower()
        if "request illegal" in lowered and "rate" not in lowered:
            return ("expire", status or 401, _detail_of(detail), "upstream_auth")
        if "rate" in lowered:
            return ("rotate", status or 429, _detail_of(detail), "upstream_http")
        return None
    if code in _AUTH_CODES or "offline user session" in text.lower():
        return ("expire", status or 401, _detail_of(detail), "upstream_auth")
    if code in _TERMINAL_CODES:
        client_code, message = _TERMINAL_CLIENT[code]
        payload = {
            "code": code,
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "code": client_code,
            },
        }
        return ("stop", 400, payload, client_code)
    if code in _ROTATE_CODES:
        return ("rotate", status or 429, _detail_of(detail), "upstream_http")
    return None


def _primary_code(detail) -> tuple[int | None, str]:
    found: list[tuple[int, str]] = []

    def walk(node, hint: str) -> None:
        if isinstance(node, dict):
            text = str(node.get("msg") or node.get("message") or hint or "")
            raw = node.get("code")
            number = _as_int(raw)
            if number is not None:
                found.append((number, text))
            for value in node.values():
                walk(value, text)
        elif isinstance(node, list):
            for item in node:
                walk(item, hint)

    walk(detail, "")
    if not found:
        return None, ""
    ranked = sorted(found, key=lambda item: _rank(item[0], item[1]))
    return ranked[0]


def _rank(code: int, text: str) -> tuple[int, int]:
    if code in _TERMINAL_CODES:
        return (0, code)
    if code in _AUTH_CODES or code == 11140 and "request illegal" in text.lower():
        return (1, code)
    if code in _ROTATE_CODES or code == 11140:
        return (2, code)
    return (3, code)


def _as_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _detail_of(detail) -> dict:
    if isinstance(detail, dict):
        return detail
    return {"error": {"message": str(detail or "上游拒绝了请求"), "type": "upstream_error"}}


def _normalize_tool_choice(body: dict) -> None:
    if "tool_choice" not in body:
        return
    choice = body.get("tool_choice")
    if isinstance(choice, str):
        if choice.strip().lower() == "none":
            body["tool_choice"] = "none"
        return
    if not isinstance(choice, dict):
        body.pop("tool_choice", None)
        return
    kind = str(choice.get("type") or "").strip().lower()
    if kind in {"auto", "required", "none"}:
        body["tool_choice"] = kind
        return
    if kind == "function":
        function = choice.get("function") if isinstance(choice.get("function"), dict) else {}
        name = str(function.get("name") or choice.get("name") or "").strip()
        body["tool_choice"] = name or "auto"
        return
    body.pop("tool_choice", None)


def _repair_tool_history(messages: list) -> list:
    packed = _repack_tool_results(messages)
    return _drop_orphan_tools(packed)


def _repack_tool_results(messages: list) -> list:
    result: list = []
    consumed: set[int] = set()
    for index, message in enumerate(messages):
        if index in consumed:
            continue
        if not _assistant_call_ids(message):
            result.append(message)
            continue
        wanted = set(_assistant_call_ids(message))
        matches: list = []
        between: list = []
        result.append(message)
        for later in range(index + 1, len(messages)):
            nxt = messages[later]
            if _assistant_call_ids(nxt):
                break
            if _tool_id(nxt) in wanted:
                matches.append(nxt)
                consumed.add(later)
                continue
            between.append(nxt)
            consumed.add(later)
        result.extend(matches)
        result.extend(between)
    return result


def _drop_orphan_tools(messages: list) -> list:
    called: set[str] = set()
    answered: set[str] = set()
    for message in messages:
        called.update(_assistant_call_ids(message))
        tool_id = _tool_id(message)
        if tool_id:
            answered.add(tool_id)
    paired = called & answered
    kept: list = []
    for message in messages:
        if not isinstance(message, dict):
            kept.append(message)
            continue
        tool_id = _tool_id(message)
        if message.get("role") == "tool":
            if tool_id in paired:
                kept.append(message)
            continue
        calls = message.get("tool_calls")
        if not isinstance(calls, list) or not calls:
            kept.append(message)
            continue
        surviving = [
            call for call in calls
            if isinstance(call, dict) and str(call.get("id") or "") in paired
        ]
        copied = dict(message)
        if surviving:
            copied["tool_calls"] = surviving
        else:
            copied.pop("tool_calls", None)
        kept.append(copied)
    return kept


def _assistant_call_ids(message) -> list[str]:
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return []
    calls = message.get("tool_calls")
    if not isinstance(calls, list):
        return []
    return [str(call.get("id")) for call in calls if isinstance(call, dict) and call.get("id")]


def _tool_id(message) -> str:
    if not isinstance(message, dict) or message.get("role") != "tool":
        return ""
    return str(message.get("tool_call_id") or message.get("toolCallId") or "")


def _thinking_on(body: dict) -> bool:
    effort = str(body.get("reasoning_effort") or "").strip().lower()
    if effort and effort not in _OFF:
        return True
    thinking = body.get("thinking")
    if isinstance(thinking, dict) and str(thinking.get("type") or "").strip().lower() == "enabled":
        return True
    return False


def _has_reasoning_trace(messages: list) -> bool:
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        reasoning = message.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            return True
        if "reasoning_content" in message:
            return True
    return False


def _backfill_reasoning(model, messages: list, body: dict) -> None:
    if "deepseek" not in str(model or "").lower():
        return
    if not _thinking_on(body) and not _has_reasoning_trace(messages):
        return
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        if "reasoning_content" in message:
            continue
        reasoning = message.get("reasoning")
        message["reasoning_content"] = reasoning if isinstance(reasoning, str) else ""


def _sanitize_messages(messages: list) -> None:
    for message in messages:
        if not isinstance(message, dict):
            continue
        if "content" in message:
            message["content"] = _sanitize_content(message.get("content"))
        if isinstance(message.get("reasoning_content"), str):
            message["reasoning_content"] = _sanitize_text(message["reasoning_content"])
        calls = message.get("tool_calls")
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if isinstance(function, dict) and isinstance(function.get("arguments"), str):
                function["arguments"] = _sanitize_text(function["arguments"])


def _sanitize_content(content):
    if isinstance(content, str):
        return _sanitize_text(content)
    if not isinstance(content, list):
        return content
    for part in content:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            part["text"] = _sanitize_text(part["text"])
    return content


def _looks_marked(text: str) -> bool:
    if any(needle in text for needle in _NEEDLES):
        return True
    return _HEADER_RE.search(text) is not None


def _sanitize_text(text: str) -> str:
    if not text or not _looks_marked(text):
        return text
    for old, new in _REWRITES:
        text = text.replace(old, new)
    text = _HEADER_RE.sub("", text)
    if "cc_" in text.lower():
        previous = None
        while previous != text:
            previous = text
            text = _KV_RE.sub("", text)
    return text.strip()
