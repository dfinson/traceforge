"""Managed live-SDK sources — stream events from vendor agent SDKs.

Unlike the file/poll/watch sources, these sources own the ingest loop: they pull
events from a live vendor agent SDK stream and emit ``RawRecord``s that the
EXISTING mapped-JSON adapter + YAML mappings (``copilot.yaml`` / ``claude.yaml``)
turn into SessionEvents — identical to the file-watch path for equivalent input.

Vendor SDKs are OPTIONAL dependencies, imported lazily so ``import traceforge``
never requires them; a missing extra yields a clear install hint rather than an
import crash. Translation is THIN: native SDK events become the same wire-format
dicts the file path carries. There are NO new mappings here.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from types import TracebackType
from typing import Any

from traceforge.sources.base import RawRecord, Source
from traceforge.types import IngestionMode

# ─── Optional-extras: lazy vendor imports ────────────────────────────────────


def _require_copilot_sdk():
    """Import and return the GitHub Copilot SDK, raising a helpful error if missing."""
    try:
        import copilot

        return copilot
    except ImportError:
        raise ImportError(
            "github-copilot-sdk is required for SdkCopilotSource. "
            "Install it with: pip install traceforge-toolkit[copilot]"
        ) from None


def _require_claude_sdk():
    """Import and return the Claude Agent SDK, raising a helpful error if missing."""
    try:
        import claude_agent_sdk

        return claude_agent_sdk
    except ImportError:
        raise ImportError(
            "claude-agent-sdk is required for SdkClaudeSource. "
            "Install it with: pip install traceforge-toolkit[claude]"
        ) from None


# ─── Serialization helpers ───────────────────────────────────────────────────


def _jsonable(value: Any) -> Any:
    """Best-effort conversion of a native SDK object into JSON-able data.

    Handles pydantic models (``model_dump``), dataclasses, mappings, sequences,
    datetimes, and objects exposing ``__dict__``. Scalars pass through untouched;
    anything else degrades to ``str`` so serialization never crashes the loop.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _jsonable(model_dump(mode="json", by_alias=True, exclude_none=True))
        except TypeError:
            try:
                return _jsonable(model_dump())
            except Exception:
                pass
    if is_dataclass(value) and not isinstance(value, type):
        try:
            return _jsonable(asdict(value))
        except Exception:
            pass
    if isinstance(value, datetime):
        return value.isoformat()
    obj_dict = getattr(value, "__dict__", None)
    if isinstance(obj_dict, dict):
        return {str(k): _jsonable(v) for k, v in obj_dict.items() if not str(k).startswith("_")}
    return str(value)


def _attr(obj: Any, name: str) -> Any:
    """Read ``name`` from a mapping (``.get``) or an object (``getattr``)."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _iso(value: Any) -> Any:
    """Normalize a timestamp to an ISO-8601 string, leaving strings untouched."""
    if isinstance(value, datetime):
        return value.isoformat()
    return value


# ─── Base managed live source ────────────────────────────────────────────────


class _SdkLiveSource(Source):
    """Shared managed ingest loop for live vendor-SDK sources.

    Consumes an async iterable of *native* vendor events, translates each into
    the framework's wire-format dict via ``_to_wire``, and emits a ``RawRecord``
    (``mode="stream"``) carrying the serialized wire dict. The SAME downstream
    ``MappedJsonAdapter`` (loaded from the framework YAML) maps those records
    into SessionEvents — the thin-translation contract. Translation that yields
    ``None`` skips the event (e.g. an unmapped native message type).
    """

    mode: IngestionMode = "stream"

    def __init__(self, name: str, events: AsyncIterable[Any]) -> None:
        self.name = name
        self._events = events
        self._sequence = 0
        self._iterating = False
        self._entered = False
        self._aiter: AsyncIterator[Any] | None = None

    async def __aenter__(self) -> "_SdkLiveSource":
        self._entered = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._entered = False
        aiter = self._aiter
        self._aiter = None
        aclose = getattr(aiter, "aclose", None)
        if callable(aclose):
            try:
                await aclose()
            except Exception:
                pass

    async def _iter_records(self) -> AsyncIterator[RawRecord]:
        if not self._entered:
            raise RuntimeError(f"{type(self).__name__} must be entered before iteration")
        if self._iterating:
            raise RuntimeError(f"{type(self).__name__} does not support concurrent iteration")
        self._iterating = True
        try:
            aiter = self._events.__aiter__()
            self._aiter = aiter
            while True:
                try:
                    native = await aiter.__anext__()
                except StopAsyncIteration:
                    break
                wire = self._to_wire(native)
                if wire is None:
                    continue
                yield RawRecord(
                    payload=json.dumps(wire, default=str),
                    source_name=self.name,
                    mode=self.mode,
                    sequence=self._sequence,
                    received_at=datetime.now(timezone.utc),
                )
                self._sequence += 1
        finally:
            self._iterating = False

    def __aiter__(self) -> AsyncIterator[RawRecord]:
        return self._iter_records()

    def _to_wire(self, native: Any) -> dict[str, Any] | None:
        """Translate a native vendor event into a wire-format dict (or None to skip)."""
        raise NotImplementedError


# ─── Copilot ─────────────────────────────────────────────────────────────────


def _copilot_data(data: Any) -> dict[str, Any]:
    """Normalize a Copilot event's ``data`` payload to a plain dict."""
    if data is None:
        return {}
    if isinstance(data, dict):
        return data
    result = _jsonable(data)
    return result if isinstance(result, dict) else {"value": result}


