"""In-process observation auto-subscriber (PR-J phase 1).

This module productizes the ad-hoc capture glue under ``scripts/capture_traces/``
into a first-class, shipped SDK feature. It subscribes to a framework's **native**
global event bus / trace processor, maps each native event through the **existing**
``traceforge/mappings/<framework>.yaml`` config (via
:class:`~traceforge.adapters.mapped_json.MappedJsonAdapter`), and pushes the resulting
:class:`~traceforge.types.SessionEvent` objects into a live SDK
:class:`~traceforge.sdk.pipeline.Pipeline` through its ``push`` seam — exactly the
same code path a file-watch adapter would use.

Phase 1 covers the two frameworks that expose a global bus / processor:

* **CrewAI** — ``crewai.events.crewai_event_bus`` (``register_handler`` / ``off``).
* **OpenAI Agents SDK** — ``agents.add_trace_processor`` /
  ``agents.set_trace_processors`` with a duck-typed ``TracingProcessor``.

LangChain/LangGraph (callback-handler based) and the OpenTelemetry bridge
(pydantic_ai / smolagents / maf / semantic_kernel) are intentionally **out of scope**
here — they are phase 2/3 and depend on mapping files another PR is still adding.

Design notes
------------
* Each ``observe_*`` returns an :class:`ObservationHandle`. The handle owns its native
  subscription and tears it down cleanly via :meth:`ObservationHandle.stop` (idempotent),
  ``with`` / ``async with``, leaving **no** residual global subscription.
* The SDK pipeline is asyncio-single-threaded, but native buses invoke callbacks from
  arbitrary threads (CrewAI dispatches sync handlers on a ``ThreadPoolExecutor``). Every
  ``observe_*`` therefore captures the running loop at call time and marshals pushes onto
  it with :func:`asyncio.run_coroutine_threadsafe` — never blocking on the result from the
  callback thread. :meth:`ObservationHandle.drain` awaits the in-flight pushes for
  deterministic teardown / testing.
* Native framework imports are **lazy** (inside the default bus / registration helpers), so
  importing this module never imports crewai / agents and never touches the network. The
  bus / processor registration hooks are injectable, which is what the test-suite uses in
  place of the real (uninstalled) frameworks.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import pkgutil
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from traceforge.adapters.mapped_json import MappedJsonAdapter
    from traceforge.sdk.pipeline import Pipeline
    from traceforge.types import SessionEvent

logger = logging.getLogger(__name__)

# ``.../traceforge/sdk/observe.py`` -> ``.../traceforge/mappings``. Resolved from this
# file (not via importing the top-level package) to avoid any import-time coupling.
_MAPPINGS_DIR = Path(__file__).resolve().parent.parent / "mappings"


def _load_adapter(framework: str, session_id: str) -> "MappedJsonAdapter":
    """Build the shared mapping adapter for ``framework`` (reuses the shipped YAML)."""
    from traceforge.adapters.mapped_json import MappedJsonAdapter

    return MappedJsonAdapter.from_yaml(str(_MAPPINGS_DIR / f"{framework}.yaml"), session_id)


# ─── JSON-able serialization (ported from scripts/capture_traces/capture_crewai.py) ──


def _jsonable(value: Any, seen: set[int] | None = None) -> Any:
    """Generic JSON conversion that keeps native field names and containers.

    Mirrors the capture-script serializer so the native CrewAI event object is turned
    into the same flat dict shape the ``crewai`` YAML mapping already expects.
    """
    seen = seen or set()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    obj_id = id(value)
    if obj_id in seen:
        return f"<circular:{type(value).__name__}>"
    if isinstance(value, dict):
        seen.add(obj_id)
        return {str(k): _jsonable(v, seen) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        seen.add(obj_id)
        return [_jsonable(v, seen) for v in value]
    if isinstance(value, type):
        return f"{value.__module__}.{value.__qualname__}"
    if hasattr(value, "model_dump"):
        seen.add(obj_id)
        try:
            return _jsonable(value.model_dump(mode="json"), seen)
        except Exception:
            try:
                return _jsonable(value.model_dump(mode="python"), seen)
            except Exception:
                return _jsonable(getattr(value, "__dict__", str(value)), seen)
    if hasattr(value, "dict"):
        seen.add(obj_id)
        try:
            return _jsonable(value.dict(), seen)
        except Exception:
            return _jsonable(getattr(value, "__dict__", str(value)), seen)
    if hasattr(value, "__dict__") and not callable(value):
        seen.add(obj_id)
        return _jsonable(vars(value), seen)
    return str(value)


# ─── Base handle ─────────────────────────────────────────────────────────────


class ObservationHandle:
    """A live in-process subscription bridging a native event bus into the pipeline.

    Constructed and activated by the ``observe_*`` functions; not meant to be
    instantiated directly. Call :meth:`stop` (or use the object as a context manager)
    to unsubscribe cleanly. Use :meth:`drain` to await pushes that native callbacks
    scheduled onto the pipeline loop.
    """

    def __init__(self, pipeline: "Pipeline", framework: str, adapter: "MappedJsonAdapter") -> None:
        self._pipeline = pipeline
        self._framework = framework
        self._adapter = adapter
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            raise RuntimeError(
                f"observe_{framework}() must be called from within a running asyncio event "
                "loop (the SDK pipeline is async). Call it inside an async function; run a "
                "framework's blocking entrypoint via asyncio.to_thread(...) so this loop stays "
                "free to receive pushes."
            ) from None
        self._lock = threading.Lock()
        self._pending: set[Future] = set()
        self._active = False

    # ---- lifecycle ----------------------------------------------------------

    def _activate(self) -> "ObservationHandle":
        """Register with the native bus and mark the handle live. Returns ``self``."""
        self._subscribe()
        self._active = True
        return self

    @property
    def active(self) -> bool:
        """True while the native subscription is installed (False after :meth:`stop`)."""
        return self._active

    @property
    def framework(self) -> str:
        """The phase-1 framework this handle observes (``crewai`` / ``openai_agents``)."""
        return self._framework

    def stop(self) -> None:
        """Unsubscribe from the native bus. Idempotent; leaves no residual subscription."""
        if not self._active:
            return
        self._active = False
        try:
            self._unsubscribe()
        except Exception:
            logger.warning("observe(%s): error during unsubscribe", self._framework, exc_info=True)

    async def drain(self) -> None:
        """Await all pushes that native callbacks have scheduled onto the pipeline loop."""
        with self._lock:
            pending = list(self._pending)
        if pending:
            await asyncio.gather(
                *(asyncio.wrap_future(fut) for fut in pending), return_exceptions=True
            )

    def __enter__(self) -> "ObservationHandle":
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    async def __aenter__(self) -> "ObservationHandle":
        return self

    async def __aexit__(self, *exc: object) -> None:
        try:
            await self.drain()
        finally:
            self.stop()

    # ---- native-callback -> pipeline bridge --------------------------------

    def _ingest(self, native: Any) -> None:
        """Map one native event dict and schedule the resulting pushes onto the loop.

        Safe to call from any thread. Never raises into the native bus: mapping errors
        are swallowed (logged) so one bad event can't tear down the framework's run.
        """
        if not self._active or not isinstance(native, dict):
            return
        loop = self._loop
        if loop.is_closed():
            return
        try:
            with self._lock:
                events = self._map(native)
        except Exception:
            logger.warning(
                "observe(%s): failed to map native event", self._framework, exc_info=True
            )
            return
        if not events:
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(self._push(events), loop)
        except RuntimeError:
            return  # loop is shutting down
        with self._lock:
            self._pending.add(fut)
        fut.add_done_callback(self._discard)

    def _discard(self, fut: Future) -> None:
        with self._lock:
            self._pending.discard(fut)

    def _map(self, native: dict[str, Any]) -> list["SessionEvent"]:
        """Run the native dict through the YAML mapping, attaching the raw event.

        Replicates :meth:`JsonLineAdapter.parse`'s raw-event attach (the base normally
        does this, but we call ``parse_dict`` directly to skip JSON (de)serialization).
        """
        out: list[SessionEvent] = []
        for event in self._adapter.parse_dict(native):
            if event.raw_event is None:
                event = event.model_copy(update={"raw_event": native})
            out.append(event)
        return out

    async def _push(self, events: "Sequence[SessionEvent]") -> None:
        for event in events:
            await self._pipeline.push(event)

    # ---- subclass hooks -----------------------------------------------------

    def _subscribe(self) -> None:  # pragma: no cover - abstract-ish
        raise NotImplementedError

    def _unsubscribe(self) -> None:  # pragma: no cover - abstract-ish
        raise NotImplementedError


# ─── CrewAI ──────────────────────────────────────────────────────────────────


def _default_crewai_bus() -> Any:
    """Return the process-global CrewAI event bus singleton (lazy native import)."""
    from crewai.events import crewai_event_bus

    return crewai_event_bus


def _discover_crewai_event_types() -> list[type]:
    """Discover every concrete ``BaseEvent`` subclass CrewAI ships.

    CrewAI's bus dispatches by **exact** ``type(event)`` (no MRO walk), so a handler must
    be registered per concrete event class. This mirrors the capture script's pkgutil walk
    over ``crewai.events.types``.
    """
    from crewai.events.types.event_bus_types import BaseEvent  # type: ignore[import-not-found]
    import crewai.events.types as event_types_pkg  # type: ignore[import-not-found]

    found: list[type] = []
    seen: set[type] = set()
    for module_info in pkgutil.iter_modules(event_types_pkg.__path__):
        module = importlib.import_module(f"{event_types_pkg.__name__}.{module_info.name}")
        for value in vars(module).values():
            if (
                isinstance(value, type)
                and issubclass(value, BaseEvent)
                and value is not BaseEvent
                and value not in seen
            ):
                seen.add(value)
                found.append(value)
    return found


class _CrewAiObserver(ObservationHandle):
    """Bridges the CrewAI event bus into the pipeline."""

    def __init__(
        self,
        pipeline: "Pipeline",
        *,
        session_id: str,
        event_bus: Any | None,
        event_types: "Iterable[type] | None",
    ) -> None:
        self._bus = event_bus if event_bus is not None else _default_crewai_bus()
        self._event_types: list[type] = (
            list(event_types) if event_types is not None else _discover_crewai_event_types()
        )
        # One shared handler registered under every event type; CrewAI stores handlers in a
        # per-type set, so ``off`` with the same object removes it cleanly (no leak).
        self._handler = self._make_handler()
        super().__init__(pipeline, "crewai", _load_adapter("crewai", session_id))

    def _make_handler(self) -> Callable[..., None]:
        observer = self

        def handler(source: Any, event: Any, *args: Any, **kwargs: Any) -> None:
            observer._ingest(_jsonable(event))

        return handler

    def _subscribe(self) -> None:
        for event_type in self._event_types:
            self._bus.register_handler(event_type, self._handler)

    def _unsubscribe(self) -> None:
        for event_type in self._event_types:
            try:
                self._bus.off(event_type, self._handler)
            except Exception:
                logger.warning(
                    "observe(crewai): failed to detach handler for %r", event_type, exc_info=True
                )


def observe_crewai(
    pipeline: "Pipeline",
    *,
    session_id: str = "crewai",
    event_bus: Any | None = None,
    event_types: "Iterable[type] | None" = None,
) -> ObservationHandle:
    """Subscribe to CrewAI's global event bus and stream mapped events into ``pipeline``.

    Args:
        pipeline: The live SDK :class:`~traceforge.sdk.pipeline.Pipeline` to push into.
        session_id: Session id stamped on every produced event (default ``"crewai"``).
        event_bus: Native bus to subscribe to; defaults to ``crewai.events.crewai_event_bus``.
            Injectable for testing.
        event_types: Concrete CrewAI event classes to register handlers for; defaults to
            auto-discovering all shipped ``BaseEvent`` subclasses. Injectable for testing.

    Returns:
        An :class:`ObservationHandle`; call :meth:`~ObservationHandle.stop` (or use it as a
        context manager) to unsubscribe.

    Must be called from within a running asyncio loop. Because ``crew.kickoff()`` blocks and
    CrewAI runs handlers on worker threads, run the kickoff via ``asyncio.to_thread(...)`` so
    this loop stays free to receive pushes.
    """
    return _CrewAiObserver(
        pipeline, session_id=session_id, event_bus=event_bus, event_types=event_types
    )._activate()


# ─── OpenAI Agents SDK ───────────────────────────────────────────────────────


def _openai_trace_provider() -> Any | None:
    """Best-effort handle on the Agents SDK global trace provider (lazy import)."""
    try:
        from agents.tracing import get_trace_provider

        return get_trace_provider()
    except Exception:
        pass
    try:
        from agents.tracing.setup import GLOBAL_TRACE_PROVIDER

        return GLOBAL_TRACE_PROVIDER
    except Exception:
        return None


def _current_openai_processors(provider: Any) -> list[Any] | None:
    if provider is None:
        return None
    multi = getattr(provider, "_multi_processor", None)
    processors = getattr(multi, "_processors", None)
    if processors is None:
        return None
    return list(processors)


def _default_openai_register(processor: Any) -> None:
    """Append ``processor`` to the Agents SDK's global processor list (additive)."""
    from agents import add_trace_processor

    add_trace_processor(processor)


