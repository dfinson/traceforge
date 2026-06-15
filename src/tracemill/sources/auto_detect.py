"""Framework auto-detection — discovers installed AI coding agents by checking well-known paths."""

from __future__ import annotations

import logging
import os
import platform
from pathlib import Path

logger = logging.getLogger(__name__)


def _expand(path: str) -> Path:
    return Path(os.path.expanduser(path))


def _vscode_global_storage() -> Path | None:
    """Locate VS Code's globalStorage directory (OS-specific)."""
    system = platform.system()
    if system == "Darwin":
        base = _expand("~/Library/Application Support/Code/User/globalStorage")
    elif system == "Windows":
        appdata = os.environ.get("APPDATA", "")
        base = Path(appdata) / "Code" / "User" / "globalStorage" if appdata else None
        if base is None:
            return None
    else:  # Linux
        base = _expand("~/.config/Code/User/globalStorage")
    return base if base.is_dir() else None


def _goose_data_dir() -> Path:
    """Goose uses XDG data dir conventions via the `etcetera` crate."""
    system = platform.system()
    if system == "Darwin":
        return _expand("~/Library/Application Support/Block/goose")
    elif system == "Windows":
        local = os.environ.get("LOCALAPPDATA", "")
        return Path(local) / "Block" / "goose" if local else _expand("~/.local/share/goose")
    else:
        return _expand("~/.local/share/goose")


def _amazonq_data_dir() -> Path:
    """Amazon Q Developer CLI data directory."""
    system = platform.system()
    if system == "Darwin":
        return _expand("~/Library/Application Support/amazon-q")
    elif system == "Windows":
        local = os.environ.get("LOCALAPPDATA", "")
        return Path(local) / "amazon-q" if local else _expand("~/.local/share/amazon-q")
    else:
        return _expand("~/.local/share/amazon-q")


# ─── Detection registry ─────────────────────────────────────────────────────


class DetectedFramework:
    """A detected framework with its source path and adapter config."""

    def __init__(self, name: str, path: Path, adapter: str, ingestion_mode: str) -> None:
        self.name = name
        self.path = path
        self.adapter = adapter
        self.ingestion_mode = ingestion_mode

    def __repr__(self) -> str:
        return f"DetectedFramework({self.name!r}, path={self.path})"


def detect_frameworks(frameworks: list[str] | None = None) -> list[DetectedFramework]:
    """Scan well-known paths for installed AI coding agent frameworks.

    Args:
        frameworks: If provided, only detect these specific frameworks.
                   If None/empty, detect all known frameworks.

    Returns:
        List of detected frameworks with their paths and adapter configs.
    """
    all_detectors = {
        "claude": _detect_claude,
        "codex": _detect_codex,
        "continue": _detect_continue,
        "cline": _detect_cline,
        "goose": _detect_goose,
        "amazonq": _detect_amazonq,
        "aider": _detect_aider,
    }

    target = frameworks if frameworks else list(all_detectors.keys())
    detected: list[DetectedFramework] = []

    for name in target:
        detector = all_detectors.get(name)
        if detector is None:
            logger.debug("Unknown framework for detection: %s", name)
            continue
        try:
            result = detector()
            if result is not None:
                detected.append(result)
                logger.info("Detected %s at %s", result.name, result.path)
        except Exception as exc:
            logger.debug("Detection failed for %s: %s", name, exc)

    return detected


# ─── Individual detectors ────────────────────────────────────────────────────


def _detect_claude() -> DetectedFramework | None:
    path = _expand("~/.claude/projects")
    if path.is_dir():
        return DetectedFramework("claude", path, "claude", "file_watch")
    return None


def _detect_codex() -> DetectedFramework | None:
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        path = Path(codex_home) / "sessions"
    else:
        path = _expand("~/.codex/sessions")
    if path.is_dir():
        return DetectedFramework("codex", path, "codex", "file_watch")
    return None


def _detect_continue() -> DetectedFramework | None:
    custom = os.environ.get("CONTINUE_GLOBAL_DIR")
    if custom:
        path = Path(custom) / "sessions"
    else:
        path = _expand("~/.continue/sessions")
    if path.is_dir():
        return DetectedFramework("continue", path, "continue", "file_watch")
    return None


def _detect_cline() -> DetectedFramework | None:
    gs = _vscode_global_storage()
    if gs is None:
        return None
    path = gs / "saoudrizwan.claude-dev" / "tasks"
    if path.is_dir():
        return DetectedFramework("cline", path, "cline", "file_watch")
    return None


def _detect_goose() -> DetectedFramework | None:
    path = _goose_data_dir() / "sessions"
    if path.is_dir():
        return DetectedFramework("goose", path, "goose", "poll")
    return None


def _detect_amazonq() -> DetectedFramework | None:
    path = _amazonq_data_dir() / "data.sqlite3"
    if path.is_file():
        return DetectedFramework("amazonq", path, "amazonq", "sqlite")
    return None


def _detect_aider() -> DetectedFramework | None:
    # Aider writes .aider.chat.history.md in the project root
    # We check CWD since aider is project-local
    path = Path.cwd() / ".aider.chat.history.md"
    if path.is_file():
        return DetectedFramework("aider", path, "aider_markdown", "file_watch")
    return None
