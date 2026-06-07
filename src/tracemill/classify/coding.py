"""Coding domain subtypes — extends core dimensions for software engineering agents.

Ships built-in with tracemill. Registered automatically on import.
"""

from __future__ import annotations

from enum import StrEnum


# ── Mechanism subtypes ──


class CodingMechanism(StrEnum):
    """Coding-specific mechanism subtypes.

    Use Mechanism + ShellDialect for shell variants; do not duplicate here.
    """

    FILE_READ = "file.read"
    FILE_WRITE = "file.write"
    FILE_DELETE = "file.delete"
    NETWORK_HTTP = "network.http"
    DATABASE_SQL = "database.sql"
    DATABASE_NOSQL = "database.nosql"
    PROCESS_REPL = "process.repl"
    PROCESS_DEBUG = "process.debug"
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
    DOCUMENTATION = "knowledge.documentation"
    API_SPEC = "knowledge.api_spec"
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
    SHELL_RUNTIME = "executor.shell_runtime"
    REPL = "executor.repl"
    SCRIPT_RUNNER = "executor.script_runner"

    # orchestrator.*
    PACKAGE_MANAGER = "orchestrator.package_manager"
    TASK_RUNNER = "orchestrator.task_runner"
    CI_CD = "orchestrator.ci_cd"
    CONTAINER_RUNTIME = "orchestrator.container_runtime"
    CLOUD_CLI = "orchestrator.cloud_cli"
    VERSION_CONTROL = "orchestrator.version_control"

    # observer.*
    DEBUGGER = "observer.debugger"
    PROFILER = "observer.profiler"
    LOGGER = "observer.logger"

    # retriever.*
    SEARCH_INDEX = "retriever.search_index"
    FILE_BROWSER = "retriever.file_browser"
    WEB_SCRAPER = "retriever.web_scraper"
    API_CLIENT = "retriever.api_client"

    # persistence.*
    VERSIONED_STORE = "persistence.versioned_store"
    CACHE = "persistence.cache"
    DATABASE_WRITER = "persistence.database_writer"

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
    CACHE = "persist.cache"

    # remove.*
    DELETE = "remove.delete"
    TEARDOWN = "remove.teardown"
    CLEAN = "remove.clean"
    UNINSTALL = "remove.uninstall"

    # analyze.*
    PROFILE = "analyze.profile"
    MEASURE = "analyze.measure"
    DIFF = "analyze.diff"
