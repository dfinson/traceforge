"""Azure OpenAI backend for the labeling / synthetic-data framework.

A single-turn chat-completion backend that mirrors
:class:`...backends.copilot_sdk.CopilotSdkBackend` (same ``complete(prompt, *,
system_message)`` shape, same :class:`CompletionResult`) so callers can swap the
slow SDK oracle for fast Azure OpenAI without touching generator code.

Why this exists: the Copilot-SDK oracle spawns a Node child per call and pays
agent-loop latency; scaling synthetic (request -> title) data through it costs
hours of wall-clock. Azure OpenAI on the ``cog-coderecon-lab`` account serves the
same task in minutes at trivial dollar cost.

Auth: the lab account has key auth disabled, so this uses an Entra (AAD) bearer
token minted by the already-authenticated ``az`` CLI
(``az account get-access-token --resource https://cognitiveservices.azure.com``).
The token is cached and refreshed a few minutes before expiry; refreshing is
serialized so a burst of concurrent calls mints at most one new token.

Reasoning vs non-reasoning models are handled by capability, not by hard-coded
model names the caller passes ``reasoning=True`` for a reasoning deployment
(e.g. ``gpt-5-mini``), which switches the request to ``max_completion_tokens`` +
``reasoning_effort='minimal'`` (0 reasoning tokens, no ``temperature``); the
non-reasoning path uses ``max_tokens`` + ``temperature``.

Footprint: pure bounded network I/O. Concurrency is enforced by a caller-owned
:class:`asyncio.Semaphore`, exactly like the SDK backend, so this object stays
stateless apart from the shared token cache.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from dataclasses import dataclass, field

import httpx

_AAD_RESOURCE = "https://cognitiveservices.azure.com"
# Refresh this many seconds before the token's own expiry so an in-flight burst
# never races a hard expiry. Not a tuned model knob -- a clock-safety margin.
_TOKEN_REFRESH_MARGIN_S = 300.0


@dataclass(frozen=True)
class CompletionResult:
    """Outcome of a single chat completion (shape-compatible with the SDK backend)."""

    text: str
    error: str | None = None
    chunks: int = 0


class _AadTokenCache:
    """Process-wide Entra token cache backed by the ``az`` CLI.

    One cache is shared across every backend instance so N deployments talking to
    the same resource mint one token, not N. Refresh is guarded by an asyncio lock
    so a concurrent burst triggers a single ``az`` subprocess.
    """

    def __init__(self) -> None:
        self._token: str = ""
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    async def token(self) -> str:
        now = time.time()
        if self._token and now < self._expires_at - _TOKEN_REFRESH_MARGIN_S:
            return self._token
        async with self._lock:
            now = time.time()
            if self._token and now < self._expires_at - _TOKEN_REFRESH_MARGIN_S:
                return self._token
            tok, exp = await asyncio.to_thread(self._mint)
            self._token, self._expires_at = tok, exp
            return self._token

    @staticmethod
    def _mint() -> tuple[str, float]:
        proc = subprocess.run(
            [
                "az",
                "account",
                "get-access-token",
                "--resource",
                _AAD_RESOURCE,
                "-o",
                "json",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=True,
            timeout=60,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"az get-access-token failed: {proc.stderr.strip()[:400]}")
        data = json.loads(proc.stdout)
        token = data["accessToken"]
        # ``expiresOn`` is local-time and format-variable across az versions; the
        # returned token is valid ~60-75 min, so anchor expiry off issue time with
        # a conservative lifetime instead of parsing the string.
        return token, time.time() + 3000.0


_TOKEN_CACHE = _AadTokenCache()


@dataclass
class AoaiConfig:
    """One Azure OpenAI deployment target."""

    endpoint: str
    deployment: str
    reasoning: bool = False
    api_version: str = "2025-04-01-preview"
    temperature: float = 0.9
    max_output_tokens: int = 900
    reasoning_effort: str = "minimal"
    max_retries: int = 5
    timeout_s: float = 120.0
    _headers: dict = field(default_factory=dict, repr=False)


class AzureOpenAIBackend:
    """Single-turn Azure OpenAI chat-completion backend (SDK-backend compatible)."""

    def __init__(self, config: AoaiConfig, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self.name = f"aoai:{config.deployment}"
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> AzureOpenAIBackend:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._config.timeout_s)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _url(self) -> str:
        c = self._config
        base = c.endpoint.rstrip("/")
        return f"{base}/openai/deployments/{c.deployment}/chat/completions?api-version={c.api_version}"

    def _body(self, prompt: str, system_message: str | None, temperature: float | None) -> dict:
        c = self._config
        messages: list[dict] = []
        if system_message:
            messages.append({"role": "system", "content": system_message})
        messages.append({"role": "user", "content": prompt})
        body: dict = {"messages": messages}
        if c.reasoning:
            body["max_completion_tokens"] = c.max_output_tokens
            body["reasoning_effort"] = c.reasoning_effort
        else:
            body["max_tokens"] = c.max_output_tokens
            body["temperature"] = c.temperature if temperature is None else temperature
        return body

    async def complete(
        self,
        prompt: str,
        *,
        system_message: str | None = None,
        temperature: float | None = None,
    ) -> CompletionResult:
        c = self._config
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=c.timeout_s)
            self._owns_client = True
        last_err: str | None = None
        for attempt in range(c.max_retries + 1):
            try:
                token = await _TOKEN_CACHE.token()
                resp = await self._client.post(
                    self._url(),
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json=self._body(prompt, system_message, temperature),
                )
                if resp.status_code == 200:
                    data = resp.json()
                    text = (data["choices"][0]["message"].get("content") or "").strip()
                    return CompletionResult(
                        text=text,
                        error=None if text else "empty",
                        chunks=1 if text else 0,
                    )
                # Retryable: throttling + transient server errors. Honor Retry-After.
                if resp.status_code in (429, 500, 502, 503, 504):
                    last_err = f"http_{resp.status_code}: {resp.text[:200]}"
                    await asyncio.sleep(self._backoff(resp, attempt))
                    continue
                # Non-retryable (4xx auth/shape): surface immediately.
                return CompletionResult(
                    text="", error=f"http_{resp.status_code}: {resp.text[:300]}", chunks=0
                )
            except Exception as exc:  # noqa: BLE001 - one call must never abort a batch
                last_err = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(self._backoff(None, attempt))
        return CompletionResult(text="", error=last_err or "unknown_error", chunks=0)

    @staticmethod
    def _backoff(resp: httpx.Response | None, attempt: int) -> float:
        if resp is not None:
            hdr = resp.headers.get("retry-after")
            if hdr:
                try:
                    return min(float(hdr), 60.0)
                except ValueError:
                    pass
        # Exponential backoff, capped. Not a model knob -- standard transport retry.
        return min(2.0**attempt, 30.0)


__all__ = ["AoaiConfig", "AzureOpenAIBackend", "CompletionResult"]
