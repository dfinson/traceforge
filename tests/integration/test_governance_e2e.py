"""End-to-end governance integration tests against a real on-disk SQLite store.

Issue #27 — the final governance-backlog item. Unit coverage already exists per
subsystem in ``tests/unit/test_governance_*.py``; this suite is the END-TO-END
layer. It drives the REAL :class:`GovernancePipeline` (built via the explicit
constructor) against a REAL on-disk :class:`SystemStore` and asserts observable
governance outcomes across the whole wired chain — classification -> labeling/PII
-> risk -> rules -> recommendation -> evidence/escalation -> persistence ->
lifecycle — plus the persistence/restart guarantees that only a real DB can prove.

Every scenario is isolated (its own ``tmp_path`` DB, always closed) and
deterministic: fixed timezone-aware timestamps, stable event ids/keys, no
``time.sleep`` and no wall-clock-dependent assertions. Bounded-buffer behavior is
driven structurally (by count), never by timing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType

import pytest

from traceforge.classify.config import ClassificationEngine, ClassifyConfig
from traceforge.classify.core import Classification
from traceforge.governance.budget import BudgetThresholds, BudgetTracker
from traceforge.governance.drift import DriftDetector
from traceforge.governance.ifc import IFCChecker
from traceforge.governance.labeler import GovernanceLabeler
from traceforge.governance.mcp_drift import MCPIntegrityScanner
from traceforge.governance.persistence import SystemStore
from traceforge.governance.pii import PIIScanner
from traceforge.governance.pipeline import GovernancePipeline
from traceforge.governance.results import RecommendedAction
from traceforge.governance.rules import Predicate, Rule, TransformTemplate, parse_rules
from traceforge.governance.types import (
    EnrichmentContext,
    ToolCallEvent,
    compute_source_event_key,
)

# ─────────────────────────── shared helpers ────────────────────────────────
#
# A small, deterministic construction harness reused by every scenario. The
# fixed timestamp and per-event-unique ids/keys keep outcomes byte-stable and
# prevent accidental idempotency/span collisions between distinct events.

_TS = datetime(2024, 1, 1, tzinfo=timezone.utc)

_RULES_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "traceforge"
    / "classify"
    / "data"
    / "recommendation_rules.yaml"
)

# The classification engine is stateless across sessions (it only scores risk and
# holds no per-session state), so one shared instance keeps the suite fast.
_ENGINE = ClassificationEngine(ClassifyConfig())


def _bundled_rules() -> list[Rule]:
    """The production recommendation ruleset (first-match-wins)."""
    return parse_rules(_RULES_PATH)


def _classification(
    *,
    mechanism: str = "shell.execute",
    effect: str | None = None,
    scope: tuple[str, ...] = (),
    capability: tuple[str, ...] = (),
    structure: tuple[str, ...] = (),
) -> Classification:
    """Build a base classification; dims are authoritative (engine only scores)."""
    return Classification(
        mechanism=mechanism,
        effect=effect,
        scope=frozenset(scope),
        capability=frozenset(capability),
        structure=frozenset(structure),
    )


def _tool_call_event(
    *,
    session_id: str,
    event_id: str,
    args: str = "{}",
    tool_name: str = "bash",
    mcp_server_name: str | None = None,
    tool_description: str | None = None,
) -> ToolCallEvent:
    """A ToolCallEvent with a fixed timestamp and per-event-unique key/span."""
    return ToolCallEvent(
        event_id=event_id,
        session_id=session_id,
        timestamp=_TS,
        source_event_key=f"key-{event_id}",
        span_id=f"span-{event_id}",
        tool_name=tool_name,
        server_namespace=None,
        tool_args_json=args,
        source_event_id=None,
        mcp_server_name=mcp_server_name,
        tool_description=tool_description,
        tool_schema_json=None,
    )


def _ctx(event: ToolCallEvent, classification: Classification) -> EnrichmentContext:
    """Wrap an event + classification the way the intake layer would."""
    return EnrichmentContext(
        event=event,
        base_classification=classification,
        command_analysis=None,
        session_state=None,
        mcp_profiles=None,
        project_root=None,
        engine="shell",
        drift_baseline=None,
        mcp_profile_key=None,
    )


def _labeler(**collaborators) -> GovernanceLabeler:
    """Phase-2 labeler wired with only the collaborators a scenario needs."""
    return GovernanceLabeler(**collaborators)


def _pipeline(
    store: SystemStore,
    *,
    rules: list[Rule],
    labeler: GovernanceLabeler,
    thresholds: BudgetThresholds | None = None,
) -> GovernancePipeline:
    """Construct the real pipeline over an on-disk store (explicit composition root)."""
    return GovernancePipeline(
        store=store,
        labeler=labeler,
        budget_tracker=BudgetTracker(thresholds),
        rules=rules,
        engine=_ENGINE,
    )


def _rows(store: SystemStore, sql: str, params: tuple = ()) -> list:
    """Read-only introspection against the real DB (test-side, serialized on the lock)."""
    with store.lock:
        return store.connection.execute(sql, params).fetchall()


@pytest.fixture
def store(tmp_path):
    """A real on-disk SystemStore, migrated to head, closed on teardown."""
    s = SystemStore(tmp_path / "gov.db")
    yield s
    s.close()


# ───────────────────────────── 1. dup ──────────────────────────────────────


def test_dup_identical_outcome_and_no_double_side_effect(store):
    """Same event processed twice -> identical SessionMeta AND a single side effect."""
    pipeline = _pipeline(store, rules=_bundled_rules(), labeler=_labeler(pii_scanner=PIIScanner()))
    ctx = _ctx(
        _tool_call_event(session_id="s-dup", event_id="evt-dup", args='{"command": "rm -rf /"}'),
        _classification(effect="destructive", scope=("host",)),
    )

    meta1 = pipeline.process_event(ctx)
    meta2 = pipeline.process_event(ctx)  # identical source_event_key -> dedup path

    assert meta1.recommendation.recommended_action is RecommendedAction.DENY
    # Replay returns the persisted verdict verbatim.
    assert meta2.recommendation.recommended_action == meta1.recommendation.recommended_action
    assert meta2.recommendation.canonical_id == meta1.recommendation.canonical_id
    assert meta2.risk_assessment.score == meta1.risk_assessment.score

    # The destructive event was counted exactly once in the normalized DB counters,
    # proving the replay produced no second state mutation.
    assert store.get_budget_counters("s-dup")["effect"]["destructive"] == 1
    assert pipeline.get_or_create_state("s-dup").snapshot().budget.total_tool_calls == 1
    assert store.is_duplicate("key-evt-dup") is not None


# ─────────────────────────── 2. lifecycle ──────────────────────────────────


def test_lifecycle_duplicate_session_end_is_noop(store):
    """A duplicate session_end never resurrects evicted state or overwrites the summary."""
    pipeline = _pipeline(store, rules=_bundled_rules(), labeler=_labeler())
    sid = "s-life"

    pipeline.process_lifecycle(sid, "session_start")
    pipeline.process_event(
        _ctx(
            _tool_call_event(
                session_id=sid, event_id="evt-life", tool_name="cat", args='{"file": "a"}'
            ),
            _classification(effect="read_only"),
        )
    )
    end1 = pipeline.process_lifecycle(sid, "session_end")
    end2 = pipeline.process_lifecycle(sid, "session_end")  # duplicate delivery

    # First end finalizes against real state; the duplicate hits the idempotency
    # short-circuit which returns a zeroed no-op meta -> it never re-ran
    # finalization against a rehydrated (resurrected) session.
    assert end1.budget_snapshot.total_tool_calls == 1
    assert end2.budget_snapshot.total_tool_calls == 0

    # Summary written exactly once (INSERT OR IGNORE); the duplicate did not touch it.
    summary_rows = _rows(
        store, "SELECT total_events FROM session_summaries WHERE session_id = ?", (sid,)
    )
    assert len(summary_rows) == 1
    assert summary_rows[0][0] == 1

    end_key = compute_source_event_key(session_id=sid, event_kind="session_end")
    assert store.is_duplicate(end_key) is not None


# ─────────────────────────────── 3. rule ───────────────────────────────────


def test_rule_fires_with_evidence_provenance(store):
    """A real recommendation rule fires -> DENY with #25 Evidence provenance populated."""
    pipeline = _pipeline(store, rules=_bundled_rules(), labeler=_labeler(pii_scanner=PIIScanner()))
    meta = pipeline.process_event(
        _ctx(
            _tool_call_event(
                session_id="s-rule", event_id="evt-rule", args='{"command": "rm -rf /"}'
            ),
            _classification(effect="destructive", scope=("host",)),
        )
    )

    assert meta.recommendation.recommended_action is RecommendedAction.DENY
    assert meta.recommendation.reason_code == "destructive_host_or_network"
    assert meta.evidence is not None
    assert meta.evidence.rule_id == "destructive_host_network"
    assert meta.evidence.matched_predicates  # #25: serialized matched predicates


