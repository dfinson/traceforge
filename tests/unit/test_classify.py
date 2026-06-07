"""Tests for tracemill.classify — tool normalization, classification, shell command parsing."""

from tracemill.classify import (
    _extract_commands_from_ast,
    classify_shell,
    classify_tool,
    normalize_tool_name,
)
from tracemill.classify.core import Classification
from tracemill.classify.rules import (
    SHELL_GIT_OPS,
    SHELL_IMPLEMENTATION,
    SHELL_INVESTIGATION,
    SHELL_SETUP,
    SHELL_VERIFICATION,
    activity_from_classification,
)


def _activity(cmd: str):
    """Helper: classify shell command and derive its ShellActivity."""
    return activity_from_classification(classify_shell(cmd))


# =============================================================================
# normalize_tool_name
# =============================================================================


class TestNormalizeToolName:
    """Tests for tool name normalization."""

    def test_empty_string(self):
        assert normalize_tool_name("") == ""

    def test_direct_canonical(self):
        assert normalize_tool_name("bash") == "bash"
        assert normalize_tool_name("edit") == "edit"
        assert normalize_tool_name("grep") == "grep"

    def test_alias_resolution(self):
        assert normalize_tool_name("powershell") == "bash"
        assert normalize_tool_name("read_file") == "view"
        assert normalize_tool_name("str_replace_editor") == "edit"
        assert normalize_tool_name("execute_command") == "bash"

    def test_case_insensitive(self):
        assert normalize_tool_name("Bash") == "bash"
        assert normalize_tool_name("GREP") == "grep"
        assert normalize_tool_name("PowerShell") == "bash"

    def test_hyphen_normalization(self):
        assert normalize_tool_name("exec-command") == "bash"
        assert normalize_tool_name("read-file") == "view"

    def test_mcp_double_underscore_prefix(self):
        assert normalize_tool_name("mcp__server__bash") == "bash"
        assert normalize_tool_name("mcp__filesystem__read_file") == "view"
        assert normalize_tool_name("mcp__myserver__grep") == "grep"

    def test_mcp_prefix_unknown_tool(self):
        # Unknown tool after stripping → lowered form
        assert normalize_tool_name("mcp__server__custom_tool") == "custom_tool"

    def test_namespace_dot_prefix(self):
        assert normalize_tool_name("functions.bash") == "bash"
        assert normalize_tool_name("tools.grep") == "grep"

    def test_dot_prefix_preserves_non_namespace(self):
        # Not stripped if prefix has uppercase or numbers
        assert normalize_tool_name("MyModule.tool") == "mymodule.tool"

    def test_unknown_tool_returns_lowered(self):
        assert normalize_tool_name("super_custom_tool") == "super_custom_tool"
        assert normalize_tool_name("MyWeirdTool") == "myweirdtool"

    def test_whitespace_stripping(self):
        assert normalize_tool_name("  bash  ") == "bash"


# =============================================================================
# classify_tool
# =============================================================================


class TestClassifyTool:
    """Tests for tool classification."""

    def test_empty_name(self):
        c = classify_tool("")
        assert isinstance(c, Classification)
        assert c.mechanism == "communication"

    def test_known_tools(self):
        assert classify_tool("bash").mechanism == "shell"
        assert classify_tool("edit").mechanism == "file.write"
        assert classify_tool("create").mechanism == "file.write"
        assert classify_tool("view").mechanism == "file.read"
        assert classify_tool("grep").mechanism == "file.read"
        assert classify_tool("glob").mechanism == "file.read"
        assert classify_tool("git_commit").mechanism == "shell"
        assert classify_tool("git_commit").has_role("orchestrator.version_control")
        assert classify_tool("report_intent").mechanism == "communication.system"
        assert classify_tool("ask_user").mechanism == "communication.user"

    def test_alias_classified(self):
        assert classify_tool("powershell").mechanism == "shell"
        assert classify_tool("read_file").mechanism == "file.read"
        assert classify_tool("str_replace_editor").mechanism == "file.write"
        assert classify_tool("greptool").mechanism == "file.read"

    def test_mcp_prefixed_classified(self):
        assert classify_tool("mcp__server__bash").mechanism == "shell"
        assert classify_tool("mcp__fs__edit").mechanism == "file.write"

    def test_unknown_tool(self):
        c = classify_tool("unknowntool")
        assert c.mechanism == "communication"
        assert c.effect is None

    def test_custom_overrides_default(self):
        custom_cls = Classification(mechanism="custom.shell", effect="mutating")
        custom = {"bash": custom_cls}
        assert classify_tool("bash", custom) is custom_cls

    def test_custom_extends_default(self):
        custom_cls = Classification(mechanism="custom.special", effect="read_only")
        custom = {"my_tool": custom_cls}
        assert classify_tool("my_tool", custom) is custom_cls
        # Default still works
        assert classify_tool("bash", custom).mechanism == "shell"

    def test_custom_on_canonical_name(self):
        custom_cls = Classification(mechanism="custom.shell", effect="mutating")
        custom = {"bash": custom_cls}
        # "exec_command" normalizes to "bash", custom has "bash" → custom_cls
        assert classify_tool("exec_command", custom) is custom_cls


