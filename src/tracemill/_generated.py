"""Auto-generated from classify/schema.yaml — DO NOT EDIT.

Generated at: 2026-06-14T09:10:51Z
Re-generate: python scripts/generate_types.py
"""

from __future__ import annotations

from typing import Literal

# Canonical event kinds in <domain>.<object>.<phase> grammar.
EventKind = Literal[
    "agent.completed",
    "agent.failed",
    "agent.handoff",
    "agent.spawned",
    "browser.action",
    "browser.launched",
    "browser.result",
    "checkpoint.created",
    "checkpoint.restored",
    "command.completed",
    "command.failed",
    "command.output",
    "command.started",
    "file.created",
    "file.deleted",
    "file.edited",
    "file.read",
    "guardrail.failed",
    "guardrail.passed",
    "guardrail.started",
    "hook.completed",
    "hook.failed",
    "hook.started",
    "input.received",
    "input.requested",
    "knowledge.query.completed",
    "knowledge.query.started",
    "llm.call.completed",
    "llm.call.failed",
    "llm.call.started",
    "llm.output.chunk",
    "llm.thinking.chunk",
    "mcp.connection.completed",
    "mcp.connection.failed",
    "mcp.connection.started",
    "memory.query.completed",
    "memory.query.started",
    "memory.save.completed",
    "memory.save.started",
    "message.assistant",
    "message.assistant.chunk",
    "message.system",
    "message.user",
    "permission.denied",
    "permission.granted",
    "permission.requested",
    "planning.completed",
    "planning.failed",
    "planning.started",
    "raw",
    "reasoning.completed",
    "reasoning.started",
    "session.abort",
    "session.ended",
    "session.error",
    "session.idle",
    "session.info",
    "session.paused",
    "session.resumed",
    "session.started",
    "session.warning",
    "skill.invoked",
    "task.completed",
    "task.failed",
    "task.started",
    "telemetry.usage",
    "tool.call.completed",
    "tool.call.failed",
    "tool.call.started",
    "tool.output",
    "tool.progress",
    "tool.result.chunk",
    "tool.validation.failed",
    "turn.ended",
    "turn.skipped",
    "turn.started",
    "workflow.completed",
    "workflow.failed",
    "workflow.started",
]

# How the tool interacts with the environment.
Mechanism = Literal[
    "communication.system",
    "communication.user",
    "database",
    "database.nosql",
    "database.sql",
    "delegation.agent",
    "filesystem",
    "network.http",
    "process",
    "process.shell",
    "unknown",
]

# What the tool does to state.
Effect = Literal["destructive", "mutating", "read_only"]

# What domain the action targets.
Scope = Literal[
    "artifact.build_output",
    "artifact.config",
    "artifact.container_image",
    "artifact.dependency",
    "artifact.documentation",
    "artifact.repository",
    "artifact.source_code",
    "artifact.test_code",
    "configuration.ci_cd",
    "configuration.dependency",
    "configuration.infrastructure",
    "external.api",
    "external.service",
    "state.deployment",
    "state.repository",
    "system.network",
    "system.os",
    "system.secrets",
]

# What persona the tool fills.
Role = Literal[
    "communicator.system_reporter",
    "communicator.user_prompt",
    "executor.container_runtime",
    "executor.script_runner",
    "modifier.file_editor",
    "orchestrator.ci_cd",
    "orchestrator.package_manager",
    "orchestrator.task_runner",
    "persistence.cache",
    "persistence.database",
    "persistence.version_control",
    "retriever.api_client",
    "retriever.file_browser",
    "retriever.search_index",
    "retriever.web_scraper",
    "validator.security_scanner",
    "validator.test_runner",
]

# What verb the tool performs.
Action = Literal[
    "analyze",
    "configure",
    "deliver",
    "deliver.push",
    "execute",
    "execute.run_script",
    "generate",
    "modify",
    "modify.edit",
    "modify.merge",
    "modify.rebase",
    "persist",
    "persist.commit",
    "persist.stage",
    "persist.write",
    "remove",
    "remove.delete",
    "retrieve",
    "retrieve.browse",
    "retrieve.diff",
    "retrieve.read",
    "retrieve.search",
    "validate",
    "validate.security_scan",
]

# What capabilities/risks the tool exposes.
Capability = Literal[
    "budget_pressure",
    "credential_exposure",
    "elevated_privilege",
    "filesystem_read",
    "filesystem_write",
    "human_interaction",
    "integrity_unverified",
    "mcp_drift",
    "network_inbound",
    "network_outbound",
    "pii_exposure",
    "subprocess",
    "uses_credentials",
]

# Structural patterns detected in the invocation.
Structure = Literal[
    "conditional",
    "ifc_violation",
    "interactive",
    "parallel",
    "phase_anomaly",
    "piped",
    "sequential",
    "tainted_flow",
]

# Risk severity tier.
RiskBand = Literal["caution", "critical", "danger", "safe"]

# Governance action to take.
Recommendation = Literal["allow", "deny", "escalate", "warn"]

# Gate verdict outcome.
Decision = Literal["allow", "deny", "escalate"]

__all__ = ["EventKind", "Mechanism", "Effect", "Scope", "Role", "Action", "Capability", "Structure", "RiskBand", "Recommendation", "Decision"]
