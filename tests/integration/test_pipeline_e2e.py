"""Integration tests: full pipeline from raw input → adapter → enricher → sink.

Tests exercise the complete event flow across all adapter surfaces to ensure
end-to-end correctness, metadata propagation, and cross-adapter consistency.
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tracemill import (
    ClaudeAdapter,
    CopilotAdapter,
    Enricher,
    EventKind,
    EventPipeline,
    SessionEvent,
)
from tracemill.adapters.mapped_json import FrameworkMapping, EventMapping, MappedJsonAdapter
from tests.conftest import RecordingSink

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _uid() -> str:
    return str(uuid.uuid4())


def _enrich_all(enricher: Enricher, events: list[SessionEvent]) -> list[SessionEvent]:
    """Helper: process events through enricher, handling None/single/list returns."""
    result = []
    for ev in events:
        out = enricher.process(ev)
        if out is None:
            continue
        elif isinstance(out, list):
            result.extend(out)
        else:
            result.append(out)
    result.extend(enricher.flush())
    return result


# ─── Full Pipeline Integration ───────────────────────────────────────────────


class TestCopilotFullPipeline:
    """End-to-end: Copilot JSONL fixture → adapter → enricher → sink."""

    def test_fixture_through_enricher(self):
        adapter = CopilotAdapter(ingestion_mode="file_watch", session_id="test-session")
        enricher = Enricher()
        fixture = FIXTURES / "copilot_session.jsonl"

        raw_events: list[SessionEvent] = []
        for line in fixture.read_text().splitlines():
            raw_events.extend(adapter.parse(line))

        enriched = _enrich_all(enricher, raw_events)

        # All events parsed
        assert len(raw_events) == 15

        # Enricher pairs tool calls
        tool_completes = [e for e in enriched if e.kind == EventKind.TOOL_CALL_COMPLETED]
        for tc in tool_completes:
            assert tc.metadata.duration_ms is not None
            assert tc.metadata.duration_ms >= 0

        # All events have consistent session_id
        session_ids = {e.session_id for e in enriched}
        assert len(session_ids) == 1

        # All events have metadata
        for ev in enriched:
            assert ev.metadata.source_framework == "copilot"

    def test_raw_event_preserved(self):
        """Every event carries the original JSON verbatim in raw_event."""
        adapter = CopilotAdapter(ingestion_mode="file_watch", session_id="test-session")
        fixture = FIXTURES / "copilot_session.jsonl"

        for line in fixture.read_text().splitlines():
            for ev in adapter.parse(line):
                assert ev.raw_event is not None
                assert isinstance(ev.raw_event, dict)
                # raw_event should be the original parsed JSON
                assert "type" in ev.raw_event

    def test_stream_mode_sets_metadata(self):
        """CopilotAdapter(ingestion_mode='stream') sets correct metadata."""
        from tracemill.adapters.copilot import CopilotAdapter

        file_adapter = CopilotAdapter(ingestion_mode="file_watch", session_id="test-session")
        stream_adapter = CopilotAdapter(ingestion_mode="stream", session_id="test-session")
        fixture = FIXTURES / "copilot_session.jsonl"

        file_events = []
        stream_events = []
        for line in fixture.read_text().splitlines():
            file_events.extend(file_adapter.parse(line))
            stream_events.extend(stream_adapter.parse(line))

        assert len(file_events) == len(stream_events)
        for f, s in zip(file_events, stream_events):
            assert f.kind == s.kind
            assert f.payload == s.payload
            assert f.session_id == s.session_id
            # Metadata differs by ingestion mode
            assert f.metadata.ingestion_mode == "file_watch"
            assert s.metadata.ingestion_mode == "stream"


class TestClaudeFullPipeline:
    """End-to-end: Claude JSONL fixture → adapter → enricher → sink."""

    def test_fixture_through_enricher(self):
        adapter = ClaudeAdapter(ingestion_mode="file_watch", session_id="test-session")
        enricher = Enricher()
        fixture = FIXTURES / "claude_session.jsonl"

        raw_events: list[SessionEvent] = []
        for line in fixture.read_text().splitlines():
            raw_events.extend(adapter.parse(line))

        enriched = _enrich_all(enricher, raw_events)

        # Events parsed
        assert len(raw_events) > 0

        # All have metadata
        for ev in enriched:
            assert ev.metadata.source_framework == "claude"

    def test_stream_mode_sets_metadata(self):
        """ClaudeAdapter(ingestion_mode='stream') sets correct metadata."""
        from tracemill.adapters.claude import ClaudeAdapter

        file_adapter = ClaudeAdapter(ingestion_mode="file_watch", session_id="test-session")
        stream_adapter = ClaudeAdapter(ingestion_mode="stream", session_id="test-session")
        fixture = FIXTURES / "claude_session.jsonl"

        file_events = []
        stream_events = []
        for line in fixture.read_text().splitlines():
            file_events.extend(file_adapter.parse(line))
            stream_events.extend(stream_adapter.parse(line))

        assert len(file_events) == len(stream_events)
        for f, s in zip(file_events, stream_events):
            assert f.kind == s.kind
            assert f.payload == s.payload

            assert s.metadata.ingestion_mode == "stream"


class TestMappedJsonFullPipeline:
    """End-to-end: MappedJsonAdapter with various framework mappings."""

    @pytest.fixture
    def crewai_adapter(self):
        return MappedJsonAdapter.from_yaml(
            str(Path(__file__).resolve().parents[1] / ".." / "src" / "tracemill" / "mappings" / "crewai.yaml"),
            session_id="test-session",
        )

    def test_crewai_session_lifecycle(self, crewai_adapter):
        """Simulate a full CrewAI session: crew start → agent → tool → task → end."""
        enricher = Enricher()
        events_raw = [
            {"type": "CrewStartedEvent", "timestamp": "2024-06-01T10:00:00Z", "event_id": "crew-1", "crew_name": "Research Crew"},
            {"type": "TaskStartedEvent", "timestamp": "2024-06-01T10:00:01Z", "event_id": "task-1", "task_id": "t1", "task_name": "Research", "agent_role": "Researcher"},
            {"type": "AgentExecutionStartedEvent", "timestamp": "2024-06-01T10:00:02Z", "event_id": "agent-1", "agent_id": "a1", "agent_role": "Researcher", "task_name": "Research"},
            {"type": "ToolUsageStartedEvent", "timestamp": "2024-06-01T10:00:03Z", "event_id": "tool-1", "tool_name": "web_search", "tool_input": {"query": "AI agents"}},
            {"type": "ToolUsageFinishedEvent", "timestamp": "2024-06-01T10:00:04Z", "event_id": "tool-2", "started_event_id": "tool-1", "tool_name": "web_search", "tool_output": "results...", "success": True},
            {"type": "AgentExecutionCompletedEvent", "timestamp": "2024-06-01T10:00:05Z", "event_id": "agent-2", "agent_id": "a1", "agent_role": "Researcher", "output": "Found relevant info"},
            {"type": "TaskCompletedEvent", "timestamp": "2024-06-01T10:00:06Z", "event_id": "task-2", "task_id": "t1", "task_name": "Research", "output": "Complete"},
            {"type": "CrewCompletedEvent", "timestamp": "2024-06-01T10:00:07Z", "event_id": "crew-2", "crew_name": "Research Crew", "output": "All tasks done"},
        ]

        parsed = []
        for evt in events_raw:
            parsed.extend(crewai_adapter.parse(json.dumps(evt)))

        all_events = _enrich_all(enricher, parsed)

        # Enricher pairs tool_start + tool_complete → emits only the complete with duration
        # So 8 raw events → 7 enriched (tool_start absorbed into tool_complete)
        assert len(all_events) == 7
        kinds = [e.kind for e in all_events]
        assert kinds[0] == "session.started"
        assert kinds[1] == "task.started"
        assert kinds[2] == "agent.spawned"
        assert "tool.call.completed" in kinds
        assert kinds[-1] == "session.ended"

        # All have consistent framework metadata
        for ev in all_events:
            assert ev.metadata.source_framework == "crewai"
            assert ev.metadata.ingestion_mode == "file_watch"

    def test_openhands_session(self):
        """Simulate an OpenHands session: message → think → command → file edit."""
        adapter = MappedJsonAdapter.from_yaml(
            str(Path(__file__).resolve().parents[1] / ".." / "src" / "tracemill" / "mappings" / "openhands.yaml"),
            session_id="test-session",
        )
        events_raw = [
            {"event_type": "MessageAction", "timestamp": 1717232400, "session_id": "oh-1", "content": "Fix the bug in main.py", "role": "user"},
            {"event_type": "AgentThinkAction", "timestamp": 1717232401, "session_id": "oh-1", "thought": "I need to read main.py first"},
            {"event_type": "FileReadAction", "timestamp": 1717232402, "session_id": "oh-1", "path": "/workspace/main.py"},
            {"event_type": "CmdRunAction", "timestamp": 1717232403, "session_id": "oh-1", "command": "python -m pytest"},
            {"event_type": "CmdOutputObservation", "timestamp": 1717232404, "session_id": "oh-1", "content": "PASSED", "exit_code": 0},
            {"event_type": "FileWriteAction", "timestamp": 1717232405, "session_id": "oh-1", "path": "/workspace/main.py", "content": "fixed code"},
            {"event_type": "AgentFinishAction", "timestamp": 1717232406, "session_id": "oh-1", "outputs": {"content": "Bug fixed"}},
        ]

        all_events = []
        for evt in events_raw:
            all_events.extend(adapter.parse(json.dumps(evt)))

        assert len(all_events) == 7
        kinds = [e.kind for e in all_events]
        assert kinds[0] == "message.user"
        assert kinds[1] == "reasoning.started"
        assert kinds[2] == "file.read"
        assert kinds[3] == "command.started"
        assert kinds[4] == "command.completed"
        assert kinds[5] == "file.edited"
        assert kinds[6] == "session.ended"

        # Session ID from constructor
        for ev in all_events:
            assert ev.session_id == "test-session"
            assert ev.metadata.source_framework == "openhands"

    def test_cline_session(self):
        """Simulate a Cline/Roo Code session."""
        adapter = MappedJsonAdapter.from_yaml(
            str(Path(__file__).resolve().parents[1] / ".." / "src" / "tracemill" / "mappings" / "cline.yaml"),
            session_id="test-session",
        )
        events_raw = [
            {"type": "say", "ts": "2024-06-01T10:00:00Z", "taskId": "cline-1", "text": "I'll fix that for you"},
            {"type": "api_req_started", "ts": "2024-06-01T10:00:01Z", "taskId": "cline-1", "model": "claude-sonnet", "tokensIn": 500},
            {"type": "api_req_finished", "ts": "2024-06-01T10:00:03Z", "taskId": "cline-1", "model": "claude-sonnet", "tokensIn": 500, "tokensOut": 200, "cost": 0.003},
            {"type": "write_to_file", "ts": "2024-06-01T10:00:04Z", "taskId": "cline-1", "path": "src/main.ts", "content": "fixed"},
            {"type": "command", "ts": "2024-06-01T10:00:05Z", "taskId": "cline-1", "text": "npm test"},
            {"type": "command_output", "ts": "2024-06-01T10:00:06Z", "taskId": "cline-1", "text": "All tests pass"},
        ]

        all_events = []
        for evt in events_raw:
            all_events.extend(adapter.parse(json.dumps(evt)))

        assert len(all_events) == 6
        kinds = [e.kind for e in all_events]
        assert kinds[0] == "message.assistant"
        assert kinds[1] == "llm.call.started"
        assert kinds[2] == "llm.call.completed"
        assert kinds[3] == "file.edited"
        assert kinds[4] == "command.started"
        assert kinds[5] == "command.completed"

        # LLM usage extracted
        assert all_events[2].payload["output_tokens"] == 200
        assert all_events[2].payload["cost"] == 0.003

        for ev in all_events:
            assert ev.session_id == "test-session"

    def test_aider_session(self):
        """Simulate an Aider session."""
        adapter = MappedJsonAdapter.from_yaml(
            str(Path(__file__).resolve().parents[1] / ".." / "src" / "tracemill" / "mappings" / "aider.yaml"),
            session_id="test-session",
        )
        events_raw = [
            {"event": "session_start", "timestamp": "2024-06-01T10:00:00Z", "session_id": "aid-1", "main_model": "gpt-4", "cwd": "/project"},
            {"event": "message_send", "timestamp": "2024-06-01T10:00:01Z", "session_id": "aid-1", "content": "Add tests", "role": "user"},
            {"event": "llm_start", "timestamp": "2024-06-01T10:00:02Z", "session_id": "aid-1", "model": "gpt-4", "input_tokens": 800},
            {"event": "llm_completion", "timestamp": "2024-06-01T10:00:05Z", "session_id": "aid-1", "model": "gpt-4", "input_tokens": 800, "output_tokens": 400, "cost": 0.05},
            {"event": "file_edit", "timestamp": "2024-06-01T10:00:06Z", "session_id": "aid-1", "fname": "tests/test_new.py", "content": "def test_hello(): ..."},
            {"event": "command_run", "timestamp": "2024-06-01T10:00:07Z", "session_id": "aid-1", "command": "pytest tests/"},
            {"event": "session_end", "timestamp": "2024-06-01T10:00:10Z", "session_id": "aid-1"},
        ]

        all_events = []
        for evt in events_raw:
            all_events.extend(adapter.parse(json.dumps(evt)))

        assert len(all_events) == 7
        kinds = [e.kind for e in all_events]
        assert kinds[0] == "session.started"
        assert kinds[1] == "message.user"
        assert kinds[2] == "llm.call.started"
        assert kinds[3] == "llm.call.completed"
        assert kinds[4] == "file.edited"
        assert kinds[5] == "command.started"
        assert kinds[6] == "session.ended"

        # Payload extraction
        assert all_events[0].payload["model"] == "gpt-4"
        assert all_events[3].payload["cost"] == 0.05
        assert all_events[4].payload["path"] == "tests/test_new.py"

    def test_goose_session(self):
        """Simulate Goose events from SQLite rows."""
        adapter = MappedJsonAdapter.from_yaml(
            str(Path(__file__).resolve().parents[1] / ".." / "src" / "tracemill" / "mappings" / "goose.yaml"),
            session_id="test-session",
        )
        events_raw = [
            {"role": "user", "created_at": 1717232400, "session_id": "goose-1", "content": "Help me refactor"},
            {"role": "tool_use", "created_at": 1717232401, "session_id": "goose-1", "id": "tu-1", "name": "shell", "input": {"command": "ls"}},
            {"role": "tool_result", "created_at": 1717232402, "session_id": "goose-1", "tool_use_id": "tu-1", "content": "file1.py file2.py", "is_error": False},
            {"role": "assistant", "created_at": 1717232403, "session_id": "goose-1", "content": "I see two files. Let me read them."},
        ]

        all_events = []
        for evt in events_raw:
            all_events.extend(adapter.parse(json.dumps(evt)))

        assert len(all_events) == 4
        kinds = [e.kind for e in all_events]
        assert kinds[0] == "message.user"
        assert kinds[1] == "tool.call.started"
        assert kinds[2] == "tool.call.completed"
        assert kinds[3] == "message.assistant"

        assert all_events[1].payload["tool_name"] == "shell"
        for ev in all_events:
            assert ev.session_id == "test-session"
            assert ev.metadata.ingestion_mode == "poll"


# ─── Cross-Adapter Consistency ───────────────────────────────────────────────


class TestCrossAdapterConsistency:
    """Verify that all adapters produce events with consistent structure."""

    def _get_all_events(self) -> list[SessionEvent]:
        """Collect events from all adapter surfaces."""
        events = []

        # Copilot
        copilot = CopilotAdapter(ingestion_mode="file_watch", session_id="test-session")
        for line in (FIXTURES / "copilot_session.jsonl").read_text().splitlines():
            events.extend(copilot.parse(line))

        # Claude
        claude = ClaudeAdapter(ingestion_mode="file_watch", session_id="test-session")
        for line in (FIXTURES / "claude_session.jsonl").read_text().splitlines():
            events.extend(claude.parse(line))

        # MappedJson (CrewAI)
        crewai = MappedJsonAdapter.from_yaml(
            str(Path(__file__).resolve().parents[1] / ".." / "src" / "tracemill" / "mappings" / "crewai.yaml"),
            session_id="test-session",
        )
        events.extend(crewai.parse(json.dumps({
            "type": "TaskStartedEvent", "timestamp": "2024-06-01T10:00:00Z",
            "event_id": "t1", "task_id": "t1", "task_name": "Test",
            "agent_role": "Worker",
        })))

        return events

    def test_all_events_have_kind(self):
        for ev in self._get_all_events():
            assert ev.kind, f"Event missing kind: {ev}"
            assert isinstance(ev.kind, str)

    def test_all_events_have_session_id(self):
        for ev in self._get_all_events():
            assert ev.session_id, f"Event missing session_id: {ev}"
            assert isinstance(ev.session_id, str)

    def test_all_events_have_timestamp(self):
        for ev in self._get_all_events():
            assert ev.timestamp, f"Event missing timestamp: {ev}"
            assert isinstance(ev.timestamp, datetime)
            assert ev.timestamp.tzinfo is not None  # timezone-aware

    def test_all_events_have_metadata(self):
        for ev in self._get_all_events():
            assert ev.metadata is not None
            assert ev.metadata.source_framework in ("copilot", "claude", "crewai")

    def test_all_events_have_payload(self):
        for ev in self._get_all_events():
            assert isinstance(ev.payload, dict)

    def test_event_serialization_roundtrip(self):
        """All events can be serialized to JSON and back."""
        for ev in self._get_all_events():
            json_str = ev.model_dump_json()
            restored = SessionEvent.model_validate_json(json_str)
            assert restored.kind == ev.kind
            assert restored.session_id == ev.session_id
            assert restored.payload == ev.payload

    def test_tool_events_have_required_payload(self):
        """Tool events from all adapters have consistent payload keys."""
        for ev in self._get_all_events():
            if ev.kind == EventKind.TOOL_CALL_STARTED:
                assert "tool_name" in ev.payload or "tool_call_id" in ev.payload, (
                    f"Tool start from {ev.metadata.source_framework} missing tool info: {ev.payload}"
                )
            if ev.kind == EventKind.TOOL_CALL_COMPLETED:
                assert "tool_call_id" in ev.payload or "result" in ev.payload, (
                    f"Tool complete from {ev.metadata.source_framework} missing result info: {ev.payload}"
                )


# ─── Enricher Integration ────────────────────────────────────────────────────


class TestEnricherIntegration:
    """Test enricher behavior across different adapter outputs."""

    def test_tool_pairing_copilot(self):
        """Enricher pairs Copilot tool start/complete and computes duration."""
        adapter = CopilotAdapter(ingestion_mode="file_watch", session_id="test-session")
        enricher = Enricher()

        start = json.dumps({
            "type": "tool.execution_start", "id": _uid(),
            "timestamp": "2024-06-01T10:00:00Z",
            "data": {"toolCallId": "tc-pair-1", "toolName": "grep", "arguments": {"pattern": "x"}},
        })
        complete = json.dumps({
            "type": "tool.execution_complete", "id": _uid(),
            "timestamp": "2024-06-01T10:00:02Z",
            "data": {"toolCallId": "tc-pair-1", "success": True, "result": {"content": "found", "detailedContent": None}},
        })

        # First: need to set session context
        session_start = json.dumps({
            "type": "session.start", "id": _uid(),
            "timestamp": "2024-06-01T09:59:59Z",
            "data": {"sessionId": _uid(), "selectedModel": "gpt-4", "copilotVersion": "1.0.0",
                     "startTime": "2024-06-01T09:59:59Z", "version": 1, "producer": "copilot-cli",
                     "context": {"cwd": "/tmp"}},
        })
        list(adapter.parse(session_start))

        start_events = list(adapter.parse(start))
        complete_events = list(adapter.parse(complete))

        all_parsed = start_events + complete_events
        results = _enrich_all(enricher, all_parsed)

        # Tool complete is emitted enriched with duration
        tool_completes = [e for e in results if e.kind == EventKind.TOOL_CALL_COMPLETED]
        assert len(tool_completes) == 1
        assert tool_completes[0].metadata.duration_ms is not None
        assert tool_completes[0].metadata.duration_ms == pytest.approx(2000, abs=100)

    def test_flush_emits_unpaired(self):
        """Unpaired tool starts are emitted on flush."""
        adapter = CopilotAdapter(ingestion_mode="file_watch", session_id="test-session")
        enricher = Enricher()

        session_start = json.dumps({
            "type": "session.start", "id": _uid(),
            "timestamp": "2024-06-01T09:59:59Z",
            "data": {"sessionId": _uid(), "selectedModel": "gpt-4", "copilotVersion": "1.0.0",
                     "startTime": "2024-06-01T09:59:59Z", "version": 1, "producer": "copilot-cli",
                     "context": {"cwd": "/tmp"}},
        })
        list(adapter.parse(session_start))

        start = json.dumps({
            "type": "tool.execution_start", "id": _uid(),
            "timestamp": "2024-06-01T10:00:00Z",
            "data": {"toolCallId": "tc-orphan", "toolName": "read", "arguments": {"path": "/x"}},
        })

        start_events = list(adapter.parse(start))
        results = _enrich_all(enricher, start_events)

        # The orphan tool start should be in results (flushed)
        tool_starts = [e for e in results if e.kind == EventKind.TOOL_CALL_STARTED]
        assert len(tool_starts) == 1
        assert tool_starts[0].metadata.duration_ms is None


# ─── Edge Cases and Robustness ───────────────────────────────────────────────


class TestAdapterRobustness:
    """Test error handling and edge cases across all adapters."""

    @pytest.mark.parametrize("make_adapter", [
        lambda: CopilotAdapter(ingestion_mode="file_watch", session_id="test-session"),
        lambda: ClaudeAdapter(ingestion_mode="file_watch", session_id="test-session"),
    ])
    def test_empty_input(self, make_adapter):
        adapter = make_adapter()
        assert list(adapter.parse("")) == []
        assert list(adapter.parse(b"")) == []
        assert list(adapter.parse("   ")) == []

    @pytest.mark.parametrize("make_adapter", [
        lambda: CopilotAdapter(ingestion_mode="file_watch", session_id="test-session"),
        lambda: ClaudeAdapter(ingestion_mode="file_watch", session_id="test-session"),
    ])
    def test_garbage_input(self, make_adapter):
        adapter = make_adapter()
        assert list(adapter.parse("not json at all {{{")) == []
        assert list(adapter.parse(b"\x00\xff\xfe")) == []

    @pytest.mark.parametrize("make_adapter", [
        lambda: CopilotAdapter(ingestion_mode="file_watch", session_id="test-session"),
        lambda: ClaudeAdapter(ingestion_mode="file_watch", session_id="test-session"),
    ])
    def test_non_dict_json(self, make_adapter):
        adapter = make_adapter()
        assert list(adapter.parse("[1,2,3]")) == []
        assert list(adapter.parse('"just a string"')) == []
        assert list(adapter.parse("42")) == []
        assert list(adapter.parse("null")) == []

    def test_mapped_json_missing_type_field(self):
        mapping = FrameworkMapping(framework="test", framework_version=">=1.0", ingestion_mode="file_watch", events={})
        adapter = MappedJsonAdapter(mapping, session_id="test-session")
        # No "type" field in the JSON — should still produce RAW event
        events = list(adapter.parse(json.dumps({"data": "hello"})))
        assert len(events) == 1
        assert events[0].kind == EventKind.RAW
        assert events[0].payload["original_type"] == "unknown"

    def test_mapped_json_huge_payload(self):
        """Large payloads don't crash the adapter."""
        mapping = FrameworkMapping(
            framework="test",
            framework_version=">=1.0",
            ingestion_mode="file_watch",
            events={"big": EventMapping(kind="message.user", payload={"content": "data"})},
        )
        adapter = MappedJsonAdapter(mapping, session_id="test-session")
        big_data = "x" * 100_000
        events = list(adapter.parse(json.dumps({"type": "big", "data": big_data})))
        assert len(events) == 1

    def test_copilot_unknown_event_type(self):
        """Unknown Copilot event types emit as RAW."""
        adapter = CopilotAdapter(ingestion_mode="file_watch", session_id="test-session")
        line = json.dumps({
            "type": "future.new_feature",
            "id": _uid(),
            "timestamp": "2024-01-01T00:00:00Z",
            "data": {"something": "new"},
        })
        events = list(adapter.parse(line))
        assert len(events) == 1
        assert events[0].kind == EventKind.RAW

    def test_claude_system_message_handling(self):
        """Claude system messages are handled gracefully."""
        adapter = ClaudeAdapter(ingestion_mode="file_watch", session_id="test-session")
        line = json.dumps({"type": "system", "message": {"content": "System prompt here"}})
        events = list(adapter.parse(line))
        # System messages may be skipped or emitted as system
        # The contract is: no crash
        assert isinstance(events, list)

    def test_mapped_json_payload_with_missing_paths(self):
        """Missing payload paths produce None (not crash)."""
        mapping = FrameworkMapping(
            framework="test",
            framework_version=">=1.0",
            ingestion_mode="file_watch",
            events={
                "evt": EventMapping(
                    kind="tool.call.started",
                    payload={"tool_name": "name", "missing_field": "nonexistent.deep.path"},
                )
            },
        )
        adapter = MappedJsonAdapter(mapping, session_id="test-session")
        line = json.dumps({"type": "evt", "name": "grep"})
        events = list(adapter.parse(line))
        assert len(events) == 1
        # Present path extracted, missing path not in payload
        assert events[0].payload["tool_name"] == "grep"
        assert "missing_field" not in events[0].payload


# ─── All YAML Mappings E2E ───────────────────────────────────────────────────


MAPPING_FILES = [p for p in (Path(__file__).resolve().parents[1] / ".." / "src" / "tracemill" / "mappings").glob("*.yaml") if p.stem != "maf"]


class TestAllMappingsE2E:
    """Every YAML mapping file can be loaded and used to parse events."""

    @pytest.fixture(params=MAPPING_FILES, ids=lambda p: p.stem)
    def adapter(self, request):
        return MappedJsonAdapter.from_yaml(str(request.param), session_id="test-session")

    def test_adapter_parses_generic_event(self, adapter):
        """Each mapping can handle at least a generic event line."""
        # Create an event using the first mapped type
        mapping = adapter._mapping
        if mapping.events:
            first_type = next(iter(mapping.events))
            line = json.dumps({mapping.type_field: first_type, "timestamp": "2024-01-01T00:00:00Z"})
            events = list(adapter.parse(line))
            assert len(events) == 1
            assert events[0].kind == mapping.events[first_type].kind
            assert events[0].metadata.source_framework == mapping.framework
