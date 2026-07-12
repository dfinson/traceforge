"""Unit tests for the serve-side span context distiller.

The distiller is the serve half of the title train/serve contract: it must run
on the same :func:`event_to_feature_row` projection the corpus was built from
and extract the same source-agnostic slots, so these tests pin the slot grammar,
the gold-intent shortcut, and the learned boilerplate filter.
"""

from __future__ import annotations

import json

from traceforge.title.context import distilled_context, has_anchor


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


def test_system_events_are_stripped_before_mining():
    # A message.system event carries tool-doc / prompt scaffolding (example file
    # names and identifiers) that is NOT what the agent did this span; it must be
    # dropped so it can't contaminate the files/symbols slots, while the real
    # assistant work is kept.
    rows = [
        _row(
            kind="message.system",
            payload={"content": "Use `example_helper` in config.py per the tool docs"},
            binaries=["config.py"],
        ),
        _row(
            kind="message.assistant",
            payload={"content": "Updated `validate_token` to reject expired tokens"},
        ),
    ]
    ctx = distilled_context(rows)
    assert "validate_token" in ctx  # real work kept
    assert "example_helper" not in ctx  # system-prompt contamination dropped
    assert "config.py" not in ctx


def test_system_only_span_has_no_signal():
    # If the ONLY events are system prompts, there is nothing the agent did ->
    # no signal, rather than a title mined from prompt boilerplate.
    rows = [
        _row(
            kind="message.system",
            tool_name="edit",
            payload={"arguments": {"path": "client.py"}},
            binaries=["client.py"],
        )
    ]
    assert distilled_context(rows) == "(no signal)"


def test_has_anchor_true_for_subject_slots():
    assert has_anchor("intent: Adding retry logic to the client")
    assert has_anchor("actions: edit | files: client.py")
    assert has_anchor("symbols: validate_token")


def test_has_anchor_false_without_subject_slots():
    assert not has_anchor("actions: edit, shell")
    assert not has_anchor("actions: edit | notes: we tried a few things")
    assert not has_anchor("(no signal)")
