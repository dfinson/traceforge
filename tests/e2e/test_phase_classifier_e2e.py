"""End-to-end phase-classifier test (issue #192): raw trace -> persisted phase labels.

This closes the one substantive pipeline subsystem that lacked a full-path
end-to-end test. The phase classifier (``src/traceforge/phase/``) is unit-tested
(``tests/unit/test_boundary_decode.py``, ``test_boundary_streaming.py``) and feeds
the (e2e-covered) titler, but nothing drove a *raw trace* all the way to
*phase-labelled rows in a DB*.

We reuse the zero-config ``traceforge watch --once`` path
(:func:`traceforge.cli.watch._process_pipeline_once`) — the same code path and
isolation pattern as :mod:`tests.e2e.test_watch_enrichment` — feeding the real
``claude_session.jsonl`` fixture into an **isolated** temp
:class:`~traceforge.sinks.sqlite_output.SqliteOutputSink` (never the live
``~/.traceforge/*.db``), then reopen that DB read-only and read back the per-event
``metadata.phase`` the trained classifier stamped live.

The phase producer is the packaged ``phase-model.joblib`` sklearn head over the
vendored ``potion-base-8M`` model2vec embedder — both default-on in
:class:`~traceforge.pipeline.EventPipeline` and both small, disk-loaded, and
network-free. The titler is left **off** (``enable_title=False``) so the test has
no dependency on the ML title-model download; only the small bundled phase models
run.

The asserted phase sequence is the REAL head's output captured against the shipped
weights (mirroring the golden discipline in
:mod:`tests.e2e.test_intelligence_determinism_e2e`) — it pins *determinism* and
that the real model ran, not a hand-authored notion of the "correct" phase. The
per-event ``id`` is a fresh UUID each run, so the golden is keyed by insertion
order (``rowid`` == emission order) and asserted as ``(kind, phase)`` pairs, never
by the non-deterministic id.
"""

from __future__ import annotations

import asyncio
import importlib
import sqlite3
from pathlib import Path

import pytest

from traceforge.cli.runner import ADAPTER_MAP, ResolvedPipeline
from traceforge.phase.features import MODEL2VEC_DIR
from traceforge.phase.inference import PACKAGED_MODEL_PATH as _PHASE_MODEL_PATH
from traceforge.sinks.sqlite_output import SqliteOutputSink

pytestmark = pytest.mark.e2e

# ``traceforge.cli`` re-exports the ``watch`` Command, shadowing the submodule, so
# fetch the real module object to reach ``_process_pipeline_once`` / monkeypatch
# ``_build_sinks`` (identical lever to test_watch_enrichment).
watch_mod = importlib.import_module("traceforge.cli.watch")

# The real Claude fixture: a small, realistic multi-turn coding session (read a
# file, fix a bug, run the tests). The per-file adapter stamps the file stem as
# the session id, so the run's session id is ``claude_session``.
_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "claude_session.jsonl"

#: The gated phase vocabulary the head can emit (``review`` folds into
#: ``verification``); every persisted label must be one of these.
_GATED_PHASES = frozenset({"planning", "implementation", "verification", "exploration"})

#: REAL phase-head output for the fixture, in emission order, as ``(kind, phase)``
#: pairs. Captured against the shipped ``phase-model.joblib`` + ``potion-base-8M``
#: weights. Content-bearing events (messages, tool calls, reasoning) are classified
#: directly; there is no plumbing here, so every row carries an intrinsic stamp.
#: Line 11 of the fixture is a ``result`` usage summary — it rides ``usage_records``
#: (not the enriched-events timeline), so it is absent here (10 events, not 11).
_PHASE_GOLDEN: list[tuple[str, str]] = [
    ("message.user", "planning"),  # "Read the contents of main.py and fix any bugs"
    ("message.assistant", "implementation"),  # "I'll read main.py first ..."
    ("tool.call.completed", "exploration"),  # read_file result (the buggy add())
    ("message.assistant", "planning"),  # "I found a bug ... Let me fix it."
    ("tool.call.completed", "implementation"),  # write_file result (the fix)
    ("message.assistant", "planning"),  # "I've fixed the bug in main.py ..."
    ("message.user", "planning"),  # "Thanks, run the tests now"
    ("tool.call.completed", "verification"),  # pytest result (3 passed)
    ("llm.thinking.chunk", "verification"),  # "The tests all pass now."
    ("message.assistant", "planning"),  # "All 3 tests pass. The fix is working ..."
]

