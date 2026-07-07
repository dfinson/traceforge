"""End-to-end tests for :class:`traceforge.sinks.parquet.ParquetSink` (issue #83).

Proves the real columnar artifact: parquet files are written then *read back with
pyarrow* to check schema, row count, and values (including multi-valued
classification dimensions that must round-trip as ``list<string>``). Also covers
the per-session title sidecar and the sink's *defined* failure behavior — unlike
the file/db sinks, a write ``OSError`` is re-raised (propagated), because silently
dropping a whole session's analytics data is worse than a loud failure.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pyarrow.parquet as pq
import pytest

from tests.conftest import make_event
from traceforge import EventKind
from traceforge.classify.core import Classification
from traceforge.sinks.parquet import ParquetSink
from traceforge.types import EventMetadata, Phase, ToolMotivation, TitleUpdate


@pytest.mark.e2e
async def test_parquet_round_trip_row_count_and_values(tmp_path: Path) -> None:
    sink = ParquetSink(path=str(tmp_path))
    for i in range(4):
        await sink.on_event(make_event(session_id="run", payload={"content": f"m{i}"}))
    await sink.on_event(make_event(kind=EventKind.SESSION_ENDED, session_id="run"))
    await sink.close()

    table = pq.read_table(tmp_path / "run.parquet")
    assert table.num_rows == 5
    assert table.column("session_id").to_pylist() == ["run"] * 5
    assert table.column("seq").to_pylist() == [0, 1, 2, 3, 4]


@pytest.mark.e2e
async def test_parquet_schema_is_stable(tmp_path: Path) -> None:
    sink = ParquetSink(path=str(tmp_path))
    await sink.on_event(make_event(session_id="sc"))
    await sink.close()

    table = pq.read_table(tmp_path / "sc.parquet")
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
        "motivation",
        "payload_json",
        "metadata_json",
        "duration_ms",
    }
    assert expected <= set(table.column_names)


@pytest.mark.e2e
async def test_parquet_classification_dimensions_round_trip_as_lists(tmp_path: Path) -> None:
    classification = Classification(
        mechanism="shell",
        effect="mutating",
        scope=frozenset({"fs.write", "fs.read"}),
        action=frozenset({"edit", "build"}),
        capability=frozenset({"writes-files"}),
        shell_dialect="bash",
        binaries=("git", "npm"),
    )
    metadata = EventMetadata(
        classification=classification,
        phases=frozenset({Phase.IMPLEMENTATION, Phase.VERIFICATION}),
        motivation=ToolMotivation(intent="add retry logic"),
    )
    sink = ParquetSink(path=str(tmp_path))
    await sink.on_event(
        make_event(
            kind=EventKind.TOOL_CALL_STARTED,
            session_id="cls",
            payload={"tool_name": "bash"},
            metadata=metadata,
        )
    )
    await sink.on_event(make_event(kind=EventKind.SESSION_ENDED, session_id="cls"))
    await sink.close()

    table = pq.read_table(tmp_path / "cls.parquet")
    first = {col: table.column(col)[0].as_py() for col in table.column_names}
    assert first["mechanism"] == "shell"
    assert first["effect"] == "mutating"
    assert sorted(first["scope"]) == ["fs.read", "fs.write"]
    assert sorted(first["action"]) == ["build", "edit"]
    assert first["capability"] == ["writes-files"]
    assert first["shell_dialect"] == "bash"
    assert first["binaries"] == ["git", "npm"]
    assert sorted(first["phase_signals"]) == ["implementation", "verification"]
    assert first["motivation"] == "add retry logic"


@pytest.mark.e2e
async def test_parquet_title_sidecar_is_written(tmp_path: Path) -> None:
    sink = ParquetSink(path=str(tmp_path))
    await sink.on_event(make_event(session_id="withtitle"))
    await sink.on_title_update(
        TitleUpdate(
            session_id="withtitle",
            segment_id="withtitle",
            kind="session",
            title="Investigate the flake",
            version=1,
        )
    )
    await sink.close()

    sidecar = tmp_path / "withtitle.titles.parquet"
    assert sidecar.exists()
    table = pq.read_table(sidecar)
    assert table.num_rows == 1
    assert table.column("title").to_pylist() == ["Investigate the flake"]
    assert table.column("kind").to_pylist() == ["session"]


@pytest.mark.e2e
async def test_parquet_write_error_propagates(tmp_path: Path) -> None:
    """Defined failure behavior: an OSError while writing the parquet file is
    logged and *re-raised* — the sink deliberately does not swallow it."""
    sink = ParquetSink(path=str(tmp_path))
    await sink.on_event(make_event(session_id="boom"))

    with mock.patch(
        "traceforge.sinks.parquet.pq.write_table", side_effect=OSError("No space left on device")
    ):
        with pytest.raises(OSError, match="No space left"):
            await sink.close()  # flush -> write_table -> re-raised
