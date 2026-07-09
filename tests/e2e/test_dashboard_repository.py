"""End-to-end tests for :mod:`traceforge.dashboard.repository`.

Seeds a temp **output-sink DB** through the *real* :class:`SqliteOutputSink` (so
the parser is validated against the exact serialization production writes) and a
temp **system.db** through hand-written SQL matching the Alembic ``0001_initial``
schema, then drives :class:`DashboardRepository` read-only over both. Also covers
the degraded / partial-data modes (no system.db, no output DB).

These are marked ``e2e`` because they touch real SQLite files via the sink.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from traceforge.classify.core import Classification
from traceforge.classify.risk import RiskAssessment
from traceforge.dashboard.repository import DashboardPaths, DashboardRepository
from traceforge.governance.envelope import ContextGapEvent, EnrichedEvent
from traceforge.governance.mcp_drift import MCPIntegrityAlert
from traceforge.governance.results import (
    Evidence,
    EvidencePointer,
    RecommendedAction,
    RiskRecommendation,
    SessionMeta,
)
from traceforge.sinks.sqlite_output import SqliteOutputSink
from traceforge.types import (
    EventKind,
    EventMetadata,
    SessionEvent,
    TitleUpdate,
    UsageRecord,
)

pytestmark = pytest.mark.e2e

SID = "sess-1"
_T0 = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _rich_event(ts: datetime) -> SessionEvent:
    """A fully governance-stamped tool event (evidence + MCP alert + phase)."""
    cls = Classification(mechanism="shell.execute", effect="destructive")
    risk = RiskAssessment(
        score=88,
        level="critical",
        confidence="high",
        factors=("shell_execute",),
        mitre=("T1059",),
        version="risk-v2",
    )
    rec = RiskRecommendation(
        recommended_action=RecommendedAction("deny"),
        assessment=risk,
        reason_code="rule.block",
        canonical_id="cid-1",
        message="Blocked: destructive shell command",
    )
    evidence = Evidence(
        canonical_id="cid-1",
        timestamp=ts,
        session_id=SID,
        mechanism="shell.execute",
        effect="destructive",
        scope=("fs",),
        role=("tool",),
        action=("delete",),
        capability=("fs.write",),
        structure=(),
        source_labels=(),
        recommended_action=RecommendedAction("deny"),
        risk_score=88,
        risk_factors=("shell_execute", "recursive_delete"),
        mitre_techniques=("T1059",),
        pointers=(
            EvidencePointer(
                event_id="e1", rule_id="rule.block", detector="regex", payload_pointer="/arguments"
            ),
        ),
        rule_id="rule.block",
        matched_predicates=("cmd matches rm -rf",),
    )
    mcp = (
        MCPIntegrityAlert(
            tool_name="rm",
            server="filesys",
            alert_type="effect_escalation",
            previous="read_only",
            current="destructive",
            severity="critical",
            timestamp=ts,
        ),
    )
    gov = SessionMeta(
        classification=cls,
        risk_assessment=risk,
        recommendation=rec,
        mcp_alerts=mcp,
        evidence=evidence,
    )
    meta = EventMetadata(
        source_framework="copilot",
        repo="acme/widgets",
        turn_id="t1",
        phase="implementation",
        activity_id="act-1",
        step_id="step-1",
        classification=cls,
        tool_display="rm -rf /tmp/x",
        duration_ms=1500.0,
        governance=gov,
    )
    return SessionEvent(
        kind=EventKind.TOOL_CALL_STARTED,
        session_id=SID,
        timestamp=ts,
        payload={
            "tool_name": "rm",
            "arguments": "-rf /tmp/x",
            "cost_usd": 0.12,
            "path": "src/app.py",
            "tokens": 40,
            "retry": False,
        },
        metadata=meta,
    )


def _bare_event(ts: datetime) -> SessionEvent:
    """A low-risk event with no governance evidence (allow verdict)."""
    risk = RiskAssessment(
        score=4, level="safe", confidence="low", factors=(), mitre=(), version="risk-v2"
    )
    rec = RiskRecommendation(
        recommended_action=RecommendedAction("allow"),
        assessment=risk,
        reason_code="ok",
        canonical_id="cid-2",
    )
    gov = SessionMeta(classification=None, risk_assessment=risk, recommendation=rec)
    meta = EventMetadata(phase="verification", turn_id="t2", governance=gov)
    return SessionEvent(
        kind=EventKind.TOOL_CALL_STARTED,
        session_id=SID,
        timestamp=ts,
        payload={"tool_name": "git"},
        metadata=meta,
    )


async def _seed_output(db: Path) -> None:
    sink = SqliteOutputSink(path=str(db))
    await sink.on_event(_rich_event(_T0))
    await sink.on_event(_bare_event(_T0 + timedelta(seconds=30)))
    await sink.on_usage(
        UsageRecord(
            session_id=SID,
            timestamp=_T0,
            model="gpt-4o",
            input_tokens=1000,
            output_tokens=500,
            cost_usd=0.9,
        )
    )
    await sink.on_title_update(
        TitleUpdate(
            session_id=SID, segment_id=SID, kind="session", title="Refactor auth", version=1
        )
    )
    await sink.on_title_update(
        TitleUpdate(
            session_id=SID,
            segment_id="act-1",
            kind="activity",
            title="Delete temp files",
            version=1,
        )
    )
    gap = ContextGapEvent(
        id="g-1",
        session_id=SID,
        timestamp=_T0 + timedelta(seconds=10),
        source_event_key="k1",
        dropped_count=3,
        first_dropped_sequence=1,
        last_dropped_sequence=3,
        gap_ordinal=0,
    )
    await sink.on_enriched_event(
        EnrichedEvent(event=gap, governance=SessionMeta(classification=None, risk_assessment=None))
    )
    await sink.close()


def _seed_system(db: Path) -> None:
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE session_summaries (
            session_id TEXT PRIMARY KEY, repo TEXT, agent_model TEXT, started_at TEXT NOT NULL,
            ended_at TEXT, total_events INTEGER, dropped_events INTEGER DEFAULT 0,
            budget_snapshot_json TEXT, recommendation_counts_json TEXT, drift_max REAL);
        CREATE TABLE session_state (session_id TEXT PRIMARY KEY, updated_at TEXT NOT NULL DEFAULT '');
        CREATE TABLE taint_entries (
            session_id TEXT NOT NULL, ordinal INTEGER NOT NULL, event_id TEXT NOT NULL,
            source_event_key TEXT NOT NULL, clearance TEXT NOT NULL, source TEXT NOT NULL,
            payload_pointer TEXT NOT NULL, PRIMARY KEY (session_id, ordinal));
        CREATE TABLE trust_grants (
            session_id TEXT NOT NULL, ordinal INTEGER NOT NULL, key TEXT NOT NULL,
            granted_at TEXT NOT NULL, ttl_seconds REAL NOT NULL, reason TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (session_id, ordinal));
        """
    )
    conn.execute(
        "INSERT INTO session_summaries VALUES (?,?,?,?,?,?,?,?,?,?)",
        (SID, "acme/widgets", "copilot/gpt-4o", _T0.isoformat(), None, 2, 0, None, None, 0.42),
    )
    conn.execute("INSERT INTO session_state (session_id) VALUES (?)", (SID,))
    conn.execute(
        "INSERT INTO taint_entries VALUES (?,?,?,?,?,?,?)",
        (SID, 0, "e1", "k1", "restricted", "user_input", "/arguments"),
    )
    conn.execute(
        "INSERT INTO trust_grants VALUES (?,?,?,?,?,?)",
        (SID, 0, "deploy-key", datetime.now(timezone.utc).isoformat(), 3600.0, "approved"),
    )
    conn.commit()
    conn.close()


