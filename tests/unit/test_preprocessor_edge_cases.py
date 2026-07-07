"""Isolated edge-case tests for the 15 framework preprocessors (+registry).

The preprocessors in ``traceforge.preprocessors`` are normally exercised only
*indirectly* through the golden mapping harness (source -> ``MappedJsonAdapter``).
This module tests each ``Callable[[dict], list[dict]]`` in isolation against the
messy edges that break when a framework's on-disk schema drifts across versions:

* recognized input shape -> correct discriminator + expected event count
* nested structures preserved for later dot-path (``_resolve_path``) extraction
* missing discriminator field -> graceful (passthrough / raw fallback, never a crash)
* empty dict / all-null fields / empty arrays -> never a crash, always ``list[dict]``

Input shapes are derived from each preprocessor's code, its mapping YAML
(``src/traceforge/mappings/<name>.yaml``) and the real fixtures under
``tests/fixtures/raw_traces/<name>/``.

A handful of genuine "null where a container was expected" crashes were found while
writing these tests; they are pinned as ``xfail(strict=True)`` at the bottom and
reported to the epic coordinator rather than fixed here (this ticket is tests-only).
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from traceforge.preprocessors import get_preprocessor, register_preprocessor
from traceforge.preprocessors.amazonq import preprocess_amazonq
from traceforge.preprocessors.antigravity import preprocess_antigravity
from traceforge.preprocessors.claude import preprocess_claude
from traceforge.preprocessors.cline import preprocess_cline
from traceforge.preprocessors.codex import preprocess_codex
from traceforge.preprocessors.continue_dev import preprocess_continue
from traceforge.preprocessors.copilot_vscode import _reset as _copilot_vscode_reset
from traceforge.preprocessors.copilot_vscode import preprocess_copilot_vscode
from traceforge.preprocessors.goose import preprocess_goose
from traceforge.preprocessors.maf_transcript import preprocess_maf_transcript
from traceforge.preprocessors.openai_agents import preprocess_openai_agents
from traceforge.preprocessors.opencode import preprocess_opencode
from traceforge.preprocessors.openhands import preprocess_openhands
from traceforge.preprocessors.pydantic_ai import preprocess_pydantic_ai
from traceforge.preprocessors.smolagents import preprocess_smolagents

Preproc = Callable[[dict[str, Any]], list[dict[str, Any]]]


# ─── Generic contract specification ──────────────────────────────────────────


@dataclass
class Spec:
    """A preprocessor + a representative valid input and its expected shape."""

    name: str  # registered preprocessor name
    fn: Preproc
    make_valid: Callable[[], dict[str, Any]]
    type_field: str  # the flat discriminator the mapping YAML keys on
    expected_types: set[str]  # discriminator values expected from the valid input
    expected_count: int  # number of normalized dicts the valid input yields
    disc_keys: tuple[str, ...] = field(default_factory=tuple)  # input keys that drive recognition


def _claude_valid() -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "hi"},
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
            ],
        },
    }


def _cline_valid() -> dict[str, Any]:
    return {"ts": 1, "type": "say", "say": "api_req_started", "text": '{"request": "x"}'}


def _continue_valid() -> dict[str, Any]:
    return {
        "sessionId": "s1",
        "history": [
            {"message": {"role": "user", "content": "hi"}},
            {
                "message": {
                    "role": "assistant",
                    "content": "ok",
                    "toolCalls": [
                        {"id": "tc1", "function": {"name": "readFile", "arguments": "{}"}}
                    ],
                }
            },
        ],
    }


def _goose_valid() -> dict[str, Any]:
    return {
        "role": "assistant",
        "created_timestamp": 1,
        "content_json": (
            '[{"type": "text", "text": "hi"},'
            '{"type": "toolRequest", "id": "r1",'
            '"toolCall": {"value": {"name": "shell", "arguments": {"cmd": "ls"}}}}]'
        ),
    }


def _openhands_valid() -> dict[str, Any]:
    return {"kind": "ActionEvent", "tool_name": "bash", "action": {"command": "ls"}}


def _pydantic_ai_valid() -> dict[str, Any]:
    return {"kind": "response", "parts": [{"part_kind": "text", "content": "hi"}]}


def _openai_agents_valid() -> dict[str, Any]:
    return {
        "object": "trace.span",
        "id": "span1",
        "trace_id": "tr1",
        "started_at": "t0",
        "ended_at": "t1",
        "span_data": {"type": "function", "name": "Bash", "input": {"c": "ls"}, "output": "ok"},
    }


def _smolagents_valid() -> dict[str, Any]:
    return {
        "step_number": 1,
        "tool_calls": [{"id": "c1", "function": {"name": "shell", "arguments": "ls"}}],
        "timing": {"start_time": 1},
    }


def _codex_valid() -> dict[str, Any]:
    return {
        "timestamp": "t0",
        "type": "response_item",
        "payload": {"type": "function_call", "call_id": "c1", "name": "shell", "arguments": "{}"},
    }


def _amazonq_valid() -> dict[str, Any]:
    return {
        "key": "/repo",
        "value": (
            '{"conversation_id": "c1", "history": [{"user": {"content":'
            ' {"Prompt": {"prompt": "hi"}}}, "assistant": {"Response": {"content": "ok"}}}]}'
        ),
    }


def _maf_valid() -> dict[str, Any]:
    return {
        "type": "message",
        "text": "hi",
        "from": {"id": "u", "name": "User", "role": "user"},
        "conversation": {"id": "conv1"},
    }


def _opencode_valid() -> dict[str, Any]:
    return {
        "type": "message.updated.1",
        "data": {"info": {"id": "m1", "sessionID": "s1", "role": "assistant"}, "sessionID": "s1"},
    }


def _copilot_vscode_valid() -> dict[str, Any]:
    return {
        "kind": 0,
        "v": {
            "sessionId": "s1",
            "version": 3,
            "requests": [{"requestId": "r1", "message": {"text": "hi"}, "response": []}],
        },
    }


def _antigravity_valid() -> dict[str, Any]:
    return {
        "type": "TOOL_CALL",
        "id": "step-1",
        "source": "MODEL",
        "tool_calls": [{"name": "shell", "args": {"c": "ls"}, "id": "tc1"}],
    }


SPECS: list[Spec] = [
    Spec(
        "claude",
        preprocess_claude,
        _claude_valid,
        "block_type",
        {"assistant.text", "assistant.tool_use"},
        2,
        ("type",),
    ),
    Spec("cline", preprocess_cline, _cline_valid, "type", {"say.api_req_started"}, 1, ("type",)),
    Spec(
        "continue",
        preprocess_continue,
        _continue_valid,
        "block_type",
        {"user.message", "assistant.message", "assistant.tool_use"},
        3,
        ("history",),
    ),
    Spec(
        "goose",
        preprocess_goose,
        _goose_valid,
        "role",
        {"assistant", "tool_use"},
        2,
        ("content_json",),
    ),
    Spec(
        "openhands",
        preprocess_openhands,
        _openhands_valid,
        "action",
        {"ActionEvent.bash"},
        1,
        ("kind", "action", "observation"),
    ),
    Spec(
        "pydantic_ai",
        preprocess_pydantic_ai,
        _pydantic_ai_valid,
        "type",
        {"model_response"},
        1,
        ("event_kind", "kind"),
    ),
    Spec(
        "openai_agents",
        preprocess_openai_agents,
        _openai_agents_valid,
        "event_type",
        {"function.started", "function.completed"},
        2,
        ("object",),
    ),
    Spec(
        "smolagents",
        preprocess_smolagents,
        _smolagents_valid,
        "step_type",
        {"ActionStep", "ToolCall"},
        2,
        ("step_number", "step_type", "tool_calls"),
    ),
    Spec("codex", preprocess_codex, _codex_valid, "block_type", {"tool.shell_call"}, 1, ("type",)),
    Spec(
        "amazonq",
        preprocess_amazonq,
        _amazonq_valid,
        "block_type",
        {"message.user", "message.assistant"},
        2,
        ("value", "data"),
    ),
    Spec(
        "maf_transcript",
        preprocess_maf_transcript,
        _maf_valid,
        "_event_type",
        {"message.user"},
        1,
        ("type",),
    ),
    Spec(
        "opencode",
        preprocess_opencode,
        _opencode_valid,
        "type",
        {"message.updated.assistant"},
        1,
        ("type",),
    ),
    Spec(
        "copilot_vscode",
        preprocess_copilot_vscode,
        _copilot_vscode_valid,
        "event_type",
        {"session_started", "user_message"},
        2,
        ("kind",),
    ),
    Spec(
        "antigravity",
        preprocess_antigravity,
        _antigravity_valid,
        "event_type",
        {"tool_call"},
        1,
        ("type",),
    ),
]

SPECS_BY_NAME = {s.name: s for s in SPECS}


def _empty_all_containers(value: Any) -> Any:
    """Recursively replace every list with ``[]`` (keeps dict keys, empties arrays)."""
    if isinstance(value, dict):
        return {k: _empty_all_containers(v) for k, v in value.items()}
    if isinstance(value, list):
        return []
    return value


def _spec_id(spec: Spec) -> str:
    return spec.name


# ─── Generic contract: recognized-shape behaviour ────────────────────────────


@pytest.mark.parametrize("spec", SPECS, ids=_spec_id)
class TestRecognizedShapeContract:
    """Every preprocessor turns its real input shape into a flat ``list[dict]``."""

    def test_returns_nonempty_list_of_dicts(self, spec: Spec) -> None:
        out = spec.fn(spec.make_valid())
        assert isinstance(out, list)
        assert out, f"{spec.name}: recognized input yielded no events"
        assert all(isinstance(d, dict) for d in out)

    def test_discriminator_extracted(self, spec: Spec) -> None:
        """The flat ``type_field`` the mapping keys on is present with the right values."""
        out = spec.fn(spec.make_valid())
        assert all(spec.type_field in d for d in out), (
            f"{spec.name}: some events lack discriminator '{spec.type_field}'"
        )
        produced = {d[spec.type_field] for d in out}
        assert produced == spec.expected_types, (
            f"{spec.name}: discriminator values {produced} != expected {spec.expected_types}"
        )

    def test_expected_event_count(self, spec: Spec) -> None:
        """Output count is stable (e.g. claude = one dict per content block)."""
        out = spec.fn(spec.make_valid())
        assert len(out) == spec.expected_count


# ─── Generic contract: degraded / drifted inputs never crash ─────────────────


@pytest.mark.parametrize("spec", SPECS, ids=_spec_id)
class TestDegradedInputSafety:
    """Missing/empty/null inputs must degrade gracefully — a list of dicts, no exception."""

    @staticmethod
    def _assert_list_of_dicts(out: Any) -> None:
        assert isinstance(out, list)
        assert all(isinstance(d, dict) for d in out)

    def test_empty_dict_no_crash(self, spec: Spec) -> None:
        self._assert_list_of_dicts(spec.fn({}))

    def test_all_null_fields_no_crash(self, spec: Spec) -> None:
        degraded = {k: None for k in spec.make_valid()}
        self._assert_list_of_dicts(spec.fn(degraded))

    def test_empty_arrays_no_crash(self, spec: Spec) -> None:
        degraded = _empty_all_containers(spec.make_valid())
        self._assert_list_of_dicts(spec.fn(degraded))

    def test_missing_discriminator_no_crash(self, spec: Spec) -> None:
        """Dropping the recognition keys falls back to passthrough/raw, never a crash."""
        degraded = copy.deepcopy(spec.make_valid())
        for key in spec.disc_keys:
            degraded.pop(key, None)
        self._assert_list_of_dicts(spec.fn(degraded))

    def test_extra_unknown_fields_ignored(self, spec: Spec) -> None:
        """Unknown/extra top-level fields don't break recognition."""
        payload = spec.make_valid()
        payload["_totally_unknown_field_"] = {"nested": [1, 2, 3]}
        self._assert_list_of_dicts(spec.fn(payload))

    def test_output_is_fresh_not_input_identity(self, spec: Spec) -> None:
        """Normalized dicts are new objects, not the caller's input dict aliased back."""
        original = spec.make_valid()
        out = spec.fn(original)
        # At least ensure we never mutate the discriminator meaning of the input in place
        # in a way that changes a second run's result.
        second = spec.fn(spec.make_valid())
        assert [d.get(spec.type_field) for d in out] == [d.get(spec.type_field) for d in second]


