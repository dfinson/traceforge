"""Read-only repository over traceforge's two SQLite databases.

The dashboard reads — and only ever reads — from:

* the **output-sink DB** (``SqliteOutputSink``, default ``~/.traceforge/traceforge.db``):
  ``enriched_events`` (the backbone), ``segment_titles``, ``context_gaps``,
  ``spans``, ``usage_records``, ``attribution_rollups``. This is the required source.
* the **system.db** (Alembic ``SystemStore``, default ``~/.traceforge/system.db``):
  ``session_summaries`` (identity/live/drift), ``session_state`` (live), and the
  governance *memory* tables ``taint_entries`` / ``trust_grants`` / ``mcp_profiles``.
  This is optional — its absence is the "SDK-embed / degraded" mode.

Every connection is opened ``mode=ro`` (read-only URI); this module never opens a
write connection and never mutates either database. The mapping helpers reshape
real rows into the shapes the ported frontend renders (see
``docs/dashboard-spec.md`` section 3, and ``dashboard/src/lib/types.ts``). Date
fields are emitted as ISO-8601 strings; the API client revives them into ``Date``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_OUTPUT_DB = Path.home() / ".traceforge" / "traceforge.db"
DEFAULT_SYSTEM_DB = Path.home() / ".traceforge" / "system.db"

# risk_level (TEXT) -> mock RiskLevel 0..3, index-aligned with RISK in types.ts.
_RISK_ORDER: dict[str, int] = {"safe": 0, "caution": 1, "danger": 2, "critical": 3}

# Categorical classification confidence -> the numeric bands Coverage expects
# (documented gap: real confidence is high/medium/low). See spec section 3.
_CONFIDENCE_NUM: dict[str, float] = {"high": 0.95, "medium": 0.8, "low": 0.6}

# Minimal ATT&CK id -> name for the techniques traceforge's risk rules emit; the
# real evidence only carries the bare id, the mock's Evidence.mitre wants a
# [code, name] pair. Unknown ids fall back to [code, code].
_MITRE_NAMES: dict[str, str] = {
    "T1485": "Data Destruction",
    "T1552": "Unsecured Credentials",
    "T1567": "Exfiltration Over Web Service",
    "T1565": "Data Manipulation",
    "T1059": "Command and Scripting Interpreter",
    "T1005": "Data from Local System",
}

# MCP integrity alert severity label -> mock level (0 info .. 2 danger).
_MCP_LEVEL: dict[str, int] = {"info": 0, "warn": 1, "warning": 1, "danger": 2, "critical": 2}


@dataclass(frozen=True)
class DashboardPaths:
    """Resolved locations of the two databases (either may be absent on disk)."""

    output_db: Path
    system_db: Path


def resolve_paths(
    output_db: str | Path | None = None,
    system_db: str | Path | None = None,
) -> DashboardPaths:
    """Resolve DB paths from explicit overrides, falling back to the defaults.

    Config-file resolution (``--config``) is layered on by the CLI command; this
    core resolver only knows explicit paths and the well-known defaults.
    """
    out = Path(output_db).expanduser() if output_db else DEFAULT_OUTPUT_DB
    sysdb = Path(system_db).expanduser() if system_db else DEFAULT_SYSTEM_DB
    return DashboardPaths(output_db=out, system_db=sysdb)


def _connect_ro(path: Path) -> sqlite3.Connection:
    """Open ``path`` read-only. Raises ``sqlite3.OperationalError`` if missing."""
    uri = f"{path.as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _loads(raw: Any) -> dict[str, Any]:
    """Parse a JSON text column into a dict, tolerating NULL / malformed values."""
    if not raw:
        return {}
    try:
        val = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return val if isinstance(val, dict) else {}


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _risk_from_level(level: str | None) -> int:
    return _RISK_ORDER.get(level or "", 0)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class DashboardRepository:
    """Read-only accessor + mapper over the two databases.

    Connections are opened per call (short-lived, ``mode=ro``) so instances are
    safe to share across the dashboard server's request threads.
    """

    def __init__(self, paths: DashboardPaths) -> None:
        self._paths = paths

    # -- availability / health ------------------------------------------------

    @property
    def paths(self) -> DashboardPaths:
        return self._paths

    def has_output_db(self) -> bool:
        return self._paths.output_db.exists()

    def has_system_memory(self) -> bool:
        """True when system.db exists and carries the governance-memory schema."""
        if not self._paths.system_db.exists():
            return False
        try:
            conn = _connect_ro(self._paths.system_db)
        except sqlite3.OperationalError:
            return False
        try:
            return _has_table(conn, "session_summaries")
        finally:
            conn.close()

    def health(self) -> dict[str, Any]:
        return {
            "output_db": str(self._paths.output_db),
            "system_db": str(self._paths.system_db),
            "has_output_db": self.has_output_db(),
            "has_system_memory": self.has_system_memory(),
        }

    # -- run listing / assembly ----------------------------------------------

    def list_run_ids(self, *, limit: int | None = None, offset: int = 0) -> list[str]:
        """Distinct session ids in the output DB, most-recent activity first.

        ``limit``/``offset`` bound the window at the SQL layer (the most-recent
        slice), so callers never materialize more than one page of ids — and,
        downstream, never assemble more than one page of full runs. ``limit=None``
        returns every id (used by the internal lightweight summary path).
        """
        sql = """SELECT session_id, MAX(timestamp) AS last_ts
                     FROM enriched_events
                    GROUP BY session_id
                    ORDER BY last_ts DESC"""
        params: list[Any] = []
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params = [limit, max(0, offset)]
        conn = _connect_ro(self._paths.output_db)
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        return [r["session_id"] for r in rows]

    def list_runs(self) -> list[dict[str, Any]]:
        """Lightweight per-session summaries for the Fleet table (no event bodies)."""
        return [s for s in (self._run_summary(sid) for sid in self.list_run_ids()) if s]

    def build_runs(self, session_ids: list[str]) -> list[dict[str, Any]]:
        """Assemble full ``Run`` shapes for many sessions with O(1) DB connections.

        Opens one read-only ``output.db`` connection and (when governance memory
        is present) one read-only ``system.db`` connection, then threads both
        through :meth:`build_run` and its identity/taint/trust/model helpers, so a
        fleet of N runs costs a small constant number of connection opens instead
        of ~8*N. Read-only throughout; the client ``Run[]`` contract is unchanged.
        """
        if not session_ids:
            return []
        out_conn = _connect_ro(self._paths.output_db)
        sys_available = self.has_system_memory()
        sys_conn: sqlite3.Connection | None = None
        if sys_available:
            try:
                sys_conn = _connect_ro(self._paths.system_db)
            except sqlite3.OperationalError:
                sys_available = False
        try:
            runs: list[dict[str, Any]] = []
            for session_id in session_ids:
                run = self.build_run(
                    session_id,
                    out_conn=out_conn,
                    sys_conn=sys_conn,
                    sys_available=sys_available,
                )
                if run is not None:
                    runs.append(run)
            return runs
        finally:
            out_conn.close()
            if sys_conn is not None:
                sys_conn.close()

    def build_run(
        self,
        session_id: str,
        *,
        out_conn: sqlite3.Connection | None = None,
        sys_conn: sqlite3.Connection | None = None,
        sys_available: bool | None = None,
    ) -> dict[str, Any] | None:
        """Assemble the full ``Run`` shape for one session, or ``None`` if unknown.

        When ``out_conn``/``sys_conn`` are supplied (the fleet path via
        :meth:`build_runs`) they are reused for every query instead of opening
        fresh read-only connections, keeping ``GET /api/runs`` at O(1) connection
        opens rather than ~8 per run. When omitted (the single-run
        ``/api/runs/{id}`` path) connections are opened and closed locally.
        """
        own_out = out_conn is None
        conn = _connect_ro(self._paths.output_db) if own_out else out_conn
        try:
            event_rows = conn.execute(
                """SELECT id, session_id, kind, timestamp, tool_name, risk_level,
                          risk_score, action, tool_display, verdict, cost, duration_ms,
                          payload_json, metadata_json
                     FROM enriched_events
                    WHERE session_id = ?
                    ORDER BY timestamp ASC, created_at ASC""",
                (session_id,),
            ).fetchall()
            if not event_rows:
                return None
            seg_rows = conn.execute(
                """SELECT segment_id, kind, session_id, title, version, parent_id
                     FROM segment_titles
                    WHERE session_id = ?""",
                (session_id,),
            ).fetchall()
            usage = _usage_breakdown(conn, session_id)
            gap_rows = conn.execute(
                """SELECT timestamp, dropped_count, reason
                     FROM context_gaps
                    WHERE session_id = ?
                    ORDER BY gap_ordinal ASC""",
                (session_id,),
            ).fetchall()

            metas = [_loads(r["metadata_json"]) for r in event_rows]
            events = [_map_event(r, m) for r, m in zip(event_rows, metas)]
            seg_risk = _segment_risk(events)
            segments = _map_segments(seg_rows, events, seg_risk)
            peak = max((e["risk"] for e in events), default=0)

            starts = [t for t in (_parse_ts(r["timestamp"]) for r in event_rows) if t]
            dur_ms = (
                (max(starts) - min(starts)).total_seconds() * 1000.0 if len(starts) >= 2 else 0.0
            )

            identity = self._identity(session_id, sys_conn=sys_conn, sys_available=sys_available)
            return {
                "id": session_id,
                "repo": identity["repo"] or _first_meta(metas, "repo") or "",
                "agent": identity["agent"] or _first_meta(metas, "source_framework") or "",
                "model": identity["model"] or _dominant_model(conn, session_id),
                "title": _session_title(seg_rows) or identity["repo"] or session_id,
                "live": identity["live"],
                "segs": segments,
                "events": events,
                # Full null-aware usage breakdown (tokens, null-or-real dollars, and
                # the real Copilot billing signals: premium-request counts +
                # cache-aware per-model token attribution). See _usage_breakdown.
                "usage": usage,
                "started": event_rows[0]["timestamp"],
                "durMs": dur_ms,
                "drift": identity["drift"],
                "peak": peak,
                "taint": self._taint(session_id, sys_conn=sys_conn, sys_available=sys_available),
                "trust": self._trust(session_id, sys_conn=sys_conn, sys_available=sys_available),
                "mcp": _mcp_alerts(metas),
                # Additive (not in the mock's Run type): raw context gaps for the
                # Coverage list + RunView banner, wired in D6/D8.
                "gaps": [
                    {"t": g["timestamp"], "dropped": g["dropped_count"], "reason": g["reason"]}
                    for g in gap_rows
                ],
            }
        finally:
            if own_out:
                conn.close()

    def _run_summary(self, session_id: str) -> dict[str, Any] | None:
        conn = _connect_ro(self._paths.output_db)
        try:
            agg = conn.execute(
                """SELECT COUNT(*) AS n,
                          MIN(timestamp) AS first_ts,
                          MAX(timestamp) AS last_ts
                     FROM enriched_events
                    WHERE session_id = ?""",
                (session_id,),
            ).fetchone()
            if not agg or not agg["n"]:
                return None
            peak_rows = conn.execute(
                """SELECT DISTINCT risk_level FROM enriched_events WHERE session_id = ?""",
                (session_id,),
            ).fetchall()
            usage = _usage_breakdown(conn, session_id)
            title_row = conn.execute(
                """SELECT title FROM segment_titles
                    WHERE session_id = ? AND kind = 'session' LIMIT 1""",
                (session_id,),
            ).fetchone()
            repo_meta = conn.execute(
                """SELECT metadata_json FROM enriched_events
                    WHERE session_id = ? AND metadata_json IS NOT NULL LIMIT 1""",
                (session_id,),
            ).fetchone()
            dominant_model = _dominant_model(conn, session_id)
        finally:
            conn.close()

        peak = max((_risk_from_level(r["risk_level"]) for r in peak_rows), default=0)
        first_ts = _parse_ts(agg["first_ts"])
        last_ts = _parse_ts(agg["last_ts"])
        dur_ms = (last_ts - first_ts).total_seconds() * 1000.0 if first_ts and last_ts else 0.0
        meta = _loads(repo_meta["metadata_json"]) if repo_meta else {}
        identity = self._identity(session_id)
        return {
            "id": session_id,
            "repo": identity["repo"] or meta.get("repo") or "",
            "agent": identity["agent"] or meta.get("source_framework") or "",
            "model": identity["model"] or dominant_model,
            "title": (title_row["title"] if title_row else None) or session_id,
            "live": identity["live"],
            "usage": usage,
            "started": agg["first_ts"],
            "durMs": dur_ms,
            "drift": identity["drift"],
            "peak": peak,
            "eventCount": int(agg["n"]),
        }

    def build_transcript(
        self, session_id: str, *, out_conn: sqlite3.Connection | None = None
    ) -> dict[str, Any] | None:
        """Assemble the full-text transcript for one session, or ``None`` if unknown.

        One turn per ``enriched_events`` row in chronological order, each carrying
        its ``role`` (user / assistant / system / tool), a display ``label``, its
        ``kind``, and the **complete, untruncated** free text from the payload.

        This is the reading view. It is deliberately separate from
        ``/api/runs/{id}`` (whose per-event ``summary`` is a one-line, truncated
        preview) and from the bounded ``GET /api/runs`` list projection — the
        transcript is fetched on demand for a single run so its full bodies never
        inflate the fleet payload.
        """
        own = out_conn is None
        conn = _connect_ro(self._paths.output_db) if own else out_conn
        try:
            rows = conn.execute(
                """SELECT id, kind, timestamp, tool_name, tool_display, payload_json
                     FROM enriched_events
                    WHERE session_id = ?
                    ORDER BY timestamp ASC, created_at ASC""",
                (session_id,),
            ).fetchall()
        finally:
            if own:
                conn.close()
        if not rows:
            return None
        return {"id": session_id, "turns": [_map_transcript_turn(r) for r in rows]}

    # -- system.db (governance memory) — optional -----------------------------

    def _identity(
        self,
        session_id: str,
        *,
        sys_conn: sqlite3.Connection | None = None,
        sys_available: bool | None = None,
    ) -> dict[str, Any]:
        """repo / agent / model / live / drift from system.db, when present."""
        blank = {"repo": None, "agent": None, "model": None, "live": False, "drift": None}
        available = sys_available if sys_available is not None else self.has_system_memory()
        if not available:
            return blank
        own = sys_conn is None
        if own:
            try:
                conn = _connect_ro(self._paths.system_db)
            except sqlite3.OperationalError:
                return blank
        else:
            conn = sys_conn
        try:
            summ = conn.execute(
                """SELECT repo, agent_model, ended_at, drift_max
                     FROM session_summaries WHERE session_id = ?""",
                (session_id,),
            ).fetchone()
            active = None
            if _has_table(conn, "session_state"):
                active = conn.execute(
                    "SELECT 1 FROM session_state WHERE session_id = ?", (session_id,)
                ).fetchone()
        finally:
            if own:
                conn.close()
        if not summ:
            # Live sessions may not have a summary yet; still detect liveness.
            return {**blank, "live": active is not None}
        agent, _, model = (summ["agent_model"] or "").partition("/")
        return {
            "repo": summ["repo"],
            "agent": agent or None,
            "model": model or None,
            "live": summ["ended_at"] is None and active is not None,
            "drift": summ["drift_max"],
        }

    def _taint(
        self,
        session_id: str,
        *,
        sys_conn: sqlite3.Connection | None = None,
        sys_available: bool | None = None,
    ) -> list[dict[str, Any]]:
        rows = self._system_rows(
            "taint_entries",
            """SELECT clearance, source, payload_pointer FROM taint_entries
                WHERE session_id = ? ORDER BY ordinal ASC""",
            session_id,
            sys_conn=sys_conn,
            sys_available=sys_available,
        )
        return [
            {"flow": f"{r['source']} → {r['clearance']}", "det": r["payload_pointer"], "lvl": 1}
            for r in rows
        ]

    def _trust(
        self,
        session_id: str,
        *,
        sys_conn: sqlite3.Connection | None = None,
        sys_available: bool | None = None,
    ) -> list[dict[str, Any]]:
        rows = self._system_rows(
            "trust_grants",
            """SELECT key, granted_at, ttl_seconds, reason FROM trust_grants
                WHERE session_id = ? ORDER BY ordinal ASC""",
            session_id,
            sys_conn=sys_conn,
            sys_available=sys_available,
        )
        return [
            {
                "who": r["key"],
                "ttl": _format_ttl(r["granted_at"], r["ttl_seconds"]),
                "lvl": 0,
                "reason": r["reason"],
            }
            for r in rows
        ]

    def _system_rows(
        self,
        table: str,
        sql: str,
        session_id: str,
        *,
        sys_conn: sqlite3.Connection | None = None,
        sys_available: bool | None = None,
    ) -> list[sqlite3.Row]:
        available = sys_available if sys_available is not None else self.has_system_memory()
        if not available:
            return []
        own = sys_conn is None
        if own:
            try:
                conn = _connect_ro(self._paths.system_db)
            except sqlite3.OperationalError:
                return []
        else:
            conn = sys_conn
        try:
            if not _has_table(conn, table):
                return []
            return conn.execute(sql, (session_id,)).fetchall()
        finally:
            if own:
                conn.close()


# --- pure mapping helpers ----------------------------------------------------


def _map_event(row: sqlite3.Row, meta: dict[str, Any]) -> dict[str, Any]:
    """Reshape one ``enriched_events`` row (+ parsed metadata) into ``TEvent``."""
    payload = _loads(row["payload_json"])
    gov = meta.get("governance") if isinstance(meta.get("governance"), dict) else {}
    cls = gov.get("classification") or meta.get("classification") or {}
    risk_a = gov.get("risk_assessment") or {}
    rec = gov.get("recommendation") or {}
    evd = gov.get("evidence")

    mechanism = cls.get("mechanism") or ""
    effect = cls.get("effect") or ""
    tool_name = (
        row["tool_name"] or row["tool_display"] or mechanism or _label_from_kind(row["kind"])
    )
    action = row["action"] or rec.get("recommended_action") or "allow"
    confidence = _CONFIDENCE_NUM.get(risk_a.get("confidence") or "", 0.9)

    return {
        "id": row["id"],
        "t": row["timestamp"],
        "tool": {"n": tool_name, "cat": effect, "canon": mechanism, "w": 0},
        "kind": row["kind"],
        "summary": row["tool_display"] or _summarize(tool_name, payload),
        "risk": _risk_from_level(row["risk_level"]),
        "score": round((row["risk_score"] or 0) / 100.0, 2),
        "action": action,
        # Event dollar cost is None when the wire carries none (GitHub Copilot
        # emits no per-event dollars) — pass null through so the inspector renders
        # "—", never a fabricated "$0.000" that reads as free.
        "cost": float(row["cost"]) if row["cost"] is not None else None,
        "tokens": int(payload.get("tokens") or 0),
        "dur": float(row["duration_ms"]) if row["duration_ms"] is not None else 0.0,
        "phase": meta.get("phase") or "",
        "seg": meta.get("step_id") or meta.get("activity_id") or "",
        "file": payload.get("path") or payload.get("file") or "",
        "turn": meta.get("turn_id") or "",
        "retry": bool(payload.get("retry", False)),
        "cls": {"canon": mechanism, "cat": effect, "conf": confidence},
        "ev": _map_evidence(evd) if evd else None,
        "reco": {
            "action": rec.get("recommended_action") or action,
            "why": rec.get("message") or rec.get("reason_code") or "",
        },
        "gap": None,
        "payload": payload,
    }


def _map_evidence(evd: dict[str, Any]) -> dict[str, Any]:
    techniques = evd.get("mitre_techniques") or []
    code = techniques[0] if techniques else ""
    mitre = [code, _MITRE_NAMES.get(code, code)] if code else ["", ""]
    pointers = evd.get("pointers") or []
    ptr = ""
    if pointers and isinstance(pointers[0], dict):
        ptr = pointers[0].get("payload_pointer") or ""
    return {
        "mitre": mitre,
        "preds": list(evd.get("matched_predicates") or evd.get("risk_factors") or []),
        # pii / ifc have no dedicated column — derived best-effort in a later task;
        # default to "none" so the inspector renders cleanly. See spec section 3.
        "pii": "none",
        "ifc": "none",
        "ptr": ptr,
    }


_KIND_LABELS: dict[str, str] = {
    "message.user": "User",
    "message.assistant": "Assistant",
    "message.system": "System",
    "telemetry.usage": "Usage",
}


def _label_from_kind(kind: str | None) -> str:
    """Human label for a non-tool event, derived from its ``kind``.

    Tool events carry their own name; message / telemetry / lifecycle events do
    not, so fall back to a readable label instead of the literal ``"event"``.
    Unknown kinds use their last dotted segment, Title-cased (``foo.bar`` -> ``Bar``).
    """
    key = (kind or "").strip()
    if not key:
        return "Event"
    if key in _KIND_LABELS:
        return _KIND_LABELS[key]
    if key.startswith("session"):
        return "Session"
    last = key.rsplit(".", 1)[-1]
    return last.replace("_", " ").title() or "Event"


def _snippet(text: str, limit: int = 140) -> str:
    """One-line preview of free text: collapse whitespace/newlines and truncate."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit].rstrip() + "\u2026"


