"""Coding domain subtypes — extends core dimensions for software engineering agents.

Ships built-in with tracemill. Registered automatically on import.
"""

from __future__ import annotations

from enum import StrEnum


# ── Shell-specific types (coding domain only) ──


class ShellDialect(StrEnum):
    """Shell language dialect for process.shell mechanism."""

    BASH = "bash"
    POWERSHELL = "powershell"
    CMD = "cmd"
    ZSH = "zsh"
    FISH = "fish"
    POSIX_SH = "posix_sh"


class ShellStructure(StrEnum):
    """Shell-specific structural patterns (extends core Structure)."""

    PIPED = "piped"
    REDIRECTED = "redirected"


# ── Mechanism subtypes ──


class CodingMechanism(StrEnum):
    """Coding-specific mechanism subtypes.

    Mechanism is the invocation surface ONLY — direction/verb is expressed
    through Action and Effect dimensions, not mechanism subtypes.
    """

    PROCESS_SHELL = "process.shell"
    PROCESS_REPL = "process.repl"
    PROCESS_DEBUG = "process.debug"
    NETWORK_HTTP = "network.http"
    DATABASE_SQL = "database.sql"
    DATABASE_NOSQL = "database.nosql"
    DELEGATION_AGENT = "delegation.agent"
    DELEGATION_SELF = "delegation.self"
    COMMUNICATION_USER = "communication.user"
    COMMUNICATION_SYSTEM = "communication.system"


# ── Scope subtypes ──


class CodingScope(StrEnum):
    """Coding-specific scope subtypes."""

    SOURCE_CODE = "artifact.source_code"
    TEST_CODE = "artifact.test_code"
    BUILD_OUTPUT = "artifact.build_output"
    CONTAINER_IMAGE = "artifact.container_image"
    PACKAGE = "artifact.package"
    DOCUMENTATION = "artifact.documentation"
    API_SPEC = "artifact.api_spec"
    DEPENDENCY = "configuration.dependency"
    ENVIRONMENT = "configuration.environment"
    INFRASTRUCTURE = "configuration.infrastructure"
    CI_CD_CONFIG = "configuration.ci_cd"
    PROCESS = "state.process"
    SERVICE = "state.service"
    DEPLOYMENT = "state.deployment"
    REPOSITORY = "state.repository"


# ── Role subtypes ──


class CodingRole(StrEnum):
    """Coding-specific role subtypes."""

    # validator.*
    LINTER = "validator.linter"
    TEST_RUNNER = "validator.test_runner"
    TYPE_CHECKER = "validator.type_checker"
    SECURITY_SCANNER = "validator.security_scanner"
    BUILD_CHECKER = "validator.build_checker"

    # transformer.*
    COMPILER = "transformer.compiler"
    FORMATTER = "transformer.formatter"
    BUNDLER = "transformer.bundler"
    TRANSPILER = "transformer.transpiler"
    MINIFIER = "transformer.minifier"
    REFACTORER = "transformer.refactorer"

    # generator.*
    CODE_GENERATOR = "generator.code_generator"
    SCAFFOLDER = "generator.scaffolder"
    DOC_GENERATOR = "generator.doc_generator"

    # executor.*
    SCRIPT_RUNNER = "executor.script_runner"
    REPL = "executor.repl"
    CONTAINER_RUNTIME = "executor.container_runtime"

    # orchestrator.*
    PACKAGE_MANAGER = "orchestrator.package_manager"
    TASK_RUNNER = "orchestrator.task_runner"
    CI_CD = "orchestrator.ci_cd"

    # observer.*
    DEBUGGER = "observer.debugger"
    PROFILER = "observer.profiler"
    LOGGER = "observer.logger"

    # retriever.*
    SEARCH_INDEX = "retriever.search_index"
    FILE_BROWSER = "retriever.file_browser"
    WEB_SCRAPER = "retriever.web_scraper"
    API_CLIENT = "retriever.api_client"

    # modifier.*
    FILE_EDITOR = "modifier.file_editor"

    # persistence.*
    VERSION_CONTROL = "persistence.version_control"
    DATABASE = "persistence.database"
    CACHE = "persistence.cache"

    # communicator.*
    USER_PROMPT = "communicator.user_prompt"
    SYSTEM_REPORTER = "communicator.system_reporter"


# ── Action subtypes ──


class CodingAction(StrEnum):
    """Coding-specific action subtypes.

    Actions are pure verbs. Use Mechanism/Scope for object/target context.
    """

    # validate.*
    LINT = "validate.lint"
    TEST = "validate.test"
    TYPECHECK = "validate.typecheck"
    SECURITY_SCAN = "validate.security_scan"
    BUILD_CHECK = "validate.build_check"

    # retrieve.*
    SEARCH = "retrieve.search"
    READ = "retrieve.read"
    QUERY = "retrieve.query"
    BROWSE = "retrieve.browse"

    # transform.*
    COMPILE = "transform.compile"
    FORMAT = "transform.format"
    BUNDLE = "transform.bundle"
    TRANSPILE = "transform.transpile"
    REFACTOR = "transform.refactor"

    # generate.*
    CODE_GEN = "generate.code"
    SCAFFOLD = "generate.scaffold"
    DOC_GEN = "generate.documentation"

    # execute.*
    RUN_SCRIPT = "execute.script"
    RUN_SERVICE = "execute.service"
    RUN_REPL = "execute.repl"

    # deliver.*
    DEPLOY = "deliver.deploy"
    PUBLISH = "deliver.publish"
    PUSH = "deliver.push"
    RELEASE = "deliver.release"

    # configure.*
    INSTALL = "configure.install"
    SETUP = "configure.setup"
    PROVISION = "configure.provision"

    # persist.*
    COMMIT = "persist.commit"
    WRITE = "persist.write"
    STAGE = "persist.stage"

    # modify.*
    EDIT = "modify.edit"
    MERGE = "modify.merge"
    REBASE = "modify.rebase"

    # remove.*
    DELETE = "remove.delete"
    TEARDOWN = "remove.teardown"
    CLEAN = "remove.clean"
    UNINSTALL = "remove.uninstall"

    # analyze.*
    PROFILE = "analyze.profile"
    MEASURE = "analyze.measure"
    DIFF = "analyze.diff"