class SdkCopilotSource(_SdkLiveSource):
    """Live source over the GitHub Copilot agent SDK event stream.

    The Copilot SDK surfaces session events whose shape already mirrors the file
    wire format (``event.type`` + typed ``event.data``), so translation is
    near-identity: re-serialize ``type`` / ``id`` / ``timestamp`` / ``data`` into
    the dict the ``copilot.yaml`` mapping consumes.

    Construct directly with an async iterable of native events, or via
    :meth:`from_session` to bridge a live SDK ``session`` push stream.
    """

    framework = "copilot"

    def _to_wire(self, event: Any) -> dict[str, Any] | None:
        event_type = _attr(event, "type")
        if event_type is None:
            return None
        wire: dict[str, Any] = {"type": str(event_type)}
        event_id = _attr(event, "id")
        if event_id is not None:
            wire["id"] = event_id
        timestamp = _attr(event, "timestamp")
        if timestamp is not None:
            wire["timestamp"] = _iso(timestamp)
        wire["data"] = _copilot_data(_attr(event, "data"))
        return wire

    @classmethod
    def from_session(cls, name: str, session: Any) -> "SdkCopilotSource":
        """Build a source that bridges a live Copilot ``session`` push stream."""
        return cls(name, copilot_event_stream(session))


async def copilot_event_stream(session: Any) -> AsyncIterator[Any]:
    """Bridge a Copilot ``session``'s push callbacks into an async iterator.

    The Copilot SDK delivers events through ``session.on(callback)`` (a push
    model). This adapts that into the pull-based async stream the source's
    managed loop consumes, marshalling callbacks onto the running loop. Requires
    the ``copilot`` extra; the returned unsubscribe (if any) is called on exit.
    """
    _require_copilot_sdk()
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Any] = asyncio.Queue()

    def _on_event(event: Any) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, event)
        except RuntimeError:
            pass

    unsubscribe = session.on(_on_event)
    try:
        while True:
            yield await queue.get()
    finally:
        if callable(unsubscribe):
            try:
                unsubscribe()
            except Exception:
                pass


# ─── Claude ──────────────────────────────────────────────────────────────────

_CLAUDE_RESULT_FIELDS = (
    "subtype",
    "duration_ms",
    "duration_api_ms",
    "is_error",
    "num_turns",
    "session_id",
    "total_cost_usd",
    "result",
)


