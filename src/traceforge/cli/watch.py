"""Watch command — primary daemon that auto-detects, watches sources, governs, and sinks."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
from pathlib import Path

import click
import yaml

from traceforge.cli.runner import (
    ResolvedPipeline,
    load_mapping_path,
    resolve_pipelines,
    watch_directory,
    watch_jsonl_file,
)
from traceforge.cli.score import ScoreServer
from traceforge.sources.auto_detect import detect_frameworks
from traceforge.types import EventKind, UsageRecord

logger = logging.getLogger(__name__)


def _policy_is_enforcing(policy) -> bool:
    """True if ``policy`` actually gates anything (a preflight or postflight gate).

    ``None`` or an empty :class:`~traceforge.sdk.gate_policy.GatePolicy` means the
    gate runs in allow-all mode.
    """
    return policy is not None and (policy.has_preflight or policy.has_postflight)


def _warn_gating_inactive() -> None:
    """Emit a LOUD warning that the gate is running in allow-all mode.

    The gate IPC server and the injected agent hooks are up, but with no policy
    every tool call is ALLOWED — enforcement is inactive. Operators must not
    mistake "hooks installed" for "protected", so this is a prominent stderr
    banner (plus a WARNING log), not a quiet line.
    """
    banner = (
        "\n"
        "  ============================================================\n"
        "  WARNING: gating enforcement is INACTIVE (allow-all).\n"
        "  No gate policy is configured, so EVERY tool call is ALLOWED.\n"
        "  The gate IPC server and agent hooks are running, but they\n"
        "  enforce nothing until you declare a policy.\n"
        "\n"
        "  Enable enforcement by declaring a policy in your config, e.g.:\n"
        "    governance:\n"
        "      gate_policy:\n"
        "        preflight:\n"
        "          - myapp.policies.block_destructive   # dotted gate, or\n"
        "          - type: http                          # external decider\n"
        "            endpoint: http://127.0.0.1:8181/v1/data/traceforge/gate\n"
        "  then re-run:  traceforge watch --config <your-config>.yaml\n"
        "  ============================================================\n"
    )
    click.echo(banner, err=True)
    logger.warning("Gating enforcement INACTIVE (allow-all): no gate policy configured.")


@click.command()
@click.option("--config", "config_path", type=click.Path(exists=True), default=None)
@click.option(
    "--frameworks",
    default=None,
    help="Comma-separated frameworks to watch (default: all detected).",
)
@click.option("--once", is_flag=True, help="Process existing files then exit (no watching).")
@click.option("--no-score", is_flag=True, help="Don't start the Score API server.")
@click.option(
    "--titles/--no-titles",
    "titles",
    default=True,
    help=(
        "Infer chapter/segment titles (feeds the dashboard chapter tree). "
        "Default on; --no-titles skips per-segment ONNX inference for max throughput."
    ),
)
@click.option(
    "--log-level", default="INFO", type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"])
)
def watch(
    config_path: str | None,
    frameworks: str | None,
    once: bool,
    no_score: bool,
    titles: bool,
    log_level: str,
) -> None:
    """Watch detected frameworks, run governance pipeline, emit to sinks."""
    logging.basicConfig(
        level=getattr(logging, log_level),
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    config = _load_config(config_path)
    fw_list = [f.strip() for f in frameworks.split(",")] if frameworks else None

    # Auto-detect
    detected = detect_frameworks(fw_list)
    if not detected:
        click.echo("No frameworks detected. Nothing to watch.")
        sys.exit(0)

    click.echo(f"Detected {len(detected)} framework(s): {', '.join(d.name for d in detected)}")

    # Resolve pipelines
    pipelines = resolve_pipelines(detected)
    if not pipelines:
        click.echo("No pipelines could be resolved.")
        sys.exit(0)

    # Initialize governance
    from traceforge.cli.factory import create_default_pipeline
    from traceforge.governance.persistence import SystemStore

    db_path = Path.home() / ".traceforge" / "system.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = SystemStore(db_path)

    # Load a full gate policy (preflight chain + postflight + external gates) from
    # config. A declared-but-broken policy fails loudly below rather than silently
    # degrading to allow-all.
    from traceforge.governance.shield import build_policy_from_config

    try:
        policy = build_policy_from_config(config)
    except Exception as exc:  # noqa: BLE001 - refuse to start on a broken policy
        click.echo(f"ERROR: gate policy failed to load — refusing to start: {exc}", err=True)
        sys.exit(1)

    pipeline = create_default_pipeline(store, policy=policy)

    # Start Gate IPC server (for CLI-based frameworks: Claude Code, Copilot CLI, etc.)
    from traceforge.gate.server import GateServer

    gate_server = GateServer(pipeline)
    gate_server.start()
    click.echo(f"Gate IPC server listening on {gate_server.sock_path}")

    # Enforce-by-default UX: the server is up, but with no policy it ALLOWS every
    # call. Make that unmistakable so operators don't believe they're protected.
    if not _policy_is_enforcing(policy):
        _warn_gating_inactive()

    # Register a default session so CLI clients can find us without knowing session_id upfront.
    # Detected framework sessions are also registered individually.
    gate_server.register_session("_default")
    for p in pipelines:
        gate_server.register_session(p.name)

    # Start Score API
    score_server: ScoreServer | None = None
    if not no_score:
        listen = (
            config.get("score", {}).get("listen", "localhost:7331") if config else "localhost:7331"
        )
        score_server = ScoreServer(pipeline, listen=listen)
        score_server.start_background()

    # Run async event loop
    try:
        if once:
            asyncio.run(_run_once(pipelines, pipeline, store, titles))
        else:
            asyncio.run(_run_watch(pipelines, pipeline, store, titles))
    except KeyboardInterrupt:
        click.echo("\nShutting down...")
    finally:
        from traceforge.gate.registry import unregister_pid

        gate_server.stop()
        unregister_pid()
        if score_server:
            score_server.stop()
        store.close()


async def _run_watch(
    pipelines: list[ResolvedPipeline],
    governance: "GovernancePipeline",
    store: "SystemStore",
    enable_title: bool = False,
) -> None:
    """Run all pipeline watchers concurrently."""
    tasks = []
    for p in pipelines:
        tasks.append(
            asyncio.create_task(
                _watch_pipeline(p, governance, enable_title), name=f"watch-{p.name}"
            )
        )

    # Handle graceful shutdown
    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def _signal_handler():
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    click.echo(f"Watching {len(pipelines)} pipeline(s). Press Ctrl+C to stop.")

    # Wait until stop signal or all tasks complete
    done, pending = await asyncio.wait(
        [*tasks, asyncio.create_task(stop_event.wait())],
        return_when=asyncio.FIRST_COMPLETED,
    )

    # Cancel remaining tasks
    for t in pending:
        t.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


async def _run_once(
    pipelines: list[ResolvedPipeline],
    governance: "GovernancePipeline",
    store: "SystemStore",
    enable_title: bool = False,
) -> None:
    """Process existing files once and exit."""
    for p in pipelines:
        await _process_pipeline_once(p, governance, enable_title)


async def _watch_pipeline(
    pipeline: ResolvedPipeline, governance: "GovernancePipeline", enable_title: bool = False
) -> None:
    """Watch a single pipeline's source and feed events through the unified pipeline.

    The shared event pipeline is built once, but a *fresh* adapter is created
    lazily per source file — keyed to that file's session id (the filename stem)
    — so each file becomes its own run and no session's stateful adapter bleeds
    into another.
    """
    from traceforge.adapters.mapped_json import MappedJsonAdapter

    logger.info("Starting watcher for %s at %s", pipeline.name, pipeline.source_path)

    if not (pipeline.source_path.is_dir() or pipeline.source_path.is_file()):
        logger.warning("Source path does not exist: %s", pipeline.source_path)
        return

    event_pipeline = _build_event_pipeline(pipeline, governance, enable_title)
    try:
        if pipeline.source_path.is_dir():
            # Watch directory for JSONL files. One adapter per file, keyed to that
            # file's session id, created lazily as each file first yields a line.
            pattern = "*.jsonl" if pipeline.name != "continue" else "*.json"
            adapters: dict[Path, MappedJsonAdapter] = {}
            seen_by_file: dict[Path, set[str]] = {}
            async for file_path, line in watch_directory(pipeline.source_path, pattern=pattern):
                adapter = adapters.get(file_path)
                if adapter is None:
                    adapter = _build_adapter(pipeline, _session_id_for_source(file_path))
                    adapters[file_path] = adapter
                    seen_by_file[file_path] = set()
                await _feed_line(line, adapter, event_pipeline, seen_by_file[file_path])
        else:
            # Watch single file; one adapter keyed to that file's session id.
            adapter = _build_adapter(pipeline, _session_id_for_source(pipeline.source_path))
            seen: set[str] = set()
            async for line in watch_jsonl_file(pipeline.source_path, start_at="end"):
                await _feed_line(line, adapter, event_pipeline, seen)
    finally:
        # Emit any buffered (unpaired tool-start) events and release sink resources.
        await event_pipeline.close()


async def _process_pipeline_once(
    pipeline: ResolvedPipeline, governance: "GovernancePipeline", enable_title: bool = False
) -> None:
    """Process existing content in a pipeline's source once (no watching).

    The shared event pipeline is built once, but a *fresh* adapter is built per
    source file keyed to that file's session id (the filename stem), so each file
    becomes its own run instead of collapsing into a single ``pipeline.name`` run.
    """
    logger.info("Processing %s at %s", pipeline.name, pipeline.source_path)

    if not (pipeline.source_path.is_dir() or pipeline.source_path.is_file()):
        return

    event_pipeline = _build_event_pipeline(pipeline, governance, enable_title)
    try:
        if pipeline.source_path.is_dir():
            pattern = "*.jsonl" if pipeline.name != "continue" else "*.json"
            for f in pipeline.source_path.rglob(pattern):
                if f.is_file():
                    adapter = _build_adapter(pipeline, _session_id_for_source(f))
                    seen: set[str] = set()
                    for line in f.read_text(encoding="utf-8").splitlines():
                        stripped = line.strip()
                        if stripped:
                            await _feed_line(stripped, adapter, event_pipeline, seen)
        else:
            adapter = _build_adapter(pipeline, _session_id_for_source(pipeline.source_path))
            seen = set()
            for line in pipeline.source_path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped:
                    await _feed_line(stripped, adapter, event_pipeline, seen)
    finally:
        await event_pipeline.close()


def _build_sinks(pipeline: ResolvedPipeline) -> list:
    """Instantiate the configured sink objects for a resolved pipeline (once).

    Delegates to the shared :func:`traceforge.sinks.factory.build_sinks` so the
    daemon and the SDK build sinks from one mapping of the full ``SinkConfig``
    union.
    """
    from traceforge.sinks.factory import build_sinks

    return build_sinks(pipeline.sinks)


def _build_event_pipeline(
    pipeline: ResolvedPipeline, governance: "GovernancePipeline", enable_title: bool = False
):
    """Build the shared ``EventPipeline`` for a resolved pipeline (built once).

    The daemon runs the same unified pipeline as the SDK: adapt -> enrich ->
    classify -> govern -> sinks, with ``governance`` wired in as a *stage* so every
    emitted event carries ``metadata.governance``. Governance is one stage here, not
    the pipeline itself. The shared governance engine also backs the Gate/Score IPC
    servers, so session budget/drift state stays unified across observation and
    preflight. Live ML structuring (phase + boundary labeling) is enabled by
    default, matching the SDK ``Pipeline``. Per-segment title inference is gated by
    ``enable_title`` (driven by the ``watch --titles/--no-titles`` flag): when on,
    the titler is built and its :class:`TitleUpdate`\\ s are flushed to sinks on
    ``close()``, populating ``segment_titles`` for the dashboard chapter tree.

    The event pipeline is stateless with respect to *which* source file feeds it,
    so it is constructed a single time and reused across every file in the source
    directory — models load once. Per-file *session identity* is supplied by a
    fresh adapter per file (see :func:`_build_adapter`), not by this pipeline.
    """
    from traceforge.enricher import Enricher
    from traceforge.pipeline import EventPipeline

    return EventPipeline(
        sinks=_build_sinks(pipeline),
        enricher=Enricher(),
        governance=governance,
        enable_title=enable_title,
    )


def _build_adapter(pipeline: ResolvedPipeline, session_id: str):
    """Build a fresh ``MappedJsonAdapter`` for one source file.

    A new adapter is created per source file for two reasons: (1) it stamps the
    given ``session_id`` onto every event it emits, so each file's events carry
    that file's own session identity instead of sharing the framework name; and
    (2) ``MappedJsonAdapter`` is *stateful* (tool-call pairing, latest
    intent/reasoning), so a fresh instance stops one session's trailing state
    from bleeding into the next file.
    """
    from traceforge.adapters.mapped_json import MappedJsonAdapter

    mapping_path = load_mapping_path(pipeline.adapter.mapping)
    return MappedJsonAdapter.from_yaml(str(mapping_path), session_id=session_id)


def _session_id_for_source(path: Path) -> str:
    """Return the session id for a single source file.

    File-per-session frameworks (claude/codex/cline/opencode) name each file
    after the session it contains — the filename stem is the real session UUID
    (it equals the ``sessionId`` recorded inside the file). So the stem is the
    correct per-file session id.
    """
    return path.stem


async def _feed_line(line: str, adapter, event_pipeline, seen_msg_ids: set[str]) -> None:
    """Parse one raw line and route each resulting event.

    Non-usage events are pushed once onto the enriched-events timeline via
    :meth:`EventPipeline.push`. Usage-kind events (:data:`EventKind.USAGE`) are
    routed to ``usage_records`` **only** (the Cost lens source) via
    :meth:`EventPipeline.push_usage` and never ride the timeline — a run emits one
    usage record per assistant message, and putting ~N of them on the timeline
    would bury the real activity in the chapter tree.

    Real Claude Code repeats each assistant message across ~3 content-block lines
    that share one ``msg_id`` and identical usage, so ``seen_msg_ids`` (one set
    per source file) dedups on ``msg_id`` before a record is built — otherwise the
    replayed lines would multiply the token totals. Records without a ``msg_id``
    (e.g. the Agent-SDK ``result`` summary) are one-per-session and never deduped.
    """
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        logger.debug("Skipping non-JSON line")
        return

    for event in adapter.parse_dict(data):
        if event.kind == EventKind.USAGE:
            msg_id = event.payload.get("msg_id")
            if msg_id is not None:
                if msg_id in seen_msg_ids:
                    continue
                seen_msg_ids.add(str(msg_id))
            record = _usage_record_from(event)
            if record is not None:
                await event_pipeline.push_usage(record)
            continue
        await event_pipeline.push(event)


def _usage_record_from(event) -> "UsageRecord | None":
    """Build a :class:`UsageRecord` from a ``telemetry.usage`` event, or ``None``.

    Headline ``input_tokens`` is the *total context the model processed* — uncached
    input plus cache-read plus cache-creation — because on Claude Code almost all
    input is replayed cached context and the uncached delta alone is misleadingly
    tiny. The uncached/cache split is preserved losslessly in ``attributes`` so a
    future weighted-cost calc can price each component (cache-read is far cheaper).

    ``cost_usd`` is passed through when the source carries it (the Agent-SDK
    ``result`` record does) and left ``None`` otherwise — the per-message Claude
    Code wire has no cost, and one is never synthesized. A ``<synthetic>`` or absent
    model normalizes to ``""`` so it never pollutes the run's dominant model while
    its tokens still count. Records with all-zero tokens are pure noise and skipped.
    """
    payload = event.payload
    model = str(payload.get("model") or "")
    if model == "<synthetic>":
        model = ""

    input_uncached = int(payload.get("input_tokens") or 0)
    output_tokens = int(payload.get("output_tokens") or 0)
    cache_read = int(payload.get("cache_read_tokens") or 0)
    cache_write = int(payload.get("cache_write_tokens") or 0)
    total_input = input_uncached + cache_read + cache_write
    if total_input == 0 and output_tokens == 0:
        return None

    cost = payload.get("cost_usd")
    return UsageRecord(
        session_id=event.session_id,
        timestamp=event.timestamp,
        model=model,
        input_tokens=total_input,
        output_tokens=output_tokens,
        cost_usd=float(cost) if cost is not None else None,
        attributes={
            "input_uncached": input_uncached,
            "cache_read_tokens": cache_read,
            "cache_creation_tokens": cache_write,
        },
    )


def _load_config(config_path: str | None) -> dict | None:
    """Load config from file path or default locations."""
    import os

    if config_path:
        path = Path(config_path)
    else:
        env = os.environ.get("TRACEFORGE_CONFIG")
        if env:
            path = Path(env)
        elif Path("traceforge.yaml").exists():
            path = Path("traceforge.yaml")
        elif (Path.home() / ".traceforge" / "config.yaml").exists():
            path = Path.home() / ".traceforge" / "config.yaml"
        else:
            return None

    if not path.exists():
        return None

    return yaml.safe_load(path.read_text(encoding="utf-8"))
