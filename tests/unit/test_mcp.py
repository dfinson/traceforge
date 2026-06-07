from functools import partial

"""Tests for MCP tool classification module."""

from tracemill.classify import get_default_engine
from tracemill.classify.mcp import (
    _infer_from_verb,
    _normalize_mcp_suffix,
    classify_mcp_tool,
    extract_mcp_namespace,
)
from tracemill.classify.tools import classify_tool


ENGINE = get_default_engine()
_infer_from_verb = partial(_infer_from_verb, engine=ENGINE)
classify_mcp_tool = partial(classify_mcp_tool, engine=ENGINE)
classify_tool = partial(classify_tool, engine=ENGINE)


class TestExtractMcpNamespace:
    def test_standard_mcp_format(self):
        assert extract_mcp_namespace("mcp__github__list_repos") == "github"

    def test_case_insensitive(self):
        assert extract_mcp_namespace("mcp__GitHub__list_repos") == "github"

    def test_non_mcp_name(self):
        assert extract_mcp_namespace("bash") == ""

    def test_incomplete_mcp_prefix(self):
        assert extract_mcp_namespace("mcp__github") == ""

    def test_empty_string(self):
        assert extract_mcp_namespace("") == ""

    def test_whitespace_trimmed(self):
        assert extract_mcp_namespace("  mcp__server__tool  ") == "server"


class TestNormalizeMcpSuffix:
    def test_mcp_format(self):
        assert _normalize_mcp_suffix("mcp__github__list-repos") == "list_repos"

    def test_non_mcp(self):
        assert _normalize_mcp_suffix("List-Repos") == "list_repos"

    def test_underscores_preserved(self):
        assert _normalize_mcp_suffix("mcp__gh__get_file_contents") == "get_file_contents"


class TestVerbInference:
    """Verb → (effect, action) inference."""

    def test_read_verbs(self):
        for verb in ("get", "list", "read", "search", "query", "fetch", "browse", "find", "show", "view"):
            effect, action = _infer_from_verb(f"{verb}_something")
            assert effect == "read_only", f"Expected read_only for {verb}"
            assert action is not None, f"Expected action for {verb}"

    def test_mutating_verbs(self):
        for verb in ("create", "update", "write", "set", "add", "apply", "run", "execute", "push", "deploy"):
            effect, action = _infer_from_verb(f"{verb}_something")
            assert effect == "mutating", f"Expected mutating for {verb}"

    def test_destructive_verbs(self):
        for verb in ("delete", "destroy", "drop", "remove", "clear", "terminate"):
            effect, action = _infer_from_verb(f"{verb}_something")
            assert effect == "destructive", f"Expected destructive for {verb}"

    def test_exact_match(self):
        """Verb by itself (no suffix) should match."""
        effect, action = _infer_from_verb("get")
        assert effect == "read_only"

    def test_no_match(self):
        effect, action = _infer_from_verb("unknownverb_something")
        assert effect is None
        assert action is None

    def test_action_for_retrieve_verbs(self):
        _, action = _infer_from_verb("get_items")
        assert action == "retrieve"

    def test_action_for_create(self):
        _, action = _infer_from_verb("create_item")
        assert action == "generate"

    def test_action_for_delete(self):
        _, action = _infer_from_verb("delete_item")
        assert action == "remove"

    def test_action_for_deploy(self):
        _, action = _infer_from_verb("deploy_service")
        assert action == "deliver"

    def test_action_for_check(self):
        _, action = _infer_from_verb("check_status")
        assert action == "validate"

    def test_action_for_merge(self):
        _, action = _infer_from_verb("merge_pull_request")
        assert action == "modify"


