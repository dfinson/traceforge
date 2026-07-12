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
from traceforge.dashboard import api, repository
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
    assert run["usage"] == {
        "in": 1000,
        "out": 500,
        "cost": 0.9,
        "aiuNano": None,
        "premiumRequests": None,
        "inputUncached": None,
        "cacheRead": None,
        "cacheCreation": None,
        "requestsTotal": None,
        "models": [
            {
                "model": "gpt-4o",
                "aiuNano": None,
                "premiumRequests": None,
                "requests": None,
                "inputUncached": None,
                "cacheRead": None,
                "cacheCreation": None,
                "input": 1000,
                "output": 500,
            }
        ],
    }
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
    assert run["usage"] == {
        "in": 1000,
        "out": 500,
        "cost": 0.9,
        "aiuNano": None,
        "premiumRequests": None,
        "inputUncached": None,
        "cacheRead": None,
        "cacheCreation": None,
        "requestsTotal": None,
        "models": [
            {
                "model": "gpt-4o",
                "aiuNano": None,
                "premiumRequests": None,
                "requests": None,
                "inputUncached": None,
                "cacheRead": None,
                "cacheCreation": None,
                "input": 1000,
                "output": 500,
            }
        ],
    }
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
    # Its wire cost is absent, so event cost projects None (unknown, → "—"),
    # never a fabricated 0.0 — the inspector must not imply a real zero dollars.
    assert run["events"][1]["cost"] is None


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


def _bare_event_for(session_id: str, ts: datetime) -> SessionEvent:
    """A minimal low-risk event for an arbitrary session (build_run needs ≥1 event)."""
    risk = RiskAssessment(
        score=4, level="safe", confidence="low", factors=(), mitre=(), version="risk-v2"
    )
    gov = SessionMeta(classification=None, risk_assessment=risk)
    return SessionEvent(
        kind=EventKind.TOOL_CALL_STARTED,
        session_id=session_id,
        timestamp=ts,
        payload={"tool_name": "git"},
        metadata=EventMetadata(governance=gov),
    )


async def _seed_premium(db: Path) -> None:
    """Seed Copilot-shaped runs: no dollars, premium-request COUNTS in attributes."""
    sink = SqliteOutputSink(path=str(db))
    # Run with premium activity across two models: counts 12 + 3 = 15, cost NULL.
    for model, premium, total in (("claude-opus-4.6", 12, 22), ("claude-opus-4.6", 3, 34)):
        await sink.on_event(_bare_event_for("cop-premium", _T0))
        await sink.on_usage(
            UsageRecord(
                session_id="cop-premium",
                timestamp=_T0,
                model=model,
                input_tokens=100,
                output_tokens=20,
                cost_usd=None,
                attributes={
                    "input_uncached": 100,
                    "cache_read_tokens": 0,
                    "cache_creation_tokens": 0,
                    "premium_requests": premium,
                    "requests_total": total,
                },
            )
        )
    # Run that genuinely made ZERO premium requests (included model) — a real 0.
    await sink.on_event(_bare_event_for("cop-zero", _T0))
    await sink.on_usage(
        UsageRecord(
            session_id="cop-zero",
            timestamp=_T0,
            model="claude-haiku-4.5",
            input_tokens=100,
            output_tokens=20,
            cost_usd=None,
            attributes={
                "input_uncached": 100,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "premium_requests": 0,
                "requests_total": 38,
            },
        )
    )
    await sink.close()


async def test_premium_requests_surface_with_null_cost_and_zero_distinct(tmp_path: Path) -> None:
    """cost stays None (unknown), while the premium-request COUNT is surfaced.

    Also guards the unknown-vs-real-0 distinction: a run that never carries the
    premium key surfaces ``None`` (→ "—"), a run that genuinely made 0 premium
    requests surfaces ``0`` (→ "0 premium requests").
    """
    out = tmp_path / "traceforge.db"
    await _seed_premium(out)
    repo = DashboardRepository(DashboardPaths(output_db=out, system_db=tmp_path / "absent.db"))

    premium_run = repo.build_run("cop-premium")
    assert premium_run is not None
    # No dollars are ever fabricated from the premium count.
    assert premium_run["usage"]["cost"] is None
    # Premium counts sum across the run's models (12 + 3).
    assert premium_run["usage"]["premiumRequests"] == 15

    zero_run = repo.build_run("cop-zero")
    assert zero_run is not None
    assert zero_run["usage"]["cost"] is None
    # A genuine zero — distinct from unknown.
    assert zero_run["usage"]["premiumRequests"] == 0

    # And via the list/summary path, the same honesty holds.
    summaries = {r["id"]: r for r in repo.list_runs()}
    assert summaries["cop-premium"]["usage"]["premiumRequests"] == 15
    assert summaries["cop-premium"]["usage"]["cost"] is None
    assert summaries["cop-zero"]["usage"]["premiumRequests"] == 0