# =============================================================================
# classify_shell
# =============================================================================


class TestClassifyShellCommand:
    """Tests for shell command classification."""

    def test_empty_command(self):
        assert _activity("") == SHELL_IMPLEMENTATION

    def test_simple_test_runners(self):
        assert _activity("pytest") == SHELL_VERIFICATION
        assert _activity("pytest tests/") == SHELL_VERIFICATION
        assert _activity("python -m pytest") == SHELL_VERIFICATION
        assert _activity("jest --coverage") == SHELL_VERIFICATION
        assert _activity("vitest run") == SHELL_VERIFICATION
        assert _activity("rspec spec/") == SHELL_VERIFICATION
        assert _activity("cargo test") == SHELL_VERIFICATION
        assert _activity("go test ./...") == SHELL_VERIFICATION
        assert _activity("npm test") == SHELL_VERIFICATION

    def test_linters(self):
        assert _activity("ruff check .") == SHELL_VERIFICATION
        assert _activity("ruff format --check .") == SHELL_VERIFICATION
        assert _activity("mypy src/") == SHELL_VERIFICATION
        assert _activity("eslint src/") == SHELL_VERIFICATION
        assert _activity("tsc --noEmit") == SHELL_VERIFICATION
        assert _activity("black --check .") == SHELL_VERIFICATION

    def test_build_commands(self):
        assert _activity("npm run build") == SHELL_VERIFICATION
        assert _activity("cargo build") == SHELL_VERIFICATION
        assert _activity("go build ./cmd/app") == SHELL_VERIFICATION

    def test_fix_flag_excludes_from_verification(self):
        # With --fix it's implementation, not verification
        assert _activity("ruff check . --fix") == SHELL_IMPLEMENTATION
        assert _activity("eslint src/ --fix") == SHELL_IMPLEMENTATION
        assert _activity("prettier --write .") == SHELL_IMPLEMENTATION

    def test_ruff_format_without_check_is_implementation(self):
        # ruff format (without --check) is reformatting = implementation
        assert _activity("ruff format .") == SHELL_IMPLEMENTATION

    def test_setup_commands(self):
        assert _activity("pip install -e '.[dev]'") == SHELL_SETUP
        assert _activity("npm install") == SHELL_SETUP
        assert _activity("yarn install") == SHELL_SETUP
        assert _activity("pnpm install") == SHELL_SETUP
        assert _activity("uv pip install requests") == SHELL_SETUP

    def test_setup_beats_verification(self):
        # The critical false-positive case: pip install pytest = SETUP not verification
        assert _activity("pip install pytest") == SHELL_SETUP
        assert _activity("pip install pytest ruff mypy") == SHELL_SETUP

    def test_git_write_operations(self):
        assert _activity("git commit -m 'fix'") == SHELL_GIT_OPS
        assert _activity("git push origin main") == SHELL_GIT_OPS
        assert _activity("git merge feature") == SHELL_GIT_OPS

    def test_git_read_operations(self):
        assert _activity("git diff") == SHELL_INVESTIGATION
        assert _activity("git log --oneline") == SHELL_INVESTIGATION
        assert _activity("git status") == SHELL_INVESTIGATION

    def test_implementation_commands(self):
        assert _activity("python main.py") == SHELL_IMPLEMENTATION
        assert _activity("node server.js") == SHELL_IMPLEMENTATION
        assert _activity("echo hello") == SHELL_IMPLEMENTATION
        assert _activity("ls -la") == SHELL_IMPLEMENTATION
        assert _activity("cat file.txt") == SHELL_IMPLEMENTATION

    def test_compound_highest_priority(self):
        # verification > git > setup > investigation > implementation
        assert _activity("cd src && pytest") == SHELL_VERIFICATION
        assert _activity("npm install && npm test") == SHELL_VERIFICATION
        assert _activity("echo 'starting' && git commit -m hi") == SHELL_GIT_OPS

    def test_sudo_stripping(self):
        assert _activity("sudo pip install flask") == SHELL_SETUP
        assert _activity("sudo pytest") == SHELL_VERIFICATION

    def test_env_var_stripping(self):
        assert _activity("CI=1 pytest --ci") == SHELL_VERIFICATION

    def test_cd_prefix_stripping(self):
        assert _activity("cd /app && pytest") == SHELL_VERIFICATION


# =============================================================================
# Edge cases & integration
# =============================================================================


