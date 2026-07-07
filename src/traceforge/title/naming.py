"""Title providers: offline floor + opt-in LiteLLM API tier (session and span).

This module owns the LiteLLM plumbing, the opt-in key-presence gate, and the
graceful-fallback contract shared by both configurable title surfaces:

* **Session naming.** :func:`build_session_titler_split` reads
  :class:`~traceforge.config.SessionNamingConfig` and returns a
  :class:`SessionTitler` (an immediate :class:`HeuristicProvider` floor + an
  optional :class:`ApiProvider` refiner). The heuristic is a free, offline
  extractive title over the user's own words; the API tier engages only when a
  key is present.
* **Activity/step titling.** :func:`build_activity_refiner` reads
  :class:`~traceforge.config.ActivityTitlingConfig` and returns an optional
  :class:`ActivityApiProvider` refiner. Here the offline floor is the *packaged
  ONNX span model* (owned by :mod:`traceforge.title.inferencer`, emitted the
  instant an activity closes); the refiner, when configured and keyed, upgrades
  the activity title and all its step titles in one call, off the hot path.

In both cases the API tier is **strictly opt-in and never load-bearing**: absent
a key -- or on any API call that fails, times out, or returns nothing usable --
the offline floor stands, so a missing key or a flaky network never errors,
blocks, or empties a title.

The API key is never read from config: LiteLLM sources it from the provider's
conventional env var; ``api.api_key_env`` only overrides *which* var to read.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from .hygiene import clean_title
from .heuristics import heuristic_title

if TYPE_CHECKING:
    from traceforge.config.models import (
        ActivityTitlingApiConfig,
        ActivityTitlingConfig,
        SessionNamingApiConfig,
        SessionNamingConfig,
        SessionNamingHeuristicConfig,
        TitleApiConfig,
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


def _api_key_present(cfg: "TitleApiConfig") -> bool:
    """True iff the API tier can authenticate right now (opt-in gate).

    Shared by both title surfaces (session naming and activity/step titling),
    since both ``api`` sub-configs derive from :class:`TitleApiConfig`. Honors an
    explicit ``api_key_env`` override; otherwise defers to LiteLLM's
    provider-aware environment validation so any provider's conventional key var
    counts. Absent LiteLLM or on any error, reports not-present (-> offline floor).
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

    This is the *blocking* single-callable form (API tier tried inline, then the
    heuristic). The live pipeline instead uses :func:`build_session_titler_split`
    so it can emit the heuristic immediately and refine via the API off the hot
    path; this form is retained for callers that want one synchronous title.
    """
    split = build_session_titler_split(cfg)
    if split.api_refiner is not None:
        return _WithFallback(split.api_refiner, split.heuristic)
    return split.heuristic


@dataclass(frozen=True)
class SessionTitler:
    """A session titler split into an immediate floor and an optional refiner.

    * ``heuristic`` -- the free, offline, non-blocking title. Always present; it
      is emitted the instant the opening request arrives so the event stream
      never waits on a title.
    * ``api_refiner`` -- present only when ``strategy=api`` *and* a usable API key
      is configured. It returns an abstractive upgrade of the title (or ``""`` on
      any failure/timeout) and is meant to run **off the hot path**: the pipeline
      runs it in a worker thread and emits its result as a later session
      :class:`~traceforge.types.TitleUpdate`. ``None`` when no usable API tier is
      configured, in which case the heuristic is the final title.
    """

    heuristic: Callable[[str], str]
    api_refiner: Callable[[str], str] | None


def build_session_titler_split(
    cfg: "SessionNamingConfig | None" = None,
) -> SessionTitler:
    """Build the session titler as a non-blocking heuristic + optional refiner.

    Unlike :func:`build_session_titler`, the API tier (when configured and keyed)
    is returned *separately* as ``api_refiner`` rather than wrapped inline, so the
    pipeline can emit the heuristic title immediately and only later replace it
    with the API result — never blocking live event emission on the network.
    """
    if cfg is None:
        from traceforge.config import get_config

        cfg = get_config().title.session_naming

    heuristic = HeuristicProvider(cfg.heuristic)
    if cfg.strategy == "api" and _api_key_present(cfg.api):
        return SessionTitler(heuristic, ApiProvider(cfg.api, cfg.heuristic.max_words))
    if cfg.strategy == "api":
        logger.info(
            "title.session_naming.strategy=api but no API key for model %r found in "
            "the environment; using the offline heuristic.",
            cfg.api.model,
        )
    return SessionTitler(heuristic, None)


# ─── Activity/step (span) title refinement ───────────────────────────────────

#: Terse, instruction-tight span prompt. The model sees each segment's distilled
#: context and must return ONLY a JSON object titling the activity and its steps.
_SPAN_SYSTEM = (
    "You title one activity in a developer's coding session and each of its "
    "numbered steps, from their distilled contexts. Reply with ONLY a JSON "
    'object of the form {{"activity": "<title>", "steps": ["<step 1>", '
    '"<step 2>", ...]}} -- exactly one step title per numbered step, in order. '
    "Each title is a short, specific phrase in imperative or gerund mood, at most "
    "{max_words} words, no trailing punctuation, no quotes. Keep every step title "
    "distinct from the activity title and from the other step titles."
)


@dataclass(frozen=True)
class ActivitySpan:
    """The distilled context of a closed activity and its steps, fed to the API.

    ``activity_context`` is the distilled context over the whole activity;
    ``step_contexts`` holds one distilled context per step, in order. Both are
    produced by :func:`traceforge.title.context.distilled_context`, so the API
    tier sees the same source-agnostic slots the packaged model was trained on.
    """

    activity_context: str
    step_contexts: list[str]


@dataclass(frozen=True)
class ActivityTitles:
    """The API's proposed titles for an :class:`ActivitySpan`.

    ``activity`` is the refined activity title (``None`` when the API declined or
    failed); ``steps`` is aligned index-for-index with the span's ``step_contexts``
    -- ``None`` in any slot the API did not usefully cover, so the caller keeps
    that segment's packaged-model title.
    """

    activity: str | None
    steps: list[str | None]


def _empty_titles(n_steps: int) -> ActivityTitles:
    return ActivityTitles(None, [None] * n_steps)


def _loads_json_object(raw: str):
    """Best-effort parse of a JSON object from a model reply.

    Tolerates a model that wraps the object in prose or code fences by falling
    back to the outermost ``{...}`` slice. Returns the parsed object or ``None``.
    """
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(raw[start : end + 1])
        except (ValueError, TypeError):
            return None
    return None


class ActivityApiProvider:
    """Abstractive activity/step titles via LiteLLM (any provider + local runtimes).

    One :meth:`refine` call per closed activity returns the activity title and all
    of its step titles together, so the model sees the full activity context and
    can keep step titles distinct. Returns :class:`ActivityTitles` with ``None``
    slots on any failure (missing key, provider/network error, timeout, or an
    unparseable reply) so the caller falls back to the packaged-model titles.
    """

    def __init__(self, cfg: "ActivityTitlingApiConfig", max_words: int = 8) -> None:
        self._cfg = cfg
        self._max_words = max_words

    @staticmethod
    def _render(span: ActivitySpan) -> str:
        lines = [f"activity: {span.activity_context}"]
        for i, ctx in enumerate(span.step_contexts, start=1):
            lines.append(f"step {i}: {ctx}")
        return "\n".join(lines)

    def refine(self, span: ActivitySpan) -> ActivityTitles:
        n = len(span.step_contexts)
        try:
            import litellm
        except ImportError:  # pragma: no cover - litellm ships as a base dep
            logger.debug("litellm unavailable; activity-titling API tier disabled")
            return _empty_titles(n)
        cfg = self._cfg
        system = _SPAN_SYSTEM.format(max_words=self._max_words)
        kwargs: dict = {
            "model": cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": self._render(span)},
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
                return _empty_titles(n)
            kwargs["api_key"] = key
        try:
            resp = litellm.completion(**kwargs)
            raw = (resp.choices[0].message.content or "").strip()
        except Exception as exc:  # noqa: BLE001 - any provider/network error -> fallback
            logger.warning("activity-titling API call failed (%s); using packaged model", exc)
            return _empty_titles(n)
        return self._parse(raw, n)

    @staticmethod
    def _parse(raw: str, n_steps: int) -> ActivityTitles:
        data = _loads_json_object(raw)
        if not isinstance(data, dict):
            return _empty_titles(n_steps)
        act = data.get("activity")
        activity = clean_title(act) if isinstance(act, str) and act.strip() else None
        steps: list[str | None] = [None] * n_steps
        raw_steps = data.get("steps")
        if isinstance(raw_steps, list):
            for i in range(min(n_steps, len(raw_steps))):
                s = raw_steps[i]
                if isinstance(s, str) and s.strip():
                    cleaned = clean_title(s)
                    steps[i] = cleaned or None
        return ActivityTitles(activity or None, steps)

    __call__ = refine


def build_activity_refiner(
    cfg: "ActivityTitlingConfig | None" = None,
) -> "ActivityApiProvider | None":
    """Build the optional off-hot-path activity/step title refiner.

    Returns an :class:`ActivityApiProvider` only when ``strategy=api`` *and* a
    usable API key is present in the environment; otherwise ``None``, in which
    case the packaged ONNX titles (emitted the instant an activity closes) are
    final. Mirrors :func:`build_session_titler_split`'s ``api_refiner`` gate --
    the packaged model itself is the always-present floor, owned by the inferencer.
    """
    if cfg is None:
        from traceforge.config import get_config

        cfg = get_config().title.activity_titling

    if cfg.strategy == "api" and _api_key_present(cfg.api):
        return ActivityApiProvider(cfg.api)
    if cfg.strategy == "api":
        logger.info(
            "title.activity_titling.strategy=api but no API key for model %r found in "
            "the environment; using the packaged ONNX titler.",
            cfg.api.model,
        )
    return None


__all__ = [
    "HeuristicProvider",
    "ApiProvider",
    "SessionTitler",
    "ActivitySpan",
    "ActivityTitles",
    "ActivityApiProvider",
    "build_session_titler",
    "build_session_titler_split",
    "build_activity_refiner",
]
