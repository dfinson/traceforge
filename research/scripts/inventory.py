"""Inventory existing legacy data — what's actually on disk and how it aligns."""
import json
from collections import Counter
from pathlib import Path

root = Path(__file__).resolve().parents[1] / "data" / "raw" / "legacy"

labels  = json.loads((root / "fulltext" / "labels_v2.json").read_text(encoding="utf-8"))
ft      = json.loads((root / "fulltext" / "all_fulltext.json").read_text(encoding="utf-8"))
swe_ext = json.loads((root / "swe_agent_extracted_v2.json").read_text(encoding="utf-8"))
oh_ext  = json.loads((root / "openhands_extracted.json").read_text(encoding="utf-8"))
ss_ext  = json.loads((root / "swesmith_extracted.json").read_text(encoding="utf-8"))

ft_by_id  = {s["session_id"]: s for s in ft}
swe_by_id = {e["instance_id"]: e for e in swe_ext}
oh_by_id  = {(e.get("instance_id") or e.get("session_id")): e for e in oh_ext}
ss_by_id  = {(e.get("instance_id") or e.get("session_id")): e for e in ss_ext}

print("=== labels_v2.json (Sonnet boundary labels) ===")
print(f"  sessions:    {len(labels)}")
src_lookup = {s["session_id"]: s["source"] for s in ft}
src_dist = Counter(src_lookup.get(k, "?") for k in labels)
print(f"  by source:   {dict(src_dist)}")
total_turns = sum(len(v) for v in labels.values())
print(f"  total turns: {total_turns}")
val_dist = Counter()
for v in labels.values():
    val_dist.update(v)
print(f"  label dist:  {dict(val_dist)}")
print()

print("=== all_fulltext.json (turn-level transcripts) ===")
src_ft = Counter(s["source"] for s in ft)
print(f"  sessions:    {len(ft)} -> {dict(src_ft)}")
print(f"  total turns: {sum(s['turn_count'] for s in ft)}")
print()

print("=== extracted (per-event tool_name, file_path) ===")
print(f"  swe-agent:  {len(swe_ext)} sessions, {sum(len(e['events']) for e in swe_ext)} events")
print(f"  openhands:  {len(oh_ext)} sessions, {sum(len(e.get('events', [])) for e in oh_ext)} events")
print(f"  swesmith:   {len(ss_ext)} sessions, {sum(len(e.get('events', [])) for e in ss_ext)} events")
print()

print("=== labeled session ID coverage ===")
in_ft  = sum(1 for k in labels if k in ft_by_id)
in_ext = sum(1 for k in labels if k in swe_by_id or k in oh_by_id or k in ss_by_id)
print(f"  in fulltext  (turn-by-turn text):      {in_ft:>4}/{len(labels)}")
print(f"  in extracted (per-event tool_name):    {in_ext:>4}/{len(labels)}")
print()

labeled_with_run = [k for k in labels if "__run" in k]
labeled_no_run   = [k for k in labels if "__run" not in k]
print(f"  labeled with __runN suffix:    {len(labeled_with_run)}")
print(f"  labeled without __runN suffix: {len(labeled_no_run)}")

swe_base_ids = {e["base_instance_id"] for e in swe_ext}
oh_base_ids  = {e.get("base_instance_id") or "" for e in oh_ext}
ss_base_ids  = {e.get("base_instance_id") or "" for e in ss_ext}
print(f"  no-run labels matching swe base_instance_id:       {sum(1 for k in labeled_no_run if k in swe_base_ids)}")
print(f"  no-run labels matching openhands base_instance_id: {sum(1 for k in labeled_no_run if k in oh_base_ids)}")
print(f"  no-run labels matching swesmith base_instance_id:  {sum(1 for k in labeled_no_run if k in ss_base_ids)}")
print()

print("=== count alignment for the 3 sessions where IDs match ===")
matched = 0
for k in labels:
    if k in ft_by_id and k in swe_by_id:
        ft_n = ft_by_id[k]["turn_count"]
        ext_n = swe_by_id[k]["node_count"]
        lbl_n = len(labels[k])
        print(f"  {k:50s} ft_turns={ft_n:>3} ext_events={ext_n:>3} labels={lbl_n:>3}")
        matched += 1
print(f"  total matched: {matched}")
