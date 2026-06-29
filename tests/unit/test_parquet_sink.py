"""Tests for ParquetSink — buffering, schema, flush."""

from __future__ import annotations

from pathlib import Path

import pyarrow.parquet as pq
import pytest

from tests.conftest import make_event
from tracemill import EventKind
from tracemill.classify.core import Classification
from tracemill.types import EventMetadata, Phase


class TestParquetSinkBuffering:
    """Buffering, per-session flush, max_buffered_events trigger."""

    @pytest.fixture
    def sink_dir(self, tmp_path: Path) -> Path:
        return tmp_path / "out"

    async def test_flushes_on_session_end(self, sink_dir: Path):
        from tracemill.sinks.parquet import ParquetSink

        sink = ParquetSink(path=str(sink_dir))
        try:
            for i in range(3):
                await sink.on_event(
                    make_event(
                        kind=EventKind.MESSAGE_USER,
                        session_id="abc",
                        payload={"content": f"msg-{i}"},
                    )
                )
            # No flush yet
            assert not list(sink_dir.glob("*.parquet"))

            await sink.on_event(make_event(kind=EventKind.SESSION_ENDED, session_id="abc"))

            files = list(sink_dir.glob("*.parquet"))
            assert len(files) == 1
            assert files[0].name == "abc.parquet"

            table = pq.read_table(files[0])
            assert table.num_rows == 4
            assert table.column("session_id").to_pylist() == ["abc"] * 4
            assert table.column("seq").to_pylist() == [0, 1, 2, 3]
        finally:
            await sink.close()

    async def test_flushes_on_max_buffered_events(self, sink_dir: Path):
        from tracemill.sinks.parquet import ParquetSink

        sink = ParquetSink(path=str(sink_dir), max_buffered_events=3)
        try:
            for i in range(3):
                await sink.on_event(
                    make_event(session_id="bigsession", payload={"i": i})
                )
            files = list(sink_dir.glob("*.parquet"))
            assert len(files) == 1
            table = pq.read_table(files[0])
            assert table.num_rows == 3
        finally:
            await sink.close()

    async def test_separate_sessions_get_separate_files(self, sink_dir: Path):
        from tracemill.sinks.parquet import ParquetSink

        sink = ParquetSink(path=str(sink_dir))
        try:
            await sink.on_event(make_event(session_id="s1"))
            await sink.on_event(make_event(session_id="s2"))
            await sink.on_event(make_event(kind=EventKind.SESSION_ENDED, session_id="s1"))
            await sink.on_event(make_event(kind=EventKind.SESSION_ENDED, session_id="s2"))

            files = sorted(p.name for p in sink_dir.glob("*.parquet"))
            assert files == ["s1.parquet", "s2.parquet"]
        finally:
            await sink.close()

    async def test_close_flushes_remaining(self, sink_dir: Path):
        from tracemill.sinks.parquet import ParquetSink

        sink = ParquetSink(path=str(sink_dir))
        await sink.on_event(make_event(session_id="dangling"))
        # No SESSION_ENDED — close() must still write the buffer.
        await sink.close()

        files = list(sink_dir.glob("*.parquet"))
        assert len(files) == 1
        assert files[0].name == "dangling.parquet"


class TestParquetSinkSchema:
    """Schema columns are present and typed correctly."""

    async def test_schema_columns_present(self, tmp_path: Path):
        from tracemill.sinks.parquet import ParquetSink

        sink = ParquetSink(path=str(tmp_path))
        await sink.on_event(make_event(session_id="schemacheck"))
        await sink.close()

        files = list(tmp_path.glob("*.parquet"))
        assert files
        table = pq.read_table(files[0])
        expected = {
            "event_id",
            "session_id",
            "kind",
            "timestamp_ns",
            "seq",
            "tool_name",
            "mechanism",
            "effect",
            "scope",
            "role",
            "action",
            "capability",
            "structure",
            "source_labels",
            "shell_dialect",
            "binaries",
            "phase_signals",
            "activity",
            "motivation",
            "payload_json",
            "metadata_json",
            "duration_ms",
        }
        assert expected.issubset(set(table.column_names))


class TestParquetSinkClassificationRoundtrip:
    """Multi-valued Classification dimensions (frozensets) write and read back as lists."""

    async def test_frozenset_dimensions_become_lists(self, tmp_path: Path):
        from tracemill.sinks.parquet import ParquetSink

        classification = Classification(
            mechanism="shell",
            effect="mutating",
            scope=frozenset({"fs.write", "fs.read"}),
            role=frozenset({"developer"}),
            action=frozenset({"edit", "build"}),
            capability=frozenset({"writes-files"}),
            structure=frozenset({"compound"}),
            source_labels=frozenset({"git"}),
            shell_dialect="bash",
            binaries=("git", "npm"),
        )
        metadata = EventMetadata(
            classification=classification,
            phases=frozenset({Phase.IMPLEMENTATION, Phase.VERIFICATION}),
        )
        event = make_event(
            kind=EventKind.TOOL_CALL_STARTED,
            session_id="cls-rt",
            payload={"tool_name": "bash"},
            metadata=metadata,
        )

        sink = ParquetSink(path=str(tmp_path))
        await sink.on_event(event)
        await sink.on_event(make_event(kind=EventKind.SESSION_ENDED, session_id="cls-rt"))
        await sink.close()

        table = pq.read_table(tmp_path / "cls-rt.parquet")
        first = {col: table.column(col)[0].as_py() for col in table.column_names}
        assert first["mechanism"] == "shell"
        assert first["effect"] == "mutating"
        assert sorted(first["scope"]) == ["fs.read", "fs.write"]
        assert first["role"] == ["developer"]
        assert sorted(first["action"]) == ["build", "edit"]
        assert first["capability"] == ["writes-files"]
        assert first["structure"] == ["compound"]
        assert first["source_labels"] == ["git"]
        assert first["shell_dialect"] == "bash"
        assert first["binaries"] == ["git", "npm"]
        assert sorted(first["phase_signals"]) == ["implementation", "verification"]


class TestParquetSinkPathTemplate:
    """{session_id} substitution and path containment."""

    async def test_session_id_in_template(self, tmp_path: Path):
        from tracemill.sinks.parquet import ParquetSink

        sink = ParquetSink(path=str(tmp_path / "{session_id}" / "events.parquet"))
        await sink.on_event(make_event(session_id="trace-001"))
        await sink.close()

        out = tmp_path / "trace-001" / "events.parquet"
        assert out.exists()

    async def test_unsafe_session_id_is_sanitized(self, tmp_path: Path):
        from tracemill.sinks.parquet import ParquetSink

        sink = ParquetSink(path=str(tmp_path))
        await sink.on_event(make_event(session_id="../../escape-attempt"))
        await sink.close()

        # Sanitized name has no slashes / dots-as-separators
        files = list(tmp_path.glob("*.parquet"))
        assert files
        for f in files:
            assert ".." not in f.name
            assert "/" not in f.name
