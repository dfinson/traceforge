"""VS Code Copilot Chat preprocessor — replay the ChatModel journal.

VS Code persists each Copilot Chat session as a line-delimited journal at
``workspaceStorage/<hash>/chatSessions/<sessionId>.jsonl`` (ChatModel version 3).
Each physical line is one journal record:

* ``{"kind": 0, "v": {...}}``            — full snapshot (session start)
* ``{"kind": 1, "k": [path...], "v": x}`` — *set* value ``x`` at ``path``
* ``{"kind": 2, "k": [path...], "v": [...]}`` — *append* (list-extend) at ``path``

Paths mix dict keys and integer array indices, e.g.
``["requests", 2, "response"]``. The interesting state lives under ``requests[]``:
each request carries the user ``message``, a streamed ``response`` part list
(``thinking`` / ``toolInvocationSerialized`` / markdown text / file refs),
request-level ``codeCitations`` and ``elapsedMs``. Final response parts may be
written after ``elapsedMs``.

Because the adapter feeds records one physical line at a time, this preprocessor
keeps a small amount of module-level state (a mirror of the ``requests`` index ->
metadata map) so streamed response parts can be attributed to their originating
request. State is reset whenever a snapshot (``kind == 0``) is seen, which is the
first record of every session file, so state never bleeds across sessions.
"""

from __future__ import annotations

from typing import Any

from traceforge.preprocessors.registry import register_preprocessor

# Per-session reconstruction state (reset on every snapshot record).
_REQ_IDS: dict[int, str | None] = {}
_REQ_MODELS: dict[int, Any] = {}
_REQ_TS: dict[int, Any] = {}
_REQ_CITATION_COUNTS: dict[int, int] = {}
_REQ_COUNT = [0]  # boxed int so helpers can mutate it


def _reset() -> None:
    _REQ_IDS.clear()
    _REQ_MODELS.clear()
    _REQ_TS.clear()
    _REQ_CITATION_COUNTS.clear()
    _REQ_COUNT[0] = 0


def _agent_id(req: dict[str, Any]) -> Any:
    agent = req.get("agent")
    if isinstance(agent, dict):
        return agent.get("id") or agent.get("name")
    return agent


def _emit_part(part: Any, idx: int) -> dict[str, Any] | None:
    """Turn one streamed ``response`` part into a flat, typed dict."""
    if not isinstance(part, dict):
        return None
    flat = dict(part)
    # Markdown content parts have no ``kind`` discriminator; everything else
    # (thinking / toolInvocationSerialized / inlineReference / ...) does.
    flat["event_type"] = part.get("kind") or "assistant_text"
    flat["request_id"] = _REQ_IDS.get(idx)
    flat["model"] = _REQ_MODELS.get(idx)
    flat["timestamp"] = _REQ_TS.get(idx)
    return flat


def _emit_citations(citations: Any, idx: int) -> list[dict[str, Any]]:
    """Turn request-level VS Code code citations into informational events."""
    if not isinstance(citations, list):
        return []

    citations = [citation for citation in citations if isinstance(citation, dict)]
    start = min(_REQ_CITATION_COUNTS.get(idx, 0), len(citations))
    _REQ_CITATION_COUNTS[idx] = len(citations)

    out = []
    for citation in citations[start:]:
        out.append(
            {
                "event_type": "code_citation",
                "request_id": _REQ_IDS.get(idx),
                "model": _REQ_MODELS.get(idx),
                "value": citation.get("value"),
                "license": citation.get("license"),
                "snippet": citation.get("snippet"),
                "timestamp": _REQ_TS.get(idx),
            }
        )
    return out


def _emit_completion(elapsed_ms: int | float, idx: int) -> dict[str, Any]:
    """Emit active generation duration from the v3 request-level field."""
    return {
        "event_type": "request_result",
        "request_id": _REQ_IDS.get(idx),
        "model": _REQ_MODELS.get(idx),
        "elapsed_ms": elapsed_ms,
        "timestamp": _REQ_TS.get(idx),
    }


def _emit_request(req: dict[str, Any], idx: int) -> list[dict[str, Any]]:
    """Register a request at ``idx`` and emit its user message + any inline parts."""
    _REQ_IDS[idx] = req.get("requestId")
    _REQ_MODELS[idx] = req.get("modelId")
    _REQ_TS[idx] = req.get("timestamp")
    _REQ_CITATION_COUNTS.pop(idx, None)

    message = req.get("message")
    text = message.get("text") if isinstance(message, dict) else message

    out: list[dict[str, Any]] = [
        {
            "event_type": "user_message",
            "request_id": req.get("requestId"),
            "model": req.get("modelId"),
            "agent": _agent_id(req),
            "text": text,
            "timestamp": req.get("timestamp"),
        }
    ]
    # A compacted journal may carry the full response/result inline on the
    # request itself; a streamed one appends them later. Emit whatever is here.
    for part in req.get("response") or []:
        emitted = _emit_part(part, idx)
        if emitted is not None:
            out.append(emitted)
    out.extend(_emit_citations(req.get("codeCitations"), idx))
    elapsed_ms = req.get("elapsedMs")
    if isinstance(elapsed_ms, (int, float)) and not isinstance(elapsed_ms, bool):
        out.append(_emit_completion(elapsed_ms, idx))
    return out


@register_preprocessor("copilot_vscode")
def preprocess_copilot_vscode(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand one VS Code ChatModel journal record into typed flat events."""
    # Passthrough for already-typed rows (generic conformance probes feed a line
    # keyed by the post-preprocessor type_field, with no journal ``kind``).
    if "kind" not in obj and obj.get("event_type"):
        return [obj]

    kind = obj.get("kind")

    # ── Snapshot: session start (+ any requests already present) ──────────────
    if kind == 0:
        _reset()
        snap = obj.get("v") if isinstance(obj.get("v"), dict) else {}
        out: list[dict[str, Any]] = [
            {
                "event_type": "session_started",
                "session_id": snap.get("sessionId"),
                "version": snap.get("version"),
                "responder": snap.get("responderUsername"),
                "initial_location": snap.get("initialLocation"),
                "timestamp": snap.get("creationDate"),
            }
        ]
        for req in snap.get("requests") or []:
            if isinstance(req, dict):
                out.extend(_emit_request(req, _REQ_COUNT[0]))
                _REQ_COUNT[0] += 1
        return out

    path = obj.get("k")
    value = obj.get("v")
    if not isinstance(path, list) or not path:
        return []

    # ── Append (list-extend) records ──────────────────────────────────────────
    if kind == 2:
        # New requests appended to the top-level requests array.
        if path == ["requests"]:
            out = []
            for req in value or []:
                if isinstance(req, dict):
                    out.extend(_emit_request(req, _REQ_COUNT[0]))
                    _REQ_COUNT[0] += 1
            return out
        # Streamed response parts appended to requests[i].response.
        if len(path) == 3 and path[0] == "requests" and path[2] == "response":
            idx = path[1]
            out = []
            for part in value or []:
                emitted = _emit_part(part, idx)
                if emitted is not None:
                    out.append(emitted)
            return out
        return []

    # ── Set records: request metadata serialized outside response parts ───────
    if kind == 1:
        if len(path) == 3 and path[0] == "requests":
            idx = path[1]
            if path[2] == "codeCitations":
                return _emit_citations(value, idx)
            if (
                path[2] == "elapsedMs"
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
            ):
                return [_emit_completion(value, idx)]
        return []

    return []
