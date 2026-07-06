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


@dataclass(frozen=True)
class IntegrityWrite:
    """A self-contained prescription to (re)baseline one file's content hash.

    Emitted during side-effect-free labeling and committed later by the monitor's
    finalization transaction (mirroring ``mcp_deferred_writes``). Carries everything
    needed to persist the row so the writer never needs the verifier's ``repo``.
    """

    repo: str
    path: str
    sha256: str
    session_id: str
    timestamp: str


class IntegrityVerifier:
    """Verifies content integrity by comparing SHA-256 hashes against stored baselines.

    The repo key is derived **per event** from ``ctx.project_root`` (mirroring
    ``drift.py``'s ``ctx.project_root or "unknown"``), not fixed at construction. A
    single verifier therefore serves every session/repo a pipeline observes, so the
    default composition can wire it unconditionally without a construction-time repo.
    """

    def __init__(self, store: "SystemStore") -> None:
        self._store = store

    @staticmethod
    def _repo_for(ctx: "EnrichmentContext") -> str:
        """Repo key for this event — matches drift.py's ``project_root or "unknown"``."""
        return ctx.project_root or "unknown"

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
        repo = self._repo_for(ctx)
        for path, content in self._extract_file_writes(ctx):
            result = self.check(repo, path, content)
            if result and not result.matched:
                cap.add("integrity_unverified")

    def check(self, repo: str, path: str, content: bytes) -> IntegrityCheck | None:
        """Low-level: compare content hash against the stored value for ``repo``."""
        expected = self._store.get_content_hash(repo, path)
        if expected is None:
            return None  # Path not tracked

        actual = hashlib.sha256(content).hexdigest()
        # Get writer from content_hashes table
        row = self._store.connection.execute(
            "SELECT updated_by_session FROM content_hashes WHERE repo = ? AND file_path = ?",
            (repo, path),
        ).fetchone()
        writer = row[0] if row else None

        return IntegrityCheck(
            path=path,
            expected_hash=expected,
            actual_hash=actual,
            matched=(actual == expected),
            last_known_writer=writer,
        )

    def record_write(
        self, repo: str, path: str, content: bytes, session_id: str, timestamp: str
    ) -> None:
        """Record a new content hash after a successful write."""
        sha = hashlib.sha256(content).hexdigest()
        self._store.store_content_hash(repo, path, sha, session_id, timestamp)

    def pending_writes(self, ctx: "EnrichmentContext") -> list[IntegrityWrite]:
        """Prescriptions to (re)baseline this event's writes. Pure — no store mutation.

        Gated by the same :meth:`should_check` as :meth:`check_event`, so only
        mutating/destructive or ``filesystem_write`` events produce baselines. The
        actual persistence is deferred to the monitor's finalization commit, which runs
        *after* :meth:`check_event` has already compared against the prior baseline — so
        drift (including cross-session drift) is detected before the baseline is updated.
        """
        if not self.should_check(ctx.base_classification):
            return []
        repo = self._repo_for(ctx)
        ts = ctx.event.timestamp
        timestamp = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        session_id = ctx.event.session_id
        return [
            IntegrityWrite(
                repo=repo,
                path=path,
                sha256=hashlib.sha256(content).hexdigest(),
                session_id=session_id,
                timestamp=timestamp,
            )
            for path, content in self._extract_file_writes(ctx)
        ]

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