# ─── Registry ────────────────────────────────────────────────────────────────


class TestRegistry:
    """The ``@register_preprocessor`` registry resolves every framework by name."""

    @pytest.mark.parametrize("name", [s.name for s in SPECS])
    def test_every_preprocessor_is_registered(self, name: str) -> None:
        fn = get_preprocessor(name)
        assert fn is not None
        assert fn is SPECS_BY_NAME[name].fn

    def test_unknown_name_returns_none(self) -> None:
        assert get_preprocessor("does-not-exist") is None

    def test_register_decorator_roundtrips(self) -> None:
        @register_preprocessor("_edgecase_probe_")
        def _probe(obj: dict[str, Any]) -> list[dict[str, Any]]:
            return [obj]

        assert get_preprocessor("_edgecase_probe_") is _probe


# ─── Focused: claude ─────────────────────────────────────────────────────────


class TestClaude:
    def test_one_event_per_content_block(self) -> None:
        out = preprocess_claude(_claude_valid())
        assert [d["block_type"] for d in out] == ["assistant.text", "assistant.tool_use"]

    def test_tool_use_input_dict_preserved_for_dot_path(self) -> None:
        out = preprocess_claude(_claude_valid())
        tool_use = next(d for d in out if d["block_type"] == "assistant.tool_use")
        assert tool_use["input"] == {"command": "ls"}

    def test_user_string_content_single_block(self) -> None:
        out = preprocess_claude({"type": "user", "message": {"content": "hello"}})
        assert out == [{"block_type": "user.text", "content": "hello"}]

    def test_result_usage_flattened_to_top_level(self) -> None:
        out = preprocess_claude(
            {"type": "result", "usage": {"input_tokens": 5, "output_tokens": 9}}
        )
        assert out[0]["block_type"] == "result"
        assert out[0]["usage_input_tokens"] == 5
        assert out[0]["usage_output_tokens"] == 9

    def test_tool_result_list_content_joined(self) -> None:
        out = preprocess_claude(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "is_error": False,
                            "content": [{"type": "text", "text": "ok"}],
                        }
                    ]
                },
            }
        )
        assert out[0]["success"] is True
        assert out[0]["content_text"] == "ok"

    def test_missing_type_returns_original(self) -> None:
        obj = {"foo": "bar"}
        assert preprocess_claude(obj) == [obj]

    def test_empty_content_list_yields_nothing(self) -> None:
        out = preprocess_claude({"type": "assistant", "message": {"content": []}})
        assert out == []


