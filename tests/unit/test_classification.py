"""Tests for the multi-dimensional classification system (core, coding, registry, detailed APIs)."""

from __future__ import annotations

import pytest

from tracemill.classify.core import (
    Action,
    Classification,
    Effect,
    Mechanism,
    Role,
    Scope,
    aggregate_effect,
)
from tracemill.classify.coding import (
    CodingAction,
    CodingMechanism,
    CodingRole,
    CodingScope,
)
from tracemill.classify.registry import DimensionRegistry, get_default_registry
from tracemill.classify.shell import classify_shell
from tracemill.classify.tools import classify_tool


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
        c = Classification(mechanism="shell", effect="read_only", role=frozenset({"validator.linter"}))
        assert c.has_role("validator.linter")
        assert c.has_role("validator")
        assert not c.has_role("transformer")

    def test_has_action_hierarchy(self):
        c = Classification(mechanism="shell", effect="read_only", action=frozenset({"validate.test"}))
        assert c.has_action("validate.test")
        assert c.has_action("validate")
        assert not c.has_action("transform")

    def test_has_scope_hierarchy(self):
        c = Classification(mechanism="filesystem", effect="mutating", scope=frozenset({"artifact.source_code"}))
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
        assert c.mechanism == "communication"
        assert c.effect is None

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