async def _seed_mixed(db: Path) -> None:
    """One run mixing every usage shape: multi-model Copilot rows carrying AIU
    (nano-AIU) plus the five token/premium attribute keys, a genuine-zero-premium
    model, a blank-model row (real tokens, unattributable, and — critically — no
    ``nano_aiu`` key so its AIU stays unknown even though its premium is a real 0),
    and a non-Copilot row with no attributes at all. The three Copilot models carry
    the real per-session ``totalNanoAiu`` values (66450771325000 + 77277060000 +
    68365100000 = 66596413485000 → 66,596.4 AIU) so the run-level sum reconstructs a
    real session total."""
    sink = SqliteOutputSink(path=str(db))
    await sink.on_event(_bare_event_for("mix", _T0))
    rows = [
        # Two opus rows → premium 600+9=609, requests 800+10=810, cacheRead 800+180=980,
        # AIU 66450771325000+77277060000=66528048385000.
        (
            "claude-opus-4.6",
            1000,
            400,
            {
                "input_uncached": 30,
                "cache_read_tokens": 800,
                "cache_creation_tokens": 170,
                "nano_aiu": 66450771325000,
                "premium_requests": 600,
                "requests_total": 800,
            },
        ),
        (
            "claude-opus-4.6",
            200,
            100,
            {
                "input_uncached": 9,
                "cache_read_tokens": 180,
                "cache_creation_tokens": 11,
                "nano_aiu": 77277060000,
                "premium_requests": 9,
                "requests_total": 10,
            },
        ),
        # A model that genuinely made ZERO premium requests — a real 0, not unknown —
        # but DID consume AIU (68365100000 nano).
        (
            "claude-haiku-4.5",
            100,
            20,
            {
                "input_uncached": 100,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "nano_aiu": 68365100000,
                "premium_requests": 0,
                "requests_total": 38,
            },
        ),
        # Blank model: real tokens the wire couldn't attribute — kept, not dropped.
        # It carries a genuine-0 premium but NO ``nano_aiu`` key, proving AIU stays
        # unknown (None) per-key independently of the premium signal.
        (
            "",
            50,
            10,
            {
                "input_uncached": 50,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "premium_requests": 0,
                "requests_total": 5,
            },
        ),
    ]
    for model, in_tok, out_tok, attrs in rows:
        await sink.on_usage(
            UsageRecord(
                session_id="mix",
                timestamp=_T0,
                model=model,
                input_tokens=in_tok,
                output_tokens=out_tok,
                cost_usd=None,
                attributes=attrs,
            )
        )
    # Non-Copilot row: no attributes at all → every count field stays unknown (None).
    await sink.on_usage(
        UsageRecord(
            session_id="mix",
            timestamp=_T0,
            model="gpt-4o",
            input_tokens=300,
            output_tokens=60,
            cost_usd=None,
        )
    )
    await sink.close()


