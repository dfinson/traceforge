"""Declarative classification rule table — shared across all shell backends.

Each rule is a frozen dataclass describing a pattern to match against
(binary, subcmd, flags) and the activity to assign. Rules are evaluated
in order; first match wins.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from tracemill.classify.config import ClassificationEngine
    from tracemill.classify.core import Classification

from tracemill.classify.core import Effect
from tracemill.classify.coding import CodingAction, CodingRole, CodingScope


class ShellActivity(StrEnum):
    """Internal: what a shell command primarily does (command-local intent).

    Used as the intermediate label in the rule table for priority resolution
    when compound commands contain multiple binaries.
    """

    VERIFICATION = "verification"
    GIT_OPS = "git_ops"
    SETUP = "setup"
    INVESTIGATION = "investigation"
    IMPLEMENTATION = "implementation"


SHELL_VERIFICATION = ShellActivity.VERIFICATION
SHELL_GIT_OPS = ShellActivity.GIT_OPS
SHELL_SETUP = ShellActivity.SETUP
SHELL_INVESTIGATION = ShellActivity.INVESTIGATION
SHELL_IMPLEMENTATION = ShellActivity.IMPLEMENTATION

def activity_from_classification(cls: Classification) -> ShellActivity:
    """Derive ShellActivity from a Classification's action/role dimensions."""
    if cls.has_action("validate"):
        return ShellActivity.VERIFICATION
    if cls.has_action("configure"):
        return ShellActivity.SETUP
    if cls.has_action("retrieve") or cls.has_action("analyze"):
        return ShellActivity.INVESTIGATION
    if cls.has_role("persistence.version_control"):
        return ShellActivity.GIT_OPS
    if cls.has_action("deliver"):
        return ShellActivity.GIT_OPS
    # persist + version_control = git_ops, persist alone = implementation
    if cls.has_action("persist") and cls.has_role("persistence"):
        return ShellActivity.GIT_OPS
    return ShellActivity.IMPLEMENTATION


@dataclass(frozen=True)
class Rule:
    """A declarative classification rule.

    Matches when:
    - binary is in `binaries`
    - subcmd is in `subcmds` (or subcmds is None → any subcmd)
    - all flags in `flags_require` are present (or flags_require is None)
    - no flags in `flags_reject` are present (or flags_reject is None)

    Optional overrides (scope, action, phase) take precedence over
    the default activity→dimension mappings in classify_single_command.
    """

    binaries: frozenset[str]
    activity: ShellActivity
    subcmds: frozenset[str] | None = None
    flags_require: frozenset[str] | None = None
    flags_reject: frozenset[str] | None = None
    role: CodingRole | str = ""
    effect: Effect | str = ""
    scope: str = ""
    action: str = ""
    phase: str = ""


