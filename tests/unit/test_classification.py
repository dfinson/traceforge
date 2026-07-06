"""Tests for the multi-dimensional classification system (core, coding, registry, detailed APIs)."""

from __future__ import annotations

from functools import partial

import pytest

from traceforge.classify.core import (
    Action,
    Classification,
    Effect,
    Mechanism,
    Role,
    Scope,
    aggregate_effect,
)
from traceforge.classify.coding import (
    CodingAction,
    CodingMechanism,
    CodingRole,
    CodingScope,
)
from traceforge.classify import get_default_engine
from traceforge.classify.registry import DimensionRegistry, get_default_registry
from traceforge.classify.shell import classify_shell
from traceforge.classify.tools import classify_tool


ENGINE = get_default_engine()
classify_shell = partial(classify_shell, engine=ENGINE)
classify_tool = partial(classify_tool, engine=ENGINE)


# ── Classification dataclass tests ──


class TestClassification:
    def test_to_dict_minimal(self):
        c = Classification(mechanism="filesystem", effect="read_only")
        d = c.to_dict()
        assert d == {"mechanism": "filesystem", "effect": "read_only"}

    def test_to_dict_full(self):
        c = Classification(
            mechanism="process.shell",
            effect="mutating",
            scope=frozenset({"artifact.source_code"}),
            role=frozenset({"validator.test_runner"}),
            action=frozenset({"validate.test"}),
            capability=frozenset({"subprocess", "filesystem_read"}),
            structure=frozenset({"piped", "sequential"}),
            shell_dialect="bash",
            binaries=("pytest", "pip"),
        )
        d = c.to_dict()
        assert d["mechanism"] == "process.shell"
        assert d["effect"] == "mutating"
        assert d["scope"] == ["artifact.source_code"]
        assert d["role"] == ["validator.test_runner"]
        assert d["action"] == ["validate.test"]
        assert sorted(d["capability"]) == ["filesystem_read", "subprocess"]
        assert sorted(d["structure"]) == ["piped", "sequential"]
        assert d["shell_dialect"] == "bash"
        assert d["binaries"] == ["pytest", "pip"]

    def test_from_dict_roundtrip(self):
        original = Classification(
            mechanism="shell",
            effect="read_only",
            scope=frozenset({"artifact.test_code"}),
            role=frozenset({"validator.test_runner"}),
            action=frozenset({"validate.test"}),
            capability=frozenset({"subprocess"}),
            structure=frozenset({"piped"}),
            shell_dialect="bash",
            binaries=("pytest",),
        )
        d = original.to_dict()
        restored = Classification.from_dict(d)
        assert restored.mechanism == original.mechanism
        assert restored.effect == original.effect
        assert restored.scope == original.scope
        assert restored.role == original.role
        assert restored.action == original.action
        assert restored.capability == original.capability
        assert restored.structure == original.structure
        assert restored.shell_dialect == original.shell_dialect
        assert restored.binaries == original.binaries

    def test_has_role_exact(self):
        c = Classification(
            mechanism="shell", effect="read_only", role=frozenset({"validator.linter"})
        )
        assert c.has_role("validator.linter")
        assert c.has_role("validator")
        assert not c.has_role("transformer")

    def test_has_action_hierarchy(self):
        c = Classification(
            mechanism="shell", effect="read_only", action=frozenset({"validate.test"})
        )
        assert c.has_action("validate.test")
        assert c.has_action("validate")
        assert not c.has_action("transform")

    def test_has_scope_hierarchy(self):
        c = Classification(
            mechanism="filesystem", effect="mutating", scope=frozenset({"artifact.source_code"})
        )
        assert c.has_scope("artifact.source_code")
        assert c.has_scope("artifact")
        assert not c.has_scope("data")

    def test_frozen(self):
        c = Classification(mechanism="filesystem", effect="read_only")
        with pytest.raises(AttributeError):
            c.mechanism = "shell"  # type: ignore[misc]


# ── Effect aggregation tests ──