async def test_usage_breakdown_multi_model_cache_and_null_vs_zero(tmp_path: Path) -> None:
    """The full null-aware usage breakdown: per-model attribution, run-level AIU,
    cache and premium sums, blank-model retention, and unknown (None) staying
    distinct from a genuine 0 within the same run — per key independently."""
    out = tmp_path / "traceforge.db"
    await _seed_mixed(out)
    repo = DashboardRepository(DashboardPaths(output_db=out, system_db=tmp_path / "absent.db"))

    run = repo.build_run("mix")
    assert run is not None
    usage = run["usage"]

    # Token grand totals always coalesce to numbers; dollars stay unknown (Copilot).
    assert usage["in"] == 1650
    assert usage["out"] == 590
    assert usage["cost"] is None
    # AIU is the PRIMARY billing signal: per-model nano-AIU summed null-until-seen.
    # The blank-model row carries no nano_aiu key and the gpt-4o row no attributes,
    # so both contribute nothing — the sum is exactly the three Copilot models, which
    # reconstructs the real session total (÷1e9 → 66,596.4 AIU).
    assert usage["aiuNano"] == 66596413485000
    assert round(usage["aiuNano"] / 1e9, 1) == 66596.4
    # Run-level counts sum only rows that carry the key (the no-attrs gpt-4o row
    # contributes nothing rather than a fabricated 0).
    assert usage["premiumRequests"] == 609
    assert usage["inputUncached"] == 189
    assert usage["cacheRead"] == 980
    assert usage["cacheCreation"] == 181
    assert usage["requestsTotal"] == 853

    by_model = {m["model"]: m for m in usage["models"]}
    # Blank-model row is kept (its tokens are real) — never dropped.
    assert set(by_model) == {"claude-opus-4.6", "claude-haiku-4.5", "", "gpt-4o"}
    assert by_model["claude-opus-4.6"] == {
        "model": "claude-opus-4.6",
        "aiuNano": 66528048385000,
        "premiumRequests": 609,
        "requests": 810,
        "inputUncached": 39,
        "cacheRead": 980,
        "cacheCreation": 181,
        "input": 1200,
        "output": 500,
    }
    # Genuine zero premium — a real 0, distinct from unknown — but real AIU consumed.
    assert by_model["claude-haiku-4.5"]["premiumRequests"] == 0
    assert by_model["claude-haiku-4.5"]["aiuNano"] == 68365100000
    assert by_model[""]["inputUncached"] == 50
    assert by_model[""]["input"] == 50
    # Blank model made a real 0 premium requests yet its AIU stays UNKNOWN (None):
    # the two signals are tracked per-key, never conflated.
    assert by_model[""]["premiumRequests"] == 0
    assert by_model[""]["aiuNano"] is None
    # Non-Copilot model carries no attributes → every count is unknown (None),
    # never coerced to 0; its tokens are still real numbers.
    gpt = by_model["gpt-4o"]
    assert gpt["aiuNano"] is None
    assert gpt["premiumRequests"] is None
    assert gpt["requests"] is None
    assert gpt["inputUncached"] is None
    assert gpt["cacheRead"] is None
    assert gpt["cacheCreation"] is None
    assert gpt["input"] == 300
    assert gpt["output"] == 60

    # Event with no wire cost projects None (unknown, → "—"), never 0.0.
    assert run["events"][0]["cost"] is None

    # The list/summary path builds the identical breakdown (parity of both sites).
    summary = {r["id"]: r for r in repo.list_runs()}["mix"]
    assert summary["usage"]["aiuNano"] == 66596413485000
    assert summary["usage"]["premiumRequests"] == 609
    assert summary["usage"]["cacheRead"] == 980
    assert summary["usage"]["cost"] is None
    assert {m["model"]: m["aiuNano"] for m in summary["usage"]["models"]} == {
        "claude-opus-4.6": 66528048385000,
        "claude-haiku-4.5": 68365100000,
        "": None,
        "gpt-4o": None,
    }
    assert {m["model"]: m["premiumRequests"] for m in summary["usage"]["models"]} == {
        "claude-opus-4.6": 609,
        "claude-haiku-4.5": 0,
        "": 0,
        "gpt-4o": None,
    }


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


# --- fleet-path connection-open guard (regression for #153) -------------------


async def _seed_many_output(db: Path, ids: list[str]) -> None:
    """One event + one usage record per session, via the real output sink."""
    sink = SqliteOutputSink(path=str(db))
    for i, sid in enumerate(ids):
        ts = _T0 + timedelta(seconds=i)
        await sink.on_event(
            SessionEvent(
                kind=EventKind.MESSAGE_USER,
                session_id=sid,
                timestamp=ts,
                payload={"tool_name": "bash"},
            )
        )
        await sink.on_usage(
            UsageRecord(
                session_id=sid,
                timestamp=ts,
                model="gpt-4o",
                input_tokens=10,
                output_tokens=5,
                cost_usd=0.01,
            )
        )
    await sink.close()


def _seed_many_system(db: Path, ids: list[str]) -> None:
    """A finalized summary + taint/trust row per session (exercises sys helpers)."""
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
    for sid in ids:
        conn.execute(
            "INSERT INTO session_summaries VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                sid,
                "acme/widgets",
                "copilot/gpt-4o",
                _T0.isoformat(),
                _T0.isoformat(),
                1,
                0,
                None,
                None,
                0.1,
            ),
        )
        conn.execute(
            "INSERT INTO taint_entries VALUES (?,?,?,?,?,?,?)",
            (sid, 0, "e1", "k1", "restricted", "user_input", "/arguments"),
        )
        conn.execute(
            "INSERT INTO trust_grants VALUES (?,?,?,?,?,?)",
            (sid, 0, "deploy-key", _T0.isoformat(), 3600.0, "approved"),
        )
    conn.commit()
    conn.close()


