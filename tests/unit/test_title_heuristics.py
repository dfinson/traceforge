"""Unit tests for the deterministic session-naming heuristics.

These are the zero-cost default floor for session naming: extractive titles over
the user's own words. The tests pin the observable contract of each method
(:func:`heuristic_title` and its four strategies) plus the shared polish steps --
preamble stripping, trailing function-word trimming, identifier-preserving
capitalization, and the word/char budgets.
"""

from __future__ import annotations

import pytest

from tracemill.title.heuristics import clip, heuristic_title, hybrid, imperative, keyphrase


def test_empty_and_whitespace_yield_empty():
    assert heuristic_title("") == ""
    assert heuristic_title("   \n\t ") == ""
    assert heuristic_title("", "clip") == ""


def test_clip_strips_preamble_and_first_sentence():
    # Conversational preamble ("can you please") is dropped; only the first
    # sentence is kept.
    out = clip("can you please fix the login bug. also update the docs later")
    assert out == "Fix the login bug"


def test_clip_trims_trailing_function_words_when_clipped():
    # An 8-word clip that would end on a preposition/article is trimmed so it
    # doesn't read as cut off mid-thought.
    out = clip("Please add retry logic to the HTTP client with backoff")
    assert out == "Add retry logic to the HTTP client"
    assert not out.lower().endswith(" with")


def test_capitalization_preserves_leading_identifier():
    # A leading code identifier must not be corrupted by title-casing.
    assert clip("getUser() returns None sometimes and it breaks").startswith("getUser()")
    assert clip("auth.py needs a refresh path").startswith("auth.py")


def test_capitalization_uppercases_plain_leading_word():
    assert clip("migrate the database to postgres").startswith("Migrate ")


def test_imperative_anchors_on_leading_verb():
    assert imperative("fix the failing pagination test") == "Fix the failing pagination test"


def test_imperative_returns_none_without_leading_verb():
    # No recognized action verb in the opening -> None (caller falls back).
    assert imperative("the login page throws a 500 error") is None


def test_imperative_dispatch_falls_back_to_clip():
    # Via heuristic_title, the imperative method falls back to clip when there is
    # no leading verb, so it never returns None to the caller.
    out = heuristic_title("the login page throws a 500 error", "imperative")
    assert out == "The login page throws a 500 error"


def test_hybrid_leads_with_salient_identifier():
    # The identifier is buried past the clip window, so hybrid surfaces it as a
    # lead rather than losing it.
    out = hybrid("can you look into why the whole checkout flow breaks in payments/gateway.py")
    assert out.startswith("payments/gateway.py")


def test_hybrid_prefers_imperative_when_no_identifier():
    out = hybrid("refactor the session manager to be async")
    assert out == "Refactor the session manager to be async"


def test_keyphrase_extracts_salient_phrase():
    out = keyphrase("I'm trying to understand why my websocket keeps disconnecting")
    assert "websocket" in out.lower()


def test_word_budget_is_respected():
    text = "add one two three four five six seven eight nine ten eleven twelve"
    out = heuristic_title(text, "clip", max_words=5)
    assert len(out.split()) <= 5


def test_char_budget_is_respected():
    text = "implement a really long and rambling description that keeps going forever"
    out = heuristic_title(text, "clip", max_words=50, max_chars=30)
    assert len(out) <= 30


def test_hybrid_caps_overlong_salient_identifier():
    # A substantive request whose salient identifier alone exceeds the char
    # budget must still respect max_chars (regression: hybrid used to return the
    # bare over-length identifier when no room remained for the gist).
    text = "fix the SomeReallyLongClassNameThatGoesOnAndOnAndKeepsGoingForever handler"
    out = heuristic_title(text, "hybrid", max_words=8, max_chars=60)
    assert len(out) <= 60


def test_unknown_method_defaults_to_hybrid():
    text = "refactor the session manager to be async"
    assert heuristic_title(text, "not-a-method") == heuristic_title(text, "hybrid")


@pytest.mark.parametrize("method", ["clip", "imperative", "keyphrase", "hybrid"])
def test_all_methods_return_nonempty_on_real_request(method):
    out = heuristic_title("please add a status endpoint that returns uptime as json", method)
    assert out and out.strip() == out
