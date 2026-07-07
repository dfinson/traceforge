"""Unit tests for the session-title providers (heuristic floor + opt-in API tier).

The provider layer (:mod:`traceforge.title.naming`) chooses between the offline
heuristic and a LiteLLM API tier based on config *and* whether an API key is
present. The load-bearing contract is that the API tier is strictly opt-in and
never load-bearing: absent a key, or on any API failure/timeout, session naming
transparently falls back to the heuristic and never errors, blocks, or empties.
"""

from __future__ import annotations

from types import SimpleNamespace

from traceforge.config.models import (
    ActivityTitlingApiConfig,
    ActivityTitlingConfig,
    SessionNamingApiConfig,
    SessionNamingConfig,
    SessionNamingHeuristicConfig,
)
from traceforge.title.naming import (
    ActivityApiProvider,
    ActivitySpan,
    ActivityTitles,
    ApiProvider,
    HeuristicProvider,
    _api_key_present,
    build_activity_refiner,
    build_session_titler,
)


def _fake_completion(content: str):
    """A stand-in for ``litellm.completion`` returning a fixed message content."""

    def _call(**_kw):
        msg = SimpleNamespace(content=content)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    return _call


# ─── heuristic provider ──────────────────────────────────────────────────────


def test_heuristic_provider_titles_from_own_words():
    prov = HeuristicProvider(SessionNamingHeuristicConfig())
    assert prov("please fix the failing pagination test") == "Fix the failing pagination test"


# ─── build_session_titler resolution ─────────────────────────────────────────


def test_default_strategy_builds_heuristic():
    titler = build_session_titler(SessionNamingConfig())
    assert isinstance(titler, HeuristicProvider)