class SdkClaudeSource(_SdkLiveSource):
    """Live source over the Claude Agent SDK message stream.

    Unlike Copilot, the Claude SDK yields typed dataclass ``Message`` objects
    (``AssistantMessage``, ``UserMessage``, ``ResultMessage``, ...) whose content
    is a list of typed blocks. These are explicitly translated back into the
    ``{type, message: {content: [...]}}`` / ``{type: "result", ...}`` wire dicts
    that ``claude.yaml`` (via its ``claude`` preprocessor) consumes. Dispatch is
    by class name so the real SDK classes are never imported.

    Construct directly with an async iterable of native messages, or via
    :meth:`from_query` over a ``query(prompt, options)`` message stream.
    """

    framework = "claude"

    def _to_wire(self, message: Any) -> dict[str, Any] | None:
        type_name = type(message).__name__
        msg_type = _attr(message, "type")

        if type_name == "UserMessage" or msg_type == "user":
            return {
                "type": "user",
                "message": {"content": self._content(_attr(message, "content"))},
            }

        if type_name == "AssistantMessage" or msg_type == "assistant":
            blocks = [self._block(b) for b in (_attr(message, "content") or [])]
            wire: dict[str, Any] = {"type": "assistant", "message": {"content": blocks}}
            model = _attr(message, "model")
            if model is not None:
                wire["message"]["model"] = model
            return wire

        if type_name == "ResultMessage" or msg_type == "result":
            return self._result(message)

        if type_name == "SystemMessage" or msg_type == "system":
            system: dict[str, Any] = {"type": "system"}
            subtype = _attr(message, "subtype")
            if subtype is not None:
                system["subtype"] = subtype
            data = _attr(message, "data")
            if data is not None:
                system["data"] = _jsonable(data)
            return system

        return None

    def _content(self, content: Any) -> Any:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return [self._block(b) for b in content]
        if content is None:
            return ""
        return str(content)

    def _block(self, block: Any) -> dict[str, Any]:
        if isinstance(block, dict):
            return block
        type_name = type(block).__name__
        if type_name == "TextBlock":
            return {"type": "text", "text": _attr(block, "text")}
        if type_name == "ThinkingBlock":
            return {
                "type": "thinking",
                "thinking": _attr(block, "thinking"),
                "signature": _attr(block, "signature"),
            }
        if type_name == "ToolUseBlock":
            return {
                "type": "tool_use",
                "id": _attr(block, "id"),
                "name": _attr(block, "name"),
                "input": _jsonable(_attr(block, "input")),
            }
        if type_name == "ToolResultBlock":
            result: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": _attr(block, "tool_use_id"),
                "content": _jsonable(_attr(block, "content")),
            }
            is_error = _attr(block, "is_error")
            if is_error is not None:
                result["is_error"] = is_error
            return result
        # Unknown block: degrade to a jsonable dict, preserving a type hint.
        data = _jsonable(block)
        if isinstance(data, dict):
            data.setdefault("type", _attr(block, "type") or "unknown")
            return data
        return {"type": _attr(block, "type") or "unknown", "value": data}

    def _result(self, message: Any) -> dict[str, Any]:
        wire: dict[str, Any] = {"type": "result"}
        for field_name in _CLAUDE_RESULT_FIELDS:
            value = _attr(message, field_name)
            if value is not None:
                wire[field_name] = value
        usage = _attr(message, "usage")
        if usage is not None:
            wire["usage"] = usage if isinstance(usage, dict) else _jsonable(usage)
        return wire

    @classmethod
    def from_query(cls, name: str, prompt: Any, options: Any = None) -> "SdkClaudeSource":
        """Build a source over a Claude ``query(prompt, options)`` message stream."""
        return cls(name, claude_event_stream(prompt, options))


async def claude_event_stream(prompt: Any, options: Any = None) -> AsyncIterator[Any]:
    """Yield native Claude ``Message`` objects from the Agent SDK ``query`` API.

    Requires the ``claude`` extra. ``options`` is forwarded verbatim when given,
    so callers control the underlying ``ClaudeAgentOptions`` without this layer
    taking a dependency on the SDK's option types.
    """
    sdk = _require_claude_sdk()
    if options is not None:
        stream = sdk.query(prompt=prompt, options=options)
    else:
        stream = sdk.query(prompt=prompt)
    async for message in stream:
        yield message
