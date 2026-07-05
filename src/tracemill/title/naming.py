"""Session-title providers: heuristic floor + opt-in LiteLLM API tier.

:func:`build_session_titler` reads :class:`~tracemill.config.SessionNamingConfig`
and returns a plain ``Callable[[str], str]`` -- the session titler used by
:meth:`tracemill.title.TitleInferencer.request_title`.

Resolution:

* ``strategy: heuristic`` (default) -> :class:`HeuristicProvider`, a free, offline
  extractive title over the user's own words (see :mod:`tracemill.title.heuristics`).
* ``strategy: api`` -> :class:`ApiProvider` (LiteLLM), **but only if the provider's
  API key is present in the environment**. When the key is absent -- or any API
  call fails or times out -- the titler transparently falls back to the heuristic,
  so a missing key or a flaky network never errors, blocks, or empties a title.

The API key is never read from config: LiteLLM sources it from the provider's
conventional env var; ``api.api_key_env`` only overrides *which* var to read.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Callable

from .hygiene import clean_title
from .heuristics import heuristic_title

if TYPE_CHECKING:
    from tracemill.config.models import (
        SessionNamingApiConfig,
        SessionNamingConfig,
        SessionNamingHeuristicConfig,
    )

logger = logging.getLogger(__name__)

#: Kept terse and instruction-tight; the model must return only the title.
_SYSTEM = (
    "You name a developer's coding session from their first message. "
    "Reply with ONLY a short, specific title in imperative mood, at most {max_words} "
    "words, no trailing punctuation, no quotes. Name the concrete task, not the tone."
)


class HeuristicProvider:
    """The offline extractive floor. Always available; needs no key or network."""

    def __init__(self, cfg: "SessionNamingHeuristicConfig") -> None:
        self._method = cfg.method
        self._max_words = cfg.max_words
        self._max_chars = cfg.max_chars

    def title(self, text: str) -> str:
        return heuristic_title(text, self._method, self._max_words, self._max_chars)

    __call__ = title


class ApiProvider:
    """Abstractive session titles via LiteLLM (any provider + local runtimes).

    Returns ``""`` on any failure so the caller can fall back to the heuristic.
    """

    def __init__(self, cfg: "SessionNamingApiConfig", max_words: int = 8) -> None:
        self._cfg = cfg
        self._max_words = max_words

    def title(self, text: str) -> str:
        try:
            import litellm
        except ImportError:  # pragma: no cover - litellm ships as a base dep
            logger.debug("litellm unavailable; session-naming API tier disabled")
            return ""
        cfg = self._cfg
        system = _SYSTEM.format(max_words=self._max_words)
        kwargs: dict = {
            "model": cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            "max_tokens": cfg.max_tokens,
            "timeout": cfg.timeout,
            "temperature": 0.0,
        }
        if cfg.api_base:
            kwargs["api_base"] = cfg.api_base
        if cfg.api_key_env:
            key = os.environ.get(cfg.api_key_env)
            if not key:
                return ""
            kwargs["api_key"] = key
        try:
            resp = litellm.completion(**kwargs)
            raw = (resp.choices[0].message.content or "").strip()
        except Exception as exc:  # noqa: BLE001 - any provider/network error -> fallback
            logger.warning("session-naming API call failed (%s); using heuristic", exc)
            return ""
        return clean_title(raw)

    __call__ = title


class _WithFallback:
    """Try the API tier; fall back to the heuristic on empty/failed output."""

    def __init__(self, primary: ApiProvider, fallback: HeuristicProvider) -> None:
        self._primary = primary
        self._fallback = fallback

    def title(self, text: str) -> str:
        out = self._primary.title(text)
        return out if out and out.strip() else self._fallback.title(text)

    __call__ = title


def _api_key_present(cfg: "SessionNamingApiConfig") -> bool:
    """True iff the API tier can authenticate right now (opt-in gate).

    Honors an explicit ``api_key_env`` override; otherwise defers to LiteLLM's
    provider-aware environment validation so any provider's conventional key var
    counts. Absent LiteLLM or on any error, reports not-present (-> heuristic).
    """
    if cfg.api_base and not cfg.api_key_env:
        # Local/self-hosted runtimes (ollama/vllm) often need no key.
        return True
    if cfg.api_key_env:
        return bool(os.environ.get(cfg.api_key_env))
    try:
        from litellm import validate_environment

        info = validate_environment(cfg.model)
        return bool(info.get("keys_in_environment"))
    except Exception:  # noqa: BLE001 - litellm missing or signature drift -> off
        return False


def build_session_titler(cfg: "SessionNamingConfig | None" = None) -> Callable[[str], str]:
    """Build the configured session titler (defaults resolved from global config).

    The returned callable maps the first substantive user message to a title.
    Falls back to the heuristic whenever the API tier is unconfigured/unusable.
    """
    if cfg is None:
        from tracemill.config import get_config

        cfg = get_config().title.session_naming

    heuristic = HeuristicProvider(cfg.heuristic)
    if cfg.strategy == "api" and _api_key_present(cfg.api):
        return _WithFallback(ApiProvider(cfg.api, cfg.heuristic.max_words), heuristic)
    if cfg.strategy == "api":
        logger.info(
            "title.session_naming.strategy=api but no API key for model %r found in "
            "the environment; using the offline heuristic.",
            cfg.api.model,
        )
    return heuristic


__all__ = [
    "HeuristicProvider",
    "ApiProvider",
    "build_session_titler",
]
