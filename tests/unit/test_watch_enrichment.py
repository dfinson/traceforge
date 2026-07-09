"""Unit tests for the ``watch`` enrichment toggles (issue #155).

Two knobs on the dashboard-feeding ``watch`` path are wired here, exercised
without running the daemon:

* ``_build_event_pipeline(..., enable_title=...)`` decides whether the per-segment
  title inferencer is constructed. The internal helpers keep the historical
  ``enable_title=False`` default so existing positional callers (e.g. the session
  -split regression test) never start loading the title model.
* the user-facing ``--titles/--no-titles`` click flag defaults **on**, because
  watch's job here is feeding the rich dashboard chapter tree; the opt-out only
  exists for max-throughput users.
"""

from __future__ import annotations

import importlib
from pathlib import Path

from traceforge.cli.runner import ADAPTER_MAP, ResolvedPipeline

# ``traceforge.cli`` re-exports the ``watch`` Command, which shadows the submodule
# attribute, so fetch the real module object to reach its private helpers and the
# Command's declared params.
watch_mod = importlib.import_module("traceforge.cli.watch")


def _minimal_pipeline() -> ResolvedPipeline:
    """A resolved pipeline whose ``source_path`` is never read by pipeline build."""
    return ResolvedPipeline(
        name="claude",
        source_path=Path("."),
        ingestion_mode="file_watch",
        adapter=ADAPTER_MAP["claude"],
        sinks=[],
    )


def test_build_event_pipeline_title_toggle(monkeypatch) -> None:
    """``enable_title`` decides whether the titler is built; default stays off."""
    monkeypatch.setattr(watch_mod, "_build_sinks", lambda _p: [])
    pipeline = _minimal_pipeline()

    with_titles = watch_mod._build_event_pipeline(pipeline, governance=None, enable_title=True)
    assert with_titles._title_inferencer is not None

    without_titles = watch_mod._build_event_pipeline(pipeline, governance=None, enable_title=False)
    assert without_titles._title_inferencer is None

    # The default must remain off so internal positional callers do not silently
    # start loading the ONNX title model (preserves existing test behavior).
    default_pipeline = watch_mod._build_event_pipeline(pipeline, governance=None)
    assert default_pipeline._title_inferencer is None


def test_watch_titles_flag_defaults_on() -> None:
    """The ``--titles/--no-titles`` boolean flag exists and defaults to True."""
    titles_opt = next(p for p in watch_mod.watch.params if p.name == "titles")

    assert titles_opt.is_flag
    assert titles_opt.default is True
    assert "--titles" in titles_opt.opts
    assert "--no-titles" in titles_opt.secondary_opts
