"""Survey the WSL session-store: how many sessions have N+ tool events?

Reads ``research/data/raw/copilot-session-store.db`` directly, walks the
recorded turn payloads, counts tool events per session, and prints a
histogram. The goal is to know the realistic supply of "agent-mode"
sessions before deciding how many external sources we need to mix in.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "raw" / "copilot-session-store.db"


def main() -> int:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    tables = [r[0] for r in con.execute(
        "select name from sqlite_master where type='table'"
    ).fetchall()]
    print(f"tables: {tables[:5]}...")

    # Tool-invocation count per session.
    rows = con.execute(
        "select session_id, count(*) as n from session_files group by session_id"
    ).fetchall()
    print(f"\nsessions with any session_files row: {len(rows)}")
    buckets = [0, 1, 5, 10, 25, 50, 100, 250, 1000]
    counts = Counter()
    for r in rows:
        n = r["n"]
        for b in buckets:
            if n >= b:
                counts[b] += 1
    print("tool-files cumulative buckets (sessions with >= N tool-file rows):")
    for b in buckets:
        print(f"  >= {b:>5}: {counts[b]}")

    # Distribution of tool_name across all rows (top 20).
    tn = con.execute(
        "select tool_name, count(*) as n from session_files "
        "group by tool_name order by n desc limit 20"
    ).fetchall()
    print("\ntop tool_name by session_files count:")
    for r in tn:
        print(f"  {r['tool_name']:<40} {r['n']}")

    # Turns: distribution of turn counts per session
    tr = con.execute(
        "select session_id, count(*) as n from turns group by session_id"
    ).fetchall()
    print(f"\nsessions with any turn: {len(tr)}")
    turn_counts = Counter()
    for r in tr:
        for b in buckets:
            if r["n"] >= b:
                turn_counts[b] += 1
    print("turn-count cumulative buckets:")
    for b in buckets:
        print(f"  >= {b:>5}: {turn_counts[b]}")

    # Cross: sessions with N+ turns AND M+ tool files
    print("\nsessions with >= K turns AND >= L tool files:")
    for k in (5, 10, 25):
        for l in (1, 5, 10, 25):
            n = con.execute(
                "select count(*) from (select session_id from turns "
                "group by session_id having count(*) >= ?) t "
                "join (select session_id from session_files "
                "group by session_id having count(*) >= ?) f "
                "using (session_id)", (k, l)
            ).fetchone()[0]
            print(f"  turns >= {k:>2}  tool_files >= {l:>2}: {n}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