def _summarize(tool_name: str, payload: dict[str, Any]) -> str:
    for key in ("command", "url", "path", "file", "query"):
        val = payload.get(key)
        if isinstance(val, str) and val:
            return val
    for key in ("content", "text", "message"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return _snippet(val)
    return tool_name


# kind -> transcript role. Prefix-based so unlisted kinds still resolve sensibly
# (e.g. every ``session.*`` lifecycle event -> system). Tool/command/file/mcp and
# anything unrecognized falls through to "tool" — the actor is the agent's tools.
def _transcript_role(kind: str | None) -> str:
    key = (kind or "").strip()
    if key.startswith("message.user") or key.startswith("input."):
        return "user"
    if (
        key.startswith("message.assistant")
        or key.startswith("llm.")
        or key.startswith("reasoning.")
        or key.startswith("planning.")
    ):
        return "assistant"
    if key.startswith("message.system") or key.startswith("session."):
        return "system"
    return "tool"


# Payload keys carrying an invocation (the "what ran") and a body (the message or
# output), in priority order. A turn shows the first of each it finds, so a tool
# call with both a ``command`` and its ``output`` renders both, in that order.
_TRANSCRIPT_INVOCATION_KEYS: tuple[str, ...] = ("command", "arguments", "url", "query")
_TRANSCRIPT_BODY_KEYS: tuple[str, ...] = (
    "content",
    "text",
    "message",
    "output",
    "result",
    "stdout",
    "body",
)


def _transcript_text(payload: dict[str, Any]) -> str:
    """Full (untruncated) free text for a transcript turn.

    Unlike :func:`_summarize` (a one-line, whitespace-collapsed, truncated preview
    for the timeline), this returns the complete text — newlines preserved — so the
    Transcript panel can render the run's actual content top-to-bottom.
    """
    parts: list[str] = []
    for keys in (_TRANSCRIPT_INVOCATION_KEYS, _TRANSCRIPT_BODY_KEYS):
        for key in keys:
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                parts.append(val.strip())
                break
    return "\n\n".join(parts)


def _map_transcript_turn(row: sqlite3.Row) -> dict[str, Any]:
    """Reshape one ``enriched_events`` row into a transcript turn (full text)."""
    payload = _loads(row["payload_json"])
    kind = row["kind"]
    label = row["tool_name"] or row["tool_display"] or _label_from_kind(kind)
    return {
        "id": row["id"],
        "t": row["timestamp"],
        "role": _transcript_role(kind),
        "label": label,
        "kind": kind,
        "text": _transcript_text(payload),
    }


def _segment_risk(events: list[dict[str, Any]]) -> dict[str, int]:
    """Max event risk per segment id (events carry their segment in ``seg``)."""
    out: dict[str, int] = {}
    for e in events:
        seg = e["seg"]
        if seg:
            out[seg] = max(out.get(seg, 0), e["risk"])
    return out


def _map_segments(
    seg_rows: list[sqlite3.Row],
    events: list[dict[str, Any]],
    seg_risk: dict[str, int],
) -> list[dict[str, Any]]:
    session_peak = max((e["risk"] for e in events), default=0)
    segments: list[dict[str, Any]] = []
    for r in seg_rows:
        kind = r["kind"]
        risk = session_peak if kind == "session" else seg_risk.get(r["segment_id"], 0)
        segments.append(
            {
                "id": r["segment_id"],
                "kind": kind,
                "parent": r["parent_id"],
                "title": r["title"],
                "risk": risk,
            }
        )
    # Bubble each segment's risk up to its parent so an activity reflects the worst
    # of its child steps (leaf→root; the session node already holds the peak).
    by_id = {s["id"]: s for s in segments}
    for _ in range(len(segments)):
        changed = False
        for s in segments:
            parent = by_id.get(s["parent"]) if s["parent"] else None
            if parent is not None and s["risk"] > parent["risk"]:
                parent["risk"] = s["risk"]
                changed = True
        if not changed:
            break
    # Stable order: session first, then activities/steps as stored.
    segments.sort(key=lambda s: 0 if s["kind"] == "session" else 1)
    return segments


def _mcp_alerts(metas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collect MCP integrity alerts stamped into events' governance metadata.

    Real alerts (``MCPIntegrityAlert``) carry ``tool_name/server/alert_type/
    previous/current/severity`` — there is no free-text message, so one is
    synthesized from the drift it describes.
    """
    out: list[dict[str, Any]] = []
    for meta in metas:
        gov = meta.get("governance") if isinstance(meta.get("governance"), dict) else {}
        for alert in gov.get("mcp_alerts") or []:
            if not isinstance(alert, dict):
                continue
            severity = str(alert.get("severity") or alert.get("level") or "").lower()
            out.append(
                {
                    "srv": alert.get("server") or alert.get("srv") or "",
                    "msg": _mcp_message(alert),
                    "lvl": _MCP_LEVEL.get(severity, 1),
                }
            )
    return out


def _mcp_message(alert: dict[str, Any]) -> str:
    """Synthesize a human-readable message for one MCP integrity alert."""
    explicit = alert.get("message") or alert.get("summary") or alert.get("msg")
    if explicit:
        return str(explicit)
    alert_type = str(alert.get("alert_type") or "mcp drift").replace("_", " ")
    tool = alert.get("tool_name") or alert.get("tool") or ""
    prev, cur = alert.get("previous"), alert.get("current")
    prefix = f"{tool}: " if tool else ""
    if prev and cur:
        return f"{prefix}{alert_type} ({prev} → {cur})"
    return f"{prefix}{alert_type}"


def _first_meta(metas: list[dict[str, Any]], key: str) -> str | None:
    for meta in metas:
        val = meta.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def _dominant_model(conn: sqlite3.Connection, session_id: str) -> str:
    # Ignore blank model strings: usage records for `<synthetic>`/absent-model
    # messages are normalized to "" upstream (their tokens still count), and a
    # blank is not a model — it must never win the run's dominant model.
    row = conn.execute(
        """SELECT model, COUNT(*) c FROM usage_records
            WHERE session_id = ? AND model != '' GROUP BY model ORDER BY c DESC LIMIT 1""",
        (session_id,),
    ).fetchone()
    return row["model"] if row else ""


def _attr_count(attrs: dict[str, Any], key: str) -> int | None:
    """One numeric usage attribute from a parsed ``attributes_json``, or ``None``.

    Mirrors :func:`_usage_breakdown`'s null semantics: a missing key, a JSON
    ``null``, a boolean, or any non-numeric value is *unknown* (``None``) and is
    never coerced to ``0`` — so a real ``0`` on the wire stays distinct from "the
    key was never there".
    """
    value = attrs.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _nsum(acc: int | None, value: int | None) -> int | None:
    """Null-aware running sum: an unknown (``None``) addend contributes nothing,
    but a present addend (including ``0``) promotes the accumulator from unknown
    to a real number — so genuine zero never collapses back into unknown."""
    if value is None:
        return acc
    return (acc or 0) + value


def _usage_breakdown(conn: sqlite3.Connection, session_id: str) -> dict[str, Any]:
    """Full null-aware ``Run.usage`` breakdown in a single pass over the run's
    ``usage_records`` rows.

    Returns the complete usage dict the frontend renders:

    * ``in`` / ``out`` — token grand totals, always numeric. ``input_tokens`` /
      ``output_tokens`` are per-row totals that already include cache reads/creation.
    * ``cost`` — the summed ``cost_usd``, or ``None`` when no row carries dollars.
      GitHub Copilot emits none, so this stays ``None`` and renders "—" (unknown),
      never a fabricated "$0.00". No dollars are ever derived from the counts below.
    * ``aiuNano`` — Copilot's **primary** billing signal: per-model AI-Unit
      consumption in *nano*-AIU (``modelMetrics.<model>.totalNanoAiu``), summed
      across models. Integer nano-AIU is kept verbatim; only the frontend divides
      by 1e9 to AIU. ``None`` until some row carries it (unknown vs a genuine 0).
    * ``premiumRequests`` — a **secondary/legacy** signal: the premium-request
      COUNT (``modelMetrics.<model>.requests.cost`` is a count, not dollars),
      summed across models.
    * ``inputUncached`` / ``cacheRead`` / ``cacheCreation`` — cache-aware token
      classes (billed/uncached vs cache read vs cache creation).
    * ``requestsTotal`` — total request count across models.
    * ``models`` — per-model rows ``{model, aiuNano, premiumRequests, requests,
      inputUncached, cacheRead, cacheCreation, input, output}``.

    The unknown-vs-zero distinction is preserved throughout (see :func:`_nsum`):
    every count field is ``None`` until some row carries its key, then the running
    sum — so a non-Copilot run (whose rows carry none of the keys) stays ``None``
    ("—") while a Copilot run that genuinely made zero premium requests reports
    ``0``. ``in`` / ``out`` and each model's ``input`` / ``output`` always coalesce
    to numbers. Blank-model ("") rows are kept — their tokens are real (the
    frontend labels a blank model "unknown model"). ``models`` is ``[]`` when the
    run has no ``usage_records`` rows.

    Takes the already-open read-only ``conn`` and reuses it (exactly like
    :func:`_dominant_model`), never opening its own — so the fleet path stays O(1)
    on connections and ``attributes_json`` is parsed exactly once per row.
    """
    rows = conn.execute(
        """SELECT model, input_tokens, output_tokens, cost_usd, attributes_json
             FROM usage_records
            WHERE session_id = ?""",
        (session_id,),
    ).fetchall()

    in_tok = 0
    out_tok = 0
    cost: float | None = None
    aiu: int | None = None
    premium: int | None = None
    uncached: int | None = None
    cache_read: int | None = None
    cache_creation: int | None = None
    requests_total: int | None = None
    models: dict[str, dict[str, Any]] = {}

    for row in rows:
        row_in = int(row["input_tokens"] or 0)
        row_out = int(row["output_tokens"] or 0)
        in_tok += row_in
        out_tok += row_out
        if row["cost_usd"] is not None:
            cost = (cost or 0.0) + float(row["cost_usd"])

        attrs = _loads(row["attributes_json"])
        a = _attr_count(attrs, "nano_aiu")
        p = _attr_count(attrs, "premium_requests")
        u = _attr_count(attrs, "input_uncached")
        cr = _attr_count(attrs, "cache_read_tokens")
        cc = _attr_count(attrs, "cache_creation_tokens")
        rt = _attr_count(attrs, "requests_total")

        aiu = _nsum(aiu, a)
        premium = _nsum(premium, p)
        uncached = _nsum(uncached, u)
        cache_read = _nsum(cache_read, cr)
        cache_creation = _nsum(cache_creation, cc)
        requests_total = _nsum(requests_total, rt)

        # Keep the raw model string; a blank ("") model is a real row (its tokens
        # count) and must not be dropped — the frontend labels it "unknown model".
        model = row["model"] or ""
        agg = models.get(model)
        if agg is None:
            agg = {
                "model": model,
                "aiuNano": None,
                "premiumRequests": None,
                "requests": None,
                "inputUncached": None,
                "cacheRead": None,
                "cacheCreation": None,
                "input": 0,
                "output": 0,
            }
            models[model] = agg
        agg["input"] += row_in
        agg["output"] += row_out
        agg["aiuNano"] = _nsum(agg["aiuNano"], a)
        agg["premiumRequests"] = _nsum(agg["premiumRequests"], p)
        agg["requests"] = _nsum(agg["requests"], rt)
        agg["inputUncached"] = _nsum(agg["inputUncached"], u)
        agg["cacheRead"] = _nsum(agg["cacheRead"], cr)
        agg["cacheCreation"] = _nsum(agg["cacheCreation"], cc)

    return {
        "in": in_tok,
        "out": out_tok,
        "cost": cost,
        "aiuNano": aiu,
        "premiumRequests": premium,
        "inputUncached": uncached,
        "cacheRead": cache_read,
        "cacheCreation": cache_creation,
        "requestsTotal": requests_total,
        "models": list(models.values()),
    }


def _session_title(seg_rows: list[sqlite3.Row]) -> str | None:
    for r in seg_rows:
        if r["kind"] == "session":
            return r["title"]
    return None


def _format_ttl(granted_at: str | None, ttl_seconds: float | None) -> str:
    start = _parse_ts(granted_at)
    if start is None or ttl_seconds is None:
        return "no expiry"
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    remaining = (start.timestamp() + ttl_seconds) - datetime.now(timezone.utc).timestamp()
    if remaining <= 0:
        return "expired"
    minutes = int(remaining // 60)
    if minutes >= 60:
        return f"{minutes // 60}h {minutes % 60}m left"
    return f"{minutes}m left"
