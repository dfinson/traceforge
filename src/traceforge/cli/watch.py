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
    "--log-level", default="INFO", type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"])
)
def watch(
    config_path: str | None, frameworks: str | None, once: bool, no_score: bool, log_level: str
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
            asyncio.run(_run_once(pipelines, pipeline, store))
        else:
            asyncio.run(_run_watch(pipelines, pipeline, store))
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
) -> None:
    """Run all pipeline watchers concurrently."""
    tasks = []
    for p in pipelines:
        tasks.append(asyncio.create_task(_watch_pipeline(p, governance), name=f"watch-{p.name}"))

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
) -> None:
    """Process existing files once and exit."""
    for p in pipelines:
        await _process_pipeline_once(p, governance)


async def _watch_pipeline(pipeline: ResolvedPipeline, governance: "GovernancePipeline") -> None:
    """Watch a single pipeline's source and feed events through the unified pipeline."""
    logger.info("Starting watcher for %s at %s", pipeline.name, pipeline.source_path)

    if not (pipeline.source_path.is_dir() or pipeline.source_path.is_file()):
        logger.warning("Source path does not exist: %s", pipeline.source_path)
        return

    adapter, event_pipeline = _build_pipeline_runtime(pipeline, governance)
    try:
        if pipeline.source_path.is_dir():
            # Watch directory for JSONL files
            pattern = "*.jsonl" if pipeline.name != "continue" else "*.json"
            async for _file_path, line in watch_directory(pipeline.source_path, pattern=pattern):
                await _feed_line(line, adapter, event_pipeline)
        else:
            # Watch single file
            async for line in watch_jsonl_file(pipeline.source_path, start_at="end"):
                await _feed_line(line, adapter, event_pipeline)
    finally:
        # Emit any buffered (unpaired tool-start) events and release sink resources.
        await event_pipeline.close()


async def _process_pipeline_once(
    pipeline: ResolvedPipeline, governance: "GovernancePipeline"
) -> None:
    """Process existing content in a pipeline's source once (no watching)."""
    logger.info("Processing %s at %s", pipeline.name, pipeline.source_path)

    if not (pipeline.source_path.is_dir() or pipeline.source_path.is_file()):
        return

    adapter, event_pipeline = _build_pipeline_runtime(pipeline, governance)
    try:
        if pipeline.source_path.is_dir():
            pattern = "*.jsonl" if pipeline.name != "continue" else "*.json"
            for f in pipeline.source_path.rglob(pattern):
                if f.is_file():
                    for line in f.read_text(encoding="utf-8").splitlines():
                        stripped = line.strip()
                        if stripped:
                            await _feed_line(stripped, adapter, event_pipeline)
        else:
            for line in pipeline.source_path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped:
                    await _feed_line(stripped, adapter, event_pipeline)
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


def _build_pipeline_runtime(pipeline: ResolvedPipeline, governance: "GovernancePipeline"):
    """Build the ``(adapter, EventPipeline)`` runtime for a resolved pipeline.

    The daemon runs the same unified pipeline as the SDK: adapt -> enrich ->
    classify -> govern -> sinks, with ``governance`` wired in as a *stage* so every
    emitted event carries ``metadata.governance``. Governance is one stage here, not
    the pipeline itself. The shared governance engine also backs the Gate/Score IPC
    servers, so session budget/drift state stays unified across observation and
    preflight. Live ML structuring (phase + boundary labeling) is enabled by
    default, matching the SDK ``Pipeline``.
    """
    from traceforge.adapters.mapped_json import MappedJsonAdapter
    from traceforge.enricher import Enricher
    from traceforge.pipeline import EventPipeline

    mapping_path = load_mapping_path(pipeline.adapter.mapping)
    adapter = MappedJsonAdapter.from_yaml(str(mapping_path), session_id=pipeline.name)
    event_pipeline = EventPipeline(
        sinks=_build_sinks(pipeline),
        enricher=Enricher(),
        governance=governance,
    )
    return adapter, event_pipeline


async def _feed_line(line: str, adapter, event_pipeline) -> None:
    """Parse one raw line and push each resulting event through the pipeline."""
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        logger.debug("Skipping non-JSON line")
        return

    for event in adapter.parse_dict(data):
        await event_pipeline.push(event)


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