async def test_get_runs_opens_constant_connections_regardless_of_run_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fleet path must not fan out connections per run.

    Regression guard for the ``GET /api/runs`` N+1 connection blow-up (#153): the
    old code opened ~8 read-only connections per run (build_run + identity/taint/
    trust/model each opening their own), so a 40-run fleet cost ~300 opens and
    ~27s. Assemble a many-run fleet and assert the total number of ``_connect_ro``
    calls is a small constant, independent of the run count.
    """
    ids = [f"sess-{i:03d}" for i in range(40)]
    out, sysdb = tmp_path / "traceforge.db", tmp_path / "system.db"
    await _seed_many_output(out, ids)
    _seed_many_system(sysdb, ids)
    repo = DashboardRepository(DashboardPaths(output_db=out, system_db=sysdb))

    opens = 0
    real_connect = repository._connect_ro

    def _counting_connect(path: Path) -> sqlite3.Connection:
        nonlocal opens
        opens += 1
        return real_connect(path)

    monkeypatch.setattr(repository, "_connect_ro", _counting_connect)

    runs = api.get_runs(repo, None, {})  # type: ignore[arg-type]

    # Correctness: every run assembled, identity + model resolved via shared conns.
    assert len(runs) == len(ids)
    assert {r["id"] for r in runs} == set(ids)
    assert all(r["model"] == "gpt-4o" for r in runs)
    assert all(r["repo"] == "acme/widgets" for r in runs)
    assert all(len(r["taint"]) == 1 and len(r["trust"]) == 1 for r in runs)
    # O(1) opens: list_run_ids (1) + has_system_memory (1) + shared output (1) +
    # shared system (1) = 4. The pre-fix fan-out would be in the hundreds.
    assert opens <= 6, f"expected a small constant number of opens, got {opens} for {len(ids)} runs"


# ─── transcript (per-run full-text reading view) ─────────────────────────────

TID = "sess-transcript"
# Deliberately longer than _snippet's 140-char cap: proves the transcript keeps the
# full body where the timeline summary would truncate + collapse newlines.
_LONG_ASSISTANT = (
    "I looked at the login handler and found the bug: the session token is compared "
    "with == instead of a constant-time check, and the expiry is read in seconds but "
    "compared against milliseconds.\n\nHere is the plan:\n1. Use hmac.compare_digest\n"
    "2. Normalise the expiry units\n3. Add a regression test for an expired token."
)


async def _seed_transcript(db: Path) -> None:
    """A run mixing user / assistant / system messages and a tool call with output."""
    sink = SqliteOutputSink(path=str(db))
    await sink.on_event(
        SessionEvent(
            kind=EventKind.MESSAGE_USER,
            session_id=TID,
            timestamp=_T0,
            payload={"content": "Please fix the login bug"},
        )
    )
    await sink.on_event(
        SessionEvent(
            kind=EventKind.MESSAGE_ASSISTANT,
            session_id=TID,
            timestamp=_T0 + timedelta(seconds=1),
            payload={"text": _LONG_ASSISTANT},
        )
    )
    await sink.on_event(
        SessionEvent(
            kind=EventKind.MESSAGE_SYSTEM,
            session_id=TID,
            timestamp=_T0 + timedelta(seconds=2),
            payload={"message": "context window trimmed"},
        )
    )
    await sink.on_event(
        SessionEvent(
            kind=EventKind.TOOL_CALL_STARTED,
            session_id=TID,
            timestamp=_T0 + timedelta(seconds=3),
            payload={"tool_name": "bash", "command": "rm -rf /tmp/x", "output": "removed"},
        )
    )
    await sink.close()


async def test_build_transcript_maps_roles_labels_and_order(tmp_path: Path) -> None:
    out = tmp_path / "traceforge.db"
    await _seed_transcript(out)
    repo = DashboardRepository(DashboardPaths(output_db=out, system_db=tmp_path / "absent.db"))

    transcript = repo.build_transcript(TID)
    assert transcript is not None
    assert transcript["id"] == TID
    turns = transcript["turns"]
    # One turn per event, in chronological order.
    assert [t["role"] for t in turns] == ["user", "assistant", "system", "tool"]
    assert [t["kind"] for t in turns] == [
        "message.user",
        "message.assistant",
        "message.system",
        "tool.call.started",
    ]
    # Message turns fall back to a readable role label (no tool name); the tool turn
    # carries its tool name.
    assert [t["label"] for t in turns] == ["User", "Assistant", "System", "bash"]
    assert all("id" in t and "t" in t for t in turns)


async def test_build_transcript_keeps_full_untruncated_text(tmp_path: Path) -> None:
    out = tmp_path / "traceforge.db"
    await _seed_transcript(out)
    repo = DashboardRepository(DashboardPaths(output_db=out, system_db=tmp_path / "absent.db"))

    turns = repo.build_transcript(TID)["turns"]  # type: ignore[index]
    # The assistant body is returned in full, newlines preserved — not the 140-char
    # collapsed preview the timeline uses.
    assert turns[1]["text"] == _LONG_ASSISTANT
    assert len(turns[1]["text"]) > 140
    assert "\n" in turns[1]["text"]
    # A tool call renders its invocation then its output, in that order.
    assert turns[3]["text"] == "rm -rf /tmp/x\n\nremoved"


async def test_build_transcript_unknown_session_returns_none(tmp_path: Path) -> None:
    out = tmp_path / "traceforge.db"
    await _seed_transcript(out)
    repo = DashboardRepository(DashboardPaths(output_db=out, system_db=tmp_path / "absent.db"))
    assert repo.build_transcript("does-not-exist") is None
