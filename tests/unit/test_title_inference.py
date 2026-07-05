"""Unit + smoke guards for the torch-free ORT titler.

The hygiene tests are pure-stdlib and always run. The model test loads the
packaged int8 ONNX titler via onnxruntime; it is skipped if the optional
``title`` deps or artifacts are absent so the core suite stays dependency-light.
"""

from __future__ import annotations

import importlib.util

import pytest

from tracemill.title._resolve import span_dir as _span_dir
from tracemill.title.hygiene import best_of, clean_title, norm_key, pick_distinct

_HAS_ORT = importlib.util.find_spec("onnxruntime") is not None
_HAS_TOK = importlib.util.find_spec("tokenizers") is not None
_HAS_MODEL = _span_dir() is not None
_SERVEABLE = _HAS_ORT and _HAS_TOK and _HAS_MODEL


def test_clean_title_collapses_repeats_and_capitalises():
    assert clean_title("  run   tests run tests ") == "Run tests"
    assert clean_title("create and create") == "Create"
    assert clean_title("-: fix the bug .") == "Fix the bug"


def test_best_of_skips_degenerate_beam():
    # First candidate has a long vowelless garble word; second is clean.
    assert best_of(["Rstrtsrvr quickly", "Restart the server"]) == "Restart the server"


def test_norm_key_ignores_plurals_and_stopwords():
    assert norm_key("Run the tests") == norm_key("Run test")


def test_pick_distinct_avoids_parent_restatement():
    used: set = set()
    parent = pick_distinct(used, ["Add retry logic"])
    child = pick_distinct(used, ["Add retry logic", "Add backoff to client"])
    assert child != parent
    assert child == "Add backoff to client"


def test_ground_order_demotes_ungrounded_identifiers():
    from tracemill.title.inference import _ground_order, _is_grounded

    ctx = "intent: fix retry | actions: edit | files: http_client.py | symbols: request_with_retry"
    # invents an identifier the context never names -> ungrounded
    assert not _is_grounded("Update github-mcp-server-ample", ctx.lower())
    # only names identifiers present in the context -> grounded
    assert _is_grounded("Fix request_with_retry in http_client.py", ctx.lower())
    # plain-English titles carry no identifier signal -> always grounded
    assert _is_grounded("Fix the retry logic", ctx.lower())

    cands = ["Update github-mcp-server-ample", "Fix retry logic in http_client.py"]
    assert _ground_order(cands, ctx)[0] == "Fix retry logic in http_client.py"


def test_ground_order_keeps_all_when_every_beam_hallucinates():
    from tracemill.title.inference import _ground_order

    ctx = "intent: do work | files: a.py"
    cands = ["Update zzz_made_up", "Edit other-invented-thing"]
    # never returns empty: ungrounded candidates are preserved, order intact
    assert _ground_order(cands, ctx) == cands


@pytest.mark.skipif(not _SERVEABLE, reason="title extra / model artifacts absent")
def test_model_titles_a_distilled_context():
    from tracemill.title import TitleModel

    model = TitleModel.load()
    ctx = (
        "intent: add retry logic to the HTTP client | actions: edit, run | "
        "files: client.py | symbols: request_with_retry"
    )
    out = model.title(ctx)
    assert isinstance(out, str) and out
    assert len(out.split()) <= 8  # short, human-readable
    assert out == clean_title(out)  # already hygienic