# ────────────────────────────── 4. budget ──────────────────────────────────


def test_budget_pressure_escalates_and_persists_counters(store):
    """A per-effect budget cap drives pressure into the recommendation; counters persist."""
    thresholds = BudgetThresholds(max_by_effect={"mutating": 2})
    pipeline = _pipeline(store, rules=_bundled_rules(), labeler=_labeler(), thresholds=thresholds)
    sid = "s-budget"

    meta1 = pipeline.process_event(
        _ctx(
            _tool_call_event(
                session_id=sid, event_id="evt-b1", tool_name="edit", args='{"command": "write a"}'
            ),
            _classification(effect="mutating"),
        )
    )
    meta2 = pipeline.process_event(
        _ctx(
            _tool_call_event(
                session_id=sid, event_id="evt-b2", tool_name="edit", args='{"command": "write b"}'
            ),
            _classification(effect="mutating"),
        )
    )

    # First mutating call is under the cap; the second trips pressure -> escalate.
    assert meta2.recommendation is not None
    assert meta2.recommendation.recommended_action is RecommendedAction.ESCALATE
    assert meta2.recommendation.reason_code == "budget_exceeded"
    assert "budget_pressure" in meta2.classification.capability
    assert meta1.recommendation is None or meta1.recommendation.reason_code != "budget_exceeded"

    # Dimensional counters durably landed in the normalized table.
    assert store.get_budget_counters(sid)["effect"]["mutating"] == 2