class TestAggregateEffect:
    def test_single(self):
        assert aggregate_effect("read_only") == "read_only"

    def test_destructive_wins(self):
        assert aggregate_effect("read_only", "mutating", "destructive") == "destructive"

    def test_mutating_over_read(self):
        assert aggregate_effect("read_only", "mutating") == "mutating"

    def test_unknown_lowest(self):
        assert aggregate_effect("unknown", "read_only") == "read_only"

    def test_empty(self):
        assert aggregate_effect() is None


# ── Registry tests ──


class TestDimensionRegistry:
    def test_register_and_validate(self):
        reg = DimensionRegistry()
        reg.register_dimension("mechanism", Mechanism)
        assert reg.validate("mechanism", "filesystem")
        assert reg.validate("mechanism", "process")
        assert not reg.validate("mechanism", "nonexistent")

    def test_extend_validates_parent(self):
        reg = DimensionRegistry()
        reg.register_dimension("mechanism", Mechanism)
        reg.extend_dimension("mechanism", CodingMechanism)
        assert reg.validate("mechanism", "process.shell")
        assert reg.validate("mechanism", "process.shell")

    def test_extend_rejects_orphan(self):
        from enum import StrEnum

        class BadExtension(StrEnum):
            ORPHAN = "nonexistent_parent.child"

        reg = DimensionRegistry()
        reg.register_dimension("mechanism", Mechanism)
        with pytest.raises(ValueError, match="parent.*not registered"):
            reg.extend_dimension("mechanism", BadExtension)

    def test_roots(self):
        reg = DimensionRegistry()
        reg.register_dimension("role", Role)
        roots = reg.roots("role")
        assert "validator" in roots
        assert "retriever" in roots

    def test_children(self):
        reg = DimensionRegistry()
        reg.register_dimension("role", Role)
        reg.extend_dimension("role", CodingRole)
        children = reg.children("role", "validator")
        assert "validator.linter" in children
        assert "validator.test_runner" in children
        assert "validator.type_checker" in children

    def test_descendants(self):
        reg = DimensionRegistry()
        reg.register_dimension("action", Action)
        reg.extend_dimension("action", CodingAction)
        descendants = reg.descendants("action", "validate")
        assert "validate.lint" in descendants
        assert "validate.test" in descendants
        assert "validate.typecheck" in descendants

    def test_is_descendant(self):
        reg = DimensionRegistry()
        reg.register_dimension("role", Role)
        reg.extend_dimension("role", CodingRole)
        assert reg.is_descendant("role", "validator.linter", "validator")
        assert reg.is_descendant("role", "validator", "validator")
        assert not reg.is_descendant("role", "validator.linter", "transformer")

    def test_default_registry_loads(self):
        reg = get_default_registry()
        assert reg.validate("mechanism", "filesystem")
        assert reg.validate("mechanism", "process.shell")
        assert reg.validate("role", "validator.test_runner")
        assert reg.validate("action", "validate.test")
        assert reg.validate("scope", "artifact.source_code")

    def test_values(self):
        reg = DimensionRegistry()
        reg.register_dimension("effect", Effect)
        vals = reg.values("effect")
        assert "read_only" in vals
        assert "mutating" in vals
        assert "destructive" in vals


# ── Detailed shell classification tests ──


class TestClassifyShell:
    def test_pytest(self):
        c = classify_shell("pytest tests/ -v")
        assert c.mechanism == "process.shell"
        assert c.effect == "read_only"
        assert c.has_role("validator.test_runner")
        assert c.has_action("validate")
        assert "subprocess" in c.capability
        assert "pytest" in c.binaries

    def test_pip_install(self):
        c = classify_shell("pip install flask")
        assert c.effect == "mutating"
        assert c.has_role("orchestrator.package_manager")
        assert "network_outbound" in c.capability
        assert "pip" in c.binaries

    def test_compound_command(self):
        c = classify_shell("pip install pytest && pytest tests/")
        assert "compound" in c.structure or "sequential" in c.structure
        assert "pip" in c.binaries
        assert "pytest" in c.binaries

    def test_git_push(self):
        c = classify_shell("git push origin main")
        assert c.has_role("persistence.version_control")
        assert c.effect == "mutating"
        assert "network_outbound" in c.capability

    def test_curl(self):
        c = classify_shell("curl https://example.com")
        assert "network_outbound" in c.capability
        assert "curl" in c.binaries

    def test_rm_destructive(self):
        c = classify_shell("rm -rf build/")
        assert c.effect == "destructive"

    def test_ruff_check_read_only(self):
        c = classify_shell("ruff check .")
        assert c.effect == "read_only"
        assert c.has_role("validator.linter")

    def test_ruff_fix_mutating(self):
        c = classify_shell("ruff check --fix .")
        assert c.effect == "mutating"

    def test_empty_command(self):
        c = classify_shell("")
        assert c.mechanism == "process.shell"
        assert c.effect is None

    def test_piped_command(self):
        c = classify_shell("cat file.txt | grep pattern")
        assert "piped" in c.structure

    def test_shell_dialect_is_bash(self):
        c = classify_shell("echo hello")
        assert c.shell_dialect == "bash"


