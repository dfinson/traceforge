"""Integration tests for the langchain + semantic_kernel YAML mappings (PR-H).

Mirrors tests/integration/test_new_mappings.py: each YAML must load, validate
against the FrameworkMapping schema, and map representative native events to the
correct canonical kinds with payloads preserved through dot-path resolution.

Field names in the sample events are grounded in the real framework surfaces:
- langchain: langchain_core.callbacks.BaseCallbackHandler callback arguments
  (serialized/input_str/inputs/output/prompts/response...), flattened under an
  "event" discriminator by scripts/capture_traces/capture_langchain.py.
- semantic_kernel: SK filter contexts (FUNCTION_INVOCATION / AUTO_FUNCTION_INVOCATION
  / PROMPT_RENDERING) flattened under a "type" discriminator by
  scripts/capture_traces/capture_semantic_kernel.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from traceforge.adapters.mapped_json import MappedJsonAdapter
from traceforge.types import EventKind

MAPPINGS_DIR = Path(__file__).resolve().parents[2] / "src" / "traceforge" / "mappings"


class TestLangChainMapping:
    """LangChain BaseCallbackHandler YAML mapping coverage."""

    @pytest.fixture
    def adapter(self) -> MappedJsonAdapter:
        return MappedJsonAdapter.from_yaml(
            str(MAPPINGS_DIR / "langchain.yaml"), session_id="lc-session"
        )

    def test_chain_lifecycle(self, adapter):
        """on_chain_start -> workflow.started, on_chain_end -> workflow.completed."""
        events_raw = [
            {
                "event": "on_chain_start",
                "timestamp": "2024-06-01T10:00:00Z",
                "run_id": "chain-1",
                "name": "AgentExecutor",
                "inputs": {"input": "list the files"},
            },
            {
                "event": "on_chain_end",
                "timestamp": "2024-06-01T10:00:05Z",
                "run_id": "chain-1",
                "name": "AgentExecutor",
                "outputs": {"output": "done"},
            },
        ]
        all_events = []
        for raw in events_raw:
            all_events.extend(adapter.parse(json.dumps(raw)))
        assert all_events[0].kind == EventKind.WORKFLOW_STARTED
        assert all_events[0].payload["run_id"] == "chain-1"
        assert all_events[0].payload["name"] == "AgentExecutor"
        assert all_events[1].kind == EventKind.WORKFLOW_COMPLETED
        assert all_events[1].payload["output"] == {"output": "done"}

    def test_chat_model_and_llm_end_tokens(self, adapter):
        """on_chat_model_start -> llm.call.started; on_llm_end -> llm.call.completed
        with token usage read from the OpenAI llm_output.token_usage shape."""
        events_raw = [
            {
                "event": "on_chat_model_start",
                "timestamp": "2024-06-01T10:00:01Z",
                "run_id": "llm-1",
                "name": "ChatOpenAI",
                "metadata": {"ls_model_name": "gpt-4o", "ls_provider": "openai"},
            },
            {
                "event": "on_llm_end",
                "timestamp": "2024-06-01T10:00:02Z",
                "run_id": "llm-1",
                "name": "ChatOpenAI",
                "response": {
                    "generations": [[{"text": "Here is the answer", "type": "ChatGeneration"}]],
                    "llm_output": {
                        "model_name": "gpt-4o",
                        "token_usage": {
                            "prompt_tokens": 120,
                            "completion_tokens": 40,
                            "total_tokens": 160,
                        },
                    },
                },
            },
        ]
        all_events = []
        for raw in events_raw:
            all_events.extend(adapter.parse(json.dumps(raw)))
        assert all_events[0].kind == EventKind.LLM_CALL_STARTED
        assert all_events[0].payload["model"] == "gpt-4o"
        assert all_events[1].kind == EventKind.LLM_CALL_COMPLETED
        assert all_events[1].payload["model"] == "gpt-4o"
        assert all_events[1].payload["output"] == "Here is the answer"
        assert all_events[1].payload["input_tokens"] == 120
        assert all_events[1].payload["output_tokens"] == 40
        assert all_events[1].payload["total_tokens"] == 160

    def test_tool_calls(self, adapter):
        """on_tool_start/end/error map to tool.call.* with name forward-filled."""
        events_raw = [
            {
                "event": "on_tool_start",
                "timestamp": "2024-06-01T10:00:03Z",
                "run_id": "tool-1",
                "name": "read_file",
                "input_str": "src/app.py",
                "inputs": {"path": "src/app.py"},
            },
            {
                "event": "on_tool_end",
                "timestamp": "2024-06-01T10:00:04Z",
                "run_id": "tool-1",
                "name": "read_file",
                "output": "print('hi')",
            },
        ]
        all_events = []
        for raw in events_raw:
            all_events.extend(adapter.parse(json.dumps(raw)))
        assert all_events[0].kind == EventKind.TOOL_CALL_STARTED
        assert all_events[0].payload["tool_name"] == "read_file"
        assert all_events[0].payload["tool_call_id"] == "tool-1"
        assert all_events[0].payload["arguments"] == {"path": "src/app.py"}
        assert all_events[1].kind == EventKind.TOOL_CALL_COMPLETED
        assert all_events[1].payload["result"] == "print('hi')"

    def test_tool_error(self, adapter):
        """on_tool_error -> tool.call.failed."""
        raw = {
            "event": "on_tool_error",
            "timestamp": "2024-06-01T10:00:05Z",
            "run_id": "tool-2",
            "name": "run_pytest",
            "error": "CalledProcessError: exit 1",
        }
        events = list(adapter.parse(json.dumps(raw)))
        assert events[0].kind == EventKind.TOOL_CALL_FAILED
        assert events[0].payload["tool_name"] == "run_pytest"
        assert events[0].payload["error"] == "CalledProcessError: exit 1"

    def test_agent_action_motivates_tool_call(self, adapter):
        """AgentAction.log is captured as intent and attached to the next tool call."""
        events_raw = [
            {
                "event": "on_agent_action",
                "timestamp": "2024-06-01T10:00:06Z",
                "run_id": "act-1",
                # AgentAction.log carries the ReAct thought/tool-selection rationale;
                # the YAML sources payload "content" from this raw "log" field.
                "log": "I should read the app module to find the bug.",
                "tool": "read_file",
                "tool_input": {"path": "src/app.py"},
            },
            {
                "event": "on_tool_start",
                "timestamp": "2024-06-01T10:00:07Z",
                "run_id": "tool-3",
                "name": "read_file",
                "input_str": "src/app.py",
                "inputs": {"path": "src/app.py"},
            },
        ]
        all_events = []
        for raw in events_raw:
            all_events.extend(adapter.parse(json.dumps(raw)))
        assert all_events[0].kind == EventKind.MESSAGE_ASSISTANT
        assert all_events[0].payload["tool"] == "read_file"
        assert all_events[0].payload["content"].startswith("I should read")
        tool_event = all_events[1]
        assert tool_event.kind == EventKind.TOOL_CALL_STARTED
        assert tool_event.metadata.motivation is not None
        assert "read the app module" in tool_event.metadata.motivation.intent

    def test_agent_finish(self, adapter):
        """on_agent_finish -> agent.completed with the final output."""
        raw = {
            "event": "on_agent_finish",
            "timestamp": "2024-06-01T10:00:08Z",
            "run_id": "fin-1",
            "return_values": {"output": "All tests pass."},
            "log": "Final Answer: All tests pass.",
        }
        events = list(adapter.parse(json.dumps(raw)))
        assert events[0].kind == EventKind.AGENT_COMPLETED
        assert events[0].payload["output"] == "All tests pass."

    def test_retriever_events(self, adapter):
        """on_retriever_start/end -> knowledge.query.*"""
        events_raw = [
            {
                "event": "on_retriever_start",
                "timestamp": "2024-06-01T10:00:09Z",
                "run_id": "ret-1",
                "name": "VectorStoreRetriever",
                "query": "how does auth work",
            },
            {
                "event": "on_retriever_end",
                "timestamp": "2024-06-01T10:00:10Z",
                "run_id": "ret-1",
                "documents": [{"page_content": "Auth uses JWT..."}],
            },
        ]
        all_events = []
        for raw in events_raw:
            all_events.extend(adapter.parse(json.dumps(raw)))
        assert all_events[0].kind == EventKind.KNOWLEDGE_QUERY_STARTED
        assert all_events[0].payload["query"] == "how does auth work"
        assert all_events[1].kind == EventKind.KNOWLEDGE_QUERY_COMPLETED

    def test_session_id_from_constructor(self, adapter):
        """Session ID always comes from the adapter constructor."""
        raw = {
            "event": "on_chain_start",
            "timestamp": "2024-06-01T10:00:00Z",
            "run_id": "r",
            "name": "g",
            "inputs": {},
        }
        events = list(adapter.parse(json.dumps(raw)))
        assert events[0].session_id == "lc-session"
        assert events[0].metadata.source_framework == "langchain"


class TestSemanticKernelMapping:
    """Semantic Kernel filter YAML mapping coverage."""

    @pytest.fixture
    def adapter(self) -> MappedJsonAdapter:
        return MappedJsonAdapter.from_yaml(
            str(MAPPINGS_DIR / "semantic_kernel.yaml"), session_id="sk-session"
        )

    def test_prompt_function_llm_call(self, adapter):
        """A prompt function (is_prompt=True) maps to llm.call.* with usage."""
        events_raw = [
            {
                "type": "prompt_function.started",
                "timestamp": "2024-06-01T10:00:00Z",
                "invocation_id": "inv-1",
                "function_name": "chat",
                "plugin_name": "assistant",
                "arguments": {"question": "list the files"},
            },
            {
                "type": "prompt_function.completed",
                "timestamp": "2024-06-01T10:00:02Z",
                "invocation_id": "inv-1",
                "function_name": "chat",
                "plugin_name": "assistant",
                "model": "gpt-4o",
                "result": "I'll list them now.",
                "usage": {"prompt_tokens": 200, "completion_tokens": 25},
            },
        ]
        all_events = []
        for raw in events_raw:
            all_events.extend(adapter.parse(json.dumps(raw)))
        assert all_events[0].kind == EventKind.LLM_CALL_STARTED
        assert all_events[0].payload["tool_call_id"] == "inv-1"
        assert all_events[1].kind == EventKind.LLM_CALL_COMPLETED
        assert all_events[1].payload["model"] == "gpt-4o"
        assert all_events[1].payload["input_tokens"] == 200
        assert all_events[1].payload["output_tokens"] == 25
        assert all_events[1].payload["output"] == "I'll list them now."

    def test_native_function_tool_call(self, adapter):
        """A native function (is_prompt=False) maps to tool.call.*"""
        events_raw = [
            {
                "type": "native_function.started",
                "timestamp": "2024-06-01T10:00:03Z",
                "invocation_id": "inv-2",
                "function_name": "read_file",
                "plugin_name": "repo",
                "arguments": {"path": "src/app.py"},
            },
            {
                "type": "native_function.completed",
                "timestamp": "2024-06-01T10:00:04Z",
                "invocation_id": "inv-2",
                "function_name": "read_file",
                "plugin_name": "repo",
                "result": "print('hi')",
            },
        ]
        all_events = []
        for raw in events_raw:
            all_events.extend(adapter.parse(json.dumps(raw)))
        assert all_events[0].kind == EventKind.TOOL_CALL_STARTED
        assert all_events[0].payload["tool_name"] == "read_file"
        assert all_events[0].payload["plugin_name"] == "repo"
        assert all_events[1].kind == EventKind.TOOL_CALL_COMPLETED
        assert all_events[1].payload["result"] == "print('hi')"

    def test_native_function_failure(self, adapter):
        """native_function.failed -> tool.call.failed."""
        raw = {
            "type": "native_function.failed",
            "timestamp": "2024-06-01T10:00:05Z",
            "invocation_id": "inv-3",
            "function_name": "run_pytest",
            "plugin_name": "repo",
            "error": "exit code 1",
        }
        events = list(adapter.parse(json.dumps(raw)))
        assert events[0].kind == EventKind.TOOL_CALL_FAILED
        assert events[0].payload["tool_name"] == "run_pytest"
        assert events[0].payload["error"] == "exit code 1"

    def test_auto_function_invocation(self, adapter):
        """auto_function.* maps to tool.call.* and carries the SK sequence indices."""
        events_raw = [
            {
                "type": "auto_function.started",
                "timestamp": "2024-06-01T10:00:06Z",
                "invocation_id": "inv-4",
                "function_name": "write_file",
                "plugin_name": "repo",
                "arguments": {"path": "src/app.py", "content": "x = 1"},
                "function_count": 2,
                "request_sequence_index": 0,
                "function_sequence_index": 1,
            },
            {
                "type": "auto_function.completed",
                "timestamp": "2024-06-01T10:00:07Z",
                "invocation_id": "inv-4",
                "function_name": "write_file",
                "plugin_name": "repo",
                "result": "wrote 5 bytes",
                "terminate": False,
            },
        ]
        all_events = []
        for raw in events_raw:
            all_events.extend(adapter.parse(json.dumps(raw)))
        assert all_events[0].kind == EventKind.TOOL_CALL_STARTED
        assert all_events[0].payload["tool_name"] == "write_file"
        assert all_events[0].payload["function_sequence_index"] == 1
        assert all_events[1].kind == EventKind.TOOL_CALL_COMPLETED
        assert all_events[1].payload["result"] == "wrote 5 bytes"

    def test_prompt_rendering_motivates_tool_call(self, adapter):
        """The rendered prompt is captured as intent and attached to later tool calls."""
        events_raw = [
            {
                "type": "prompt_rendering.completed",
                "timestamp": "2024-06-01T10:00:08Z",
                "function_name": "chat",
                "plugin_name": "assistant",
                # PromptRenderContext.rendered_prompt is the fully rendered prompt;
                # the YAML sources payload "content" from this raw "rendered_prompt".
                "rendered_prompt": "System: you are a coding agent. User: fix the bug in app.py",
            },
            {
                "type": "auto_function.started",
                "timestamp": "2024-06-01T10:00:09Z",
                "invocation_id": "inv-5",
                "function_name": "read_file",
                "plugin_name": "repo",
                "arguments": {"path": "src/app.py"},
            },
        ]
        all_events = []
        for raw in events_raw:
            all_events.extend(adapter.parse(json.dumps(raw)))
        assert all_events[0].kind == "prompt.render.completed"
        assert all_events[0].payload["content"].startswith("System:")
        tool_event = all_events[1]
        assert tool_event.kind == EventKind.TOOL_CALL_STARTED
        assert tool_event.metadata.motivation is not None
        assert "coding agent" in tool_event.metadata.motivation.intent

    def test_session_id_and_framework(self, adapter):
        """Session ID from constructor; framework provenance recorded."""
        raw = {
            "type": "prompt_rendering.started",
            "timestamp": "2024-06-01T10:00:00Z",
            "function_name": "chat",
            "plugin_name": "assistant",
        }
        events = list(adapter.parse(json.dumps(raw)))
        assert events[0].kind == "prompt.render.started"
        assert events[0].session_id == "sk-session"
        assert events[0].metadata.source_framework == "semantic_kernel"
