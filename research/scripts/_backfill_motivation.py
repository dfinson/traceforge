"""One-shot corpus back-fill: recover the ``motivation`` column from the
preserved ``metadata_json`` blob.

Context: ``event_rows.event_to_feature_row`` (the single train/serve projection)
historically read a non-existent ``md.tool_intent`` attribute, so the
``motivation`` feature column was written all-null across every corpus shard.
The real value survives inside ``metadata_json`` (``md.model_dump``), so it can
be restored WITHOUT re-ingesting (which would mint fresh ``event_id`` uuid4s and
orphan the existing phase/boundary labels).

Source-agnostic: every parquet under ``labeling-corpus/<source>/`` is scanned;
rows whose ``motivation`` is null but whose ``metadata_json.motivation.intent``
is present get back-filled. Sources without report_intent (e.g. swe-agent)
simply yield nothing and are left untouched. Writes are atomic
(tempfile + ``os.replace``) and preserve ``event_id`` and all other columns.
"""

from __future__ import annotations

import glob
import json
import os
import tempfile

import pyarrow as pa
import pyarrow.parquet as pq

CORPUS_GLOB = "data/interim/labeling-corpus/*/*.parquet"


def _intent(metadata_json: str | None) -> str | None:
    if not metadata_json:
        return None
    try:
        md = json.loads(metadata_json)
    except (ValueError, TypeError):
        return None
    mot = md.get("motivation")
    if isinstance(mot, dict):
        intent = mot.get("intent")
        if isinstance(intent, str) and intent.strip():
            return intent
    return None


def main() -> None:
    files = sorted(glob.glob(CORPUS_GLOB))
    touched = filled = scanned = 0
    for path in files:
        table = pq.read_table(path)
        cols = table.column_names
        if "metadata_json" not in cols or "motivation" not in cols:
            continue
        mot = table.column("motivation").to_pylist()
        meta = table.column("metadata_json").to_pylist()
        scanned += len(mot)
        new_mot = list(mot)
        changed = False
        for i, cur in enumerate(mot):
            if cur:
                continue
            recovered = _intent(meta[i])
            if recovered is not None:
                new_mot[i] = recovered
                filled += 1
                changed = True
        if not changed:
            continue
        idx = cols.index("motivation")
        table = table.set_column(idx, "motivation", pa.array(new_mot, type=pa.string()))
        d = os.path.dirname(path)
        fd, tmp = tempfile.mkstemp(suffix=".parquet", dir=d)
        os.close(fd)
        pq.write_table(table, tmp)
        os.replace(tmp, path)
        touched += 1

    print(f"files scanned : {len(files)}")
    print(f"rows scanned  : {scanned}")
    print(f"rows filled   : {filled}")
    print(f"files rewritten: {touched}")


if __name__ == "__main__":
    main()