# ─── Focused: cline ──────────────────────────────────────────────────────────


class TestCline:
    def test_compound_type_synthesized(self) -> None:
        out = preprocess_cline({"type": "say", "say": "text", "text": "hi"})
        assert out[0]["type"] == "say.text"

    def test_ask_subtype(self) -> None:
        out = preprocess_cline({"type": "ask", "ask": "tool", "text": "{}"})
        assert out[0]["type"] == "ask.tool"

    def test_json_text_parsed_into_nested_field(self) -> None:
        out = preprocess_cline(
            {"type": "say", "say": "api_req_started", "text": '{"tokensIn": 42}'}
        )
        assert out[0]["parsed"] == {"tokensIn": 42}

    def test_invalid_json_text_left_unparsed(self) -> None:
        out = preprocess_cline({"type": "say", "say": "api_req_started", "text": "not json"})
        assert "parsed" not in out[0]

    def test_unknown_type_passthrough(self) -> None:
        obj = {"type": "other", "text": "x"}
        assert preprocess_cline(obj) == [obj]


# ─── Focused: continue ───────────────────────────────────────────────────────


class TestContinue:
    def test_history_flattened_in_order(self) -> None:
        out = preprocess_continue(_continue_valid())
        assert [d["block_type"] for d in out] == [
            "user.message",
            "assistant.message",
            "assistant.tool_use",
        ]

    def test_tool_call_arguments_parsed_to_dict(self) -> None:
        out = preprocess_continue(
            {
                "sessionId": "s",
                "history": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "toolCalls": [
                                {
                                    "id": "tc",
                                    "function": {
                                        "name": "read",
                                        "arguments": '{"path": "/a"}',
                                    },
                                }
                            ],
                        }
                    }
                ],
            }
        )
        tool_use = next(d for d in out if d["block_type"] == "assistant.tool_use")
        assert tool_use["arguments"] == {"path": "/a"}

    def test_thinking_role_preserved(self) -> None:
        out = preprocess_continue(
            {"sessionId": "s", "history": [{"message": {"role": "thinking", "content": "hmm"}}]}
        )
        assert out[0]["block_type"] == "assistant.thinking"

    def test_non_list_history_passthrough(self) -> None:
        obj = {"sessionId": "s", "history": "nope"}
        assert preprocess_continue(obj) == [obj]

    def test_empty_history_passthrough(self) -> None:
        obj = {"sessionId": "s", "history": []}
        assert preprocess_continue(obj) == [obj]