def _default_openai_unregister(processor: Any) -> None:
    """Remove exactly ``processor`` from the Agents SDK's global processor list.

    The SDK exposes no public "remove one processor" API, so this reads the provider's
    current processors and re-sets the list without ours, leaving every other processor
    (e.g. the default batch exporter) untouched.
    """
    from agents import set_trace_processors

    current = _current_openai_processors(_openai_trace_provider())
    if current is None:
        logger.warning(
            "observe(openai_agents): could not read current trace processors; "
            "teardown could not confirm removal"
        )
        return
    set_trace_processors([proc for proc in current if proc is not processor])


class _OpenAiAgentsObserver(ObservationHandle):
    """Bridges the OpenAI Agents SDK tracing pipeline into the pipeline."""

    def __init__(
        self,
        pipeline: "Pipeline",
        *,
        session_id: str,
        register: Callable[[Any], None] | None,
        unregister: Callable[[Any], None] | None,
    ) -> None:
        self._register = register if register is not None else _default_openai_register
        self._unregister = unregister if unregister is not None else _default_openai_unregister
        super().__init__(pipeline, "openai_agents", _load_adapter("openai_agents", session_id))
        self._processor = self._make_processor()

    def _make_processor(self) -> Any:
        observer = self

        class _Processor:
            """Duck-typed ``agents.tracing.TracingProcessor``.

            The SDK's multi-processor calls these by name without an isinstance check, so a
            plain object suffices and we avoid importing ``agents`` to build it.
            """

            def on_trace_start(self, trace: Any) -> None:
                observer._ingest_export(trace)

            def on_trace_end(self, trace: Any) -> None:
                pass

            def on_span_start(self, span: Any) -> None:
                pass

            def on_span_end(self, span: Any) -> None:
                observer._ingest_export(span)

            def shutdown(self) -> None:
                pass

            def force_flush(self) -> None:
                pass

        return _Processor()

    @property
    def processor(self) -> Any:
        """The duck-typed tracing processor registered with the Agents SDK."""
        return self._processor

    def _ingest_export(self, obj: Any) -> None:
        export = getattr(obj, "export", None)
        data = export() if callable(export) else obj
        if isinstance(data, dict):
            self._ingest(data)

    def _subscribe(self) -> None:
        self._register(self._processor)

    def _unsubscribe(self) -> None:
        self._unregister(self._processor)


