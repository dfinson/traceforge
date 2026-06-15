"""Pipeline runner — resolves detected frameworks into runnable pipelines and multiplexes sources."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

from tracemill.config.models import (
    ConsoleSinkConfig,
    MappedJsonAdapterConfig,
    SqliteSinkConfig,
)
from tracemill.sources.auto_detect import DetectedFramework

logger = logging.getLogger(__name__)


# ─── Mapping loader ──────────────────────────────────────────────────────────

_BUNDLED_MAPPINGS_DIR = Path(__file__).resolve().parent.parent / "mappings"


def load_mapping_path(name: str) -> Path:
    """Resolve a mapping name to its YAML file path.

    Searches bundled mappings directory. Raises FileNotFoundError if not found.
    """
    path = _BUNDLED_MAPPINGS_DIR / f"{name}.yaml"
    if path.exists():
        return path
    raise FileNotFoundError(f"No mapping found for {name!r} (searched {_BUNDLED_MAPPINGS_DIR})")


# ─── Adapter resolution ─────────────────────────────────────────────────────

ADAPTER_MAP: dict[str, MappedJsonAdapterConfig] = {
    "claude": MappedJsonAdapterConfig(type="mapped_json", mapping="claude"),
    "codex": MappedJsonAdapterConfig(type="mapped_json", mapping="codex"),
    "continue": MappedJsonAdapterConfig(type="mapped_json", mapping="continue_dev"),
    "cline": MappedJsonAdapterConfig(type="mapped_json", mapping="cline"),
    "goose": MappedJsonAdapterConfig(type="mapped_json", mapping="goose"),
    "amazonq": MappedJsonAdapterConfig(type="mapped_json", mapping="amazonq"),
    "aider_markdown": MappedJsonAdapterConfig(type="mapped_json", mapping="aider"),
}


# ─── Resolved pipeline config ───────────────────────────────────────────────


@dataclass(frozen=True)
class ResolvedPipeline:
    """A fully-resolved pipeline ready to run."""

    name: str
    source_path: Path
    ingestion_mode: str
    adapter: MappedJsonAdapterConfig
    sinks: list = field(default_factory=list)


def resolve_pipelines(
    detected: list[DetectedFramework],
    explicit_source_paths: set[str] | None = None,
    default_sinks: list | None = None,
) -> list[ResolvedPipeline]:
    """Map detected frameworks to runnable pipeline configs.

    Args:
        detected: Frameworks found by auto-detection.
        explicit_source_paths: Paths already covered by explicit pipeline configs.
            Auto-detected frameworks at these paths are skipped (explicit wins).
        default_sinks: Sink configs to attach when none specified.

    Returns:
        List of resolved pipelines ready to instantiate.
    """
    explicit = explicit_source_paths or set()
    sinks = default_sinks if default_sinks is not None else _default_sinks()
    pipelines: list[ResolvedPipeline] = []

    for fw in detected:
        path_key = str(fw.path)
        if path_key in explicit:
            logger.debug("Skipping auto-detected %s (explicit pipeline exists)", fw.name)
            continue

        adapter = ADAPTER_MAP.get(fw.adapter)
        if adapter is None:
            logger.warning("No adapter mapping for framework %r (adapter=%r)", fw.name, fw.adapter)
            continue

        pipelines.append(
            ResolvedPipeline(
                name=fw.name,
                source_path=fw.path,
                ingestion_mode=fw.ingestion_mode,
                adapter=adapter,
                sinks=sinks,
            )
        )

    return pipelines


def _default_sinks() -> list:
    """Default sinks when user hasn't configured any."""
    return [
        SqliteSinkConfig(type="sqlite", path=Path.home() / ".tracemill" / "tracemill.db"),
        ConsoleSinkConfig(type="console"),
    ]


# ─── Source multiplexer ──────────────────────────────────────────────────────


async def watch_jsonl_file(path: Path, start_at: str = "end") -> AsyncIterator[str]:
    """Watch a JSONL file for new lines using polling.

    Yields raw JSON line strings as they appear.
    """
    if not path.exists():
        logger.info("Waiting for file to appear: %s", path)
        while not path.exists():
            await asyncio.sleep(1.0)

    offset = path.stat().st_size if start_at == "end" else 0

    while True:
        current_size = path.stat().st_size
        if current_size > offset:
            with open(path, "r", encoding="utf-8") as f:
                f.seek(offset)
                for line in f:
                    stripped = line.strip()
                    if stripped:
                        yield stripped
                offset = f.tell()
        await asyncio.sleep(0.5)


async def watch_directory(
    directory: Path, pattern: str = "*.jsonl", start_at: str = "end"
) -> AsyncIterator[tuple[Path, str]]:
    """Watch a directory for new JSONL files and yield (path, line) tuples.

    Monitors existing files for growth and picks up new files as they appear.
    """
    known_files: dict[Path, int] = {}

    # Initial scan
    if directory.exists():
        for f in directory.rglob(pattern):
            if f.is_file():
                known_files[f] = f.stat().st_size if start_at == "end" else 0

    while True:
        # Check for new files
        if directory.exists():
            for f in directory.rglob(pattern):
                if f.is_file() and f not in known_files:
                    known_files[f] = 0
                    logger.info("Discovered new file: %s", f)

        # Read new content from all tracked files
        for fpath, offset in list(known_files.items()):
            if not fpath.exists():
                continue
            current_size = fpath.stat().st_size
            if current_size > offset:
                with open(fpath, "r", encoding="utf-8") as fh:
                    fh.seek(offset)
                    for line in fh:
                        stripped = line.strip()
                        if stripped:
                            yield (fpath, stripped)
                    known_files[fpath] = fh.tell()

        await asyncio.sleep(1.0)