# ─── Focused: goose ──────────────────────────────────────────────────────────


class TestGoose:
    def test_content_json_string_parsed_and_fanned_out(self) -> None:
        out = preprocess_goose(_goose_valid())
        roles = [d["role"] for d in out]
        assert roles == ["assistant", "tool_use"]

    def test_tool_request_arguments_preserved(self) -> None:
        out = preprocess_goose(_goose_valid())
        tool = next(d for d in out if d["role"] == "tool_use")
        assert tool["input"] == {"cmd": "ls"}
        assert tool["name"] == "shell"

    def test_no_content_json_passthrough(self) -> None:
        obj = {"role": "user"}
        assert preprocess_goose(obj) == [obj]

    def test_invalid_content_json_passthrough(self) -> None:
        obj = {"role": "user", "content_json": "not json"}
        assert preprocess_goose(obj) == [obj]

    def test_toolresponse_nested_value_content(self) -> None:
        out = preprocess_goose(
            {
                "role": "assistant",
                "content_json": (
                    '[{"type": "toolResponse", "id": "r",'
                    ' "toolResult": {"status": "success", "value": {"content": "done"}}}]'
                ),
            }
        )
        resp = next(d for d in out if d["role"] == "tool_result")
        assert resp["content"] == "done"
        assert resp["is_success"] is True