def observe_openai_agents(
    pipeline: "Pipeline",
    *,
    session_id: str = "openai_agents",
    register: Callable[[Any], None] | None = None,
    unregister: Callable[[Any], None] | None = None,
) -> ObservationHandle:
    """Register an Agents SDK trace processor that streams mapped events into ``pipeline``.

    Args:
        pipeline: The live SDK :class:`~traceforge.sdk.pipeline.Pipeline` to push into.
        session_id: Session id stamped on every produced event (default ``"openai_agents"``).
        register: Callable that installs the processor; defaults to ``agents.add_trace_processor``.
            Injectable for testing.
        unregister: Callable that removes the processor; defaults to filtering it out of
            ``agents.set_trace_processors``. Injectable for testing.

    Returns:
        An :class:`ObservationHandle`; call :meth:`~ObservationHandle.stop` (or use it as a
        context manager) to unregister the processor.

    Must be called from within a running asyncio loop (the Agents SDK ``Runner`` is async).
    """
    return _OpenAiAgentsObserver(
        pipeline, session_id=session_id, register=register, unregister=unregister
    )._activate()


# ─── Unified dispatch ────────────────────────────────────────────────────────

_OBSERVERS: dict[str, Callable[..., ObservationHandle]] = {
    "crewai": observe_crewai,
    "openai_agents": observe_openai_agents,
}


def observe(pipeline: "Pipeline", framework: str, **kwargs: Any) -> ObservationHandle:
    """Subscribe to ``framework``'s native bus by name (phase 1: crewai / openai_agents).

    Thin dispatcher over :func:`observe_crewai` / :func:`observe_openai_agents`; extra
    keyword arguments are forwarded to the selected observer.
    """
    try:
        factory = _OBSERVERS[framework]
    except KeyError:
        supported = ", ".join(sorted(_OBSERVERS))
        raise ValueError(
            f"observe: unsupported framework {framework!r}; phase-1 supports: {supported}"
        ) from None
    return factory(pipeline, **kwargs)


__all__ = [
    "ObservationHandle",
    "observe",
    "observe_crewai",
    "observe_openai_agents",
]