# ─────────────────────────────── 5. drift ──────────────────────────────────


def test_drift_phase_transition_flags_anomaly_and_denies(store):
    """A suspicious phase transition past warmup surfaces a drift signal end-to-end."""
    pipeline = _pipeline(
        store, rules=_bundled_rules(), labeler=_labeler(drift_detector=DriftDetector(store))
    )
    sid = "s-drift"

    # Four exploration (read_only) events clear the 5-event drift warmup.
    for i in range(4):
        pipeline.process_event(
            _ctx(
                _tool_call_event(
                    session_id=sid, event_id=f"evt-expl-{i}", tool_name="cat", args='{"file": "a"}'
                ),
                _classification(effect="read_only"),
            )
        )
    # A "testing" phase (mutating + a test-y tool name).
    pipeline.process_event(
        _ctx(
            _tool_call_event(
                session_id=sid,
                event_id="evt-test",
                tool_name="pytest",
                args='{"command": "pytest"}',
            ),
            _classification(effect="mutating"),
        )
    )
    # A destructive action with EMPTY scope, so the deny is attributable to drift
    # (drift_destructive) rather than the host/network scope rule.
    meta = pipeline.process_event(
        _ctx(
            _tool_call_event(
                session_id=sid,
                event_id="evt-dest",
                tool_name="shred",
                args='{"command": "shred x"}',
            ),
            _classification(effect="destructive"),
        )
    )

    assert meta.drift is not None
    assert meta.drift.anomaly is True
    assert meta.drift.risk_bonus >= 18  # ("testing" -> "destructive") transition bonus
    assert meta.drift.current_phase == "destructive"
    assert "phase_anomaly" in meta.classification.structure
    assert meta.recommendation.recommended_action is RecommendedAction.DENY
    assert meta.recommendation.reason_code == "drift_plus_destructive"


