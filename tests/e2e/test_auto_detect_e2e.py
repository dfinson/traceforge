"""End-to-end tests for framework auto-detection (issue #81, Wave 2).

``detect_frameworks`` scans OS-specific home / app-data locations and honored
environment variables to discover installed agent frameworks. These tests build
fake install trees under the sandboxed ``$HOME`` provided by
``tmp_traceforge_home`` (which also scrubs ``CODEX_HOME`` /
``CONTINUE_GLOBAL_DIR`` and redirects ``APPDATA`` / ``LOCALAPPDATA``), then
assert each framework is discovered with the correct path, adapter, and
ingestion mode — plus the env-var overrides, the framework filter, and the
none-installed empty case.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path

import pytest

from traceforge.sources.auto_detect import detect_frameworks

pytestmark = pytest.mark.e2e


def _framework_layout(home: Path) -> dict[str, tuple[Path, bool, str, str]]:
    """Map framework name → (path_to_create, is_file, adapter, ingestion_mode).

    Mirrors the OS-specific lookups in ``auto_detect`` so the fake install tree
    lands exactly where the detector looks on the running platform. The app-data
    roots resolve under ``home`` because ``tmp_traceforge_home`` points
    ``APPDATA``/``LOCALAPPDATA`` there on Windows and ``$HOME`` there on POSIX.
    """
    system = platform.system()
    if system == "Darwin":
        app_support = home / "Library" / "Application Support"
        global_storage = app_support / "Code" / "User" / "globalStorage"
        goose_dir = app_support / "Block" / "goose"
        amazonq_dir = app_support / "amazon-q"
    elif system == "Windows":
        appdata = Path(os.environ["APPDATA"])
        localappdata = Path(os.environ["LOCALAPPDATA"])
        global_storage = appdata / "Code" / "User" / "globalStorage"
        goose_dir = localappdata / "Block" / "goose"
        amazonq_dir = localappdata / "amazon-q"
    else:  # Linux / other POSIX
        global_storage = home / ".config" / "Code" / "User" / "globalStorage"
        goose_dir = home / ".local" / "share" / "goose"
        amazonq_dir = home / ".local" / "share" / "amazon-q"

    return {
        "claude": (home / ".claude" / "projects", False, "claude", "file_watch"),
        "codex": (home / ".codex" / "sessions", False, "codex", "file_watch"),
        "continue": (home / ".continue" / "sessions", False, "continue", "file_watch"),
        "cline": (
            global_storage / "saoudrizwan.claude-dev" / "tasks",
            False,
            "cline",
            "file_watch",
        ),
        "goose": (goose_dir / "sessions", False, "goose", "poll"),
        "amazonq": (amazonq_dir / "data.sqlite3", True, "amazonq", "sqlite"),
        # OpenCode uses the same ~/.local/share layout on every platform (on
        # Windows this resolves under %USERPROFILE%, since tmp_traceforge_home
        # redirects HOME/USERPROFILE here), so it is not OS-branched above.
        "opencode": (
            home / ".local" / "share" / "opencode" / "opencode.db",
            True,
            "opencode",
            "sqlite",
        ),
    }


def _install(path: Path, is_file: bool) -> None:
    if is_file:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
    else:
        path.mkdir(parents=True, exist_ok=True)


def _same_path(a: Path, b: Path) -> bool:
    return os.path.normcase(str(a)) == os.path.normcase(str(b))


def test_none_installed_returns_empty(
    tmp_traceforge_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Detection also probes the CWD for aider; run from a clean empty dir so a
    # stray .aider.chat.history.md in the repo can't leak into the result.
    monkeypatch.chdir(tmp_path)
    assert detect_frameworks() == []


def test_each_framework_detected_with_path_adapter_mode(
    tmp_traceforge_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # keep aider (CWD-based) out of this home-based scan
    layout = _framework_layout(tmp_traceforge_home)
    for path, is_file, _adapter, _mode in layout.values():
        _install(path, is_file)

    detected = {d.name: d for d in detect_frameworks()}

    assert set(detected) == set(layout)
    for name, (path, _is_file, adapter, mode) in layout.items():
        found = detected[name]
        assert _same_path(found.path, path), f"{name}: {found.path} != {path}"
        assert found.adapter == adapter
        assert found.ingestion_mode == mode


def test_codex_home_env_var_is_honored(
    tmp_traceforge_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom = tmp_traceforge_home / "xdg-codex"
    (custom / "sessions").mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(custom))

    detected = detect_frameworks(["codex"])
    assert len(detected) == 1
    assert _same_path(detected[0].path, custom / "sessions")


def test_continue_global_dir_env_var_is_honored(
    tmp_traceforge_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom = tmp_traceforge_home / "xdg-continue"
    (custom / "sessions").mkdir(parents=True)
    monkeypatch.setenv("CONTINUE_GLOBAL_DIR", str(custom))

    detected = detect_frameworks(["continue"])
    assert len(detected) == 1
    assert _same_path(detected[0].path, custom / "sessions")


def test_framework_filter_limits_detection(tmp_traceforge_home: Path) -> None:
    layout = _framework_layout(tmp_traceforge_home)
    for path, is_file, _adapter, _mode in layout.values():
        _install(path, is_file)

    detected = detect_frameworks(["claude", "goose"])
    assert sorted(d.name for d in detected) == ["claude", "goose"]


def test_unknown_framework_in_filter_is_ignored(tmp_traceforge_home: Path) -> None:
    layout = _framework_layout(tmp_traceforge_home)
    path, is_file, _adapter, _mode = layout["claude"]
    _install(path, is_file)

    detected = detect_frameworks(["claude", "does-not-exist"])
    assert [d.name for d in detected] == ["claude"]


def test_aider_detected_from_cwd(
    tmp_traceforge_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    history = project / ".aider.chat.history.md"
    history.write_text("# aider chat history\n", encoding="utf-8")
    monkeypatch.chdir(project)

    detected = detect_frameworks(["aider"])
    assert len(detected) == 1
    assert detected[0].name == "aider"
    assert detected[0].adapter == "aider_markdown"
    assert detected[0].ingestion_mode == "file_watch"
    assert _same_path(detected[0].path, history)


def test_opencode_detected_at_verified_sqlite_path(tmp_traceforge_home: Path) -> None:
    """OpenCode is detected at its verified host path with the sqlite mode.

    The event-sourced store lives at ``~/.local/share/opencode/opencode.db`` on
    every platform (on Windows that expands under ``%USERPROFILE%``, not
    ``%LOCALAPPDATA%``). The opencode mapping + preprocessor already ship; this
    asserts the detector wires that existing ingestion support in.
    """
    db = tmp_traceforge_home / ".local" / "share" / "opencode" / "opencode.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"")

    detected = detect_frameworks(["opencode"])

    assert len(detected) == 1
    found = detected[0]
    assert found.name == "opencode"
    assert found.adapter == "opencode"
    assert found.ingestion_mode == "sqlite"
    assert _same_path(found.path, db)


def test_opencode_missing_store_is_clean_no_match(tmp_traceforge_home: Path) -> None:
    # No opencode.db on disk → the detector returns no match rather than
    # fabricating a detection for an absent store.
    assert detect_frameworks(["opencode"]) == []
