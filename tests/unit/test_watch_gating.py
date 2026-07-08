"""PART E — enforce-by-default UX: config-declared GatePolicy + inactive warning.

``traceforge watch`` starts the gate IPC server; with **no** policy the gate runs
in allow-all mode and silently enforces nothing. These tests pin the two fixes:

* :func:`build_policy_from_config` turns a config-declared policy — an ordered
  preflight CHAIN (dotted in-process gates + external deciders), a postflight
  chain, and an ``external:`` bucket — into a live :class:`GatePolicy`, enforces
  it through a real pipeline, and returns ``None`` when nothing is declared so the
  caller can warn.
* the watch daemon emits a LOUD "enforcement INACTIVE (allow-all)" banner when the
  resolved policy does not actually gate anything.

The dotted gates below are module-level so the config loader can import them by
their ``tests.unit.test_watch_gating.<name>`` dotted path — exactly how a real
deployment references its own ``myapp.policies.<gate>`` callables.
"""

from __future__ import annotations

import pytest

from traceforge.cli.watch import _policy_is_enforcing, _warn_gating_inactive
from traceforge.gate.external import HttpGate, SubprocessGate
from traceforge.governance.pipeline import GovernancePipeline
from traceforge.governance.shield import build_policy_from_config
from traceforge.sdk.gate_policy import GatePolicy
from traceforge.sdk.gate_types import PostflightAction, PostflightVerdict
from traceforge.sdk.verdict import Verdict

_DENY_MARKER = "blocked by dotted preflight gate"
_SUPPRESS_MARKER = "stripped by dotted postflight gate"


# ── dotted gates referenced from config (imported via importlib) ────────────────


def deny_rm(request, ctx) -> Verdict:
    """In-process preflight gate: DENY the destructive ``rm`` tool, else ALLOW."""
    if "rm" in request.tool:
        return Verdict.deny(_DENY_MARKER)
    return Verdict.allow()


def allow_everything(request, ctx) -> Verdict:
    """In-process preflight gate that allows every call."""
    return Verdict.allow()


def suppress_postflight(result, ctx) -> PostflightVerdict:
    """In-process postflight gate that suppresses output."""
    return PostflightVerdict(action=PostflightAction.SUPPRESS, reason=_SUPPRESS_MARKER)


# ── build_policy_from_config: nothing declared → None ───────────────────────────


@pytest.mark.parametrize(
    "config",
    [
        None,
        {},
        {"governance": None},
        {"governance": {}},
        {"governance": {"gate_policy": {}}},
        {"governance": {"gate_policy": {"preflight": [], "postflight": [], "external": []}}},
    ],
)
def test_no_policy_config_returns_none(config) -> None:
    """A config that declares no gates yields ``None`` (allow-all → caller warns)."""
    assert build_policy_from_config(config) is None


# ── build_policy_from_config: full policy loads all gate kinds ───────────────────


def test_full_policy_loads_preflight_chain_postflight_and_external() -> None:
    """A full ``gate_policy`` block loads a preflight CHAIN (dotted + http +
    external subprocess), a postflight chain, and the ``external:`` bucket."""
    config = {
        "governance": {
            "gate_policy": {
                "preflight": [
                    "tests.unit.test_watch_gating.deny_rm",  # dotted in-process
                    {  # inline external HTTP decider
                        "type": "http",
                        "endpoint": "http://127.0.0.1:8181/v1/data/traceforge/gate",
                    },
                ],
                "postflight": ["tests.unit.test_watch_gating.suppress_postflight"],
                "external": [  # convenience bucket, appended to the preflight chain
                    {"type": "subprocess", "command": "opa eval -I -f raw data.gate.deny"},
                ],
            }
        }
    }

    policy = build_policy_from_config(config)

    assert policy is not None
    assert policy.has_preflight and policy.has_postflight
    # preflight chain = dotted + http + subprocess(external bucket) = 3, in order.
    pre = policy.preflight_gates
    assert len(pre) == 3
    assert pre[0] is deny_rm
    assert isinstance(pre[1], HttpGate)
    assert isinstance(pre[2], SubprocessGate)
    # postflight chain = the one dotted gate.
    post = policy.postflight_gates
    assert len(post) == 1
    assert post[0] is suppress_postflight


def test_full_policy_external_gates_are_fail_closed_by_default() -> None:
    """External gates built from config inherit the fail-CLOSED default posture."""
    config = {
        "governance": {
            "gate_policy": {
                "preflight": [
                    {"type": "http", "endpoint": "http://127.0.0.1:9/x"},
                    {"type": "subprocess", "command": "decide.sh"},
                ]
            }
        }
    }

    http_gate, subprocess_gate = build_policy_from_config(config).preflight_gates

    assert http_gate.fail_open is False
    assert subprocess_gate.fail_open is False


