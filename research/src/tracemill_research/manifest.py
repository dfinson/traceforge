"""Manifest loader + path resolver for data sources."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from tracemill_research.paths import DATA_MANIFEST


@dataclass(frozen=True)
class Source:
    id: str
    kind: str  # local-file | local-dir | hf-dataset | hf-file
    description: str
    path: str | None = None
    repo: str | None = None
    url: str | None = None
    fetched: str | None = None
    sha256: str | None = None
    count: int | None = None

    def resolved_path(self) -> Path | None:
        """Expand ~ and env vars in `path` (if local). Returns None for HF sources."""
        if not self.path:
            return None
        return Path(os.path.expandvars(os.path.expanduser(self.path)))


def load_manifest(path: Path | None = None) -> dict[str, Source]:
    """Load manifest.yaml and return a dict of source_id → Source."""
    p = path or DATA_MANIFEST
    with p.open() as f:
        raw = yaml.safe_load(f)
    sources: dict[str, Source] = {}
    for sid, entry in (raw.get("sources") or {}).items():
        sources[sid] = Source(
            id=sid,
            kind=entry["kind"],
            description=entry.get("description", ""),
            path=entry.get("path"),
            repo=entry.get("repo"),
            url=entry.get("url"),
            fetched=entry.get("fetched"),
            sha256=entry.get("sha256"),
            count=entry.get("count"),
        )
    return sources


def get(source_id: str) -> Source:
    return load_manifest()[source_id]


def get_path(source_id: str) -> Path:
    """Resolve a local source's path. Raises if source is not local or path missing."""
    src = get(source_id)
    p = src.resolved_path()
    if p is None:
        raise ValueError(f"Source {source_id!r} is not local (kind={src.kind})")
    if not p.exists():
        raise FileNotFoundError(f"Source {source_id!r} path does not exist: {p}")
    return p


def file_sha256(path: Path, chunk_size: int = 1 << 20) -> str:
    """SHA256 of a file (streaming)."""
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            buf = f.read(chunk_size)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()
