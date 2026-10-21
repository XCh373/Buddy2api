"""Normalize OpenAI tool history before a channel forwards it.

Several upstreams reject a tool result unless it sits immediately after the
assistant message that requested it, with string arguments and a string body.
"""

from __future__ import annotations

import json


def _text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content") or ""
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part for part in parts if part).strip()
    if value is None:
        return ""
    return str(value)


def _raw_calls(message: dict) -> list[dict]:
    calls = message.get("tool_calls")
    if not isinstance(calls, list):
        calls = message.get("toolCalls")
    if not isinstance(calls, list):
        return []
    return [call for call in calls if isinstance(call, dict)]


def _normalize_call(call: dict, index: int) -> dict:
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    name = str(function.get("name") or call.get("name") or "")
    arguments = function.get("arguments", call.get("arguments", call.get("input", {})))
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments if arguments is not None else {}, ensure_ascii=False, separators=(",", ":"))
    return {
        "id": str(call.get("id") or f"call_{index}"),
        "type": "function",
        "function": {"name": name, "arguments": arguments or "{}"},
    }


def _call_ids(message: dict) -> list[str]:
    return [str(call.get("id")) for call in _raw_calls(message) if call.get("id")]


def _tool_id(message: dict) -> str:
    return str(message.get("tool_call_id") or message.get("toolCallId") or "")


def _as_user_note(message: dict) -> dict:
    name = str(message.get("name") or _tool_id(message) or "tool")
    return {"role": "user", "content": f"[工具结果 {name}]\n{_text(message.get('content'))}".strip()}


def repair_openai_tool_history(messages: list) -> list:
    source = [item for item in messages if isinstance(item, dict)]
    consumed: set[int] = set()
    ordered: list[dict] = []
    for index, message in enumerate(source):
        if index in consumed:
            continue
        role = message.get("role")
        if role != "assistant" or not _call_ids(message):
            if role == "tool":
                ordered.append(_as_user_note(message))
            else:
                ordered.append(message)
            continue
        wanted = set(_call_ids(message))
        matches = []
        between = []
        for later in range(index + 1, len(source)):
            nxt = source[later]
            if nxt.get("role") == "assistant" and _call_ids(nxt):
                break
            if nxt.get("role") == "tool" and _tool_id(nxt) in wanted:
                matches.append(nxt)
                consumed.add(later)
                continue
            if nxt.get("role") == "tool":
                between.append(_as_user_note(nxt))
                consumed.add(later)
                continue
            between.append(nxt)
            consumed.add(later)
        calls = [_normalize_call(call, call_index) for call_index, call in enumerate(_raw_calls(message))]
        matched_ids = {_tool_id(item) for item in matches}
        surviving = [call for call in calls if call["id"] in matched_ids]
        assistant = dict(message)
        assistant.pop("toolCalls", None)
        if surviving:
            text = _text(assistant.get("content")).strip()
            assistant["content"] = text or " "
            assistant["tool_calls"] = surviving
            ordered.append(assistant)
            for item in matches:
                ordered.append({
                    "role": "tool",
                    "tool_call_id": _tool_id(item),
                    "content": _text(item.get("content")),
                })
        else:
            assistant.pop("tool_calls", None)
            ordered.append(assistant)
            ordered.extend(_as_user_note(item) for item in matches)
        ordered.extend(between)
    return ordered
