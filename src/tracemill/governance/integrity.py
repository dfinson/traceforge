"""Content integrity verification via SHA-256 hashing."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tracemill.governance.persistence import SystemStore
    from tracemill.governance.types import EnrichmentContext


@dataclass(frozen=True)
class IntegrityCheck:
    """Result of verifying a file's content hash."""
    path: str
    expected_hash: str
    actual_hash: str
    matched: bool
    last_known_writer: str | None


class IntegrityVerifier:
    """Verifies content integrity by comparing SHA-256 hashes against stored baselines."""

    def __init__(self, store: "SystemStore", repo: str) -> None:
        self._store = store
        self._repo = repo

    def should_check(self, classification: "object") -> bool:
        """Whether to verify integrity for this classification."""
        from tracemill.classify.core import Classification
        if not isinstance(classification, Classification):
            return False
        return (
            classification.effect in ("mutating", "destructive")
            or "filesystem_write" in classification.capability
        )

    def check_event(self, ctx: "EnrichmentContext", cap: set[str]) -> None:
        """High-level: check integrity for all file writes in event."""
        if not self.should_check(ctx.base_classification):
            return
        for path, content in self._extract_file_writes(ctx):
            result = self.check(path, content)
            if result and not result.matched:
                cap.add("integrity_unverified")

    def check(self, path: str, content: bytes) -> IntegrityCheck | None:
        """Low-level: compare content hash against stored value."""
        expected = self._store.get_content_hash(self._repo, path)
        if expected is None:
            return None  # Path not tracked

        actual = hashlib.sha256(content).hexdigest()
        # Get writer from content_hashes table
        row = self._store.connection.execute(
            "SELECT updated_by_session FROM content_hashes WHERE repo = ? AND file_path = ?",
            (self._repo, path),
        ).fetchone()
        writer = row[0] if row else None

        return IntegrityCheck(
            path=path,
            expected_hash=expected,
            actual_hash=actual,
            matched=(actual == expected),
            last_known_writer=writer,
        )

    def record_write(self, path: str, content: bytes, session_id: str, timestamp: str) -> None:
        """Record a new content hash after a successful write."""
        sha = hashlib.sha256(content).hexdigest()
        self._store.store_content_hash(self._repo, path, sha, session_id, timestamp)

    def _extract_file_writes(self, ctx: "EnrichmentContext") -> list[tuple[str, bytes]]:
        """Extract file paths and content from a write event."""
        from tracemill.governance.types import ToolCallEvent
        import json

        writes: list[tuple[str, bytes]] = []
        if not isinstance(ctx.event, ToolCallEvent):
            return writes

        try:
            args = json.loads(ctx.event.tool_args_json)
        except (json.JSONDecodeError, TypeError):
            return writes

        # Common patterns: path+content, file+data, filename+text
        path = args.get("path") or args.get("file") or args.get("filename")
        content = args.get("content") or args.get("data") or args.get("text")

        if path and content:
            if isinstance(content, str):
                content = content.encode("utf-8")
            elif isinstance(content, bytes):
                pass  # Already bytes
            else:
                # Non-string content (dict, list, int) — serialize deterministically
                content = json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
            writes.append((str(path), content))

        return writes