@dataclass(frozen=True)
class BinaryInfo:
    """Static metadata about a known binary."""

    role: CodingRole | str
    default_effect: Effect | None
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
         activity=SHELL_VERIFICATION, role=CodingRole.TASK_RUNNER, effect=None),
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

    # ── Container tools (docker/podman) ──
    Rule(binaries=frozenset({"docker", "podman"}), subcmds=frozenset({"build"}),
         activity=SHELL_VERIFICATION, role=CodingRole.CONTAINER_RUNTIME, effect=Effect.MUTATING,
         scope=CodingScope.CONTAINER_IMAGE, action=CodingAction.BUILD_CHECK),
    Rule(binaries=frozenset({"docker", "podman"}), subcmds=frozenset({"push"}),
         activity=SHELL_IMPLEMENTATION, role=CodingRole.CONTAINER_RUNTIME, effect=Effect.MUTATING,
         scope=CodingScope.CONTAINER_IMAGE, action=CodingAction.PUSH),
    Rule(binaries=frozenset({"docker", "podman"}), subcmds=frozenset({"pull"}),
         activity=SHELL_SETUP, role=CodingRole.CONTAINER_RUNTIME, effect=Effect.MUTATING,
         scope=CodingScope.CONTAINER_IMAGE, action=CodingAction.INSTALL),
    Rule(binaries=frozenset({"docker", "podman"}),
         subcmds=frozenset({"run", "exec", "start", "restart", "stop"}),
         activity=SHELL_IMPLEMENTATION, role=CodingRole.CONTAINER_RUNTIME, effect=Effect.MUTATING,
         scope=CodingScope.PROCESS, action=CodingAction.RUN_SERVICE),
    Rule(binaries=frozenset({"docker", "podman"}),
         subcmds=frozenset({"ps", "images", "logs", "inspect", "stats"}),
         activity=SHELL_INVESTIGATION, role=CodingRole.CONTAINER_RUNTIME, effect=Effect.READ_ONLY,
         scope=CodingScope.PROCESS, action=CodingAction.BROWSE),
    Rule(binaries=frozenset({"docker", "podman"}),
         subcmds=frozenset({"rm", "rmi", "prune", "system"}),
         activity=SHELL_IMPLEMENTATION, role=CodingRole.CONTAINER_RUNTIME, effect=Effect.DESTRUCTIVE,
         scope=CodingScope.CONTAINER_IMAGE, action=CodingAction.DELETE),

    # ── docker-compose / docker compose ──
    Rule(binaries=frozenset({"docker-compose"}), subcmds=frozenset({"up", "start", "restart"}),
         activity=SHELL_IMPLEMENTATION, role=CodingRole.CONTAINER_RUNTIME, effect=Effect.MUTATING,
         scope=CodingScope.SERVICE, action=CodingAction.RUN_SERVICE),
    Rule(binaries=frozenset({"docker-compose"}), subcmds=frozenset({"down", "stop", "rm"}),
         activity=SHELL_IMPLEMENTATION, role=CodingRole.CONTAINER_RUNTIME, effect=Effect.DESTRUCTIVE,
         scope=CodingScope.SERVICE, action=CodingAction.TEARDOWN),
    Rule(binaries=frozenset({"docker-compose"}), subcmds=frozenset({"build"}),
         activity=SHELL_VERIFICATION, role=CodingRole.CONTAINER_RUNTIME, effect=Effect.MUTATING,
         scope=CodingScope.CONTAINER_IMAGE, action=CodingAction.BUILD_CHECK),
    Rule(binaries=frozenset({"docker-compose"}), subcmds=frozenset({"ps", "logs"}),
         activity=SHELL_INVESTIGATION, role=CodingRole.CONTAINER_RUNTIME, effect=Effect.READ_ONLY,
         scope=CodingScope.SERVICE, action=CodingAction.BROWSE),

    # ── Kubernetes ──
    Rule(binaries=frozenset({"kubectl"}), subcmds=frozenset({"apply", "create", "patch", "replace"}),
         activity=SHELL_IMPLEMENTATION, role=CodingRole.CONTAINER_RUNTIME, effect=Effect.MUTATING,
         scope=CodingScope.DEPLOYMENT, action=CodingAction.DEPLOY),
    Rule(binaries=frozenset({"kubectl"}),
         subcmds=frozenset({"get", "describe", "logs", "top", "events"}),
         activity=SHELL_INVESTIGATION, role=CodingRole.CONTAINER_RUNTIME, effect=Effect.READ_ONLY,
         scope=CodingScope.DEPLOYMENT, action=CodingAction.BROWSE),
    Rule(binaries=frozenset({"kubectl"}), subcmds=frozenset({"delete"}),
         activity=SHELL_IMPLEMENTATION, role=CodingRole.CONTAINER_RUNTIME, effect=Effect.DESTRUCTIVE,
         scope=CodingScope.DEPLOYMENT, action=CodingAction.DELETE),
    Rule(binaries=frozenset({"kubectl"}), subcmds=frozenset({"exec", "port-forward", "run"}),
         activity=SHELL_IMPLEMENTATION, role=CodingRole.CONTAINER_RUNTIME, effect=Effect.MUTATING,
         scope=CodingScope.PROCESS, action=CodingAction.RUN_SCRIPT),

    # ── Infrastructure as code (terraform/helm/pulumi) ──
    Rule(binaries=frozenset({"terraform", "tofu"}), subcmds=frozenset({"plan", "validate"}),
         activity=SHELL_VERIFICATION, role=CodingRole.TASK_RUNNER, effect=Effect.READ_ONLY,
         scope=CodingScope.INFRASTRUCTURE, action=CodingAction.BUILD_CHECK),
    Rule(binaries=frozenset({"terraform", "tofu"}), subcmds=frozenset({"apply"}),
         activity=SHELL_IMPLEMENTATION, role=CodingRole.TASK_RUNNER, effect=Effect.MUTATING,
         scope=CodingScope.INFRASTRUCTURE, action=CodingAction.DEPLOY),
    Rule(binaries=frozenset({"terraform", "tofu"}), subcmds=frozenset({"destroy"}),
         activity=SHELL_IMPLEMENTATION, role=CodingRole.TASK_RUNNER, effect=Effect.DESTRUCTIVE,
         scope=CodingScope.INFRASTRUCTURE, action=CodingAction.TEARDOWN),
    Rule(binaries=frozenset({"terraform", "tofu"}), subcmds=frozenset({"init"}),
         activity=SHELL_SETUP, role=CodingRole.TASK_RUNNER, effect=Effect.MUTATING,
         scope=CodingScope.INFRASTRUCTURE, action=CodingAction.SETUP),
    Rule(binaries=frozenset({"terraform", "tofu"}), subcmds=frozenset({"show", "state", "output"}),
         activity=SHELL_INVESTIGATION, role=CodingRole.TASK_RUNNER, effect=Effect.READ_ONLY,
         scope=CodingScope.INFRASTRUCTURE, action=CodingAction.BROWSE),
    Rule(binaries=frozenset({"helm"}), subcmds=frozenset({"install", "upgrade"}),
         activity=SHELL_IMPLEMENTATION, role=CodingRole.PACKAGE_MANAGER, effect=Effect.MUTATING,
         scope=CodingScope.DEPLOYMENT, action=CodingAction.DEPLOY),
    Rule(binaries=frozenset({"helm"}), subcmds=frozenset({"template", "lint"}),
         activity=SHELL_VERIFICATION, role=CodingRole.TASK_RUNNER, effect=Effect.READ_ONLY,
         scope=CodingScope.DEPLOYMENT, action=CodingAction.BUILD_CHECK),
    Rule(binaries=frozenset({"helm"}), subcmds=frozenset({"uninstall", "delete"}),
         activity=SHELL_IMPLEMENTATION, role=CodingRole.PACKAGE_MANAGER, effect=Effect.DESTRUCTIVE,
         scope=CodingScope.DEPLOYMENT, action=CodingAction.TEARDOWN),
    Rule(binaries=frozenset({"helm"}), subcmds=frozenset({"list", "status", "get"}),
         activity=SHELL_INVESTIGATION, role=CodingRole.TASK_RUNNER, effect=Effect.READ_ONLY,
         scope=CodingScope.DEPLOYMENT, action=CodingAction.BROWSE),
    Rule(binaries=frozenset({"pulumi"}), subcmds=frozenset({"preview"}),
         activity=SHELL_VERIFICATION, role=CodingRole.TASK_RUNNER, effect=Effect.READ_ONLY,
         scope=CodingScope.INFRASTRUCTURE, action=CodingAction.BUILD_CHECK),
    Rule(binaries=frozenset({"pulumi"}), subcmds=frozenset({"up"}),
         activity=SHELL_IMPLEMENTATION, role=CodingRole.TASK_RUNNER, effect=Effect.MUTATING,
         scope=CodingScope.INFRASTRUCTURE, action=CodingAction.DEPLOY),
    Rule(binaries=frozenset({"pulumi"}), subcmds=frozenset({"destroy"}),
         activity=SHELL_IMPLEMENTATION, role=CodingRole.TASK_RUNNER, effect=Effect.DESTRUCTIVE,
         scope=CodingScope.INFRASTRUCTURE, action=CodingAction.TEARDOWN),

    # ── HTTP clients ──
    # curl with write-ish flags → mutating
    Rule(binaries=frozenset({"curl"}),
         flags_require=frozenset({"-X"}),
         activity=SHELL_IMPLEMENTATION, role=CodingRole.API_CLIENT, effect=Effect.MUTATING,
         action=CodingAction.RUN_SCRIPT),
    Rule(binaries=frozenset({"curl"}),
         flags_require=frozenset({"-d"}),
         activity=SHELL_IMPLEMENTATION, role=CodingRole.API_CLIENT, effect=Effect.MUTATING,
         action=CodingAction.RUN_SCRIPT),
    Rule(binaries=frozenset({"curl"}),
         flags_require=frozenset({"--data"}),
         activity=SHELL_IMPLEMENTATION, role=CodingRole.API_CLIENT, effect=Effect.MUTATING,
         action=CodingAction.RUN_SCRIPT),
    Rule(binaries=frozenset({"curl"}),
         flags_require=frozenset({"-F"}),
         activity=SHELL_IMPLEMENTATION, role=CodingRole.API_CLIENT, effect=Effect.MUTATING,
         action=CodingAction.RUN_SCRIPT),
    Rule(binaries=frozenset({"curl"}),
         flags_require=frozenset({"--form"}),
         activity=SHELL_IMPLEMENTATION, role=CodingRole.API_CLIENT, effect=Effect.MUTATING,
         action=CodingAction.RUN_SCRIPT),
    Rule(binaries=frozenset({"curl"}),
         flags_require=frozenset({"-T"}),
         activity=SHELL_IMPLEMENTATION, role=CodingRole.API_CLIENT, effect=Effect.MUTATING,
         action=CodingAction.RUN_SCRIPT),
    Rule(binaries=frozenset({"curl"}),
         flags_require=frozenset({"--upload-file"}),
         activity=SHELL_IMPLEMENTATION, role=CodingRole.API_CLIENT, effect=Effect.MUTATING,
         action=CodingAction.RUN_SCRIPT),
    # curl default → investigation/read_only
    Rule(binaries=frozenset({"curl"}),
         activity=SHELL_INVESTIGATION, role=CodingRole.API_CLIENT, effect=Effect.READ_ONLY,
         action=CodingAction.READ),
    # wget → always writes to disk
    Rule(binaries=frozenset({"wget"}),
         activity=SHELL_IMPLEMENTATION, role=CodingRole.API_CLIENT, effect=Effect.MUTATING,
         action=CodingAction.WRITE),

    # ── Text processors (default read_only, -i makes mutating) ──
    Rule(binaries=frozenset({"sed"}), flags_require=frozenset({"-i"}),
         activity=SHELL_IMPLEMENTATION, role=CodingRole.FILE_EDITOR, effect=Effect.MUTATING,
         action=CodingAction.EDIT),
    Rule(binaries=frozenset({"sed"}),
         activity=SHELL_INVESTIGATION, role=CodingRole.SEARCH_INDEX, effect=Effect.READ_ONLY,
         action=CodingAction.SEARCH),
    Rule(binaries=frozenset({"perl"}), flags_require=frozenset({"-i"}),
         activity=SHELL_IMPLEMENTATION, role=CodingRole.FILE_EDITOR, effect=Effect.MUTATING,
         action=CodingAction.EDIT),
    Rule(binaries=frozenset({"awk", "gawk", "jq", "yq", "perl"}),
         activity=SHELL_INVESTIGATION, role=CodingRole.SEARCH_INDEX, effect=Effect.READ_ONLY,
         action=CodingAction.SEARCH),

    # ── File operations ──
    Rule(binaries=frozenset({"cp", "copy"}),
         activity=SHELL_IMPLEMENTATION, role=CodingRole.FILE_EDITOR, effect=Effect.MUTATING,
         action=CodingAction.WRITE),
    Rule(binaries=frozenset({"mv", "move", "rename"}),
         activity=SHELL_IMPLEMENTATION, role=CodingRole.FILE_EDITOR, effect=Effect.MUTATING,
         action=CodingAction.EDIT),
    Rule(binaries=frozenset({"mkdir"}),
         activity=SHELL_SETUP, role=CodingRole.FILE_EDITOR, effect=Effect.MUTATING,
         action=CodingAction.SETUP),
    Rule(binaries=frozenset({"rm", "rmdir", "del", "erase", "rd"}),
         activity=SHELL_IMPLEMENTATION, role=CodingRole.SCRIPT_RUNNER, effect=Effect.DESTRUCTIVE,
         action=CodingAction.DELETE),
    Rule(binaries=frozenset({"chmod", "chown", "chgrp", "icacls"}),
         activity=SHELL_SETUP, role=CodingRole.SCRIPT_RUNNER, effect=Effect.MUTATING,
         scope=CodingScope.ENVIRONMENT, action=CodingAction.SETUP),
    Rule(binaries=frozenset({"touch"}),
         activity=SHELL_IMPLEMENTATION, role=CodingRole.FILE_EDITOR, effect=Effect.MUTATING,
         action=CodingAction.WRITE),
    Rule(binaries=frozenset({"ln"}),
         activity=SHELL_IMPLEMENTATION, role=CodingRole.FILE_EDITOR, effect=Effect.MUTATING,
         action=CodingAction.WRITE),

    # ── Investigation/read-only utilities ──
    Rule(binaries=frozenset({"cat", "head", "tail", "less", "more", "bat", "hexdump", "xxd"}),
         activity=SHELL_INVESTIGATION, role=CodingRole.FILE_BROWSER, effect=Effect.READ_ONLY,
         action=CodingAction.READ),
    Rule(binaries=frozenset({"ls", "ll", "exa", "eza", "lsd"}),
         activity=SHELL_INVESTIGATION, role=CodingRole.FILE_BROWSER, effect=Effect.READ_ONLY,
         action=CodingAction.BROWSE),
    Rule(binaries=frozenset({"wc", "sort", "uniq", "cut", "tr", "tee", "xargs",
                             "diff", "comm", "paste"}),
         activity=SHELL_INVESTIGATION, role=CodingRole.SEARCH_INDEX, effect=Effect.READ_ONLY,
         action=CodingAction.READ),
    Rule(binaries=frozenset({"grep", "rg", "ag", "fgrep", "egrep", "ripgrep"}),
         activity=SHELL_INVESTIGATION, role=CodingRole.SEARCH_INDEX, effect=Effect.READ_ONLY,
         action=CodingAction.SEARCH),
    Rule(binaries=frozenset({"fd", "fdfind"}),
         activity=SHELL_INVESTIGATION, role=CodingRole.FILE_BROWSER, effect=Effect.READ_ONLY,
         action=CodingAction.BROWSE),
    Rule(binaries=frozenset({"pwd", "which", "whereis", "whoami", "hostname", "uname", "env",
                             "printenv", "date", "uptime", "free", "df", "du"}),
         activity=SHELL_INVESTIGATION, role=CodingRole.SCRIPT_RUNNER, effect=Effect.READ_ONLY,
         action=CodingAction.READ),

    # ── Environment managers ──
    Rule(binaries=frozenset({"conda"}), subcmds=frozenset({"install", "create", "update"}),
         activity=SHELL_SETUP, role=CodingRole.PACKAGE_MANAGER, effect=Effect.MUTATING,
         scope=CodingScope.ENVIRONMENT, action=CodingAction.INSTALL),
    Rule(binaries=frozenset({"conda"}), subcmds=frozenset({"activate", "deactivate"}),
         activity=SHELL_SETUP, role=CodingRole.PACKAGE_MANAGER, effect=Effect.READ_ONLY,
         scope=CodingScope.ENVIRONMENT, action=CodingAction.SETUP),
    Rule(binaries=frozenset({"conda"}), subcmds=frozenset({"list", "info", "search"}),
         activity=SHELL_INVESTIGATION, role=CodingRole.PACKAGE_MANAGER, effect=Effect.READ_ONLY,
         scope=CodingScope.ENVIRONMENT, action=CodingAction.BROWSE),
    Rule(binaries=frozenset({"virtualenv", "venv"}),
         activity=SHELL_SETUP, role=CodingRole.PACKAGE_MANAGER, effect=Effect.MUTATING,
         scope=CodingScope.ENVIRONMENT, action=CodingAction.SETUP),
    Rule(binaries=frozenset({"nvm", "rbenv", "pyenv", "fnm", "asdf", "mise", "rtx"}),
         activity=SHELL_SETUP, role=CodingRole.PACKAGE_MANAGER, effect=Effect.MUTATING,
         scope=CodingScope.ENVIRONMENT, action=CodingAction.SETUP),

    # ── Security / audit tools ──
    Rule(binaries=frozenset({"bandit", "semgrep", "trivy", "snyk", "safety"}),
         activity=SHELL_VERIFICATION, role=CodingRole.SECURITY_SCANNER, effect=Effect.READ_ONLY,
         action=CodingAction.SECURITY_SCAN),
    Rule(binaries=frozenset({"npm", "pnpm", "yarn"}), subcmds=frozenset({"audit"}),
         activity=SHELL_VERIFICATION, role=CodingRole.SECURITY_SCANNER, effect=Effect.READ_ONLY,
         action=CodingAction.SECURITY_SCAN),
    Rule(binaries=frozenset({"cargo"}), subcmds=frozenset({"audit"}),
         activity=SHELL_VERIFICATION, role=CodingRole.SECURITY_SCANNER, effect=Effect.READ_ONLY,
         action=CodingAction.SECURITY_SCAN),

    # ── Build tools (cmake/ninja/bazel) ──
    Rule(binaries=frozenset({"cmake"}),
         activity=SHELL_SETUP, role=CodingRole.BUILD_CHECKER, effect=Effect.MUTATING,
         action=CodingAction.SETUP),
    Rule(binaries=frozenset({"ninja", "bazel"}), subcmds=frozenset({"build"}),
         activity=SHELL_VERIFICATION, role=CodingRole.BUILD_CHECKER, effect=Effect.MUTATING,
         action=CodingAction.BUILD_CHECK),
    Rule(binaries=frozenset({"bazel"}), subcmds=frozenset({"test"}),
         activity=SHELL_VERIFICATION, role=CodingRole.TEST_RUNNER, effect=Effect.READ_ONLY,
         action=CodingAction.TEST),

    # ── CLI tools for cloud/platforms ──
    Rule(binaries=frozenset({"gh"}), subcmds=frozenset({"pr", "issue", "release"}),
         activity=SHELL_GIT_OPS, role=CodingRole.VERSION_CONTROL, effect=Effect.MUTATING,
         scope=CodingScope.REPOSITORY),
    Rule(binaries=frozenset({"gh"}), subcmds=frozenset({"repo", "gist", "browse", "api"}),
         activity=SHELL_INVESTIGATION, role=CodingRole.API_CLIENT, effect=Effect.READ_ONLY,
         scope=CodingScope.REPOSITORY),
    Rule(binaries=frozenset({"az", "aws", "gcloud"}),
         activity=SHELL_IMPLEMENTATION, role=CodingRole.API_CLIENT, effect=None,
         scope=CodingScope.INFRASTRUCTURE),

    # ── Publishing / delivery ──
    Rule(binaries=frozenset({"twine"}), subcmds=frozenset({"upload"}),
         activity=SHELL_IMPLEMENTATION, role=CodingRole.TASK_RUNNER, effect=Effect.MUTATING,
         scope=CodingScope.PACKAGE, action=CodingAction.PUBLISH),
    Rule(binaries=frozenset({"npm", "pnpm"}), subcmds=frozenset({"publish"}),
         activity=SHELL_IMPLEMENTATION, role=CodingRole.TASK_RUNNER, effect=Effect.MUTATING,
         scope=CodingScope.PACKAGE, action=CodingAction.PUBLISH),
    Rule(binaries=frozenset({"cargo"}), subcmds=frozenset({"publish"}),
         activity=SHELL_IMPLEMENTATION, role=CodingRole.TASK_RUNNER, effect=Effect.MUTATING,
         scope=CodingScope.PACKAGE, action=CodingAction.PUBLISH),
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
    "npm": BinaryInfo(role=CodingRole.TASK_RUNNER, default_effect=None, network=True),
    "pnpm": BinaryInfo(role=CodingRole.TASK_RUNNER, default_effect=None, network=True),
    "yarn": BinaryInfo(role=CodingRole.TASK_RUNNER, default_effect=None, network=True),
    "cargo": BinaryInfo(role=CodingRole.TASK_RUNNER, default_effect=None),
    "uv": BinaryInfo(role=CodingRole.PACKAGE_MANAGER, default_effect=Effect.MUTATING, network=True),
    "poetry": BinaryInfo(role=CodingRole.PACKAGE_MANAGER, default_effect=Effect.MUTATING, network=True),
    "brew": BinaryInfo(role=CodingRole.PACKAGE_MANAGER, default_effect=Effect.MUTATING, network=True),
    "apt": BinaryInfo(role=CodingRole.PACKAGE_MANAGER, default_effect=Effect.MUTATING, network=True),
    "apt-get": BinaryInfo(role=CodingRole.PACKAGE_MANAGER, default_effect=Effect.MUTATING, network=True),
    "docker": BinaryInfo(role=CodingRole.CONTAINER_RUNTIME, default_effect=None, network=True),
    "podman": BinaryInfo(role=CodingRole.CONTAINER_RUNTIME, default_effect=None, network=True),
    "docker-compose": BinaryInfo(role=CodingRole.CONTAINER_RUNTIME, default_effect=None, network=True),
    "kubectl": BinaryInfo(role=CodingRole.CONTAINER_RUNTIME, default_effect=None, network=True),
    "helm": BinaryInfo(role=CodingRole.PACKAGE_MANAGER, default_effect=None, network=True),
    "terraform": BinaryInfo(role=CodingRole.TASK_RUNNER, default_effect=None, network=True),
    "tofu": BinaryInfo(role=CodingRole.TASK_RUNNER, default_effect=None, network=True),
    "pulumi": BinaryInfo(role=CodingRole.TASK_RUNNER, default_effect=None, network=True),
    "git": BinaryInfo(role=CodingRole.VERSION_CONTROL, default_effect=None),
    "gh": BinaryInfo(role=CodingRole.API_CLIENT, default_effect=None, network=True),
    "make": BinaryInfo(role=CodingRole.TASK_RUNNER, default_effect=None),
    "cmake": BinaryInfo(role=CodingRole.BUILD_CHECKER, default_effect=Effect.MUTATING),
    "ninja": BinaryInfo(role=CodingRole.BUILD_CHECKER, default_effect=Effect.MUTATING),
    "bazel": BinaryInfo(role=CodingRole.BUILD_CHECKER, default_effect=None),
    "gradle": BinaryInfo(role=CodingRole.TASK_RUNNER, default_effect=None),
    "mvn": BinaryInfo(role=CodingRole.TASK_RUNNER, default_effect=None),
    "webpack": BinaryInfo(role=CodingRole.BUNDLER, default_effect=Effect.MUTATING),
    "vite": BinaryInfo(role=CodingRole.BUNDLER, default_effect=None),
    "dotnet": BinaryInfo(role=CodingRole.TASK_RUNNER, default_effect=None),
    "go": BinaryInfo(role=CodingRole.TASK_RUNNER, default_effect=None),
    "python": BinaryInfo(role=CodingRole.SCRIPT_RUNNER, default_effect=None),
    "python3": BinaryInfo(role=CodingRole.SCRIPT_RUNNER, default_effect=None),
    "node": BinaryInfo(role=CodingRole.SCRIPT_RUNNER, default_effect=None),
    "curl": BinaryInfo(role=CodingRole.API_CLIENT, default_effect=Effect.READ_ONLY, network=True),
    "wget": BinaryInfo(role=CodingRole.API_CLIENT, default_effect=Effect.MUTATING, network=True),
    "rm": BinaryInfo(role=CodingRole.SCRIPT_RUNNER, default_effect=Effect.DESTRUCTIVE, destructive=True),
    "rmdir": BinaryInfo(role=CodingRole.SCRIPT_RUNNER, default_effect=Effect.DESTRUCTIVE, destructive=True),
    "sudo": BinaryInfo(role=CodingRole.SCRIPT_RUNNER, default_effect=None),
    "choco": BinaryInfo(role=CodingRole.PACKAGE_MANAGER, default_effect=Effect.MUTATING, network=True),
    "winget": BinaryInfo(role=CodingRole.PACKAGE_MANAGER, default_effect=Effect.MUTATING, network=True),
    "scoop": BinaryInfo(role=CodingRole.PACKAGE_MANAGER, default_effect=Effect.MUTATING, network=True),
    # Text processors
    "sed": BinaryInfo(role=CodingRole.SEARCH_INDEX, default_effect=Effect.READ_ONLY),
    "awk": BinaryInfo(role=CodingRole.SEARCH_INDEX, default_effect=Effect.READ_ONLY),
    "gawk": BinaryInfo(role=CodingRole.SEARCH_INDEX, default_effect=Effect.READ_ONLY),
    "jq": BinaryInfo(role=CodingRole.SEARCH_INDEX, default_effect=Effect.READ_ONLY),
    "yq": BinaryInfo(role=CodingRole.SEARCH_INDEX, default_effect=Effect.READ_ONLY),
    "perl": BinaryInfo(role=CodingRole.SCRIPT_RUNNER, default_effect=None),
    # File ops
    "cp": BinaryInfo(role=CodingRole.FILE_EDITOR, default_effect=Effect.MUTATING),
    "mv": BinaryInfo(role=CodingRole.FILE_EDITOR, default_effect=Effect.MUTATING),
    "mkdir": BinaryInfo(role=CodingRole.FILE_EDITOR, default_effect=Effect.MUTATING),
    "chmod": BinaryInfo(role=CodingRole.SCRIPT_RUNNER, default_effect=Effect.MUTATING),
    "chown": BinaryInfo(role=CodingRole.SCRIPT_RUNNER, default_effect=Effect.MUTATING),
    "touch": BinaryInfo(role=CodingRole.FILE_EDITOR, default_effect=Effect.MUTATING),
    "ln": BinaryInfo(role=CodingRole.FILE_EDITOR, default_effect=Effect.MUTATING),
    # Investigation utilities
    "cat": BinaryInfo(role=CodingRole.FILE_BROWSER, default_effect=Effect.READ_ONLY),
    "head": BinaryInfo(role=CodingRole.FILE_BROWSER, default_effect=Effect.READ_ONLY),
    "tail": BinaryInfo(role=CodingRole.FILE_BROWSER, default_effect=Effect.READ_ONLY),
    "less": BinaryInfo(role=CodingRole.FILE_BROWSER, default_effect=Effect.READ_ONLY),
    "grep": BinaryInfo(role=CodingRole.SEARCH_INDEX, default_effect=Effect.READ_ONLY),
    "rg": BinaryInfo(role=CodingRole.SEARCH_INDEX, default_effect=Effect.READ_ONLY),
    "ag": BinaryInfo(role=CodingRole.SEARCH_INDEX, default_effect=Effect.READ_ONLY),
    "fd": BinaryInfo(role=CodingRole.FILE_BROWSER, default_effect=Effect.READ_ONLY),
    "fdfind": BinaryInfo(role=CodingRole.FILE_BROWSER, default_effect=Effect.READ_ONLY),
    # Security scanners
    "bandit": BinaryInfo(role=CodingRole.SECURITY_SCANNER, default_effect=Effect.READ_ONLY),
    "semgrep": BinaryInfo(role=CodingRole.SECURITY_SCANNER, default_effect=Effect.READ_ONLY),
    "trivy": BinaryInfo(role=CodingRole.SECURITY_SCANNER, default_effect=Effect.READ_ONLY),
    "snyk": BinaryInfo(role=CodingRole.SECURITY_SCANNER, default_effect=Effect.READ_ONLY, network=True),
    "safety": BinaryInfo(role=CodingRole.SECURITY_SCANNER, default_effect=Effect.READ_ONLY, network=True),
    # Environment managers
    "conda": BinaryInfo(role=CodingRole.PACKAGE_MANAGER, default_effect=None, network=True),
    "virtualenv": BinaryInfo(role=CodingRole.PACKAGE_MANAGER, default_effect=Effect.MUTATING),
    "nvm": BinaryInfo(role=CodingRole.PACKAGE_MANAGER, default_effect=Effect.MUTATING),
    "rbenv": BinaryInfo(role=CodingRole.PACKAGE_MANAGER, default_effect=Effect.MUTATING),
    "pyenv": BinaryInfo(role=CodingRole.PACKAGE_MANAGER, default_effect=Effect.MUTATING),
    # Cloud CLIs
    "az": BinaryInfo(role=CodingRole.API_CLIENT, default_effect=None, network=True),
    "aws": BinaryInfo(role=CodingRole.API_CLIENT, default_effect=None, network=True),
    "gcloud": BinaryInfo(role=CodingRole.API_CLIENT, default_effect=None, network=True),
}

