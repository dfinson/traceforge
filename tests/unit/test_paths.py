"""File-target normalization across host path dialects."""

from __future__ import annotations

from traceforge import Enricher, EventKind, normalize_file_target
from tests.conftest import make_event


def test_windows_absolute_path_is_root_relative_and_preserves_raw() -> None:
    raw = r"c:\Work\Repo\src\api.py"
    target = normalize_file_target(raw, r"C:\work\repo")

    assert target.raw_path == raw
    assert target.path == "src/api.py"
    assert target.inside_root is True


def test_windows_mixed_separators_and_drive_case_normalize() -> None:
    target = normalize_file_target(r"c:/Work\Repo/src\api.py", r"C:\Work\Repo")

    assert target.path == "src/api.py"
    assert target.inside_root is True


def test_windows_sibling_prefix_is_outside_root() -> None:
    target = normalize_file_target(r"C:\work\repository\file.py", r"C:\work\repo")

    assert target.path == "C:/work/repository/file.py"
    assert target.inside_root is False


def test_windows_different_drive_is_outside_root() -> None:
    target = normalize_file_target(r"D:\work\repo\file.py", r"C:\work\repo")

    assert target.path == "D:/work/repo/file.py"
    assert target.inside_root is False


def test_posix_inside_and_outside_root() -> None:
    inside = normalize_file_target("/srv/repo/src/api.py", "/srv/repo")
    outside = normalize_file_target("/srv/repository/api.py", "/srv/repo")

    assert (inside.path, inside.inside_root) == ("src/api.py", True)
    assert (outside.path, outside.inside_root) == ("/srv/repository/api.py", False)


def test_relative_escape_is_outside_root() -> None:
    target = normalize_file_target("../secret.txt", "/srv/repo")

    assert target.path == "/srv/secret.txt"
    assert target.inside_root is False


def test_posix_absolute_target_with_relative_root_is_outside() -> None:
    raw = "/srv/repo/src/api.py"
    event = make_event(
        kind=EventKind.TOOL_CALL_COMPLETED,
        payload={"tool_name": "edit", "arguments": {"path": raw}},
    )

    enriched = Enricher(workspace_root="repo").process(event)

    assert enriched is not None
    assert enriched.payload["arguments"]["path"] == raw
    assert enriched.metadata.file_targets[0].raw_path == raw
    assert enriched.metadata.file_targets[0].path == raw
    assert enriched.metadata.file_targets[0].inside_root is False


def test_enricher_stamps_normalized_target_without_rewriting_payload() -> None:
    raw = r"C:\Work\Repo\tests\test_api.py"
    event = make_event(
        kind=EventKind.TOOL_CALL_COMPLETED,
        payload={
            "tool_name": "edit",
            "arguments": {"path": raw},
        },
    )

    enriched = Enricher(workspace_root=r"c:\work\repo").process(event)

    assert enriched is not None
    assert enriched.payload["arguments"]["path"] == raw
    assert len(enriched.metadata.file_targets) == 1
    target = enriched.metadata.file_targets[0]
    assert target.raw_path == raw
    assert target.path == "tests/test_api.py"
    assert target.inside_root is True
    assert "artifact.test_code" in enriched.metadata.classification.scope