class TestMcpProfileMatching:
    """Profile matching via namespace aliases."""

    # ── GitHub ──

    def test_github_list_repos(self):
        cls = classify_mcp_tool("mcp__github__list_repos")
        assert cls is not None
        assert cls.mechanism == "network.http"
        assert cls.has_role("persistence.version_control")
        assert cls.effect == "read_only"

    def test_github_create_issue(self):
        cls = classify_mcp_tool("mcp__github__create_issue")
        assert cls is not None
        assert cls.mechanism == "network.http"
        assert cls.effect == "mutating"

    def test_github_delete_file(self):
        cls = classify_mcp_tool("mcp__github__delete_file")
        assert cls is not None
        assert cls.effect == "destructive"

    def test_github_actions_ci_cd_scope(self):
        cls = classify_mcp_tool("mcp__github__actions_list")
        assert cls is not None
        assert cls.has_role("orchestrator.ci_cd")

    def test_github_copilot_delegation(self):
        cls = classify_mcp_tool("mcp__github__assign_copilot_to_issue")
        assert cls is not None
        assert cls.mechanism == "delegation.agent"

    # ── GitLab ──

    def test_gitlab_tool(self):
        cls = classify_mcp_tool("mcp__gitlab__create_merge_request")
        assert cls is not None
        assert cls.mechanism == "network.http"
        assert cls.has_role("persistence.version_control")

    # ── Git (local) ──

    def test_git_local(self):
        cls = classify_mcp_tool("mcp__git__git_commit")
        assert cls is not None
        assert cls.mechanism == "process.shell"
        assert cls.has_role("persistence.version_control")

    # ── Filesystem ──

    def test_filesystem_read(self):
        cls = classify_mcp_tool("mcp__filesystem__read_file")
        assert cls is not None
        assert cls.mechanism == "filesystem"
        assert cls.effect == "read_only"

    def test_filesystem_write(self):
        cls = classify_mcp_tool("mcp__filesystem__write_file")
        assert cls is not None
        assert cls.mechanism == "filesystem"
        assert cls.effect == "mutating"
        assert cls.has_role("modifier.file_editor")

    def test_fs_alias(self):
        cls = classify_mcp_tool("mcp__fs__list_directory")
        assert cls is not None
        assert cls.mechanism == "filesystem"

    # ── Database ──

    def test_postgres(self):
        cls = classify_mcp_tool("mcp__postgres__query")
        assert cls is not None
        assert cls.mechanism == "database.sql"
        assert cls.effect == "read_only"

    def test_sqlite_write(self):
        cls = classify_mcp_tool("mcp__sqlite__write_query")
        assert cls is not None
        assert cls.mechanism == "database.sql"
        assert cls.effect == "mutating"

    def test_mongodb(self):
        cls = classify_mcp_tool("mcp__mongo__find")
        assert cls is not None
        assert cls.mechanism == "database.nosql"

    def test_mongodb_drop(self):
        cls = classify_mcp_tool("mcp__mongodb__drop_collection")
        assert cls is not None
        assert cls.effect == "destructive"

    def test_redis(self):
        cls = classify_mcp_tool("mcp__redis__get_key")
        assert cls is not None
        assert cls.mechanism == "database.nosql"
        assert cls.has_role("persistence.cache")

    def test_generic_database(self):
        cls = classify_mcp_tool("mcp__database__query")
        assert cls is not None
        assert cls.mechanism.startswith("database")

    # ── Browser / Web ──

    def test_playwright_snapshot(self):
        cls = classify_mcp_tool("mcp__playwright__browser_snapshot")
        assert cls is not None
        assert cls.effect == "read_only"

    def test_playwright_navigate(self):
        cls = classify_mcp_tool("mcp__browser__browser_navigate")
        assert cls is not None
        assert cls.mechanism == "network.http"

    def test_playwright_destructive(self):
        cls = classify_mcp_tool("mcp__playwright__browser_cookie_clear")
        assert cls is not None
        assert cls.effect == "destructive"

    # ── Search ──

    def test_brave_search(self):
        cls = classify_mcp_tool("mcp__brave__brave_web_search")
        assert cls is not None
        assert cls.mechanism == "network.http"
        assert cls.effect == "read_only"
        assert cls.has_role("retriever.search_index")

    def test_exa_search(self):
        cls = classify_mcp_tool("mcp__exa__search")
        assert cls is not None
        assert cls.effect == "read_only"

    def test_tavily_search(self):
        cls = classify_mcp_tool("mcp__tavily__tavily_search")
        assert cls is not None
        assert cls.effect == "read_only"

    # ── Communication ──

    def test_slack(self):
        cls = classify_mcp_tool("mcp__slack__slack_post_message")
        assert cls is not None
        assert cls.mechanism == "network.http"
        # Verb inference doesn't match "slack_post_message" (starts with "slack_", not "post_")
        # Effect comes from profile default (None) since no verb match

    def test_slack_post_direct(self):
        """When tool name starts with verb directly."""
        cls = classify_mcp_tool("mcp__slack__post_message")
        assert cls is not None
        assert cls.effect == "mutating"

    def test_slack_read(self):
        cls = classify_mcp_tool("mcp__slack__list_channels")
        assert cls is not None
        assert cls.effect == "read_only"

    # ── Cloud providers ──

    def test_aws(self):
        cls = classify_mcp_tool("mcp__aws__describe_instances")
        assert cls is not None
        assert cls.mechanism == "network.http"
        assert "uses_credentials" in cls.capability

    def test_azure(self):
        cls = classify_mcp_tool("mcp__azure__azure_storage_list_containers")
        assert cls is not None
        assert cls.mechanism == "network.http"

    def test_gcp(self):
        cls = classify_mcp_tool("mcp__gcp__gcloud_list_projects")
        assert cls is not None
        assert cls.mechanism == "network.http"

    # ── Containers ──

    def test_docker_ps(self):
        cls = classify_mcp_tool("mcp__docker__docker_ps")
        assert cls is not None
        assert cls.mechanism == "process"
        assert cls.has_role("executor.container_runtime")

    def test_docker_rm(self):
        cls = classify_mcp_tool("mcp__docker__docker_rm")
        assert cls is not None
        assert cls.effect == "destructive"

    def test_kubernetes_pods(self):
        cls = classify_mcp_tool("mcp__kubernetes__get_pods")
        assert cls is not None
        assert cls.mechanism == "network.http"
        assert cls.effect == "read_only"

    def test_kubernetes_delete(self):
        cls = classify_mcp_tool("mcp__k8s__delete_pod")
        assert cls is not None
        assert cls.effect == "destructive"

    # ── Documentation ──

    def test_notion(self):
        cls = classify_mcp_tool("mcp__notion__notion_search")
        assert cls is not None
        assert cls.effect == "read_only"

    def test_notion_delete(self):
        cls = classify_mcp_tool("mcp__notion__notion_delete_block")
        assert cls is not None
        assert cls.effect == "destructive"

    # ── CI/CD ──

    def test_circleci(self):
        cls = classify_mcp_tool("mcp__circleci__trigger_pipeline")
        assert cls is not None
        assert cls.has_role("orchestrator.ci_cd")
        assert cls.effect == "mutating"

    # ── Package registries ──

    def test_npm(self):
        cls = classify_mcp_tool("mcp__npm__npm_search")
        assert cls is not None
        assert cls.effect == "read_only"

    def test_pypi(self):
        cls = classify_mcp_tool("mcp__pypi__pypi_get_package")
        assert cls is not None
        assert cls.effect == "read_only"

    # ── Observability ──

    def test_sentry(self):
        cls = classify_mcp_tool("mcp__sentry__get_sentry_issue")
        assert cls is not None
        assert cls.effect == "read_only"

    def test_datadog(self):
        cls = classify_mcp_tool("mcp__datadog__get_monitors")
        assert cls is not None
        assert cls.effect == "read_only"

    # ── Knowledge / Memory ──

    def test_memory_read(self):
        cls = classify_mcp_tool("mcp__memory__read_graph")
        assert cls is not None
        assert cls.mechanism == "database"

    def test_memory_delete(self):
        cls = classify_mcp_tool("mcp__memory__delete_entities")
        assert cls is not None
        assert cls.effect == "destructive"

    # ── Code analysis ──

    def test_semgrep(self):
        cls = classify_mcp_tool("mcp__semgrep__semgrep_scan")
        assert cls is not None
        assert cls.effect == "read_only"
        assert cls.has_role("validator.security_scanner")

    # ── Time ──

    def test_time(self):
        cls = classify_mcp_tool("mcp__time__get_current_time")
        assert cls is not None
        assert cls.effect == "read_only"


