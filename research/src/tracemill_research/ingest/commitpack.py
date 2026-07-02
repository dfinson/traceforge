"""Harvest permissive-licensed commit subjects from ``bigcode/commitpackft``.

CommitPackFT is bigcode's *filtered* commit corpus (bot/merge/opaque commits
already removed, Levenshtein-deduplicated upstream): one row per single-file
commit carrying ``old_contents`` / ``new_contents`` (the change), ``subject``
(the one-line imperative commit title -- verb-first, ~5-8 words) and a per-row
SPDX ``license`` tag. The ``subject`` is exactly the span-title shape the
tracemill titler must learn (context -> short imperative summary), and it is
real human-written text at ~560K permissive-licensed rows, so it is the
clean-license one-line-summarization gold the organic agent corpus is too thin
to supply. See ``research/experiments/titler-commit-data.yaml``.

This ingester follows the **production pipeline** exactly (mirrors
``ingest/codeplane.py`` / ``ingest/swe_agent.py``): each commit is expanded into
one canonical ``tool.call.completed`` code-edit event carrying the touched file
and the *unified diff* of the change, flows through the real
:class:`~tracemill.enricher.Enricher` and
:class:`~tracemill.sinks.parquet.ParquetSink`, so the on-disk schema is
byte-identical to the copilot/swe/codeplane corpora. The commit sha is stamped
onto ``metadata.step_id`` so the enriched corpus regroups per commit, and the
serve-side :func:`~tracemill.title.context.distilled_context` is recomputed from
the enriched rows -- zero train/serve skew.

Two invariants keep this leak-free and heuristic-free:

* **The gold is never in the context.** The commit ``message`` (whose first line
  *is* the ``subject``) is never emitted; only the code diff + file path feed the
  distilled context. The target therefore cannot leak into the input.
* **No content filtering.** The only row filter is the per-row SPDX
  ``license`` -- a legal constraint, not a quality heuristic. CommitPackFT is
  already curated upstream; we take its natural distribution and let the
  source-parity training sampler bound the commit source's mass.

Never re-ingest labelled tracemill sessions: commitpackft is a **new** source
keyed by commit sha, so no ``event_id`` collision concern applies.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field

from ..paths import DATA_PROCESSED

logger = logging.getLogger(__name__)

_FROZEN = ConfigDict(frozen=True, extra="forbid")

_HF_REPO = "bigcode/commitpackft"

#: SPDX tags that are unambiguously permissive / public-domain and safe to ship a
#: model trained on. This is a **legal** allowlist, not a quality filter: any row
#: whose ``license`` is outside this set (copyleft agpl/lgpl/gpl, ``unknown``,
#: empty, or anything unrecognised) is dropped so the shipped model carries no
#: copyleft or unclear-provenance training text.
PERMISSIVE_LICENSES = frozenset(
    {
        "mit",
        "apache-2.0",
        "bsd-3-clause",
        "bsd-2-clause",
        "bsd-2-clause-patent",
        "isc",
        "mpl-2.0",
        "unlicense",
        "cc0-1.0",
        "0bsd",
        "zlib",
        "boost-1.0",
        "epl-1.0",
        "epl-2.0",
        "artistic-2.0",
        "ncsa",
        "postgresql",
    }
)


class CommitpackIngestConfig(BaseModel):
    """YAML-loadable config for the CommitPackFT harvest."""

    model_config = _FROZEN

    langs: tuple[str, ...] | None = Field(
        None,
        description="Language subdirs (data/<lang>/data.jsonl) to ingest. None = "
        "every language file in the repo (no hand-selection).",
    )
    output_dir: Path = Field(
        ...,
        description="Per-language enriched-corpus output. Files named commitpackft-<lang>.parquet.",
    )
    distill_shard_dir: Path = Field(
        ...,
        description="Per-language distillation-gold shards (checkpointing: an "
        "existing shard is skipped so the run resumes).",
    )
    distill_out: Path = Field(
        ...,
        description="Combined per-commit distillation gold table (concat of shards).",
    )
    max_commits_per_lang: int | None = Field(
        None,
        ge=1,
        description="Optional hard cap per language (debug / smoke only).",
    )


def default_output_dir() -> Path:
    from ..paths import DATA_INTERIM

    return DATA_INTERIM / "labeling-corpus" / "commitpackft"


def default_distill_shard_dir() -> Path:
    return DATA_PROCESSED / "commitpack-distill"


def default_distill_out() -> Path:
    return DATA_PROCESSED / "commitpack-distill.parquet"


# ---------------------------------------------------------------------------
# HF file access (the dataset ships a loader script datasets>=3 rejects, so we
# read the per-language jsonl data files directly).
# ---------------------------------------------------------------------------


def list_langs() -> list[str]:
    """Every ``data/<lang>/data.jsonl`` language present in the repo."""
    from huggingface_hub import HfApi

    files = HfApi().list_repo_files(_HF_REPO, repo_type="dataset")
    langs = [f.split("/")[1] for f in files if f.startswith("data/") and f.endswith("/data.jsonl")]
    return sorted(langs)


def _download_lang(lang: str) -> Path:
    from huggingface_hub import hf_hub_download

    return Path(hf_hub_download(_HF_REPO, f"data/{lang}/data.jsonl", repo_type="dataset"))


def _iter_commits(path: Path, cap: int | None) -> Iterable[dict[str, Any]]:
    n = 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if str(row.get("license", "")).strip().lower() not in PERMISSIVE_LICENSES:
                continue
            subject = str(row.get("subject") or "").strip()
            if not subject:
                continue
            yield row
            n += 1
            if cap is not None and n >= cap:
                return


# ---------------------------------------------------------------------------
# Commit -> canonical events
# ---------------------------------------------------------------------------


def _unified_diff(old: str, new: str) -> str:
    """Deterministic unified diff of the change (difflib defaults, no tuning)."""
    return "\n".join(
        difflib.unified_diff(
            (old or "").splitlines(),
            (new or "").splitlines(),
            lineterm="",
        )
    )


def _edit_kind(old: str, new: str) -> str:
    if not (old or "").strip():
        return "create"
    if not (new or "").strip():
        return "delete"
    return "edit"


def _events_from_commit(
    commit: dict[str, Any],
    session_id: str,
    seq_start: int,
    next_ts,
) -> tuple[list[Any], int]:
    """One code-edit ``tool.call.completed`` event per commit.

    The commit ``message`` is deliberately not emitted (its first line is the
    gold ``subject``); only the file path + unified diff feed the context, so the
    target can never leak into the input.
    """
    from tracemill.types import EventKind, EventMetadata, SessionEvent

    sha = str(commit.get("commit") or "")
    new_file = str(commit.get("new_file") or "").strip()
    old_file = str(commit.get("old_file") or "").strip()
    path = new_file or old_file
    old_c = str(commit.get("old_contents") or "")
    new_c = str(commit.get("new_contents") or "")
    kind = _edit_kind(old_c, new_c)
    repos = str(commit.get("repos") or "")
    repo = repos.split(",")[0].strip() if repos else None

    payload: dict[str, Any] = {"tool_name": kind}
    if path:
        payload["files"] = [path]
    diff = _unified_diff(old_c, new_c)
    if diff:
        payload["snippet"] = diff

    ev = SessionEvent(
        id=f"{sha}::0",
        kind=EventKind.TOOL_CALL_COMPLETED,
        session_id=session_id,
        timestamp=next_ts(),
        payload=payload,
        raw_event={"commit": sha, "edit_kind": kind},
        metadata=EventMetadata(
            source_framework="commitpackft",
            ingestion_mode="replay",
            raw_kind=kind,
            repo=repo,
            step_id=sha,
            sequence=seq_start,
            partial=False,
        ),
    )
    return [ev], seq_start + 1


# ---------------------------------------------------------------------------
# Main ingest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IngestStats:
    langs_seen: int
    langs_emitted: int
    commits_kept: int
    events_emitted: int
    distill_rows: int
    langs: tuple[str, ...]


def _iter_enriched(result: Any) -> Iterable[Any]:
    if result is None:
        return ()
    if isinstance(result, list):
        return result
    return (result,)


async def _ingest_lang(
    lang: str,
    config: CommitpackIngestConfig,
) -> list[dict[str, Any]]:
    """Enrich one language into its session parquet and return its gold rows.

    Idempotent / resumable: if the language's distill shard already exists it is
    loaded and returned without re-enriching.
    """
    from tracemill.enricher import Enricher
    from tracemill.sinks.parquet import ParquetSink

    import pyarrow.parquet as pq

    shard = config.distill_shard_dir / f"{lang}.parquet"
    if shard.is_file():
        logger.info("resume: shard exists for %s", lang)
        return pq.read_table(shard).to_pandas().to_dict("records")

    path = _download_lang(lang)
    session_id = f"commitpackft-{lang}"
    sink = ParquetSink(path=str(config.output_dir / "{session_id}.parquet"))
    enricher = Enricher()
    base = datetime(2026, 1, 1, tzinfo=UTC)
    tick = {"n": 0}

    def _next_ts():
        tick["n"] += 1
        return base + timedelta(seconds=tick["n"])

    gold: list[dict[str, Any]] = []
    seq = 0
    try:
        for commit in _iter_commits(path, config.max_commits_per_lang):
            events, seq = _events_from_commit(commit, session_id, seq, _next_ts)
            for ev in events:
                for emitted in _iter_enriched(enricher.process(ev)):
                    await sink.on_event(emitted)
            gold.append(
                {
                    "session_id": session_id,
                    "lang": lang,
                    "commit": str(commit.get("commit") or ""),
                    "license": str(commit.get("license") or ""),
                    "subject": str(commit.get("subject") or "").strip(),
                    "context": "(no signal)",  # filled by _attach_distilled_context
                }
            )
    finally:
        await sink.close()

    _attach_distilled_context(config.output_dir, session_id, gold)
    _write_shard(shard, gold)
    logger.info("ingested %s: %d commits", lang, len(gold))
    return gold


async def ingest(config: CommitpackIngestConfig) -> IngestStats:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    config.distill_shard_dir.mkdir(parents=True, exist_ok=True)
    config.distill_out.parent.mkdir(parents=True, exist_ok=True)

    langs = list(config.langs) if config.langs else list_langs()

    all_gold: list[dict[str, Any]] = []
    langs_emitted = 0
    for lang in langs:
        try:
            gold = await _ingest_lang(lang, config)
        except Exception:  # noqa: BLE001
            logger.exception("ingest failed for lang %s", lang)
            continue
        if gold:
            langs_emitted += 1
            all_gold.extend(gold)

    _write_shard(config.distill_out, all_gold)

    return IngestStats(
        langs_seen=len(langs),
        langs_emitted=langs_emitted,
        commits_kept=len(all_gold),
        events_emitted=len(all_gold),
        distill_rows=len(all_gold),
        langs=tuple(langs),
    )


def _session_shards(output_dir: Path, session_id: str) -> list[Path]:
    """All parquet shards ParquetSink wrote for one session (it rotates large
    sessions into ``<sid>.parquet`` + ``<sid>.<n>.parquet``)."""
    return sorted(
        list(output_dir.glob(f"{session_id}.parquet"))
        + list(output_dir.glob(f"{session_id}.*.parquet")),
        key=lambda p: p.name,
    )


def _attach_distilled_context(
    output_dir: Path, session_id: str, gold_rows: list[dict[str, Any]]
) -> None:
    """Recompute serve-side ``distilled_context`` per commit from the enriched
    corpus, grouping feature rows (across ALL session shards) by
    ``metadata.step_id`` (== commit sha)."""
    import pyarrow.parquet as pq

    from tracemill.title.context import distilled_context

    shards = _session_shards(output_dir, session_id)
    if not shards:
        return
    groups: dict[str, list[dict[str, Any]]] = {}
    for parquet in shards:
        df = pq.read_table(parquet).to_pandas()
        for rec in df.to_dict("records"):
            step_id = _step_id_of(rec)
            if step_id is None:
                continue
            groups.setdefault(step_id, []).append(rec)
    ctx_by_sha: dict[str, str] = {}
    for sha, recs in groups.items():
        recs.sort(key=lambda r: r.get("seq", 0))
        ctx_by_sha[sha] = distilled_context(recs)
    for g in gold_rows:
        g["context"] = ctx_by_sha.get(g["commit"], "(no signal)")


def _step_id_of(rec: dict[str, Any]) -> str | None:
    raw = rec.get("metadata_json")
    if not isinstance(raw, str):
        return None
    try:
        md = json.loads(raw)
    except ValueError:
        return None
    sid = md.get("step_id")
    return str(sid) if sid else None


def _write_shard(out: Path, gold_rows: list[dict[str, Any]]) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    out.parent.mkdir(parents=True, exist_ok=True)
    if not gold_rows:
        logger.warning("no gold rows to write -> %s", out)
        return
    pq.write_table(pa.Table.from_pylist(gold_rows), out)
    logger.info("wrote %d rows -> %s", len(gold_rows), out)


def run_sync(config: CommitpackIngestConfig) -> IngestStats:
    return asyncio.run(ingest(config))


def reattach(config: CommitpackIngestConfig) -> int:
    """Recompute ``distilled_context`` for every existing distill shard from the
    already-enriched corpus parquets (reads ALL session shards) and rewrite the
    shards + combined table. Use after a projection fix; no re-enrichment."""
    import pyarrow.parquet as pq

    shards = sorted(config.distill_shard_dir.glob("*.parquet"))
    all_gold: list[dict[str, Any]] = []
    for shard in shards:
        gold = pq.read_table(shard).to_pandas().to_dict("records")
        if not gold:
            continue
        session_id = str(gold[0]["session_id"])
        _attach_distilled_context(config.output_dir, session_id, gold)
        _write_shard(shard, gold)
        all_gold.extend(gold)
        with_ctx = sum(1 for g in gold if g["context"] != "(no signal)")
        logger.info("reattached %s: %d/%d with context", shard.stem, with_ctx, len(gold))
    _write_shard(config.distill_out, all_gold)
    total = sum(1 for g in all_gold if g["context"] != "(no signal)")
    logger.info("reattach done: %d/%d rows with context", total, len(all_gold))
    return 0


__all__ = [
    "CommitpackIngestConfig",
    "IngestStats",
    "PERMISSIVE_LICENSES",
    "default_distill_out",
    "default_distill_shard_dir",
    "default_output_dir",
    "ingest",
    "list_langs",
    "run_sync",
]
