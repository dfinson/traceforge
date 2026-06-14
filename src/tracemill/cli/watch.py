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

from tracemill.cli.runner import (
    ResolvedPipeline,
    resolve_pipelines,
    watch_directory,
    watch_jsonl_file,
)
from tracemill.cli.score import ScoreServer
from tracemill.sources.auto_detect import detect_frameworks

logger = logging.getLogger(__name__)


@click.command()
@click.option("--config", "config_path", type=click.Path(exists=True), default=None)
@click.option("--frameworks", default=None, help="Comma-separated frameworks to watch (default: all detected).")
@click.option("--once", is_flag=True, help="Process existing files then exit (no watching).")
@click.option("--no-score", is_flag=True, help="Don't start the Score API server.")
@click.option("--log-level", default="INFO", type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"]))
def watch(config_path: str | None, frameworks: str | None, once: bool, no_score: bool, log_level: str) -> None:
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
    from tracemill.cli.factory import create_default_pipeline
    from tracemill.governance.persistence import SystemStore

    db_path = Path.home() / ".tracemill" / "system.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = SystemStore(db_path)

    # Load policy from config if available
    policy = None
    if config and config.get("governance", {}).get("tool_preflight_gate"):
        from tracemill.sdk.gate_policy import GatePolicy
        dotted = config["governance"]["tool_preflight_gate"]
        from tracemill.governance.pipeline import _import_dotted
        gate_fn = _import_dotted(dotted)
        policy = GatePolicy().preflight(gate_fn)

    pipeline = create_default_pipeline(store, policy=policy)

    # Start Gate IPC server (for CLI-based frameworks: Claude Code, Copilot CLI, etc.)
    from tracemill.gate.server import GateServer
    gate_server = GateServer(pipeline)
    gate_server.start()
    click.echo(f"Gate IPC server listening on {gate_server.sock_path}")

    # Register a default session so CLI clients can find us without knowing session_id upfront.
    # Detected framework sessions are also registered individually.
    gate_server.register_session("_default")
    for p in pipelines:
        gate_server.register_session(p.name)

    # Start Score API
    score_server: ScoreServer | None = None
    if not no_score:
        listen = config.get("score", {}).get("listen", "localhost:7331") if config else "localhost:7331"
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
        from tracemill.gate.registry import unregister_pid
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
    """Watch a single pipeline's source and process events."""
    logger.info("Starting watcher for %s at %s", pipeline.name, pipeline.source_path)

    if pipeline.source_path.is_dir():
        # Watch directory for JSONL files
        pattern = "*.jsonl" if pipeline.name != "continue" else "*.json"
        async for file_path, line in watch_directory(pipeline.source_path, pattern=pattern):
            await _process_line(line, pipeline, governance)
    elif pipeline.source_path.is_file():
        # Watch single file
        async for line in watch_jsonl_file(pipeline.source_path, start_at="end"):
            await _process_line(line, pipeline, governance)
    else:
        logger.warning("Source path does not exist: %s", pipeline.source_path)


async def _process_pipeline_once(pipeline: ResolvedPipeline, governance: "GovernancePipeline") -> None:
    """Process existing content in a pipeline's source (no watching)."""
    logger.info("Processing %s at %s", pipeline.name, pipeline.source_path)

    if pipeline.source_path.is_dir():
        pattern = "*.jsonl" if pipeline.name != "continue" else "*.json"
        for f in pipeline.source_path.rglob(pattern):
            if f.is_file():
                for line in f.read_text(encoding="utf-8").splitlines():
                    stripped = line.strip()
                    if stripped:
                        await _process_line(stripped, pipeline, governance)
    elif pipeline.source_path.is_file():
        for line in pipeline.source_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped:
                await _process_line(stripped, pipeline, governance)


async def _process_line(line: str, pipeline: ResolvedPipeline, governance: "GovernancePipeline") -> None:
    """Parse a raw line through the adapter and governance pipeline."""
    from tracemill.adapters.mapped_json import MappedJsonAdapter
    from tracemill.cli.runner import load_mapping_path
    from tracemill.sinks.console import ConsoleSink
    from tracemill.sinks.jsonl import JsonlSink
    from tracemill.sinks.sqlite_output import SqliteOutputSink

    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        logger.debug("Skipping non-JSON line in %s", pipeline.name)
        return

    # Adapt raw data to SessionEvent
    mapping_path = load_mapping_path(pipeline.adapter.mapping)
    adapter = MappedJsonAdapter.from_yaml(str(mapping_path), session_id=pipeline.name)
    events = list(adapter.parse_dict(data))

    for event in events:
        # Run through governance (bridge SessionEvent → EnrichmentContext)
        ctx = governance.context_from_session_event(event)
        governance.process_event(ctx)

        # Emit to sinks
        for sink_config in pipeline.sinks:
            try:
                sink_type = getattr(sink_config, "type", None)
                if sink_type == "console":
                    sink = ConsoleSink(
                        filter_actions=getattr(sink_config, "filter", None),
                        color=getattr(sink_config, "color", True),
                    )
                    await sink.emit(event)
                elif sink_type == "sqlite":
                    sink = SqliteOutputSink(path=sink_config.path)
                    await sink.emit(event)
                elif sink_type == "jsonl":
                    sink = JsonlSink(base_path=sink_config.path)
                    await sink.emit(event)
            except Exception as exc:
                logger.warning("Sink error (%s): %s", sink_config, exc)


def _load_config(config_path: str | None) -> dict | None:
    """Load config from file path or default locations."""
    import os

    if config_path:
        path = Path(config_path)
    else:
        env = os.environ.get("TRACEMILL_CONFIG")
        if env:
            path = Path(env)
        elif Path("tracemill.yaml").exists():
            path = Path("tracemill.yaml")
        elif (Path.home() / ".tracemill" / "config.yaml").exists():
            path = Path.home() / ".tracemill" / "config.yaml"
        else:
            return None

    if not path.exists():
        return None

    return yaml.safe_load(path.read_text(encoding="utf-8"))