def test_api_strategy_without_key_falls_back_to_heuristic(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = SessionNamingConfig(
        strategy="api", api=SessionNamingApiConfig(api_key_env="OPENAI_API_KEY")
    )
    titler = build_session_titler(cfg)
    # No key -> the opt-in gate keeps us on the pure heuristic (no wrapper).
    assert isinstance(titler, HeuristicProvider)
    assert titler("refactor the auth module") == "Refactor the auth module"


def test_api_strategy_with_key_wraps_with_fallback(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    cfg = SessionNamingConfig(
        strategy="api", api=SessionNamingApiConfig(api_key_env="OPENAI_API_KEY")
    )
    titler = build_session_titler(cfg)
    # A wrapper (not a bare HeuristicProvider) that tries the API first.
    assert not isinstance(titler, HeuristicProvider)
    assert callable(titler)


# ─── _api_key_present opt-in gate ────────────────────────────────────────────


def test_api_key_present_honors_explicit_env(monkeypatch):
    cfg = SessionNamingApiConfig(api_key_env="MY_KEY")
    monkeypatch.delenv("MY_KEY", raising=False)
    assert _api_key_present(cfg) is False
    monkeypatch.setenv("MY_KEY", "x")
    assert _api_key_present(cfg) is True


def test_api_key_present_local_base_needs_no_key(monkeypatch):
    # A local runtime (api_base set, no key var) is treated as reachable.
    cfg = SessionNamingApiConfig(api_base="http://localhost:11434", api_key_env=None)
    assert _api_key_present(cfg) is True


# ─── ApiProvider behavior (litellm mocked) ───────────────────────────────────


def test_api_provider_returns_cleaned_title(monkeypatch):
    import litellm

    monkeypatch.setattr(litellm, "completion", _fake_completion("  Fix the auth bug.  "))
    prov = ApiProvider(SessionNamingApiConfig())
    # clean_title trims whitespace + trailing punctuation and caps the first char.
    assert prov("some rambling request about auth") == "Fix the auth bug"


def test_api_provider_returns_empty_on_exception(monkeypatch):
    import litellm

    def _boom(**_kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(litellm, "completion", _boom)
    prov = ApiProvider(SessionNamingApiConfig())
    assert prov("anything") == ""


def test_api_provider_missing_key_env_returns_empty(monkeypatch):
    # api_key_env names a var that is absent -> provider declines (empty) so the
    # caller falls back, rather than calling the API without a key.
    monkeypatch.delenv("MY_KEY", raising=False)
    prov = ApiProvider(SessionNamingApiConfig(api_key_env="MY_KEY"))
    assert prov("anything") == ""


def test_with_fallback_uses_heuristic_when_api_empty(monkeypatch):
    import litellm

    # API returns empty content -> the wrapper falls back to the heuristic.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(litellm, "completion", _fake_completion("   "))
    cfg = SessionNamingConfig(
        strategy="api", api=SessionNamingApiConfig(api_key_env="OPENAI_API_KEY")
    )
    titler = build_session_titler(cfg)
    assert titler("please fix the failing pagination test") == "Fix the failing pagination test"


def test_with_fallback_prefers_api_when_available(monkeypatch):
    import litellm

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(litellm, "completion", _fake_completion("Wire up the retry backoff"))
    cfg = SessionNamingConfig(
        strategy="api", api=SessionNamingApiConfig(api_key_env="OPENAI_API_KEY")
    )
    titler = build_session_titler(cfg)
    assert titler("some request") == "Wire up the retry backoff"


# ─── activity/step (span) title refinement ───────────────────────────────────
#
# The span refiner mirrors the session API tier exactly (same LiteLLM plumbing,
# key-presence gate, and graceful-fallback contract) but titles a whole closed
# activity and its steps in ONE call, returning JSON. The offline floor here is
# the packaged ONNX span model (owned by the inferencer), so absent a key -- or
# on any API failure -- ``build_activity_refiner`` returns ``None`` and the
# refiner returns all-``None`` titles, leaving the packaged titles standing.


def _span(n_steps: int = 2) -> ActivitySpan:
    return ActivitySpan(
        activity_context="intent: add retry backoff | files: client.py",
        step_contexts=[f"step {i} context" for i in range(n_steps)],
    )


# ─── build_activity_refiner resolution ───────────────────────────────────────


def test_activity_default_strategy_builds_no_refiner():
    # strategy=model (the default) -> the packaged ONNX titles are final; no
    # API refiner is built and the pipeline pays nothing.
    assert build_activity_refiner(ActivityTitlingConfig()) is None


def test_activity_api_without_key_falls_back_to_packaged(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = ActivityTitlingConfig(
        strategy="api", api=ActivityTitlingApiConfig(api_key_env="OPENAI_API_KEY")
    )
    # No key -> the opt-in gate declines, so the packaged model stands (None).
    assert build_activity_refiner(cfg) is None


def test_activity_api_with_key_builds_provider(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    cfg = ActivityTitlingConfig(
        strategy="api", api=ActivityTitlingApiConfig(api_key_env="OPENAI_API_KEY")
    )
    refiner = build_activity_refiner(cfg)
    assert isinstance(refiner, ActivityApiProvider)


# ─── _api_key_present opt-in gate (shared, over the activity api config) ──────


def test_activity_api_key_present_honors_explicit_env(monkeypatch):
    cfg = ActivityTitlingApiConfig(api_key_env="MY_KEY")
    monkeypatch.delenv("MY_KEY", raising=False)
    assert _api_key_present(cfg) is False
    monkeypatch.setenv("MY_KEY", "x")
    assert _api_key_present(cfg) is True


def test_activity_api_key_present_local_base_needs_no_key():
    cfg = ActivityTitlingApiConfig(api_base="http://localhost:11434", api_key_env=None)
    assert _api_key_present(cfg) is True


# ─── ActivityApiProvider behavior (litellm mocked) ───────────────────────────


def test_activity_provider_parses_activity_and_step_titles(monkeypatch):
    import litellm

    reply = '{"activity": "Add retry backoff", "steps": ["Wire client", "Add tests"]}'
    monkeypatch.setattr(litellm, "completion", _fake_completion(reply))
    titles = ActivityApiProvider(ActivityTitlingApiConfig())(_span(2))
    assert titles.activity == "Add retry backoff"
    assert titles.steps == ["Wire client", "Add tests"]


def test_activity_provider_cleans_titles(monkeypatch):
    import litellm

    reply = '{"activity": "  Add retry backoff.  ", "steps": ["  wire client.  "]}'
    monkeypatch.setattr(litellm, "completion", _fake_completion(reply))
    titles = ActivityApiProvider(ActivityTitlingApiConfig())(_span(1))
    # clean_title trims whitespace + trailing punctuation and caps the first char.
    assert titles.activity == "Add retry backoff"
    assert titles.steps == ["Wire client"]


def test_activity_provider_tolerates_prose_wrapped_json(monkeypatch):
    import litellm

    reply = 'Sure! Here you go:\n```json\n{"activity": "Fix bug", "steps": ["Patch it"]}\n```'
    monkeypatch.setattr(litellm, "completion", _fake_completion(reply))
    titles = ActivityApiProvider(ActivityTitlingApiConfig())(_span(1))
    assert titles.activity == "Fix bug"
    assert titles.steps == ["Patch it"]


def test_activity_provider_pads_missing_steps_with_none(monkeypatch):
    import litellm

    # Reply covers only the first of two steps -> the second stays None so its
    # packaged-model title is kept.
    reply = '{"activity": "Add retry backoff", "steps": ["Wire client"]}'
    monkeypatch.setattr(litellm, "completion", _fake_completion(reply))
    titles = ActivityApiProvider(ActivityTitlingApiConfig())(_span(2))
    assert titles.steps == ["Wire client", None]


def test_activity_provider_returns_empty_on_exception(monkeypatch):
    import litellm

    def _boom(**_kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(litellm, "completion", _boom)
    titles = ActivityApiProvider(ActivityTitlingApiConfig())(_span(2))
    # Any provider/network error -> all-None so the caller keeps packaged titles.
    assert titles == ActivityTitles(None, [None, None])


def test_activity_provider_unparseable_reply_returns_empty(monkeypatch):
    import litellm

    monkeypatch.setattr(litellm, "completion", _fake_completion("not json at all"))
    titles = ActivityApiProvider(ActivityTitlingApiConfig())(_span(2))
    assert titles == ActivityTitles(None, [None, None])


def test_activity_provider_missing_key_env_returns_empty(monkeypatch):
    # api_key_env names an absent var -> decline (all-None) rather than call the
    # API keyless; the caller falls back to packaged titles.
    monkeypatch.delenv("MY_KEY", raising=False)
    titles = ActivityApiProvider(ActivityTitlingApiConfig(api_key_env="MY_KEY"))(_span(2))
    assert titles == ActivityTitles(None, [None, None])