@pytest.fixture
async def full_repo(tmp_path: Path) -> DashboardRepository:
    """A repository wired to both a seeded output DB and a seeded system.db."""
    out, sysdb = tmp_path / "traceforge.db", tmp_path / "system.db"
    await _seed_output(out)
    _seed_system(sysdb)
    return DashboardRepository(DashboardPaths(output_db=out, system_db=sysdb))


async def test_health_reports_both_databases_present(full_repo: DashboardRepository) -> None:
    health = full_repo.health()
    assert health["has_output_db"] is True
    assert health["has_system_memory"] is True
    assert health["output_db"].endswith("traceforge.db")
    assert health["system_db"].endswith("system.db")


async def test_list_runs_summarizes_identity_and_usage(full_repo: DashboardRepository) -> None:
    runs = full_repo.list_runs()
    assert len(runs) == 1
    run = runs[0]
    assert run["id"] == SID
    assert run["repo"] == "acme/widgets"
    assert run["agent"] == "copilot"
    assert run["model"] == "gpt-4o"
    assert run["title"] == "Refactor auth"
    assert run["live"] is True
    assert run["usage"] == {"in": 1000, "out": 500, "cost": 0.9}
    assert run["drift"] == 0.42
    assert run["peak"] == 3  # critical
    assert run["eventCount"] == 2
    assert run["durMs"] == 30000.0


