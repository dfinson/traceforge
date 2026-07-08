"""Deterministic tests for the protected-path glob primitive (U10).

Covers the general matcher (``normalize_path``, ``path_matches_glob``,
``first_matching_glob``, ``extract_candidate_paths``) and the built-in
``ProtectedPathAssessor``. The consumer supplies the glob patterns and the
action; TraceForge supplies only the mechanism. With no patterns configured the
assessor never fires.
"""

from datetime import datetime, timezone

from traceforge.classify.core import Classification
from traceforge.governance.results import RecommendedAction
from traceforge.governance.rules import (
    ProtectedPathAssessor,
    extract_candidate_paths,
    first_matching_glob,
    normalize_path,
    path_matches_glob,
)
from traceforge.governance.types import EnrichmentContext, ToolCallEvent

NOW = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _event(tool_args_json: str) -> ToolCallEvent:
    return ToolCallEvent(
        event_id="e1",
        session_id="s1",
        timestamp=NOW,
        source_event_key="k1",
        span_id="sp1",
        tool_name="write_file",
        server_namespace=None,
        tool_args_json=tool_args_json,
        source_event_id=None,
    )


def _ctx(event: ToolCallEvent) -> EnrichmentContext:
    return EnrichmentContext(
        event=event,
        base_classification=Classification(mechanism="fs.write"),
        command_analysis=None,
        session_state=None,
        mcp_profiles=None,
        project_root=None,
        engine="mcp",
        drift_baseline=None,
        mcp_profile_key=None,
    )


# ─── normalize_path ──────────────────────────────────────────────────────────


class TestNormalizePath:
    def test_backslashes_become_slashes(self):
        assert normalize_path("a\\b\\c") == "a/b/c"

    def test_lowercased(self):
        assert normalize_path("Secrets/PROD.PEM") == "secrets/prod.pem"

    def test_stripped(self):
        assert normalize_path("  /x/y  ") == "/x/y"


# ─── path_matches_glob ───────────────────────────────────────────────────────


class TestPathMatchesGlob:
    def test_star_within_segment(self):
        assert path_matches_glob("dir/file.pem", "dir/*.pem") is True
        # * does not cross a separator
        assert path_matches_glob("dir/sub/file.pem", "dir/*.pem") is False

    def test_double_star_crosses_separators(self):
        assert path_matches_glob("a/b/c/file.pem", "a/**/file.pem") is True

    def test_leading_double_star_matches_any_depth(self):
        assert path_matches_glob("x/y/secrets/prod.key", "**/secrets/**") is True
        # ** as a leading directory allows zero dirs too
        assert path_matches_glob("secrets/prod.key", "**/secrets/**") is True

    def test_question_mark_single_char(self):
        assert path_matches_glob("a/fileX.txt", "a/file?.txt") is True
        assert path_matches_glob("a/fileXX.txt", "a/file?.txt") is False

    def test_slashless_pattern_matches_basename_anywhere(self):
        assert path_matches_glob("/repo/deep/.env", ".env") is True
        assert path_matches_glob("/repo/a/b/secret.pem", "*.pem") is True

    def test_case_insensitive(self):
        assert path_matches_glob("/repo/Secrets/Prod.PEM", "**/secrets/*.pem") is True

    def test_backslash_paths_normalized(self):
        assert path_matches_glob("C:\\repo\\secrets\\a.key", "**/secrets/**") is True

    def test_empty_inputs_return_false(self):
        assert path_matches_glob("", "*.pem") is False
        assert path_matches_glob("a.pem", "") is False


# ─── first_matching_glob ─────────────────────────────────────────────────────


class TestFirstMatchingGlob:
    def test_returns_first_pattern_in_order(self):
        patterns = ["*.txt", "*.pem", "*.key"]
        assert first_matching_glob(["a/b.pem"], patterns) == "*.pem"

    def test_none_when_no_match(self):
        assert first_matching_glob(["a/b.txt"], ["*.pem"]) is None

    def test_empty_paths(self):
        assert first_matching_glob([], ["*.pem"]) is None

    def test_empty_patterns(self):
        assert first_matching_glob(["a.pem"], []) is None