# ──────────────────────────────── 6. mcp ───────────────────────────────────


def test_mcp_profile_persists_then_fingerprint_drift_warns(store):
    """First sighting registers an MCP profile; a changed fingerprint later warns."""
    pipeline = _pipeline(
        store, rules=_bundled_rules(), labeler=_labeler(mcp_scanner=MCPIntegrityScanner(store))
    )
    sid = "s-mcp"

    meta1 = pipeline.process_event(
        _ctx(
            _tool_call_event(
                session_id=sid,
                event_id="evt-mcp-1",
                tool_name="query",
                mcp_server_name="db",
                tool_description="Run a read query",
            ),
            _classification(effect="read_only"),
        )
    )
    # First sighting registers, no alert; the profile is durably persisted.
    assert meta1.mcp_alerts == ()
    profile = store.get_mcp_profile("db", "query")
    assert profile is not None
    assert profile["registered_effect"] == "read_only"

    meta2 = pipeline.process_event(
        _ctx(
            _tool_call_event(
                session_id=sid,
                event_id="evt-mcp-2",
                tool_name="query",
                mcp_server_name="db",
                tool_description="Run a read query v2",  # fingerprint changed
            ),
            _classification(effect="read_only"),
        )
    )
    assert any(a.alert_type == "description_change" for a in meta2.mcp_alerts)
    assert "mcp_drift" in meta2.classification.capability
    assert meta2.recommendation.recommended_action is RecommendedAction.WARN
    assert meta2.recommendation.reason_code == "mcp_tool_fingerprint_changed"


# ──────────────────────────────── 7. pii ───────────────────────────────────


def test_pii_to_network_denies_with_tainted_flow(store):
    """PII in tool args flowing to a network-capable tool -> taint label + deny."""
    pipeline = _pipeline(store, rules=_bundled_rules(), labeler=_labeler(pii_scanner=PIIScanner()))
    meta = pipeline.process_event(
        _ctx(
            _tool_call_event(
                session_id="s-pii",
                event_id="evt-pii",
                tool_name="http",
                args='{"body": "user ssn 123-45-6789"}',
            ),
            _classification(effect="read_only", capability=("network_outbound",)),
        )
    )

    assert meta.recommendation.recommended_action is RecommendedAction.DENY
    assert meta.recommendation.reason_code == "pii_network_exfiltration"
    assert "pii_exposure" in meta.classification.capability
    assert "network_outbound" in meta.classification.capability
    assert "tainted_flow" in meta.classification.structure  # egress label on the SessionMeta


# ──────────────────────────────── 8. ifc ───────────────────────────────────


def test_ifc_secret_read_then_mutation_escalates(store):
    """Reading a SECRET-clearance file then mutating raises an IFC clearance violation."""
    pipeline = _pipeline(store, rules=_bundled_rules(), labeler=_labeler(ifc_checker=IFCChecker()))
    sid = "s-ifc"

    # Event A: read a SECRET file (.env) -> Phase-1 records taint on the ledger.
    pipeline.process_event(
        _ctx(
            _tool_call_event(
                session_id=sid, event_id="evt-ifc-read", tool_name="cat", args='{"path": ".env"}'
            ),
            _classification(effect="read_only"),
        )
    )
    taints = store.get_taint_entries(sid)
    assert any(t["clearance"] == "secret" for t in taints)

    # Event B: a mutating action while the session already carries taint -> violation.
    meta = pipeline.process_event(
        _ctx(
            _tool_call_event(
                session_id=sid,
                event_id="evt-ifc-write",
                tool_name="edit",
                args='{"path": "out.txt"}',
            ),
            _classification(effect="mutating"),
        )
    )
    # "ifc_violation" is a STRUCTURE label; "ifc:<clearance>" is the SOURCE label.
    assert "ifc_violation" in meta.classification.structure
    assert any(label.startswith("ifc:") for label in meta.classification.source_labels)
    assert meta.recommendation.recommended_action is RecommendedAction.ESCALATE
    assert meta.recommendation.reason_code == "ifc_clearance_violation"


