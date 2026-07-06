"""Replay command — one-shot processing of captured session files."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import click

logger = logging.getLogger(__name__)


@click.command()
@click.argument("path", type=click.Path(exists=True))
@click.option("--adapter", required=True, help="Adapter/mapping name (e.g., claude, codex, cline).")
@click.option(
    "--output",
    "output_path",
    type=click.Path(),
    default=None,
    help="Output file (JSONL). Default: console.",
)
@click.option(
    "--log-level", default="INFO", type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"])
)
def replay(path: str, adapter: str, output_path: str | None, log_level: str) -> None:
    """Process a captured session file or directory through the governance pipeline."""
    logging.basicConfig(
        level=getattr(logging, log_level),
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    source = Path(path)
    asyncio.run(_replay(source, adapter, output_path))


async def _replay(source: Path, adapter_name: str, output_path: str | None) -> None:
    from traceforge.adapters.mapped_json import MappedJsonAdapter
    from traceforge.cli.factory import create_default_pipeline
    from traceforge.cli.runner import load_mapping_path
    from traceforge.enricher import Enricher
    from traceforge.governance.persistence import SystemStore
    from traceforge.pipeline import EventPipeline
    from traceforge.sinks.callback import CallbackSink
    from traceforge.sinks.console import ConsoleSink
    from traceforge.sinks.jsonl import JsonlSink

    # Initialize governance
    db_path = Path.home() / ".traceforge" / "system.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = SystemStore(db_path)
    governance = create_default_pipeline(store)

    # Load adapter
    mapping_path = load_mapping_path(adapter_name)
    adapter = MappedJsonAdapter.from_yaml(str(mapping_path), session_id="replay")

    # Output sink
    if output_path:
        output_sink = JsonlSink(path=Path(output_path))
    else:
        output_sink = ConsoleSink(filter_actions=None, color=True)

    # Count governed events as the unified pipeline stamps and emits them.
    total_governed = 0

    async def _count(event) -> None:
        nonlocal total_governed
        if event.metadata is not None and event.metadata.governance is not None:
            total_governed += 1

    # Unified pipeline: adapt -> enrich -> classify -> govern (stage) -> sinks, so
    # governance is one stage and each emitted event carries metadata.governance.
    event_pipeline = EventPipeline(
        sinks=[output_sink, CallbackSink(on_event=_count)],
        enricher=Enricher(),
        governance=governance,
    )

    # Collect files to process
    files: list[Path] = []
    if source.is_dir():
        files = sorted(source.rglob("*.jsonl")) + sorted(source.rglob("*.json"))
    else:
        files = [source]

    total_events = 0

    try:
        for f in files:
            click.echo(f"Processing: {f.name}")
            content = f.read_text(encoding="utf-8")

            # Handle JSONL (line-delimited) vs single JSON
            if f.suffix == ".jsonl":
                lines = [l.strip() for l in content.splitlines() if l.strip()]
            else:
                # Single JSON file — try as array or single object
                try:
                    parsed = json.loads(content)
                    lines = (
                        [json.dumps(item) for item in parsed]
                        if isinstance(parsed, list)
                        else [content]
                    )
                except json.JSONDecodeError:
                    click.echo(f"  Skipping (invalid JSON): {f.name}", err=True)
                    continue

            for line in lines:
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                for event in adapter.parse_dict(data):
                    total_events += 1
                    await event_pipeline.push(event)
    finally:
        # Flush buffered (unpaired tool-start) events and release resources even
        # if processing raised, so the Enricher buffer is never silently dropped.
        try:
            await event_pipeline.close()
        finally:
            store.close()

    click.echo(f"\nReplay complete: {total_events} events, {total_governed} governed")
