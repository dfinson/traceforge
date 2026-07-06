"""Shared helpers for capturing real raw framework traces.

A *raw trace* is the verbatim output a framework writes to disk (a session
file / JSONL) or the verbatim serialization of its native event stream. Raw
traces are NEVER hand-edited — they are the ground-truth input that golden e2e
tests (tests/e2e/test_raw_traces.py) feed through the real traceforge pipeline.

Each capture script lives next to this file as ``capture_<framework>.py`` and
calls :func:`write_trace` to emit:

  tests/fixtures/raw_traces/<framework>/<scenario>.jsonl   # one native dict/line
  tests/fixtures/raw_traces/<framework>/meta.yaml          # provenance

The golden test only requires the JSONL; ``meta.yaml`` records provenance so a
reviewer can tell which framework version / model produced the bytes.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import subprocess
from importlib import metadata
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_ROOT = REPO_ROOT / "tests" / "fixtures" / "raw_traces"

_REDACTED = "REDACTED"
# Value-shaped secrets (provider API keys/tokens) that frameworks sometimes
# serialize into their native event objects (e.g. an LLM client's api_key).
_SECRET_VALUE_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{20,})|(?i:bearer\s+[A-Za-z0-9._\-]{20,})")
# Field names whose values are credentials regardless of shape.
_SECRET_KEY_RE = re.compile(r"(?i)(api[_-]?key|secret|token|authorization|password)")


def _redact_secrets(value: Any) -> Any:
    """Recursively scrub credential-shaped values from a native event object.

    Committed fixtures must never contain secrets (GitHub push protection blocks
    them). Frameworks like crewai serialize their LLM client config — including
    ``api_key`` — into native events, so every row is sanitized before it is
    written. This is the only edit applied to otherwise-verbatim traces.
    """
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, val in value.items():
            if isinstance(key, str) and _SECRET_KEY_RE.search(key) and isinstance(val, str) and val:
                out[key] = _REDACTED
            else:
                out[key] = _redact_secrets(val)
        return out
    if isinstance(value, list):
        return [_redact_secrets(item) for item in value]
    if isinstance(value, str):
        return _SECRET_VALUE_RE.sub(_REDACTED, value)
    return value


def _git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def package_version(dist_name: str) -> str:
    """Best-effort installed version of a distribution (for meta provenance)."""
    try:
        return metadata.version(dist_name)
    except Exception:
        return "unknown"


def write_trace(
    framework: str,
    scenario: str,
    lines: Iterable[dict[str, Any]],
    *,
    source_repo: str,
    framework_version: str,
    model: str,
    notes: str = "",
) -> Path:
    """Write a raw trace JSONL plus a sibling meta.yaml. Returns the JSONL path.

    ``lines`` are native framework event dicts — exactly what the framework
    emits, with no traceforge-side normalization. They are serialized one per
    line so the golden test can stream them through MappedJsonAdapter.
    """
    out_dir = FIXTURES_ROOT / framework
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = [_redact_secrets(row) for row in lines]
    jsonl_path = out_dir / f"{scenario}.jsonl"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            fh.write("\n")

    meta_path = out_dir / "meta.yaml"
    meta = {
        "framework": framework,
        "scenario": scenario,
        "source_repo": source_repo,
        "framework_version": framework_version,
        "model": model,
        "captured_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "traceforge_commit": _git_commit(),
        "line_count": len(rows),
        "notes": notes,
    }
    # Write YAML by hand (stdlib only) to keep capture scripts dependency-light.
    with meta_path.open("w", encoding="utf-8", newline="\n") as fh:
        for key, value in meta.items():
            if isinstance(value, str) and (value == "" or ":" in value or value.startswith("#")):
                fh.write(f'{key}: "{value}"\n')
            else:
                fh.write(f"{key}: {value}\n")

    print(f"wrote {len(rows)} line(s) -> {jsonl_path.relative_to(REPO_ROOT)}")
    print(f"wrote meta           -> {meta_path.relative_to(REPO_ROOT)}")
    return jsonl_path