# ─────────────────────────── 9. backpressure ───────────────────────────────


def test_backpressure_taint_ledger_bounded_fifo(store):
    """The taint ledger is a bounded FIFO ring buffer, in memory and in the DB."""
    pipeline = _pipeline(store, rules=_bundled_rules(), labeler=_labeler(ifc_checker=IFCChecker()))
    sid = "s-backpressure"
    total = 250  # structurally past the TAINT_LEDGER_MAX=200 bound (not timing-driven)

    for i in range(total):
        pipeline.process_event(
            _ctx(
                _tool_call_event(
                    session_id=sid,
                    event_id=f"evt-taint-{i:04d}",
                    tool_name="cat",
                    args='{"path": ".env"}',
                ),
                _classification(effect="read_only"),
            )
        )

    # In-memory ledger is bounded.
    snap = pipeline.get_or_create_state(sid).snapshot()
    assert len(snap.taint_ledger) == 200

    # The bound is durable and the eviction is FIFO (oldest 50 dropped), as reflected
    # by the normalized taint_entries ordered by append ordinal.
    entries = store.get_taint_entries(sid)
    assert len(entries) == 200
    assert [e["event_id"] for e in entries] == [f"evt-taint-{i:04d}" for i in range(50, 250)]


# ────────────────────────────── 10. restart ────────────────────────────────


def test_restart_recovers_state_and_dup_holds(tmp_path):
    """The real-SQLite payoff: reopen the same file and everything is recovered."""
    db_path = tmp_path / "gov.db"
    ctx = _ctx(
        _tool_call_event(
            session_id="s-restart",
            event_id="evt-restart",
            tool_name="query",
            mcp_server_name="vault",
            tool_description="read secret",
            args='{"path": ".env"}',
        ),
        _classification(effect="read_only"),
    )

    # ── First lifetime: populate state, budget, taint, MCP profile, processed marker.
    store1 = SystemStore(db_path)
    try:
        p1 = _pipeline(
            store1,
            rules=_bundled_rules(),
            labeler=_labeler(ifc_checker=IFCChecker(), mcp_scanner=MCPIntegrityScanner(store1)),
        )
        p1.process_event(ctx)
    finally:
        store1.close()

    # ── Second lifetime: a brand-new store + pipeline on the SAME file.
    store2 = SystemStore(db_path)
    try:
        # Recovered straight off disk, before any new processing.
        assert store2.get_budget_counters("s-restart")["effect"]["read_only"] == 1
        assert any(t["clearance"] == "secret" for t in store2.get_taint_entries("s-restart"))
        assert store2.get_mcp_profile("vault", "query") is not None

        p2 = _pipeline(
            store2,
            rules=_bundled_rules(),
            labeler=_labeler(ifc_checker=IFCChecker(), mcp_scanner=MCPIntegrityScanner(store2)),
        )
        # The registry rehydrates session state from the DB on first touch.
        rehydrated = p2.get_or_create_state("s-restart").snapshot()
        assert rehydrated.budget.total_tool_calls == 1
        assert len(rehydrated.taint_ledger) == 1

        # Dup idempotency survives the restart: the persisted processed-events marker
        # short-circuits a replay of the same event (fresh in-memory cache -> DB hit).
        p2.process_event(ctx)
        assert store2.get_budget_counters("s-restart")["effect"]["read_only"] == 1
        assert p2.get_or_create_state("s-restart").snapshot().budget.total_tool_calls == 1
    finally:
        store2.close()


# ──────────────────────────── 11. determinism ──────────────────────────────