class TestMcpNoCollisions:
    """MCP classification must not collide with first-party canonical tools."""

    def test_mcp_search_not_grep(self):
        """mcp__github__search should not become grep → filesystem."""
        cls = classify_tool("mcp__github__search_code")
        assert cls.mechanism == "network.http"
        # Without MCP module, this would normalize to grep → filesystem

    def test_mcp_create_not_filesystem_create(self):
        """mcp__github__create_repository should stay network, not filesystem create."""
        cls = classify_tool("mcp__github__create_repository")
        assert cls.mechanism == "network.http"

    def test_git_commit_not_hijacked(self):
        """First-party git_commit should still work via canonical classification."""
        cls = classify_tool("git_commit")
        assert cls.mechanism == "process.shell"
        assert cls.effect == "mutating"
        assert cls.has_action("persist.commit")

    def test_bash_via_unknown_server(self):
        """mcp__server__bash should normalize to shell via canonical lookup."""
        cls = classify_tool("mcp__server__bash")
        assert cls.mechanism == "process.shell"

    def test_first_party_view_unchanged(self):
        """First-party 'view' tool classification unchanged."""
        cls = classify_tool("view")
        assert cls.mechanism == "filesystem"
        assert cls.effect == "read_only"

    def test_first_party_edit_unchanged(self):
        cls = classify_tool("edit")
        assert cls.mechanism == "filesystem"
        assert cls.effect == "mutating"

    def test_mcp_filesystem_edit_separate(self):
        """MCP filesystem edit is classified via profile, not canonical."""
        cls = classify_tool("mcp__filesystem__edit_file")
        assert cls.mechanism == "filesystem"
        assert cls.effect == "mutating"
        assert cls.has_role("modifier.file_editor")