class TestEdgeCases:
    """Edge case tests combining normalization and classification."""

    def test_mcp_prefixed_shell_classified(self):
        """MCP-prefixed shell tool still classified as shell."""
        assert classify_tool("mcp__terminal__bash").mechanism == "shell"

    def test_namespace_prefixed_search_classified(self):
        """Namespace-prefixed grep still classified correctly."""
        assert classify_tool("tools.grep").mechanism == "file.read"

    def test_case_insensitive_custom(self):
        """Custom map works case-insensitively."""
        custom_cls = Classification(mechanism="custom.special", effect=None)
        custom = {"MyTool": custom_cls}
        assert classify_tool("mytool", custom) is custom_cls

    def test_classify_tool_with_none_custom(self):
        """None custom_classifications doesn't crash."""
        assert classify_tool("bash", None).mechanism == "shell"

    def test_all_git_tools_classified(self):
        """All git_* variants have version_control role."""
        git_tools = [
            "git_commit",
            "git_push",
            "git_diff",
            "git_status",
            "git_add",
            "git_log",
            "git_pull",
            "git_merge",
        ]
        for tool in git_tools:
            assert classify_tool(tool).has_role("orchestrator.version_control"), f"{tool} should have version_control role"


# =============================================================================
# tree-sitter AST decomposition (_extract_commands_from_ast)
# =============================================================================


class TestASTDecomposition:
    """Tests for tree-sitter-bash AST command extraction."""

    def test_simple_command(self):
        result = _extract_commands_from_ast("echo hello")
        assert result == ["echo hello"]

    def test_split_on_and(self):
        result = _extract_commands_from_ast("cd src && pytest")
        assert "cd src" in result
        assert "pytest" in result
        assert len(result) == 2

    def test_split_on_or(self):
        result = _extract_commands_from_ast("test -f x || exit 1")
        assert len(result) == 2

    def test_split_on_semicolon(self):
        result = _extract_commands_from_ast("echo a; echo b")
        assert len(result) == 2

    def test_split_on_pipe(self):
        result = _extract_commands_from_ast("cat file | grep foo")
        assert len(result) == 2

    def test_quoted_double_and_not_split(self):
        """Double-quoted && must NOT cause a split."""
        result = _extract_commands_from_ast('echo "a && b"')
        assert len(result) == 1
        assert "echo" in result[0]

    def test_quoted_single_and_not_split(self):
        """Single-quoted && must NOT cause a split."""
        result = _extract_commands_from_ast("echo 'a && b'")
        assert len(result) == 1

    def test_mixed_quoted_and_real_operator(self):
        """Operators inside quotes preserved, real operator splits."""
        result = _extract_commands_from_ast('echo "a && b" && pytest')
        assert len(result) == 2
        # First command has the echo with quoted content
        assert "echo" in result[0]
        assert result[1] == "pytest"

    def test_empty_string(self):
        assert _extract_commands_from_ast("") == []

    def test_multiple_operators(self):
        result = _extract_commands_from_ast("a && b || c; d")
        assert len(result) == 4

    def test_subshell_not_extracted(self):
        """Commands inside $(...) should NOT be extracted as top-level commands."""
        result = _extract_commands_from_ast("echo $(pytest)")
        # Only 'echo $(pytest)' as a single command — the inner pytest is a subshell
        assert len(result) == 1
        assert "echo" in result[0]

    def test_pipeline_extracts_all(self):
        """Each command in a pipeline is extracted."""
        result = _extract_commands_from_ast("pytest | tee log.txt | grep PASS")
        assert len(result) == 3

    def test_nested_quotes(self):
        """Double quotes inside single quotes are inert."""
        result = _extract_commands_from_ast("""echo '"hello"' && pytest""")
        assert len(result) == 2


# =============================================================================
# Shell classification with quote-aware splitting (integration)
# =============================================================================


class TestShellClassificationQuoteAware:
    """Tests that prove the quote-aware parser prevents misclassification."""

    def test_quoted_pytest_not_verification(self):
        """echo 'run pytest' is implementation, not verification."""
        assert _activity("echo 'run pytest'") == SHELL_IMPLEMENTATION

    def test_quoted_and_operator_with_real_test(self):
        """Only the real (unquoted) pytest triggers verification."""
        assert _activity('echo "a && b" && pytest') == SHELL_VERIFICATION

    def test_echo_with_quoted_git_not_git_ops(self):
        """git inside quotes is not a real git command."""
        assert _activity("echo 'git push origin main'") == SHELL_IMPLEMENTATION

    def test_piped_grep_after_test(self):
        """pytest | tee log.txt — pytest is the meaningful command."""
        assert _activity("pytest | tee log.txt") == SHELL_VERIFICATION

    def test_wrapper_unwrapping(self):
        """sudo/env/nohup transparently unwrap to the real command."""
        assert _activity("sudo pytest") == SHELL_VERIFICATION
        assert _activity("nohup git push &") == SHELL_GIT_OPS