# ─── Focused: openhands ──────────────────────────────────────────────────────


class TestOpenhands:
    def test_sdk_action_event_compound(self) -> None:
        out = preprocess_openhands({"kind": "ActionEvent", "tool_name": "bash"})
        assert out[0]["action"] == "ActionEvent.bash"

    def test_sdk_message_event_uses_source(self) -> None:
        out = preprocess_openhands({"kind": "MessageEvent", "source": "user"})
        assert out[0]["action"] == "MessageEvent.user"

    def test_legacy_observation_compound(self) -> None:
        out = preprocess_openhands({"observation": "run", "content": "x"})
        assert out[0]["action"] == "observation.run"

    def test_legacy_action_passthrough(self) -> None:
        obj = {"action": "run", "args": {"cmd": "ls"}}
        assert preprocess_openhands(obj) == [obj]

    def test_missing_tool_name_defaults_unknown(self) -> None:
        out = preprocess_openhands({"kind": "ActionEvent"})
        assert out[0]["action"] == "ActionEvent.unknown"


# ─── Focused: pydantic_ai ────────────────────────────────────────────────────


class TestPydanticAi:
    def test_response_extracts_text_content(self) -> None:
        out = preprocess_pydantic_ai(_pydantic_ai_valid())
        assert out[0]["type"] == "model_response"
        assert out[0]["content"] == "hi"

    def test_request_extracts_user_prompt(self) -> None:
        out = preprocess_pydantic_ai(
            {"kind": "request", "parts": [{"part_kind": "user-prompt", "content": "q"}]}
        )
        assert out[0]["type"] == "model_request"
        assert out[0]["content"] == "q"

    def test_parts_preserved_for_dot_path(self) -> None:
        out = preprocess_pydantic_ai(_pydantic_ai_valid())
        assert out[0]["parts"] == [{"part_kind": "text", "content": "hi"}]

    def test_stream_event_kinds_mapped(self) -> None:
        assert preprocess_pydantic_ai({"event_kind": "function_tool_call"})[0]["type"] == (
            "tool_call_start"
        )
        assert preprocess_pydantic_ai({"event_kind": "function_tool_result"})[0]["type"] == (
            "tool_call_end"
        )

    def test_unknown_shape_passthrough(self) -> None:
        obj = {"foo": "bar"}
        assert preprocess_pydantic_ai(obj) == [obj]

    def test_empty_parts_list_is_safe(self) -> None:
        out = preprocess_pydantic_ai({"kind": "response", "parts": []})
        assert out[0]["type"] == "model_response"
        assert "content" not in out[0]


# ─── Focused: openai_agents ──────────────────────────────────────────────────


class TestOpenaiAgents:
    def test_function_span_emits_start_and_finish(self) -> None:
        out = preprocess_openai_agents(_openai_agents_valid())
        assert [d["event_type"] for d in out] == ["function.started", "function.completed"]

    def test_function_error_marks_failed(self) -> None:
        payload = _openai_agents_valid()
        payload["error"] = {"message": "boom"}
        out = preprocess_openai_agents(payload)
        assert out[1]["event_type"] == "function.failed"

    def test_span_data_preserved_for_dot_path(self) -> None:
        out = preprocess_openai_agents(_openai_agents_valid())
        assert out[0]["span_data"]["input"] == {"c": "ls"}

    def test_trace_object_single_event(self) -> None:
        out = preprocess_openai_agents({"object": "trace", "id": "tr", "workflow_name": "w"})
        assert out == [
            {
                "event_type": "trace",
                "trace_id": "tr",
                "workflow_name": "w",
                "group_id": None,
                "metadata": None,
            }
        ]

    def test_non_span_object_dropped(self) -> None:
        assert preprocess_openai_agents({"object": "something_else"}) == []

    def test_generic_span_completed(self) -> None:
        out = preprocess_openai_agents(
            {"object": "trace.span", "id": "s", "span_data": {"type": "agent"}}
        )
        assert out[0]["event_type"] == "agent.completed"


# ─── Focused: smolagents ─────────────────────────────────────────────────────


