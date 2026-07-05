"""Unit tests for the session-title providers (heuristic floor + opt-in API tier).

The provider layer (:mod:`tracemill.title.naming`) chooses between the offline
heuristic and a LiteLLM API tier based on config *and* whether an API key is
present. The load-bearing contract is that the API tier is strictly opt-in and
never load-bearing: absent a key, or on any API failure/timeout, session naming
transparently falls back to the heuristic and never errors, blocks, or empties.
"""

from __future__ import annotations

from types import SimpleNamespace

from tracemill.config.models import (
    SessionNamingApiConfig,
    SessionNamingConfig,
    SessionNamingHeuristicConfig,
)
from tracemill.title.naming import (
    ApiProvider,
    HeuristicProvider,
    _api_key_present,
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