class TestMcpUnknownNamespace:
    """MCP tools with unknown namespaces fall through correctly."""

    def test_unknown_namespace_known_suffix(self):
        """mcp__randomserver__bash → canonical 'shell' → process.shell."""
        cls = classify_tool("mcp__randomserver__bash")
        assert cls.mechanism == "process.shell"

    def test_unknown_namespace_unknown_suffix(self):
        """Completely unknown MCP tool → verb inference or UNKNOWN."""
        cls = classify_tool("mcp__randomserver__do_stuff")
        assert cls.mechanism == "unknown"

    def test_unknown_namespace_verb_inference(self):
        """MCP tool with unknown namespace but known verb → effect inferred."""
        cls = classify_tool("mcp__randomserver__get_items")
        assert cls.effect == "read_only"

    def test_unknown_namespace_delete_verb(self):
        cls = classify_tool("mcp__randomserver__delete_item")
        assert cls.effect == "destructive"


class TestMcpPhaseMap:
    """All MCP classifications carry phase_map."""

    def test_github_has_phase_map(self):
        cls = classify_mcp_tool("mcp__github__list_repos")
        assert cls is not None
        assert cls.phase_map
        assert len(cls.phase_map) == 1

    def test_filesystem_has_phase_map(self):
        cls = classify_mcp_tool("mcp__filesystem__read_file")
        assert cls is not None
        assert cls.phase_map

    def test_database_has_phase_map(self):
        cls = classify_mcp_tool("mcp__postgres__query")
        assert cls is not None
        assert cls.phase_map


class TestMcpCustomOverrides:
    """Custom user classifications override MCP profiles."""

    def test_custom_overrides_mcp(self):
        from tracemill.classify.core import Classification

        custom = {"list_repos": Classification(mechanism="custom.thing", effect="read_only")}
        cls = classify_tool("mcp__github__list_repos", custom)
        assert cls.mechanism == "custom.thing"


class TestMcpVerbInferenceUpgrade:
    """Verb inference should upgrade default_effect when verb implies higher risk."""

    def test_delete_upgrades_read_only_to_destructive(self):
        """A delete verb should override a read_only default_effect."""
        cls = classify_mcp_tool("mcp__postgres__delete_rows")
        assert cls is not None
        assert cls.effect in ("destructive", "mutating")

    def test_create_upgrades_read_only_to_mutating(self):
        cls = classify_mcp_tool("mcp__postgres__create_table")
        assert cls is not None
        assert cls.effect == "mutating"

    def test_explicit_override_takes_priority_over_verb(self):
        """Per-tool override should not be overridden by verb inference."""
        # coderecon's checkpoint has explicit override effect=mutating
        cls = classify_mcp_tool("mcp__coderecon__checkpoint")
        assert cls is not None
        assert cls.effect == "mutating"

    def test_verb_action_merges_with_profile_action(self):
        """Verb-inferred action should merge with (not replace) profile defaults."""
        cls = classify_mcp_tool("mcp__github__create_branch")
        assert cls is not None
        # Should have both the profile's default actions AND the verb-inferred action
        assert cls.action  # non-empty

    def test_filesystem_delete_gets_write_capability(self):
        """Filesystem tool with destructive verb should get filesystem_write."""
        cls = classify_mcp_tool("mcp__filesystem__delete_file")
        assert cls is not None
        assert cls.effect in ("destructive", "mutating")
        assert "filesystem_write" in cls.capability

    def test_filesystem_delete_upgrades_retriever_role(self):
        """Filesystem tool with destructive verb should not keep retriever role."""
        cls = classify_mcp_tool("mcp__filesystem__delete_file")
        assert cls is not None
        assert not any(r.startswith("retriever.") for r in cls.role)

    def test_read_verb_stays_read_only(self):
        """A read verb should not upgrade effect."""
        cls = classify_mcp_tool("mcp__filesystem__read_file")
        assert cls is not None
        assert cls.effect == "read_only"