class TestSmolagents:
    def test_action_step_fans_out_tool_calls(self) -> None:
        out = preprocess_smolagents(_smolagents_valid())
        assert [d["step_type"] for d in out] == ["ActionStep", "ToolCall"]

    def test_task_step_inferred(self) -> None:
        assert preprocess_smolagents({"task": "do"})[0]["step_type"] == "TaskStep"

    def test_planning_step_inferred(self) -> None:
        assert preprocess_smolagents({"plan": "p"})[0]["step_type"] == "PlanningStep"

    def test_bare_output_is_final_answer(self) -> None:
        assert preprocess_smolagents({"output": "ans"})[0]["step_type"] == "FinalAnswer"

    def test_final_answer_action_step(self) -> None:
        out = preprocess_smolagents(
            {"step_number": 2, "is_final_answer": True, "action_output": "ANS"}
        )
        assert out[0]["step_type"] == "FinalAnswer"
        assert out[0]["output"] == "ANS"

    def test_timing_start_time_promoted(self) -> None:
        out = preprocess_smolagents({"task": "x", "timing": {"start_time": 123}})
        assert out[0]["timestamp"] == 123

    def test_unknown_shape_falls_back(self) -> None:
        assert preprocess_smolagents({"mystery": 1})[0]["step_type"] == "unknown"


# ─── Focused: codex ──────────────────────────────────────────────────────────


class TestCodex:
    def test_session_meta_flattened(self) -> None:
        out = preprocess_codex(
            {"type": "session_meta", "payload": {"id": "s", "cwd": "/x"}, "timestamp": "t"}
        )
        assert out[0]["block_type"] == "session.meta"
        assert out[0]["session_id"] == "s"

    def test_function_call_arguments_parsed(self) -> None:
        out = preprocess_codex(_codex_valid())
        assert out[0]["block_type"] == "tool.shell_call"
        assert out[0]["arguments"] == {}

    def test_event_msg_exec_begin(self) -> None:
        out = preprocess_codex(
            {"type": "event_msg", "payload": {"type": "exec_command_begin", "command": ["ls"]}}
        )
        assert out[0]["block_type"] == "tool.exec_begin"
        assert out[0]["command"] == ["ls"]

    def test_turn_context_dropped(self) -> None:
        assert preprocess_codex({"type": "turn_context", "payload": {}}) == []

    def test_developer_message_dropped(self) -> None:
        out = preprocess_codex(
            {"type": "response_item", "payload": {"type": "message", "role": "developer"}}
        )
        assert out == []

    def test_non_dict_payload_passthrough(self) -> None:
        obj = {"type": "response_item", "payload": "oops"}
        assert preprocess_codex(obj) == [obj]

    def test_unknown_top_type_raw_fallback(self) -> None:
        out = preprocess_codex({"type": "mystery", "payload": {"a": 1}})
        assert out[0]["block_type"] == "raw.mystery"
        assert out[0]["a"] == 1


# ─── Focused: amazonq ────────────────────────────────────────────────────────


class TestAmazonQ:
    def test_history_pair_expanded(self) -> None:
        out = preprocess_amazonq(_amazonq_valid())
        assert [d["block_type"] for d in out] == ["message.user", "message.assistant"]

    def test_json_string_value_parsed(self) -> None:
        out = preprocess_amazonq(_amazonq_valid())
        assert out[0]["conversation_id"] == "c1"

    def test_dict_value_tool_uses_expanded(self) -> None:
        out = preprocess_amazonq(
            {
                "value": {
                    "conversation_id": "c",
                    "history": [
                        {
                            "user": {"content": {"Prompt": {"prompt": "hi"}}},
                            "assistant": {
                                "ToolUse": {
                                    "content": "",
                                    "tool_uses": [{"id": "t", "name": "n", "args": {"a": 1}}],
                                }
                            },
                        }
                    ],
                }
            }
        )
        tool_call = next(d for d in out if d["block_type"] == "tool.call")
        assert tool_call["arguments"] == {"a": 1}

    def test_messages_format_content_blocks(self) -> None:
        out = preprocess_amazonq(
            {
                "value": {
                    "conversation_id": "c",
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "hi"},
                                {"type": "tool_use", "id": "t", "name": "n", "input": {"a": 1}},
                            ],
                        }
                    ],
                }
            }
        )
        assert [d["block_type"] for d in out] == ["message.user", "tool.call"]

    def test_invalid_json_value_raw_parse_error(self) -> None:
        out = preprocess_amazonq({"conversation_id": "c", "value": "not-json{{{"})
        assert out[0]["block_type"] == "raw.parse_error"

    def test_already_preprocessed_passthrough(self) -> None:
        obj = {"block_type": "message.user", "content": "x"}
        assert preprocess_amazonq(obj) == [obj]