# ── Detailed tool classification tests ──


class TestClassifyToolDetailed:
    def test_view(self):
        c = classify_tool("view")
        assert c.mechanism == "filesystem"
        assert c.effect == "read_only"
        assert c.has_scope("artifact.source_code")
        assert "filesystem_read" in c.capability

    def test_edit(self):
        c = classify_tool("edit")
        assert c.mechanism == "filesystem"
        assert c.effect == "mutating"
        assert "filesystem_write" in c.capability

    def test_grep(self):
        c = classify_tool("grep")
        assert c.mechanism == "filesystem"
        assert c.effect == "read_only"
        assert c.has_role("retriever.search_index")

    def test_git_commit(self):
        c = classify_tool("git_commit")
        assert c.mechanism == "process.shell"
        assert c.effect == "mutating"
        assert c.has_role("persistence.version_control")
        assert c.has_action("persist.commit")

    def test_report_intent(self):
        c = classify_tool("report_intent")
        assert c.mechanism == "communication.system"
        assert c.effect == "read_only"

    def test_ask_user(self):
        c = classify_tool("ask_user")
        assert c.mechanism == "communication.user"
        assert "human_interaction" in c.capability

    def test_web_fetch(self):
        c = classify_tool("web_fetch")
        assert c.mechanism == "network.http"
        assert "network_outbound" in c.capability

    def test_shell_tool(self):
        c = classify_tool("bash")
        assert c.mechanism == "process.shell"
        # Dialect is None at tool-level; determined by shell classifier from command content
        assert c.shell_dialect is None

    def test_unknown_tool(self):
        c = classify_tool("totally_unknown_tool")
        assert c.mechanism == "unknown"

    def test_normalized_aliases(self):
        c = classify_tool("read_file")
        assert c.mechanism == "filesystem"

        c2 = classify_tool("str_replace_editor")
        assert c2.mechanism == "filesystem"

    def test_custom_classifications(self):
        custom = {
            "my_tool": Classification(
                mechanism="database",
                effect="mutating",
                role=frozenset({"store.database_writer"}),
                action=frozenset({"persist.commit"}),
                capability=frozenset({"network_outbound"}),
            )
        }
        c = classify_tool("my_tool", custom_classifications=custom)
        assert c.mechanism == "database"
        assert c.effect == "mutating"


# ── Core enum tests ──


class TestCoreEnums:
    def test_mechanism_values(self):
        assert Mechanism.FILESYSTEM == "filesystem"
        assert CodingMechanism.PROCESS_SHELL == "process.shell"

    def test_effect_values(self):
        assert Effect.READ_ONLY == "read_only"
        assert Effect.DESTRUCTIVE == "destructive"

    def test_coding_subtypes_have_correct_parents(self):
        for v in CodingMechanism:
            parent = v.value.rsplit(".", 1)[0]
            assert parent in [m.value for m in Mechanism], f"{v.value} has invalid parent {parent}"

        for v in CodingRole:
            parent = v.value.rsplit(".", 1)[0]
            assert parent in [r.value for r in Role], f"{v.value} has invalid parent {parent}"

        for v in CodingAction:
            parent = v.value.rsplit(".", 1)[0]
            assert parent in [a.value for a in Action], f"{v.value} has invalid parent {parent}"

        for v in CodingScope:
            parent = v.value.rsplit(".", 1)[0]
            assert parent in [s.value for s in Scope], f"{v.value} has invalid parent {parent}"