def test_determinism_identical_outcome_across_fresh_pipelines(tmp_path):
    """Same input -> byte-identical governance outcome across two fresh DBs/pipelines."""
    rules = _bundled_rules()

    def run(db_name: str):
        s = SystemStore(tmp_path / db_name)
        try:
            pipeline = _pipeline(s, rules=rules, labeler=_labeler(pii_scanner=PIIScanner()))
            return pipeline.process_event(
                _ctx(
                    _tool_call_event(
                        session_id="s-det", event_id="evt-det", args='{"command": "rm -rf /"}'
                    ),
                    _classification(effect="destructive", scope=("host",)),
                )
            )
        finally:
            s.close()

    meta_a = run("a.db")
    meta_b = run("b.db")

    assert meta_a.risk_assessment.score == meta_b.risk_assessment.score
    assert meta_a.risk_assessment.level == meta_b.risk_assessment.level
    assert meta_a.recommendation.recommended_action == meta_b.recommendation.recommended_action
    assert meta_a.recommendation.canonical_id == meta_b.recommendation.canonical_id
    assert meta_a.evidence.rule_id == meta_b.evidence.rule_id
    assert meta_a.evidence.matched_predicates == meta_b.evidence.matched_predicates
    assert meta_a.evidence.risk_factors == meta_b.evidence.risk_factors

    # Non-vacuous: this is a real deny with populated evidence, not an empty equality.
    assert meta_a.recommendation.recommended_action is RecommendedAction.DENY
    assert meta_a.evidence.rule_id == "destructive_host_network"


# ───────────────────────────── 12. transform ───────────────────────────────


def test_transform_rule_emits_field_suggestion(store):
    """A field-style TRANSFORM rule emits a live TransformSuggestion through process_event."""
    field_rule = Rule(
        id="field-transform",
        index=0,
        when=(Predicate(dim="mechanism", operator="exact", target="shell.execute"),),
        recommend=RecommendedAction.TRANSFORM,
        reason="field_transform_test",
        transform=TransformTemplate(
            target_field="outer.secret",
            strategy="redact",
            parameters={"mask": "***"},
            description="redact secret",
        ),
    )
    pipeline = _pipeline(store, rules=[field_rule], labeler=_labeler())

    meta = pipeline.process_event(
        _ctx(
            _tool_call_event(
                session_id="s-transform",
                event_id="evt-transform",
                args='{"outer": {"secret": "hunter2"}}',
            ),
            _classification(mechanism="shell.execute", effect="mutating"),
        )
    )

    assert meta.recommendation is not None
    assert meta.recommendation.recommended_action is RecommendedAction.TRANSFORM
    sugg = meta.recommendation.transform
    assert sugg is not None
    # The render-time #21 detail fields (resolved against the event's own data).
    assert sugg.target_kind == "field"
    assert sugg.target_field == "outer.secret"
    assert sugg.strategy == "redact"
    assert sugg.original_value == "hunter2"
    assert isinstance(sugg.parameters, MappingProxyType)
    assert dict(sugg.parameters) == {"mask": "***"}


# ────────────────────── 13. escalation / evidence detail ────────────────────


def test_escalation_and_evidence_survive_real_db_round_trip(store):
    """#24 EscalationContext + #25 Evidence fields survive a real persisted round-trip."""
    pipeline = _pipeline(store, rules=_bundled_rules(), labeler=_labeler(pii_scanner=PIIScanner()))
    ctx = _ctx(
        _tool_call_event(session_id="s-esc", event_id="evt-esc", args='{"command": "rm -rf /"}'),
        _classification(effect="destructive", scope=("host",)),
    )

    meta1 = pipeline.process_event(ctx)  # in-memory: evidence + escalation freshly built
    meta2 = pipeline.process_event(ctx)  # dup -> deserialized from persisted JSON (real DB)

    assert meta1.recommendation.recommended_action is RecommendedAction.DENY

    evidence = meta2.evidence
    assert evidence is not None
    # #25 rule provenance survived the codec round-trip.
    assert evidence.rule_id == "destructive_host_network"
    assert evidence.matched_predicates
    assert evidence.matched_predicates == meta1.evidence.matched_predicates

    # #24 richer escalation context, recovered from the DB.
    escalation = evidence.escalation
    assert escalation is not None
    assert escalation.event_id == "evt-esc"
    assert escalation.classification_summary
    assert escalation.session_event_count == 1
    assert escalation.recent_phase_window == ("destructive",)
    assert escalation.risk_factors == meta1.evidence.escalation.risk_factors
    assert escalation.recent_phase_window == meta1.evidence.escalation.recent_phase_window