# Scripts that count as verification when run via `npm run <script>`
NPM_VERIFY_SCRIPTS: Final[frozenset[str]] = frozenset(
    {"test", "tests", "lint", "check", "typecheck", "build"}
)

# Modules that count as verification when run via `python -m <module>`
INTERPRETER_VERIFY_MODULES: Final[frozenset[str]] = frozenset(
    {"pytest", "unittest", "mypy", "pyright", "ruff"}
)


def match_rule(
    binary: str,
    subcmd: str | None,
    flags: list[str],
    engine: ClassificationEngine | None = None,
) -> Rule | None:
    """Find the first matching rule for a (binary, subcmd, flags) tuple.

    Returns the matched Rule or None if no rule matches.
    """
    rules = engine.shell_rules if engine is not None else RULES
    for rule in rules:
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
    binary: str,
    subcmd: str | None,
    flags: list[str],
    all_words: list[str] | None = None,
    engine: ClassificationEngine | None = None,
) -> ShellActivity:
    """Classify a command into a ShellActivity using the rule table + special cases."""
    if not binary:
        return ShellActivity.IMPLEMENTATION

    rule = match_rule(binary, subcmd, flags, engine=engine)
    npm_scripts = engine.npm_verify_scripts if engine is not None else NPM_VERIFY_SCRIPTS
    interp_modules = (
        engine.interpreter_verify_modules if engine is not None else INTERPRETER_VERIFY_MODULES
    )
    if rule:
        # Special case: npm/pnpm/yarn "run" needs script-name inspection
        if binary in ("npm", "pnpm", "yarn") and subcmd == "run" and all_words:
            script = all_words[2].lower() if len(all_words) > 2 else ""
            if script in npm_scripts:
                return SHELL_VERIFICATION
            return SHELL_IMPLEMENTATION
        return rule.activity

    # Special cases that don't fit the rule table pattern
    # (interpreter -m module pattern)
    if binary in ("python", "python3", "node") and all_words and "-m" in all_words:
        try:
            m_idx = all_words.index("-m")
            if m_idx + 1 < len(all_words) and all_words[m_idx + 1].lower() in interp_modules:
                return SHELL_VERIFICATION
        except ValueError:
            pass

    return SHELL_IMPLEMENTATION


