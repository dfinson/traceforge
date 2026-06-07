"""Declarative classification rule table — shared across all shell backends.

Each rule is a frozen dataclass describing a pattern to match against
(binary, subcmd, flags) and the activity to assign. Rules are evaluated
in order; first match wins.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from tracemill.classify.core import Effect
from tracemill.classify.coding import CodingRole

# Legacy activity constants
SHELL_VERIFICATION = "verification"
SHELL_GIT_OPS = "git_ops"
SHELL_SETUP = "setup"
SHELL_INVESTIGATION = "investigation"
SHELL_IMPLEMENTATION = "implementation"

ACTIVITY_PRIORITY: Final[dict[str, int]] = {
    SHELL_IMPLEMENTATION: 0,
    SHELL_INVESTIGATION: 1,
    SHELL_SETUP: 2,
    SHELL_GIT_OPS: 3,
    SHELL_VERIFICATION: 4,
}


@dataclass(frozen=True)
class Rule:
    """A declarative classification rule.

    Matches when:
    - binary is in `binaries`
    - subcmd is in `subcmds` (or subcmds is None → any subcmd)
    - all flags in `flags_require` are present (or flags_require is None)
    - no flags in `flags_reject` are present (or flags_reject is None)
    - if `subcmd_from_words_idx` is set, uses all_words[idx] as the subcmd check
    """

    binaries: frozenset[str]
    activity: str
    subcmds: frozenset[str] | None = None
    flags_require: frozenset[str] | None = None
    flags_reject: frozenset[str] | None = None
    role: str = ""
    effect: str = ""


@dataclass(frozen=True)
class BinaryInfo:
    """Static metadata about a known binary."""

    role: str
    default_effect: str
    network: bool = False
    destructive: bool = False


# ── Rule table (evaluated in order, first match wins) ──

RULES: Final[tuple[Rule, ...]] = (
    # ── Setup (package installation) ──
    Rule(binaries=frozenset({"pip", "pip3"}), subcmds=frozenset({"install"}),
         activity=SHELL_SETUP, role=CodingRole.PACKAGE_MANAGER, effect=Effect.MUTATING),
    Rule(binaries=frozenset({"npm", "pnpm", "yarn"}), subcmds=frozenset({"install", "add", "ci"}),
         activity=SHELL_SETUP, role=CodingRole.PACKAGE_MANAGER, effect=Effect.MUTATING),
    Rule(binaries=frozenset({"cargo"}), subcmds=frozenset({"add"}),
         activity=SHELL_SETUP, role=CodingRole.PACKAGE_MANAGER, effect=Effect.MUTATING),
    Rule(binaries=frozenset({"brew", "apt", "apt-get"}), subcmds=frozenset({"install"}),
         activity=SHELL_SETUP, role=CodingRole.PACKAGE_MANAGER, effect=Effect.MUTATING),
    Rule(binaries=frozenset({"uv"}), subcmds=frozenset({"sync", "pip", "add"}),
         activity=SHELL_SETUP, role=CodingRole.PACKAGE_MANAGER, effect=Effect.MUTATING),
    Rule(binaries=frozenset({"poetry"}), subcmds=frozenset({"install", "add"}),
         activity=SHELL_SETUP, role=CodingRole.PACKAGE_MANAGER, effect=Effect.MUTATING),
    Rule(binaries=frozenset({"choco", "winget", "scoop"}), subcmds=frozenset({"install"}),
         activity=SHELL_SETUP, role=CodingRole.PACKAGE_MANAGER, effect=Effect.MUTATING),

    # ── Test runners (binary alone is enough) ──
    Rule(binaries=frozenset({"pytest", "jest", "vitest", "mocha", "rspec",
                             "phpunit", "bats", "pest", "tox", "nox", "playwright"}),
         activity=SHELL_VERIFICATION, role=CodingRole.TEST_RUNNER, effect=Effect.READ_ONLY),

    # ── Test via subcommand ──
    Rule(binaries=frozenset({"cargo", "go", "swift", "dart", "dotnet", "mvn",
                             "gradle", "npm", "pnpm", "yarn", "make"}),
         subcmds=frozenset({"test", "tests"}),
         activity=SHELL_VERIFICATION, role=CodingRole.TEST_RUNNER, effect=Effect.READ_ONLY),

    # ── Linters (always read-only, no fix flag) ──
    Rule(binaries=frozenset({"mypy", "pyright", "flake8", "pylint"}),
         activity=SHELL_VERIFICATION, role=CodingRole.LINTER, effect=Effect.READ_ONLY),
    Rule(binaries=frozenset({"tsc"}),
         activity=SHELL_VERIFICATION, role=CodingRole.TYPE_CHECKER, effect=Effect.READ_ONLY),

    # ── Linters that have a --fix mode (only verify if NOT fixing) ──
    Rule(binaries=frozenset({"ruff"}), subcmds=frozenset({"check"}),
         flags_reject=frozenset({"--fix"}),
         activity=SHELL_VERIFICATION, role=CodingRole.LINTER, effect=Effect.READ_ONLY),
    Rule(binaries=frozenset({"ruff"}), subcmds=frozenset({"format"}),
         flags_require=frozenset({"--check"}),
         activity=SHELL_VERIFICATION, role=CodingRole.FORMATTER, effect=Effect.READ_ONLY),
    Rule(binaries=frozenset({"eslint"}), flags_reject=frozenset({"--fix"}),
         activity=SHELL_VERIFICATION, role=CodingRole.LINTER, effect=Effect.READ_ONLY),
    Rule(binaries=frozenset({"rubocop", "clippy"}), flags_reject=frozenset({"--fix"}),
         activity=SHELL_VERIFICATION, role=CodingRole.LINTER, effect=Effect.READ_ONLY),
    Rule(binaries=frozenset({"golangci-lint"}), subcmds=frozenset({"run"}),
         activity=SHELL_VERIFICATION, role=CodingRole.LINTER, effect=Effect.READ_ONLY),

    # ── Formatters in check mode = verification, otherwise implementation ──
    Rule(binaries=frozenset({"black", "prettier"}), flags_require=frozenset({"--check"}),
         activity=SHELL_VERIFICATION, role=CodingRole.FORMATTER, effect=Effect.READ_ONLY),

    # ── cargo clippy ──
    Rule(binaries=frozenset({"cargo"}), subcmds=frozenset({"clippy"}),
         activity=SHELL_VERIFICATION, role=CodingRole.LINTER, effect=Effect.READ_ONLY),

    # ── Build commands ──
    Rule(binaries=frozenset({"cargo", "go", "make", "dotnet"}), subcmds=frozenset({"build"}),
         activity=SHELL_VERIFICATION, role=CodingRole.BUILD_CHECKER, effect=Effect.MUTATING),
    Rule(binaries=frozenset({"webpack"}),
         activity=SHELL_VERIFICATION, role=CodingRole.BUNDLER, effect=Effect.MUTATING),
    Rule(binaries=frozenset({"vite"}), subcmds=frozenset({"build"}),
         activity=SHELL_VERIFICATION, role=CodingRole.BUNDLER, effect=Effect.MUTATING),

    # ── npm/pnpm/yarn run <verify-script> ──
    Rule(binaries=frozenset({"npm", "pnpm", "yarn"}), subcmds=frozenset({"run"}),
         activity=SHELL_VERIFICATION, role=CodingRole.TASK_RUNNER, effect=Effect.UNKNOWN),
    # Note: the npm "run" rule needs script-name inspection handled by the special-case handler

    # ── Git operations ──
    Rule(binaries=frozenset({"git"}),
         subcmds=frozenset({"commit", "push", "merge", "rebase", "cherry-pick", "tag", "reset", "stash"}),
         activity=SHELL_GIT_OPS, role=CodingRole.VERSION_CONTROL, effect=Effect.MUTATING),
    Rule(binaries=frozenset({"git"}),
         subcmds=frozenset({"diff", "log", "status", "show", "blame", "branch"}),
         activity=SHELL_INVESTIGATION, role=CodingRole.VERSION_CONTROL, effect=Effect.READ_ONLY),

    # ── PowerShell specific cmdlets ──
    Rule(binaries=frozenset({"invoke-pester", "invoke-scriptanalyzer", "test-path",
                             "test-connection", "test-netconnection", "invoke-build", "build-module"}),
         activity=SHELL_VERIFICATION, role=CodingRole.TEST_RUNNER, effect=Effect.READ_ONLY),
    Rule(binaries=frozenset({"install-module", "install-package", "install-psresource",
                             "update-module", "register-psrepository"}),
         activity=SHELL_SETUP, role=CodingRole.PACKAGE_MANAGER, effect=Effect.MUTATING),
    Rule(binaries=frozenset({"get-childitem", "get-content", "get-item", "get-itemproperty",
                             "get-process", "get-service", "get-command", "get-help",
                             "get-module", "get-variable", "select-string", "where-object",
                             "select-object", "sort-object", "format-table", "format-list",
                             "out-string", "measure-object"}),
         activity=SHELL_INVESTIGATION, role=CodingRole.FILE_BROWSER, effect=Effect.READ_ONLY),

    # ── cmd.exe builtins ──
    Rule(binaries=frozenset({"dir", "type", "find", "findstr", "where", "tree", "set"}),
         activity=SHELL_INVESTIGATION, role=CodingRole.FILE_BROWSER, effect=Effect.READ_ONLY),
)

# ── Binary metadata (for detailed Classification) ──

BINARY_INFO: Final[dict[str, BinaryInfo]] = {
    "pytest": BinaryInfo(role=CodingRole.TEST_RUNNER, default_effect=Effect.READ_ONLY),
    "jest": BinaryInfo(role=CodingRole.TEST_RUNNER, default_effect=Effect.READ_ONLY),
    "vitest": BinaryInfo(role=CodingRole.TEST_RUNNER, default_effect=Effect.READ_ONLY),
    "mocha": BinaryInfo(role=CodingRole.TEST_RUNNER, default_effect=Effect.READ_ONLY),
    "rspec": BinaryInfo(role=CodingRole.TEST_RUNNER, default_effect=Effect.READ_ONLY),
    "phpunit": BinaryInfo(role=CodingRole.TEST_RUNNER, default_effect=Effect.READ_ONLY),
    "bats": BinaryInfo(role=CodingRole.TEST_RUNNER, default_effect=Effect.READ_ONLY),
    "pest": BinaryInfo(role=CodingRole.TEST_RUNNER, default_effect=Effect.READ_ONLY),
    "tox": BinaryInfo(role=CodingRole.TEST_RUNNER, default_effect=Effect.READ_ONLY),
    "nox": BinaryInfo(role=CodingRole.TEST_RUNNER, default_effect=Effect.READ_ONLY),
    "playwright": BinaryInfo(role=CodingRole.TEST_RUNNER, default_effect=Effect.READ_ONLY),
    "mypy": BinaryInfo(role=CodingRole.TYPE_CHECKER, default_effect=Effect.READ_ONLY),
    "pyright": BinaryInfo(role=CodingRole.TYPE_CHECKER, default_effect=Effect.READ_ONLY),
    "tsc": BinaryInfo(role=CodingRole.TYPE_CHECKER, default_effect=Effect.READ_ONLY),
    "flake8": BinaryInfo(role=CodingRole.LINTER, default_effect=Effect.READ_ONLY),
    "pylint": BinaryInfo(role=CodingRole.LINTER, default_effect=Effect.READ_ONLY),
    "eslint": BinaryInfo(role=CodingRole.LINTER, default_effect=Effect.READ_ONLY),
    "rubocop": BinaryInfo(role=CodingRole.LINTER, default_effect=Effect.READ_ONLY),
    "golangci-lint": BinaryInfo(role=CodingRole.LINTER, default_effect=Effect.READ_ONLY),
    "clippy": BinaryInfo(role=CodingRole.LINTER, default_effect=Effect.READ_ONLY),
    "ruff": BinaryInfo(role=CodingRole.LINTER, default_effect=Effect.READ_ONLY),
    "black": BinaryInfo(role=CodingRole.FORMATTER, default_effect=Effect.MUTATING),
    "prettier": BinaryInfo(role=CodingRole.FORMATTER, default_effect=Effect.MUTATING),
    "pip": BinaryInfo(role=CodingRole.PACKAGE_MANAGER, default_effect=Effect.MUTATING, network=True),
    "pip3": BinaryInfo(role=CodingRole.PACKAGE_MANAGER, default_effect=Effect.MUTATING, network=True),
    "npm": BinaryInfo(role=CodingRole.TASK_RUNNER, default_effect=Effect.UNKNOWN, network=True),
    "pnpm": BinaryInfo(role=CodingRole.TASK_RUNNER, default_effect=Effect.UNKNOWN, network=True),
    "yarn": BinaryInfo(role=CodingRole.TASK_RUNNER, default_effect=Effect.UNKNOWN, network=True),
    "cargo": BinaryInfo(role=CodingRole.TASK_RUNNER, default_effect=Effect.UNKNOWN),
    "uv": BinaryInfo(role=CodingRole.PACKAGE_MANAGER, default_effect=Effect.MUTATING, network=True),
    "poetry": BinaryInfo(role=CodingRole.PACKAGE_MANAGER, default_effect=Effect.MUTATING, network=True),
    "brew": BinaryInfo(role=CodingRole.PACKAGE_MANAGER, default_effect=Effect.MUTATING, network=True),
    "apt": BinaryInfo(role=CodingRole.PACKAGE_MANAGER, default_effect=Effect.MUTATING, network=True),
    "apt-get": BinaryInfo(role=CodingRole.PACKAGE_MANAGER, default_effect=Effect.MUTATING, network=True),
    "docker": BinaryInfo(role=CodingRole.CONTAINER_RUNTIME, default_effect=Effect.UNKNOWN, network=True),
    "kubectl": BinaryInfo(role=CodingRole.CLOUD_CLI, default_effect=Effect.UNKNOWN, network=True),
    "terraform": BinaryInfo(role=CodingRole.CLOUD_CLI, default_effect=Effect.UNKNOWN, network=True),
    "git": BinaryInfo(role=CodingRole.VERSION_CONTROL, default_effect=Effect.UNKNOWN),
    "make": BinaryInfo(role=CodingRole.TASK_RUNNER, default_effect=Effect.UNKNOWN),
    "gradle": BinaryInfo(role=CodingRole.TASK_RUNNER, default_effect=Effect.UNKNOWN),
    "mvn": BinaryInfo(role=CodingRole.TASK_RUNNER, default_effect=Effect.UNKNOWN),
    "webpack": BinaryInfo(role=CodingRole.BUNDLER, default_effect=Effect.MUTATING),
    "vite": BinaryInfo(role=CodingRole.BUNDLER, default_effect=Effect.UNKNOWN),
    "dotnet": BinaryInfo(role=CodingRole.TASK_RUNNER, default_effect=Effect.UNKNOWN),
    "go": BinaryInfo(role=CodingRole.TASK_RUNNER, default_effect=Effect.UNKNOWN),
    "python": BinaryInfo(role=CodingRole.SCRIPT_RUNNER, default_effect=Effect.UNKNOWN),
    "python3": BinaryInfo(role=CodingRole.SCRIPT_RUNNER, default_effect=Effect.UNKNOWN),
    "node": BinaryInfo(role=CodingRole.SCRIPT_RUNNER, default_effect=Effect.UNKNOWN),
    "curl": BinaryInfo(role=CodingRole.API_CLIENT, default_effect=Effect.READ_ONLY, network=True),
    "wget": BinaryInfo(role=CodingRole.API_CLIENT, default_effect=Effect.MUTATING, network=True),
    "rm": BinaryInfo(role=CodingRole.SHELL_RUNTIME, default_effect=Effect.DESTRUCTIVE),
    "rmdir": BinaryInfo(role=CodingRole.SHELL_RUNTIME, default_effect=Effect.DESTRUCTIVE),
    "sudo": BinaryInfo(role=CodingRole.SHELL_RUNTIME, default_effect=Effect.UNKNOWN),
    "choco": BinaryInfo(role=CodingRole.PACKAGE_MANAGER, default_effect=Effect.MUTATING, network=True),
    "winget": BinaryInfo(role=CodingRole.PACKAGE_MANAGER, default_effect=Effect.MUTATING, network=True),
    "scoop": BinaryInfo(role=CodingRole.PACKAGE_MANAGER, default_effect=Effect.MUTATING, network=True),
}

# Scripts that count as verification when run via `npm run <script>`
NPM_VERIFY_SCRIPTS: Final[frozenset[str]] = frozenset(
    {"test", "tests", "lint", "check", "typecheck", "build"}
)

# Modules that count as verification when run via `python -m <module>`
INTERPRETER_VERIFY_MODULES: Final[frozenset[str]] = frozenset(
    {"pytest", "unittest", "mypy", "pyright", "ruff"}
)


def match_rule(binary: str, subcmd: str | None, flags: list[str]) -> Rule | None:
    """Find the first matching rule for a (binary, subcmd, flags) tuple.

    Returns the matched Rule or None if no rule matches.
    """
    for rule in RULES:
        if binary not in rule.binaries:
            continue
        if rule.subcmds is not None and subcmd not in rule.subcmds:
            continue
        if rule.flags_require is not None and not rule.flags_require.issubset(flags):
            continue
        if rule.flags_reject is not None and rule.flags_reject.intersection(flags):
            continue
        return rule
    return None


def classify_binary(
    binary: str, subcmd: str | None, flags: list[str], all_words: list[str] | None = None
) -> str:
    """Classify a command into an activity string using the rule table + special cases.

    This replaces the old _classify_from_words if-else chains.
    """
    if not binary:
        return SHELL_IMPLEMENTATION

    rule = match_rule(binary, subcmd, flags)
    if rule:
        # Special case: npm/pnpm/yarn "run" needs script-name inspection
        if binary in ("npm", "pnpm", "yarn") and subcmd == "run" and all_words:
            script = all_words[2].lower() if len(all_words) > 2 else ""
            if script in NPM_VERIFY_SCRIPTS:
                return SHELL_VERIFICATION
            return SHELL_IMPLEMENTATION
        return rule.activity

    # Special cases that don't fit the rule table pattern
    # (interpreter -m module pattern)
    if binary in ("python", "python3", "node") and all_words and "-m" in all_words:
        try:
            m_idx = all_words.index("-m")
            if m_idx + 1 < len(all_words) and all_words[m_idx + 1].lower() in INTERPRETER_VERIFY_MODULES:
                return SHELL_VERIFICATION
        except ValueError:
            pass

    return SHELL_IMPLEMENTATION


def effect_for_binary(binary: str, subcmd: str | None, flags: list[str]) -> str:
    """Determine effect from binary + context, using rule table and binary info."""
    # Flag-dependent overrides
    if binary in ("ruff", "eslint", "rubocop", "clippy"):
        return Effect.MUTATING if "--fix" in flags else Effect.READ_ONLY
    if binary in ("black", "prettier"):
        return Effect.READ_ONLY if "--check" in flags else Effect.MUTATING

    # Git subcmd determines effect
    if binary == "git":
        git_write = {"commit", "push", "merge", "rebase", "cherry-pick", "tag", "reset", "stash"}
        return Effect.MUTATING if subcmd in git_write else Effect.READ_ONLY

    # Look up in binary info
    info = BINARY_INFO.get(binary)
    if info:
        if info.destructive:
            return Effect.DESTRUCTIVE
        return info.default_effect

    return Effect.UNKNOWN