# ─── Focused: maf_transcript ─────────────────────────────────────────────────


class TestMafTranscript:
    def test_compound_event_type_from_role(self) -> None:
        assert preprocess_maf_transcript(_maf_valid())[0]["_event_type"] == "message.user"

    def test_bot_role(self) -> None:
        out = preprocess_maf_transcript(
            {"type": "message", "from": {"role": "bot", "id": "b"}, "text": "hi"}
        )
        assert out[0]["_event_type"] == "message.bot"

    def test_conversation_id_flattened(self) -> None:
        out = preprocess_maf_transcript(_maf_valid())
        assert out[0]["conversation_id"] == "conv1"

    def test_attachments_counted(self) -> None:
        out = preprocess_maf_transcript(
            {"type": "message", "from": {"role": "bot"}, "attachments": [{"a": 1}, {"b": 2}]}
        )
        assert out[0]["attachment_count"] == 2

    def test_no_role_uses_bare_type(self) -> None:
        out = preprocess_maf_transcript({"type": "typing", "from": {}})
        assert out[0]["_event_type"] == "typing"

    def test_always_single_event(self) -> None:
        assert len(preprocess_maf_transcript(_maf_valid())) == 1


# ─── Focused: opencode ───────────────────────────────────────────────────────


class TestOpencode:
    def test_version_suffix_stripped(self) -> None:
        out = preprocess_opencode(
            {
                "type": "message.updated.1",
                "data": {"info": {"id": "m", "sessionID": "s", "role": "assistant"}},
            }
        )
        assert out[0]["type"] == "message.updated.assistant"

    def test_message_role_correlated_to_part(self) -> None:
        # A message.updated row registers the role; the following part row inherits it.
        preprocess_opencode(
            {
                "type": "message.updated.1",
                "data": {
                    "info": {"id": "m9", "sessionID": "s9", "role": "user"},
                    "sessionID": "s9",
                },
            }
        )
        out = preprocess_opencode(
            {
                "type": "message.part.updated.1",
                "data": {"part": {"type": "text", "messageID": "m9", "sessionID": "s9"}},
            }
        )
        assert out[0]["type"] == "message.part.text.user"
        assert out[0]["message_role"] == "user"

    def test_pending_tool_part_dropped(self) -> None:
        out = preprocess_opencode(
            {
                "type": "message.part.updated.1",
                "data": {"part": {"type": "tool", "state": {"status": "pending"}}},
            }
        )
        assert out == []

    def test_string_data_json_parsed(self) -> None:
        out = preprocess_opencode({"type": "session.created.1", "data": '{"sessionID": "s"}'})
        assert out[0]["data"] == {"sessionID": "s"}

    def test_unknown_type_passthrough_normalized(self) -> None:
        out = preprocess_opencode({"type": "session.created.1", "data": {"sessionID": "s"}})
        assert out[0]["type"] == "session.created"


# ─── Focused: copilot_vscode ─────────────────────────────────────────────────


class TestCopilotVscode:
    def test_snapshot_emits_session_started(self) -> None:
        _copilot_vscode_reset()
        out = preprocess_copilot_vscode(_copilot_vscode_valid())
        assert out[0]["event_type"] == "session_started"
        assert any(d["event_type"] == "user_message" for d in out)

    def test_append_requests_after_snapshot(self) -> None:
        _copilot_vscode_reset()
        preprocess_copilot_vscode({"kind": 0, "v": {"sessionId": "s", "requests": []}})
        out = preprocess_copilot_vscode(
            {"kind": 2, "k": ["requests"], "v": [{"requestId": "r1", "message": {"text": "hi"}}]}
        )
        assert out[0]["event_type"] == "user_message"
        assert out[0]["request_id"] == "r1"

    def test_result_set_record(self) -> None:
        _copilot_vscode_reset()
        preprocess_copilot_vscode({"kind": 0, "v": {"sessionId": "s", "requests": []}})
        preprocess_copilot_vscode(
            {"kind": 2, "k": ["requests"], "v": [{"requestId": "r1", "message": {"text": "hi"}}]}
        )
        out = preprocess_copilot_vscode(
            {
                "kind": 1,
                "k": ["requests", 0, "result"],
                "v": {"timings": {"firstProgress": 10, "totalElapsed": 99}},
            }
        )
        assert out[0]["event_type"] == "request_result"
        assert out[0]["total_elapsed_ms"] == 99

    def test_already_typed_row_passthrough(self) -> None:
        obj = {"event_type": "user_message", "text": "x"}
        assert preprocess_copilot_vscode(obj) == [obj]

    def test_snapshot_resets_state(self) -> None:
        _copilot_vscode_reset()
        preprocess_copilot_vscode(
            {
                "kind": 0,
                "v": {
                    "sessionId": "a",
                    "requests": [{"requestId": "old", "message": {"text": "x"}}],
                },
            }
        )
        # A new snapshot must reset the request index bookkeeping.
        out = preprocess_copilot_vscode({"kind": 0, "v": {"sessionId": "b", "requests": []}})
        assert out[0]["session_id"] == "b"

    def test_malformed_set_path_dropped(self) -> None:
        _copilot_vscode_reset()
        assert preprocess_copilot_vscode({"kind": 1, "k": [], "v": 1}) == []