# ── build_policy_from_config: the loaded policy actually ENFORCES ────────────────


def test_config_policy_enforces_preflight_and_postflight() -> None:
    """A config-declared dotted preflight + postflight policy enforces end to end
    through a real pipeline: preflight DENIES ``rm``, ALLOWS reads, and the
    postflight gate's action is applied."""
    config = {
        "governance": {
            "gate_policy": {
                "preflight": ["tests.unit.test_watch_gating.deny_rm"],
                "postflight": ["tests.unit.test_watch_gating.suppress_postflight"],
            }
        }
    }
    policy = build_policy_from_config(config)
    pipeline = GovernancePipeline.create(policy=policy)

    # Preflight DENY on the destructive tool.
    _, deny_v = pipeline._score_and_gate_preflight(
        {"tool_name": "rm", "tool_input": {"path": "/"}, "session_id": "s1"}
    )
    assert deny_v.denied
    assert _DENY_MARKER in deny_v.reason

    # Preflight ALLOW on a benign tool, then the postflight gate suppresses output.
    trace, allow_v = pipeline._score_and_gate_preflight(
        {"tool_name": "read_file", "tool_input": {"path": "/tmp/x"}, "session_id": "s1"}
    )
    assert allow_v.allowed
    pv = pipeline._enforce_postflight(trace, session_id="s1", output={"result": "secret"})
    assert pv.action == PostflightAction.SUPPRESS
    assert _SUPPRESS_MARKER in pv.reason


def test_external_gate_from_config_enforces_and_fails_closed() -> None:
    """An external subprocess gate declared in config is wired into enforcement
    and DENIES fail-closed on a broken decider (empty command → no spawn).

    This proves the *external* leg of a config policy enforces, deterministically
    and cross-platform, with no network and no child process."""
    config = {
        "governance": {
            "gate_policy": {
                # empty command → SubprocessGate can't launch → fail-closed DENY.
                "external": [{"type": "subprocess", "command": ""}],
            }
        }
    }
    policy = build_policy_from_config(config)
    assert isinstance(policy.preflight_gates[0], SubprocessGate)

    pipeline = GovernancePipeline.create(policy=policy)
    _, verdict = pipeline._score_and_gate_preflight(
        {"tool_name": "read_file", "tool_input": {}, "session_id": "s1"}
    )
    assert verdict.denied
    assert "fail-closed" in verdict.reason


def test_legacy_single_field_forms_still_honored() -> None:
    """Back-compat: the legacy ``tool_preflight_gate`` (dotted) single-field form is
    still honored when no ``gate_policy`` block is present."""
    config = {"governance": {"tool_preflight_gate": "tests.unit.test_watch_gating.deny_rm"}}

    policy = build_policy_from_config(config)

    assert policy is not None
    assert policy.preflight_gates == (deny_rm,)


def test_malformed_policy_raises_rather_than_silently_allowing() -> None:
    """A *declared but broken* policy must fail loudly (so watch refuses to start),
    never silently degrade to allow-all."""
    config = {
        "governance": {
            "gate_policy": {"preflight": ["tests.unit.test_watch_gating.does_not_exist"]}
        }
    }
    with pytest.raises((ImportError, AttributeError)):
        build_policy_from_config(config)


# ── enforce-by-default warning ──────────────────────────────────────────────────


def test_policy_is_enforcing_reflects_registered_gates() -> None:
    """``_policy_is_enforcing`` is True iff the policy gates something."""
    assert _policy_is_enforcing(None) is False
    assert _policy_is_enforcing(GatePolicy()) is False  # empty policy = allow-all
    assert _policy_is_enforcing(GatePolicy().preflight(allow_everything)) is True
    assert _policy_is_enforcing(GatePolicy().postflight(suppress_postflight)) is True


def test_no_policy_config_is_not_enforcing() -> None:
    """The no-policy config path resolves to a non-enforcing (warn) state."""
    assert _policy_is_enforcing(build_policy_from_config({})) is False


def test_warn_gating_inactive_emits_loud_banner(capsys: pytest.CaptureFixture) -> None:
    """The inactive-gating warning is a prominent stderr banner that names the
    allow-all risk and tells the operator how to enable enforcement."""
    _warn_gating_inactive()  # must not raise

    err = capsys.readouterr().err
    assert "INACTIVE" in err
    assert "allow-all" in err
    assert "ALLOWED" in err
    # Actionable: points at the config key that turns enforcement on.
    assert "gate_policy" in err
    assert "preflight" in err
