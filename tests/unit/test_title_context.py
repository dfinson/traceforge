"""Unit tests for the serve-side span context distiller.

The distiller is the serve half of the title train/serve contract: it must run
on the same :func:`event_to_feature_row` projection the corpus was built from
and extract the same source-agnostic slots, so these tests pin the slot grammar,
the gold-intent shortcut, and the learned boilerplate filter.
"""

from __future__ import annotations

import json

from traceforge.title.context import distilled_context


def _row(kind="tool.call", tool_name=None, payload=None, binaries=(), structure=()):
    return {
        "kind": kind,
        "tool_name": tool_name,
        "payload_json": json.dumps(payload) if payload is not None else None,
        "binaries": list(binaries),
        "structure": list(structure),
    }


def test_no_signal_when_empty():
    assert distilled_context([_row()]) == "(no signal)"


def test_intent_slot_uses_report_intent_gerund():
    rows = [
        _row(
            tool_name="report_intent",
            payload={
                "tool_name": "report_intent",
                "arguments": {"intent": "Adding retry logic to the client"},
            },
        )
    ]
    ctx = distilled_context(rows)
    assert ctx.startswith("intent: Adding retry logic to the client")


def test_actions_slot_dedups_and_orders_tools():
    rows = [_row(tool_name="edit"), _row(tool_name="shell"), _row(tool_name="edit")]
    ctx = distilled_context(rows)
    assert "actions: edit, shell" in ctx


def test_files_slot_excludes_learned_boilerplate():
    # users.js is in the packaged boilerplate set; client.py is not.
    rows = [
        _row(
            tool_name="edit", payload={"arguments": {"path": "client.py"}}, binaries=["client.py"]
        ),
        _row(tool_name="edit", payload={"arguments": {"path": "users.js"}}, binaries=["users.js"]),
    ]
    ctx = distilled_context(rows)
    assert "files:" in ctx
    assert "client.py" in ctx
    assert "users.js" not in ctx


def test_symbols_slot_prefers_backticked_identifiers():
    rows = [
        _row(
            kind="message.assistant",
            payload={"content": "Updated `validate_token` to reject expired tokens"},
        )
    ]
    ctx = distilled_context(rows)
    assert "validate_token" in ctx


def test_notes_slot_from_assistant_narration():
    rows = [
        _row(
            kind="message.assistant",
            payload={"content": "I refactored the parser to stream tokens lazily."},
        )
    ]
    ctx = distilled_context(rows)
    assert "notes:" in ctx
    assert "refactored the parser" in ctx
