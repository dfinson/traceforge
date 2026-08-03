"""Host-independent file-target normalization."""

from __future__ import annotations

import ntpath
import posixpath
import re
from collections.abc import Iterable

from traceforge.types import FileTarget

_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


def _is_windows_path(path: str) -> bool:
    return bool(_WINDOWS_DRIVE.match(path)) or path.startswith(("\\\\", "//"))


def _normalize_windows(path: str) -> str:
    normalized = ntpath.normpath(path.replace("/", "\\"))
    drive, rest = ntpath.splitdrive(normalized)
    if drive:
        drive = drive.upper()
    return (drive + rest).replace("\\", "/")


def _normalize_posix(path: str) -> str:
    return posixpath.normpath(path.replace("\\", "/"))


def normalize_file_target(raw_path: str, workspace_root: str | None = None) -> FileTarget:
    """Normalize one target, relativizing it only when it is within ``workspace_root``.

    Windows drive comparisons are case-insensitive even when this function runs on
    POSIX. Mixed separators are accepted for both Windows and POSIX inputs.
    """

    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("raw_path must be a non-empty string")

    raw = raw_path.strip()
    root = workspace_root.strip() if isinstance(workspace_root, str) else None
    windows = _is_windows_path(raw) or bool(root and _is_windows_path(root))

    if windows:
        normalized_root = _normalize_windows(root) if root else None
        candidate = raw
        if normalized_root and not _is_windows_path(candidate):
            candidate = ntpath.join(normalized_root.replace("/", "\\"), candidate)
        normalized = _normalize_windows(candidate)
        if normalized_root is None:
            return FileTarget(raw_path=raw_path, path=normalized)
        try:
            inside = ntpath.commonpath(
                [ntpath.normcase(normalized), ntpath.normcase(normalized_root)]
            ) == ntpath.normcase(normalized_root)
        except ValueError:
            inside = False
        path = (
            ntpath.relpath(normalized, normalized_root).replace("\\", "/") if inside else normalized
        )
        return FileTarget(raw_path=raw_path, path=path, inside_root=inside)

    normalized_root = _normalize_posix(root) if root else None
    candidate = raw
    if normalized_root and not candidate.startswith("/"):
        candidate = posixpath.join(normalized_root, candidate)
    normalized = _normalize_posix(candidate)
    if normalized_root is None:
        return FileTarget(raw_path=raw_path, path=normalized)
    inside = posixpath.commonpath([normalized, normalized_root]) == normalized_root
    path = posixpath.relpath(normalized, normalized_root) if inside else normalized
    return FileTarget(raw_path=raw_path, path=path, inside_root=inside)


def normalize_file_targets(
    raw_paths: Iterable[str], workspace_root: str | None = None
) -> tuple[FileTarget, ...]:
    """Normalize and de-duplicate targets while preserving first-seen order."""

    seen: set[tuple[str, str]] = set()
    targets: list[FileTarget] = []
    for raw_path in raw_paths:
        target = normalize_file_target(raw_path, workspace_root)
        key = (target.raw_path, target.path)
        if key not in seen:
            seen.add(key)
            targets.append(target)
    return tuple(targets)


__all__ = ["normalize_file_target", "normalize_file_targets"]