def effect_for_binary(
    binary: str,
    subcmd: str | None,
    flags: list[str],
    engine: ClassificationEngine | None = None,
) -> Effect | None:
    """Determine effect from binary + context, using rule table and binary info."""
    # Flag-dependent overrides
    if binary in ("ruff", "eslint", "rubocop", "clippy"):
        return Effect.MUTATING if "--fix" in flags else Effect.READ_ONLY
    if binary in ("black", "prettier"):
        return Effect.READ_ONLY if "--check" in flags else Effect.MUTATING

    # sed/perl with -i → in-place edit
    if binary in ("sed", "perl") and "-i" in flags:
        return Effect.MUTATING

    # curl: explicit write flags → mutating, otherwise read_only
    if binary == "curl":
        _curl_write_flags = {"-X", "-d", "--data", "--data-raw", "--data-binary",
                             "--data-urlencode", "-F", "--form", "-T", "--upload-file"}
        if _curl_write_flags.intersection(flags):
            return Effect.MUTATING
        return Effect.READ_ONLY

    # Git subcmd determines effect
    if binary == "git":
        git_write = {"commit", "push", "merge", "rebase", "cherry-pick", "tag", "reset", "stash"}
        return Effect.MUTATING if subcmd in git_write else Effect.READ_ONLY

    # Docker/kubectl subcmd effects
    if binary in ("docker", "podman"):
        if subcmd in ("rm", "rmi", "prune"):
            return Effect.DESTRUCTIVE
        if subcmd in ("build", "push", "run", "exec", "start", "restart", "stop", "pull"):
            return Effect.MUTATING
        if subcmd in ("ps", "images", "logs", "inspect", "stats"):
            return Effect.READ_ONLY
    if binary == "kubectl":
        if subcmd == "delete":
            return Effect.DESTRUCTIVE
        if subcmd in ("apply", "create", "patch", "replace", "exec", "run"):
            return Effect.MUTATING
        if subcmd in ("get", "describe", "logs", "top", "events"):
            return Effect.READ_ONLY

    # Terraform
    if binary in ("terraform", "tofu"):
        if subcmd == "destroy":
            return Effect.DESTRUCTIVE
        if subcmd in ("apply", "init"):
            return Effect.MUTATING
        if subcmd in ("plan", "validate", "show", "state", "output"):
            return Effect.READ_ONLY

    # Look up in binary info
    bi = engine.binary_info if engine is not None else BINARY_INFO
    info = bi.get(binary)
    if info:
        if info.destructive:
            return Effect.DESTRUCTIVE
        return info.default_effect

    return None