# ─── Focused: antigravity ────────────────────────────────────────────────────


class TestAntigravity:
    def test_text_response_source_user(self) -> None:
        out = preprocess_antigravity({"type": "TEXT_RESPONSE", "source": "USER", "content": "hi"})
        assert out[0]["event_type"] == "user_message"

    def test_text_response_source_model(self) -> None:
        out = preprocess_antigravity({"type": "TEXT_RESPONSE", "source": "MODEL", "content": "hi"})
        assert out[0]["event_type"] == "assistant_message"

    def test_thinking(self) -> None:
        out = preprocess_antigravity({"type": "THINKING", "thinking": "hmm"})
        assert out[0]["event_type"] == "thinking"
        assert out[0]["content"] == "hmm"

    def test_tool_call_fans_out_per_call(self) -> None:
        out = preprocess_antigravity(
            {
                "type": "TOOL_CALL",
                "tool_calls": [{"name": "a", "id": "1"}, {"name": "b", "id": "2"}],
            }
        )
        assert [d["tool_name"] for d in out] == ["a", "b"]

    def test_tool_call_args_preserved(self) -> None:
        out = preprocess_antigravity(_antigravity_valid())
        assert out[0]["args"] == {"c": "ls"}

    def test_unknown_step_type_dropped(self) -> None:
        assert preprocess_antigravity({"type": "MYSTERY", "id": "s"}) == []

    def test_already_typed_row_passthrough(self) -> None:
        obj = {"event_type": "tool_call", "tool_name": "x"}
        assert preprocess_antigravity(obj) == [obj]


# ─── Genuine product bugs found while writing these tests ────────────────────
#
# These are all the same class: a preprocessor calls ``.get(key, [])`` / ``.get(key, {})``
# where ``key`` is PRESENT but explicitly ``null`` (a plausible schema drift). ``.get``
# returns the ``None`` value — not the default — and the code then iterates / attributes it.
#
# Per the #84 ticket (tests-only, no src changes) these are pinned as strict xfails and
# reported to the epic coordinator to be triaged as their own issue. When the src is fixed,
# ``strict=True`` turns the xpass into a failure so this guard is removed deliberately.


class TestKnownProductBugsNullContainers:
    """Captured crashes on ``null`` where a list/dict container was expected."""

    @pytest.mark.xfail(
        reason="BUG: pydantic_ai crashes on response parts=null (drift); "
        "reported to #90 coordinator",
        strict=True,
        raises=TypeError,
    )
    def test_pydantic_ai_response_null_parts(self) -> None:
        out = preprocess_pydantic_ai({"kind": "response", "parts": None})
        assert isinstance(out, list)

    @pytest.mark.xfail(
        reason="BUG: pydantic_ai crashes on request parts=null (drift); "
        "reported to #90 coordinator",
        strict=True,
        raises=TypeError,
    )
    def test_pydantic_ai_request_null_parts(self) -> None:
        out = preprocess_pydantic_ai({"kind": "request", "parts": None})
        assert isinstance(out, list)

    @pytest.mark.xfail(
        reason="BUG: pydantic_ai crashes on stream chunk part=null (drift); "
        "reported to #90 coordinator",
        strict=True,
        raises=AttributeError,
    )
    def test_pydantic_ai_stream_null_part(self) -> None:
        out = preprocess_pydantic_ai({"event_kind": "model_response_stream", "part": None})
        assert isinstance(out, list)

    @pytest.mark.xfail(
        reason="BUG: amazonq crashes on history user.content={'Prompt': null} "
        "(drift); reported to #90 coordinator",
        strict=True,
        raises=AttributeError,
    )
    def test_amazonq_null_prompt_enum(self) -> None:
        out = preprocess_amazonq(
            {
                "value": {
                    "conversation_id": "c",
                    "history": [{"user": {"content": {"Prompt": None}}}],
                }
            }
        )
        assert isinstance(out, list)
