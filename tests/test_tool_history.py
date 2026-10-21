from providers.tool_history import repair_openai_tool_history


def test_tool_result_moves_next_to_the_assistant_call():
    repaired = repair_openai_tool_history([
        {"role": "user", "content": "查一下"},
        {
            "role": "assistant",
            "content": None,
            "toolCalls": [{"id": "call-1", "function": {"name": "read_file", "arguments": {"path": "a.txt"}}}],
        },
        {"role": "user", "content": "继续"},
        {"role": "tool", "toolCallId": "call-1", "content": [{"type": "text", "text": "ok"}]},
    ])
    assert [item["role"] for item in repaired] == ["user", "assistant", "tool", "user"]
    assert repaired[1]["content"] == " "
    assert repaired[1]["tool_calls"][0]["function"]["arguments"] == '{"path":"a.txt"}'
    assert repaired[2]["tool_call_id"] == "call-1"
    assert repaired[2]["content"] == "ok"
