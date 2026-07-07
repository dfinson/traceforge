"""Config-surface + wiring tests for external preflight gates.

Covers:

* the discriminated ``preflight_gate`` union parses ``http`` / ``subprocess`` into
  the right config class;
* the mutually-exclusive validator fires when both ``tool_preflight_gate`` and
  ``preflight_gate`` are set;
* :meth:`GovernancePipeline.from_config` builds a ``GatePolicy`` whose single
  preflight gate is the right gate class with fields propagated;
* the async correctness fix: a DENY from a gate still blocks an ``async`` adapter
  (Semantic Kernel) the same way as before, now routed through
  ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio

import pytest
import yaml
from pydantic import ValidationError

import traceforge.config as config_pkg
from traceforge.config import (
    ExternalGateConfig,
    GovernanceConfig,
    HttpGateConfig,
    SubprocessGateConfig,
)
from traceforge.gate.external import HttpGate, SubprocessGate
from traceforge.governance.pipeline import GovernancePipeline
from traceforge.sdk.gate_policy import GatePolicy
from traceforge.sdk.verdict import Verdict


# ─── Package re-export ────────────────────────────────────────────────────────


def test_config_classes_reexported_from_package():
    for name in ("ExternalGateConfig", "HttpGateConfig", "SubprocessGateConfig"):
        assert name in config_pkg.__all__
    assert ExternalGateConfig is not None


# ─── Discriminated union parsing ──────────────────────────────────────────────


class TestExternalGateConfigParsing:
    def test_http_variant(self):
        gov = GovernanceConfig.model_validate(
            {
                "preflight_gate": {
                    "type": "http",
                    "endpoint": "http://localhost:8181/v1/data/tf/allow",
                    "headers": {"Authorization": "Bearer x"},
                    "timeout": 1.5,
                }
            }
        )
        assert isinstance(gov.preflight_gate, HttpGateConfig)
        assert gov.preflight_gate.endpoint.endswith("/allow")
        assert gov.preflight_gate.fail_open is False  # fail-closed default
        assert gov.preflight_gate.headers == {"Authorization": "Bearer x"}

    def test_subprocess_variant(self):
        gov = GovernanceConfig.model_validate(
            {"preflight_gate": {"type": "subprocess", "command": "opa eval"}}
        )
        assert isinstance(gov.preflight_gate, SubprocessGateConfig)
        assert gov.preflight_gate.command == "opa eval"
        assert gov.preflight_gate.fail_open is False
        assert gov.preflight_gate.timeout == 10.0

    def test_unknown_type_rejected(self):
        with pytest.raises(ValidationError):
            GovernanceConfig.model_validate(
                {"preflight_gate": {"type": "carrier-pigeon", "endpoint": "x"}}
            )

    def test_extra_field_forbidden(self):
        with pytest.raises(ValidationError):
            GovernanceConfig.model_validate(
                {"preflight_gate": {"type": "http", "endpoint": "x", "bogus": 1}}
            )

    def test_nonpositive_timeout_rejected(self):
        with pytest.raises(ValidationError):
            GovernanceConfig.model_validate(
                {"preflight_gate": {"type": "http", "endpoint": "x", "timeout": 0}}
            )


# ─── Mutual-exclusion validator ───────────────────────────────────────────────


class TestPreflightGateExclusivity:
    def test_both_set_raises(self):
        with pytest.raises(ValidationError, match="mutually exclusive"):
            GovernanceConfig.model_validate(
                {
                    "tool_preflight_gate": "myapp.policies.gate",
                    "preflight_gate": {"type": "http", "endpoint": "http://pdp"},
                }
            )

    def test_only_dotted_path_ok(self):
        gov = GovernanceConfig.model_validate({"tool_preflight_gate": "myapp.policies.gate"})
        assert gov.preflight_gate is None
        assert gov.tool_preflight_gate == "myapp.policies.gate"

    def test_only_external_ok(self):
        gov = GovernanceConfig.model_validate(
            {"preflight_gate": {"type": "subprocess", "command": "decider"}}
        )
        assert gov.tool_preflight_gate is None
        assert isinstance(gov.preflight_gate, SubprocessGateConfig)

    def test_neither_set_ok(self):
        gov = GovernanceConfig()
        assert gov.preflight_gate is None
        assert gov.tool_preflight_gate is None


# ─── from_config wiring (builds a real gate instance) ─────────────────────────


class TestFromConfigWiring:
    def _write(self, tmp_path, governance: dict) -> str:
        cfg = tmp_path / "traceforge.yaml"
        cfg.write_text(yaml.safe_dump({"governance": governance}))
        return str(cfg)

    def test_http_gate_built(self, tmp_path):
        path = self._write(
            tmp_path,
            {
                "preflight_gate": {
                    "type": "http",
                    "endpoint": "http://localhost:8181/decide",
                    "fail_open": True,
                    "timeout": 3.0,
                    "headers": {"Authorization": "Bearer tok"},
                }
            },
        )
        pipeline = GovernancePipeline.from_config(path=path)
        gates = pipeline.policy.preflight_gates
        assert len(gates) == 1
        gate = gates[0]
        assert isinstance(gate, HttpGate)
        assert gate.endpoint == "http://localhost:8181/decide"
        assert gate.fail_open is True
        assert gate.timeout == 3.0
        assert gate.headers == {"Authorization": "Bearer tok"}

    def test_subprocess_gate_built(self, tmp_path):
        path = self._write(
            tmp_path,
            {
                "preflight_gate": {
                    "type": "subprocess",
                    "command": 'decider --mode "two words"',
                    "max_input_bytes": 1024,
                }
            },
        )
        pipeline = GovernancePipeline.from_config(path=path)
        gates = pipeline.policy.preflight_gates
        assert len(gates) == 1
        gate = gates[0]
        assert isinstance(gate, SubprocessGate)
        assert gate.command == 'decider --mode "two words"'
        assert gate.max_input_bytes == 1024
        assert gate.fail_open is False  # fail-closed default survives round-trip

    def test_explicit_policy_overrides_config(self, tmp_path):
        path = self._write(
            tmp_path,
            {"preflight_gate": {"type": "subprocess", "command": "decider"}},
        )
        override = GatePolicy().preflight(lambda request, ctx: Verdict.allow())
        pipeline = GovernancePipeline.from_config(path=path, policy=override)
        assert pipeline.policy is override
        assert not isinstance(pipeline.policy.preflight_gates[0], SubprocessGate)

    def test_both_set_in_yaml_raises(self, tmp_path):
        path = self._write(
            tmp_path,
            {
                "tool_preflight_gate": "myapp.policies.gate",
                "preflight_gate": {"type": "http", "endpoint": "http://pdp"},
            },
        )
        with pytest.raises(ValidationError):
            GovernancePipeline.from_config(path=path)


# ─── Async correctness fix (behavior preserved through asyncio.to_thread) ─────


class TestAsyncAdapterOffload:
    """The async fix wraps the sync preflight call in ``asyncio.to_thread``.

    SK's ``auto_function_invocation`` filter only fires during LLM auto-calling,
    not on a direct ``kernel.invoke()`` (that is why the e2e suite's DENY test only
    checks filter registration). So we retrieve the registered async filter and
    drive its coroutine directly, inside a real event loop, against a fake context.
    This exercises the exact code path that now offloads via ``asyncio.to_thread``.
    """

    def _register_and_get_filter(self, pipeline):
        from semantic_kernel import Kernel
        from semantic_kernel.functions import kernel_function

        kernel = Kernel()

        @kernel_function(name="rm", description="Remove a file")
        def rm(path: str) -> str:
            return f"removed {path}"

        kernel.add_function("tools", rm)
        pipeline.gate_semantic_kernel(kernel)
        _priority, filt = kernel.auto_function_invocation_filters[0]
        func = kernel.get_function("tools", "rm")
        return filt, func

    def test_semantic_kernel_deny_blocks_and_preserves_reason(self):
        pytest.importorskip("semantic_kernel")
        from types import SimpleNamespace

        def deny_rm(request, ctx) -> Verdict:
            if request.tool == "rm":
                return Verdict.deny("blocked rm")
            return Verdict.allow()

        pipeline = GovernancePipeline.create()
        pipeline.policy = GatePolicy().preflight(deny_rm)
        filt, func = self._register_and_get_filter(pipeline)

        next_called = {"v": False}

        async def next_handler(ctx):
            next_called["v"] = True

        ctx = SimpleNamespace(
            function=func,
            arguments={"path": "/tmp/x"},
            function_result=None,
            terminate=False,
        )
        asyncio.run(filt(ctx, next_handler))

        assert next_called["v"] is False  # denied → next_handler never awaited
        assert ctx.terminate is True
        assert "Tool blocked by policy" in str(ctx.function_result.value)
        assert "blocked rm" in str(ctx.function_result.value)

    def test_semantic_kernel_allow_runs_next_handler(self):
        pytest.importorskip("semantic_kernel")
        from types import SimpleNamespace

        pipeline = GovernancePipeline.create()
        pipeline.policy = GatePolicy().preflight(lambda request, ctx: Verdict.allow())
        filt, func = self._register_and_get_filter(pipeline)

        next_called = {"v": False}

        async def next_handler(ctx):
            next_called["v"] = True
            ctx.function_result = SimpleNamespace(value="removed /tmp/x")

        ctx = SimpleNamespace(
            function=func,
            arguments={"path": "/tmp/x"},
            function_result=None,
            terminate=False,
        )
        asyncio.run(filt(ctx, next_handler))

        assert next_called["v"] is True  # allowed → next_handler awaited
        assert ctx.terminate is False