# ─── Defensive Git-LFS pointer guard ─────────────────────────────────────────
#
# The phase weights ship via Git LFS; CI checks out with ``lfs: true`` so they are
# smudged and the real assertions run. An un-smudged pointer is a ~133-byte text
# stub beginning with the spec magic below; a real binary never starts with it, so
# this only skips when the weights are genuinely absent (never a false skip in CI).
_LFS_MAGIC = b"version https://git-lfs.github.com/spec/v1"


def _is_lfs_pointer(path: Path) -> bool:
    """True if ``path`` is missing/unreadable or a Git LFS pointer stub."""
    try:
        with open(path, "rb") as fh:
            return fh.read(len(_LFS_MAGIC)) == _LFS_MAGIC
    except OSError:
        return True


def _require_phase_weights() -> None:
    """Skip (with a clear reason) unless the small bundled phase weights are real.

    The titler is intentionally NOT required — this test disables it — so only the
    phase head (``phase-model.joblib``) and its model2vec embedder are checked.
    """
    weights = [Path(_PHASE_MODEL_PATH), Path(MODEL2VEC_DIR) / "model.safetensors"]
    missing = [str(p) for p in weights if _is_lfs_pointer(p)]
    if missing:
        pytest.skip(
            "bundled phase weights unavailable (Git LFS pointer or missing): "
            + ", ".join(missing)
            + " — CI checks out with lfs:true, so real assertions run there."
        )


def _run_fixture_once(tmp_path: Path, monkeypatch) -> Path:
    """Ingest the fixture once into a fresh **isolated** temp sqlite DB via the
    ``watch --once`` path with the titler OFF, and return the DB path."""
    db_path = tmp_path / "phase_out.db"
    sink = SqliteOutputSink(path=str(db_path))
    # Swap the daemon's sink factory for our isolated temp sink so nothing touches
    # the live ``~/.traceforge`` DB (mirrors test_watch_enrichment).
    monkeypatch.setattr(watch_mod, "_build_sinks", lambda _p: [sink])

    pipeline = ResolvedPipeline(
        name="claude",
        source_path=_FIXTURE,
        ingestion_mode="file_watch",
        adapter=ADAPTER_MAP["claude"],
        sinks=[],  # swapped for the isolated temp sink above
    )
    asyncio.run(watch_mod._process_pipeline_once(pipeline, governance=None, enable_title=False))
    return db_path


def _read_phase_sequence(db_path: Path) -> list[tuple[str, str]]:
    """Reopen the temp DB read-only and read back ``(kind, phase)`` in emission
    order.

    The sink writes one row per event as it is emitted (single-threaded, in file
    order), so ``rowid`` is insertion == emission order and gives a stable,
    deterministic per-event sequence. Phase lives in the ``metadata_json`` blob
    under ``$.phase`` (the singular authoritative stamp).
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT kind, json_extract(metadata_json, '$.phase') "
            "FROM enriched_events ORDER BY rowid"
        ).fetchall()
    finally:
        conn.close()
    return [(kind, phase) for kind, phase in rows]


def test_watch_once_persists_deterministic_phase_labels(tmp_path, monkeypatch) -> None:
    """Raw trace -> ``watch --once`` -> isolated temp DB yields the golden per-event
    phase sequence, using only the small bundled phase model (titler off)."""
    _require_phase_weights()

    db_path = _run_fixture_once(tmp_path, monkeypatch)

    # Isolation: the sink wrote to our temp path, never the live ~/.traceforge DB.
    assert db_path.exists()
    assert tmp_path in db_path.parents

    sequence = _read_phase_sequence(db_path)

    # Non-vacuous: the run actually processed events through the phase head.
    assert sequence, "no events were emitted through the --once path"

    # Every persisted label is a real gated phase (the head stamped each event).
    assert all(phase in _GATED_PHASES for _kind, phase in sequence), sequence

    # The titler was OFF, so no title-model download was needed and no titles land.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        (title_count,) = conn.execute("SELECT COUNT(*) FROM segment_titles").fetchone()
    finally:
        conn.close()
    assert title_count == 0, "titler was disabled but segment_titles were persisted"

    # The substantive assertion: the exact deterministic per-event phase sequence.
    assert sequence == _PHASE_GOLDEN


def test_phase_labels_are_deterministic_across_runs(tmp_path, monkeypatch) -> None:
    """Two independent ingests of the same fixture into two isolated temp DBs
    produce byte-identical per-event phase sequences (and both match the golden)."""
    _require_phase_weights()

    first = _read_phase_sequence(_run_fixture_once(tmp_path / "a", monkeypatch))
    second = _read_phase_sequence(_run_fixture_once(tmp_path / "b", monkeypatch))

    assert first == second == _PHASE_GOLDEN