# ── Red team regression tests ──


class TestRedTeamRegressions:
    """Regression tests for issues identified in classification engine audit."""

    def test_sudo_propagates_elevated_privilege(self):
        """sudo wrapper must propagate elevated_privilege capability."""
        c = classify_shell("sudo rm -rf /")
        assert c.effect == "destructive"
        assert "elevated_privilege" in c.capability

    def test_find_delete_is_destructive(self):
        """find -delete must be classified as destructive, not read_only."""
        c = classify_shell("find . -delete")
        assert c.effect == "destructive"

    def test_git_checkout_is_mutating(self):
        """git checkout modifies the working tree — must be mutating."""
        c = classify_shell("git checkout main")
        assert c.effect == "mutating"

    def test_git_clean_is_destructive(self):
        """git clean removes untracked files — must be destructive."""
        c = classify_shell("git clean -fdx")
        assert c.effect == "destructive"

    def test_curl_output_is_mutating(self):
        """curl -o writes to filesystem — must be mutating."""
        c = classify_shell("curl -o file.txt https://example.com")
        assert c.effect == "mutating"

    def test_curl_O_is_mutating(self):
        """curl -O writes to filesystem — must be mutating."""
        c = classify_shell("curl -O https://example.com/file.tar.gz")
        assert c.effect == "mutating"

    def test_redirect_produces_mutating_effect(self):
        """Output redirection (>) must produce mutating effect + filesystem_write."""
        c = classify_shell("echo hi > out.txt")
        assert c.effect == "mutating"
        assert "filesystem_write" in c.capability

    def test_append_redirect_produces_mutating_effect(self):
        """Append redirection (>>) must produce mutating effect."""
        c = classify_shell("echo hi >> out.txt")
        assert c.effect == "mutating"
        assert "filesystem_write" in c.capability

    def test_docker_system_df_is_read_only(self):
        """docker system df is informational — must be read_only."""
        c = classify_shell("docker system df")
        assert c.effect == "read_only"

    def test_mcp_filesystem_delete_valid_role(self):
        """MCP filesystem delete must get modifier.file_editor, not modifier.file_browser."""
        from traceforge.classify.mcp import classify_mcp_tool
        from functools import partial

        cm = partial(classify_mcp_tool, engine=ENGINE)
        c = cm("mcp__filesystem__delete_file")
        assert c is not None
        assert "modifier.file_editor" in c.role
        assert "modifier.file_browser" not in c.role

    def test_enum_values_in_tool_classifications_are_registered(self):
        """All values used in tool_classifications.yaml must be valid registered values."""
        from traceforge.classify.registry import get_default_registry

        reg = get_default_registry()
        for name, cls in ENGINE.tool_classifications.items():
            assert reg.validate("mechanism", cls.mechanism), (
                f"{name}: mechanism '{cls.mechanism}' not registered"
            )
            for s in cls.scope:
                assert reg.validate("scope", s), f"{name}: scope '{s}' not registered"
            for r in cls.role:
                assert reg.validate("role", r), f"{name}: role '{r}' not registered"
            for a in cls.action:
                assert reg.validate("action", a), f"{name}: action '{a}' not registered"

    def test_rule_effects_override_binary_info_defaults(self):
        """Shell rules with explicit effect must override binary_info defaults."""
        # find -delete rule has effect=destructive, binary_info has read_only default
        c = classify_shell("find /tmp -name '*.log' -delete")
        assert c.effect == "destructive"

    def test_sudo_with_mutating_command(self):
        """sudo pip install must have both elevated_privilege and mutating."""
        c = classify_shell("sudo pip install flask")
        assert c.effect == "mutating"
        assert "elevated_privilege" in c.capability

    # --- Round 2 regression tests ---

    def test_input_redirect_is_not_mutating(self):
        """Input redirection (< file) must NOT produce mutating effect."""
        c = classify_shell("grep foo < input.txt")
        assert c.effect == "read_only"
        assert "filesystem_write" not in c.capability

    def test_heredoc_is_not_mutating(self):
        """Heredoc (<<EOF) is input, not output — must not be mutating."""
        c = classify_shell("cat <<EOF\nhello\nEOF")
        assert c.effect == "read_only"

    def test_git_branch_delete_is_destructive(self):
        """git branch -D deletes a branch — must be destructive."""
        c = classify_shell("git branch -D old-branch")
        assert c.effect == "destructive"

    def test_git_pull_is_mutating(self):
        """git pull modifies the working tree — must be mutating."""
        c = classify_shell("git pull")
        assert c.effect == "mutating"

    def test_git_clone_is_mutating(self):
        """git clone creates files — must be mutating."""
        c = classify_shell("git clone https://github.com/example/repo.git")
        assert c.effect == "mutating"

    def test_git_restore_is_mutating(self):
        """git restore modifies working tree — must be mutating."""
        c = classify_shell("git restore file.txt")
        assert c.effect == "mutating"

    def test_docker_system_prune_is_destructive(self):
        """docker system prune removes data — must be destructive."""
        c = classify_shell("docker system prune -f")
        assert c.effect == "destructive"

    def test_docker_image_prune_is_destructive(self):
        """docker image prune removes images — must be destructive."""
        c = classify_shell("docker image prune")
        assert c.effect == "destructive"

    def test_sed_i_with_suffix_is_mutating(self):
        """sed -i.bak (with backup suffix) must still be mutating."""
        c = classify_shell("sed -i.bak 's/a/b/' file.txt")
        assert c.effect == "mutating"

    def test_tee_is_mutating(self):
        """tee always writes to a file — must be mutating."""
        c = classify_shell("echo hi | tee out.txt")
        assert c.effect == "mutating"

    def test_curl_request_long_flag_is_mutating(self):
        """curl --request POST must be mutating."""
        c = classify_shell("curl --request POST https://example.com")
        assert c.effect == "mutating"

    def test_curl_attached_X_flag_is_mutating(self):
        """curl -XPOST (attached value) must be mutating."""
        c = classify_shell("curl -XPOST https://example.com")
        assert c.effect == "mutating"

    def test_sort_output_flag_is_mutating(self):
        """sort -o writes to file — must be mutating."""
        c = classify_shell("sort -o out.txt in.txt")
        assert c.effect == "mutating"

    # --- Round 3 regression tests ---

    def test_sudo_u_flag_unwraps_correctly(self):
        """sudo -u <user> must unwrap to the inner command, not the user."""
        c = classify_shell("sudo -u deploy rm -rf /")
        assert c.effect == "destructive"
        assert "rm" in c.binaries
        assert "elevated_privilege" in c.capability

    def test_timeout_unwraps_correctly(self):
        """timeout <duration> must unwrap to the inner command."""
        c = classify_shell("timeout 5 pytest")
        assert "pytest" in c.binaries

    def test_env_u_flag_unwraps_correctly(self):
        """env -u VAR must unwrap correctly to inner command."""
        c = classify_shell("env -u FOO pytest tests/")
        assert "pytest" in c.binaries

    def test_apt_get_remove_is_destructive(self):
        """apt-get remove is destructive — it removes packages."""
        c = classify_shell("apt-get remove nginx")
        assert c.effect == "destructive"

    def test_pip_uninstall_is_destructive(self):
        """pip uninstall is destructive — it removes packages."""
        c = classify_shell("pip uninstall flask")
        assert c.effect == "destructive"

    def test_npm_uninstall_is_destructive(self):
        """npm uninstall is destructive — it removes packages."""
        c = classify_shell("npm uninstall express")
        assert c.effect == "destructive"

    def test_mkfs_ext4_is_destructive(self):
        """mkfs.ext4 formats a disk — must be destructive."""
        c = classify_shell("mkfs.ext4 /dev/sda1")
        assert c.effect == "destructive"

    def test_pkill_is_destructive(self):
        """pkill kills processes — must be destructive."""
        c = classify_shell("pkill -9 nginx")
        assert c.effect == "destructive"

    def test_killall_is_destructive(self):
        """killall kills processes — must be destructive."""
        c = classify_shell("killall node")
        assert c.effect == "destructive"