async def test_build_run_core_fields(full_repo: DashboardRepository) -> None:
    run = full_repo.build_run(SID)
    assert run is not None
    assert run["id"] == SID
    assert run["repo"] == "acme/widgets"
    assert run["model"] == "gpt-4o"
    assert run["live"] is True
    assert run["drift"] == 0.42
    assert run["peak"] == 3
    assert run["usage"] == {"in": 1000, "out": 500, "cost": 0.9}
    assert len(run["events"]) == 2


async def test_build_run_maps_first_event_richly(full_repo: DashboardRepository) -> None:
    run = full_repo.build_run(SID)
    assert run is not None
    ev = run["events"][0]
    assert ev["tool"] == {"n": "rm", "cat": "destructive", "canon": "shell.execute", "w": 0}
    assert ev["risk"] == 3
    assert ev["score"] == 0.88
    assert ev["action"] == "deny"
    assert ev["cost"] == 0.12
    assert ev["tokens"] == 40
    assert ev["dur"] == 1500.0
    assert ev["phase"] == "implementation"
    assert ev["seg"] == "step-1"
    assert ev["file"] == "src/app.py"
    assert ev["turn"] == "t1"
    assert ev["cls"]["conf"] == 0.95
    assert ev["reco"] == {"action": "deny", "why": "Blocked: destructive shell command"}
    assert ev["ev"]["mitre"] == ["T1059", "Command and Scripting Interpreter"]
    assert ev["ev"]["preds"] == ["cmd matches rm -rf"]
    assert ev["ev"]["ptr"] == "/arguments"
    # The low-risk second event carries no evidence.
    assert run["events"][1]["ev"] is None


async def test_build_run_governance_memory_from_system_db(full_repo: DashboardRepository) -> None:
    run = full_repo.build_run(SID)
    assert run is not None
    assert run["taint"] == [{"flow": "user_input → restricted", "det": "/arguments", "lvl": 1}]
    assert run["trust"][0]["who"] == "deploy-key"
    assert run["trust"][0]["ttl"].endswith("left")
    assert run["mcp"] == [
        {"srv": "filesys", "msg": "rm: effect escalation (read_only → destructive)", "lvl": 2}
    ]


async def test_build_run_segments_and_gaps(full_repo: DashboardRepository) -> None:
    run = full_repo.build_run(SID)
    assert run is not None
    segs = {s["id"]: s for s in run["segs"]}
    assert segs[SID]["kind"] == "session"
    assert segs[SID]["risk"] == 3
    assert segs["act-1"]["title"] == "Delete temp files"
    assert run["segs"][0]["kind"] == "session"  # session ordered first
    assert run["gaps"] == [
        {"t": "2024-06-01T12:00:10+00:00", "dropped": 3, "reason": "backpressure"}
    ]


async def test_build_run_unknown_session_returns_none(full_repo: DashboardRepository) -> None:
    assert full_repo.build_run("does-not-exist") is None


async def test_degraded_mode_without_system_db(tmp_path: Path) -> None:
    out = tmp_path / "traceforge.db"
    await _seed_output(out)
    repo = DashboardRepository(DashboardPaths(output_db=out, system_db=tmp_path / "absent.db"))

    assert repo.has_output_db() is True
    assert repo.has_system_memory() is False

    run = repo.build_run(SID)
    assert run is not None
    # Identity falls back to per-event metadata (output DB carries repo/framework/model).
    assert run["repo"] == "acme/widgets"
    assert run["agent"] == "copilot"
    assert run["model"] == "gpt-4o"
    # Cross-session governance memory is unavailable.
    assert run["drift"] is None
    assert run["live"] is False
    assert run["taint"] == []
    assert run["trust"] == []
    # MCP alerts are stamped per-event in the output DB, so they survive degraded mode.
    assert len(run["mcp"]) == 1


async def test_health_without_output_db(tmp_path: Path) -> None:
    repo = DashboardRepository(
        DashboardPaths(output_db=tmp_path / "none.db", system_db=tmp_path / "none-sys.db")
    )
    health = repo.health()
    assert health["has_output_db"] is False
    assert health["has_system_memory"] is False
