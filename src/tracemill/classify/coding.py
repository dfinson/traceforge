"""Coding domain subtypes — extends core dimensions for software engineering agents.

Ships built-in with tracemill. Registered automatically on import.
"""

from __future__ import annotations

from enum import StrEnum


# ── Mechanism subtypes ──


class CodingMechanism(StrEnum):
    """Coding-specific mechanism subtypes."""

    FILE_READ = "file.read"
    FILE_WRITE = "file.write"
    FILE_SEARCH = "file.search"
    FILE_DELETE = "file.delete"
    SHELL_BASH = "shell.bash"
    SHELL_POWERSHELL = "shell.powershell"
    SHELL_CMD = "shell.cmd"
    SHELL_ZSH = "shell.zsh"
    SHELL_SH = "shell.sh"
    NETWORK_HTTP = "network.http"
    NETWORK_BROWSER = "network.browser"
    NETWORK_SEARCH = "network.search"
    DATABASE_SQL = "database.sql"
    DATABASE_NOSQL = "database.nosql"
    RUNTIME_REPL = "runtime.repl"
    RUNTIME_DEBUG = "runtime.debug"
    AGENT_DELEGATE = "agent.delegate"
    AGENT_SELF = "agent.self"
    COMMUNICATION_USER = "communication.user"
    COMMUNICATION_SYSTEM = "communication.system"


# ── Scope subtypes ──


class CodingScope(StrEnum):
    """Coding-specific scope subtypes."""

    SOURCE_CODE = "artifact.source_code"
    TEST_CODE = "artifact.test_code"
    BUILD_ARTIFACT = "artifact.build_artifact"
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

    # store.*
    VCS = "store.version_control"
    CACHE = "store.cache"
    DATABASE_WRITER = "store.database_writer"

    # communicator.*
    USER_PROMPT = "communicator.user_prompt"
    SYSTEM_REPORTER = "communicator.system_reporter"


# ── Action subtypes ──


class CodingAction(StrEnum):
    """Coding-specific action subtypes."""

    # validate.*
    LINT = "validate.lint"
    TEST = "validate.test"
    TYPECHECK = "validate.typecheck"
    SECURITY_SCAN = "validate.security_scan"
    BUILD_CHECK = "validate.build"

    # retrieve.*
    SEARCH_FILES = "retrieve.search_files"
    SEARCH_WEB = "retrieve.search_web"
    READ_FILE = "retrieve.read_file"
    QUERY_DB = "retrieve.query_db"
    BROWSE_DIR = "retrieve.browse_dir"

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
    PUSH_VCS = "deliver.push"
    RELEASE = "deliver.release"

    # configure.*
    INSTALL_DEPS = "configure.dependencies"
    SETUP_ENV = "configure.environment"
    PROVISION_INFRA = "configure.infrastructure"

    # store.*
    COMMIT = "store.commit"
    WRITE_FILE = "store.write_file"
    CACHE_DATA = "store.cache"

    # destroy.*
    DELETE_FILE = "destroy.file"
    TEARDOWN = "destroy.teardown"
    CLEAN = "destroy.clean"
    UNINSTALL = "destroy.uninstall"

    # analyze.*
    PROFILE = "analyze.profile"
    MEASURE = "analyze.measure"
    DIFF = "analyze.diff"