# ─── extract_candidate_paths ─────────────────────────────────────────────────


class TestExtractCandidatePaths:
    def test_scalar_path_keys(self):
        assert extract_candidate_paths('{"path": "/a/b.pem"}') == ("/a/b.pem",)

    def test_multiple_keys_collected_in_key_order(self):
        # 'path' precedes 'dest' in _PATH_ARG_KEYS
        args = '{"dest": "/d.txt", "path": "/p.txt"}'
        assert extract_candidate_paths(args) == ("/p.txt", "/d.txt")

    def test_list_valued_keys(self):
        assert extract_candidate_paths('{"paths": ["/a", "/b"]}') == ("/a", "/b")

    def test_dedupes_preserving_first_seen(self):
        args = '{"path": "/x", "src": "/x", "dest": "/y"}'
        assert extract_candidate_paths(args) == ("/x", "/y")

    def test_ignores_non_path_keys(self):
        assert extract_candidate_paths('{"command": "rm -rf /"}') == ()

    def test_malformed_json_returns_empty(self):
        assert extract_candidate_paths("not json") == ()
        assert extract_candidate_paths("") == ()

    def test_non_object_json_returns_empty(self):
        assert extract_candidate_paths("[1, 2, 3]") == ()

    def test_non_string_scalar_ignored(self):
        assert extract_candidate_paths('{"path": 123}') == ()


# ─── ProtectedPathAssessor ───────────────────────────────────────────────────


class TestProtectedPathAssessor:
    def test_no_patterns_never_fires(self):
        assessor = ProtectedPathAssessor()  # default: empty patterns
        ctx = _ctx(_event('{"path": "/repo/secrets/prod.pem"}'))
        assert assessor.assess(ctx, NOW) is None

    def test_escalates_on_match(self):
        assessor = ProtectedPathAssessor(patterns=("**/secrets/**",))
        ctx = _ctx(_event('{"path": "/repo/secrets/prod.pem"}'))
        decision = assessor.assess(ctx, NOW)
        assert decision is not None
        assert decision.action == RecommendedAction.ESCALATE
        assert decision.reason_code == "protected_path"

    def test_denies_when_configured(self):
        assessor = ProtectedPathAssessor(patterns=("*.pem",), action=RecommendedAction.DENY)
        ctx = _ctx(_event('{"path": "/repo/x/prod.pem"}'))
        decision = assessor.assess(ctx, NOW)
        assert decision is not None
        assert decision.action == RecommendedAction.DENY

    def test_no_match_returns_none(self):
        assessor = ProtectedPathAssessor(patterns=("**/secrets/**",))
        ctx = _ctx(_event('{"path": "/repo/src/main.py"}'))
        assert assessor.assess(ctx, NOW) is None

    def test_custom_reason_code(self):
        assessor = ProtectedPathAssessor(patterns=("*.key",), reason_code="sensitive_key")
        ctx = _ctx(_event('{"path": "/a/b.key"}'))
        decision = assessor.assess(ctx, NOW)
        assert decision is not None
        assert decision.reason_code == "sensitive_key"

    def test_non_toolcall_event_returns_none(self):
        # A plain SessionEvent (not a ToolCallEvent) carries no tool args.
        from traceforge.governance.types import SessionEvent

        assessor = ProtectedPathAssessor(patterns=("*.pem",))
        ev = SessionEvent(event_id="e", session_id="s", timestamp=NOW, source_event_key="k")
        ctx = EnrichmentContext(
            event=ev,
            base_classification=Classification(mechanism="fs.write"),
            command_analysis=None,
            session_state=None,
            mcp_profiles=None,
            project_root=None,
            engine="mcp",
            drift_baseline=None,
            mcp_profile_key=None,
        )
        assert assessor.assess(ctx, NOW) is None
