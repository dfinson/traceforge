"""Regression tests for the Continue.dev preprocessor.

Continue.dev persists session JSON with camelCase keys (toolCalls, toolCallId)
produced by JSON.stringify of its internal TypeScript ChatMessage types.
snake_case (tool_calls/tool_call_id) only appears on the OpenAI provider wire
format, never in the on-disk session file. These tests pin the preprocessor to
the real on-disk shape so tool calls are not silently dropped.
"""

from __future__ import annotations

from traceforge.preprocessors.continue_dev import preprocess_continue


def _raw_session() -> dict:
    """A raw Continue.dev session as written to ~/.continue/sessions/{id}.json."""
    return {
        "sessionId": "sess-1",
        "title": "demo",
        "history": [
            {"message": {"role": "user", "content": "read app.ts"}},
            {
                "message": {
                    "role": "assistant",
                    "content": "Reading the file.",
                    "toolCalls": [
                        {
                            "id": "tc_001",
                            "type": "function",
                            "function": {
                                "name": "readFile",
                                "arguments": '{"filepath": "/src/app.ts"}',
                            },
                        }
                    ],
                }
            },
            {
                "message": {
                    "role": "tool",
                    "toolCallId": "tc_001",
                    "content": "export const x = 1;",
                }
            },
        ],
    }


def test_camelcase_tool_call_is_emitted() -> None:
    blocks = preprocess_continue(_raw_session())
    tool_uses = [b for b in blocks if b["block_type"] == "assistant.tool_use"]
    assert len(tool_uses) == 1, "camelCase toolCalls must not be dropped"
    tc = tool_uses[0]
    assert tc["tool_call_id"] == "tc_001"
    assert tc["tool_name"] == "readFile"
    assert tc["arguments"] == {"filepath": "/src/app.ts"}


def test_camelcase_tool_result_correlates() -> None:
    blocks = preprocess_continue(_raw_session())
    results = [b for b in blocks if b["block_type"] == "tool.result"]
    assert len(results) == 1
    # camelCase toolCallId must populate tool_call_id for call/result correlation
    assert results[0]["tool_call_id"] == "tc_001"


def test_snake_case_is_not_read_from_disk() -> None:
    """A snake_case session (provider-wire shape) must NOT yield tool calls.

    This guards against silently re-introducing the snake_case bug: the on-disk
    format is camelCase only.
    """
    session = {
        "sessionId": "sess-2",
        "history": [
            {
                "message": {
                    "role": "assistant",
                    "content": "x",
                    "tool_calls": [{"id": "tc_x", "function": {"name": "n", "arguments": "{}"}}],
                }
            },
        ],
    }
    blocks = preprocess_continue(session)
    tool_uses = [b for b in blocks if b["block_type"] == "assistant.tool_use"]
    assert tool_uses == []
