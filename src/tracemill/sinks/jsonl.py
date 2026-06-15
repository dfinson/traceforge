"""JSONL file sink — append one JSON line per event."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from tracemill.sinks.base import StorageSink
from tracemill.types import SessionEvent, TelemetrySpan, UsageRecord

logger = logging.getLogger(__name__)


class JsonlSink(StorageSink):
    """Appends events as JSON lines to a file.

    Supports ``{session_id}`` template in path for per-session output files.
    Creates parent directories on first write.
    """

    def __init__(self, path: str | Path, rotate_size_mb: float | None = None) -> None:
        self._path_template = str(path)
        self._rotate_size_mb = rotate_size_mb
        self._handles: dict[str, object] = {}

    _SAFE_SESSION_RE = __import__("re").compile(r"[^a-zA-Z0-9_\-.]")

    def _resolve_path(self, session_id: str) -> Path:
        sanitized = self._SAFE_SESSION_RE.sub("_", session_id)[:128]
        resolved = self._path_template.replace("{session_id}", sanitized)
        path = Path(resolved).expanduser().resolve()
        # Ensure resolved path stays under the template's parent directory
        base = Path(self._path_template.split("{session_id}")[0]).expanduser().resolve()
        if not str(path).startswith(str(base)):
            raise ValueError(f"JsonlSink: resolved path escapes base directory: {path}")
        return path

    async def on_event(self, event: SessionEvent) -> None:
        path = self._resolve_path(event.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)

        line = json.dumps(
            {
                "id": event.id,
                "kind": event.kind,
                "session_id": event.session_id,
                "timestamp": event.timestamp.isoformat(),
                "payload": event.payload,
                "metadata": event.metadata.model_dump(exclude_none=True)
                if event.metadata
                else None,
            },
            default=str,
        )

        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as exc:
            logger.error("JsonlSink: failed to write to %s: %s", path, exc)

    async def on_span(self, span: TelemetrySpan) -> None:
        pass

    async def on_usage(self, usage: UsageRecord) -> None:
        pass
