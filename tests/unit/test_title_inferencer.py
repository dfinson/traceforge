"""Live activity/step titling stream tests.

The stream is a *streaming enrichment*: every event is stamped with its live
``activity_id``/``step_id`` and emitted immediately (never buffered), and a
closed activity's titles are returned out-of-band as append-only
:class:`~tracemill.types.TitleUpdate` records keyed by those ids.

Two layers, mirroring the boundary-stream tests:

* *fake model* — a deterministic stub titler lets us assert the live id
  stamping, immediate emission, the title-at-close updates, parent/child
  distinctness, and the flush of the trailing activity, without the heavy ONNX
  titler; and
* *real model* — the packaged titler proves the end-to-end serve path produces
  non-empty, distinct titles from real span context (skipped when the span model
  artifacts are absent).
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timezone

import pytest

from tracemill.title import SessionTitleStream, TitleInferencer
from tracemill.title._resolve import span_dir as _span_dir
from tracemill.types import EventMetadata, SessionEvent


def _event(i, tool="edit", boundary=None, payload=None, kind="tool.call", metadata=True):
    md = EventMetadata(source_framework="copilot", boundary=boundary) if metadata else None
    return SessionEvent(
        id=f"e{i}",
        kind=kind,
        session_id="S",
        timestamp=datetime.now(timezone.utc),
        payload={"tool_name": tool, **(payload or {})},
        metadata=md,
    )


def _msg(i, text, kind="message.user"):
    """A user/assistant message event carrying free text in its payload."""
    return SessionEvent(
        id=f"e{i}",
        kind=kind,
        session_id="S",
        timestamp=datetime.now(timezone.utc),
        payload={"content": text},
        metadata=EventMetadata(source_framework="copilot"),
    )


class _FakeTitle:
    """Returns a fresh ``"Title <n>"`` candidate list on each call, so titles
    are deterministic and we can assert which span got which call."""

    def __init__(self):
        self.n = 0
        self.contexts: list[str] = []

    def candidates(self, ctx, **_kw):
        self.contexts.append(ctx)
        out = [f"Title {self.n}"]
        self.n += 1
        return out


def _stream() -> tuple[SessionTitleStream, _FakeTitle]:
    fake = _FakeTitle()
    return TitleInferencer(model=fake).new_stream("S", "copilot"), fake


# ─── live emission + segment ids ─────────────────────────────────────────────


def test_events_emit_immediately_with_live_segment_ids():
    stream, _ = _stream()
    # Every push returns its event right away (no buffering) with ids stamped.
    e0, up0 = stream.push(_event(0))
    assert e0.id == "e0" and up0 == []
    assert e0.metadata.activity_id == "e0"  # opener event id
    assert e0.metadata.step_id == "e0"

    e1, up1 = stream.push(_event(1, boundary="step-boundary"))
    assert up1 == []
    assert e1.metadata.activity_id == "e0"  # same activity
    assert e1.metadata.step_id == "e1"  # new step opens on e1

    e2, up2 = stream.push(_event(2))  # continues step e1
    assert up2 == []
    assert e2.metadata.activity_id == "e0" and e2.metadata.step_id == "e1"


def test_activity_close_emits_titles_for_activity_and_steps():
    stream, _ = _stream()
    stream.push(_event(0))  # activity e0 / step e0
    stream.push(_event(1))  # step e0 (continues)
    stream.push(_event(2, boundary="step-boundary"))  # step e2
    stream.push(_event(3))  # step e2 (continues)
    # The activity-boundary closes activity e0 and returns its titles.
    e4, updates = stream.push(_event(4, boundary="activity-boundary"))

    assert e4.metadata.activity_id == "e4"  # new activity already open on e4
    by = {(u.kind, u.segment_id): u for u in updates}
    # Activity title (computed first -> "Title 0"), keyed by the activity id.
    assert by[("activity", "e0")].title == "Title 0"
    # One step update per step, distinct titles, parented to the activity.
    assert by[("step", "e0")].title == "Title 1"
    assert by[("step", "e2")].title == "Title 2"
    assert by[("step", "e0")].parent_id == "e0"
    assert by[("step", "e2")].parent_id == "e0"
    assert by[("activity", "e0")].title != by[("step", "e0")].title


def test_flush_emits_trailing_activity_updates():
    stream, _ = _stream()
    stream.push(_event(0))
    stream.push(_event(1, boundary="step-boundary"))
    updates = stream.flush()
    kinds = sorted({u.kind for u in updates})
    assert kinds == ["activity", "step"]
    assert {u.segment_id for u in updates if u.kind == "step"} == {"e0", "e1"}
    assert stream.flush() == []  # nothing left after draining


def test_activity_context_spans_all_steps():
    # The activity title must see the FULL activity (both steps' rows), while
    # each step title sees only its own rows -> the activity context is longer.
    stream, fake = _stream()
    stream.push(_event(0, tool="edit"))
    stream.push(_event(1, tool="shell", boundary="step-boundary"))
    stream.push(_event(2, tool="grep", boundary="activity-boundary"))
    # contexts[0] is the activity (rows e0+e1), contexts[1] the single step.
    assert "edit" in fake.contexts[0] and "shell" in fake.contexts[0]


def test_no_signal_span_emits_no_updates():
    stream, fake = _stream()
    e0, up0 = stream.push(_event(0, tool=None, payload={}))  # no signal
    assert up0 == []
    # Event still emitted immediately, stamped with its ids.
    assert e0.metadata.activity_id == "e0"
    updates = stream.flush()
    assert updates == []  # no title -> no update records
    assert fake.contexts == []  # model never invoked on a signal-less span


def test_stamp_passes_through_event_without_metadata():
    # SessionEvent always carries metadata, but the stamp guard must no-op on a
    # metadata-less event rather than crash (defensive parity with boundary).
    from types import SimpleNamespace

    ev = SimpleNamespace(metadata=None)
    assert SessionTitleStream._stamp(ev, "A", "B") is ev


# ─── session title (live, from the first substantive user message) ───────────


def _recording_titler():
    """A deterministic session titler that records the texts it was asked to name."""
    calls: list[str] = []

    def titler(text: str) -> str:
        calls.append(text)
        return f"Session {len(calls)}"

    return titler, calls


def test_first_substantive_user_message_titles_session():
    titler, calls = _recording_titler()
    stream = TitleInferencer(model=_FakeTitle(), session_titler=titler).new_stream("S", "copilot")
    _e, updates = stream.push(_msg(0, "Please add retry logic to the HTTP client with backoff"))
    sess = [u for u in updates if u.kind == "session"]
    assert len(sess) == 1
    # Keyed by the session id (the session is the outermost segment), no parent.
    assert sess[0].segment_id == "S" and sess[0].session_id == "S"
    assert sess[0].parent_id is None and sess[0].title
    # The session titler (not the span model) saw the raw user message.
    assert calls == ["Please add retry logic to the HTTP client with backoff"]
    # Set-once: a later substantive message never re-titles the session.
    _e2, updates2 = stream.push(_msg(1, "Also write unit tests for the limiter please"))
    assert [u for u in updates2 if u.kind == "session"] == []
    assert len(calls) == 1


def test_non_substantive_user_message_does_not_title_session():
    titler, calls = _recording_titler()
    stream = TitleInferencer(model=_FakeTitle(), session_titler=titler).new_stream("S", "copilot")
    # A bare greeting yields no sentence under narration hygiene -> no title, and
    # the titler is never invoked (parameter-free substance gate, no threshold).
    _e, updates = stream.push(_msg(0, "hi"))
    assert [u for u in updates if u.kind == "session"] == []
    assert calls == []
    # The first SUBSTANTIVE message then titles the session.
    _e2, updates2 = stream.push(_msg(1, "Fix the failing pagination test in the users API"))
    assert len([u for u in updates2 if u.kind == "session"]) == 1
    assert calls == ["Fix the failing pagination test in the users API"]


def test_session_title_uses_heuristic_by_default():
    # No injected session_titler -> lazily built from config (heuristic default),
    # so the end-to-end serve path names a session from the user's own words with
    # no model, key, or network.
    stream = TitleInferencer(model=_FakeTitle()).new_stream("S", "copilot")
    _e, updates = stream.push(_msg(0, "please refactor the auth module to use async tokens"))
    sess = [u for u in updates if u.kind == "session"]
    assert len(sess) == 1
    assert sess[0].title and "auth" in sess[0].title.lower()


def test_non_message_events_never_title_session():
    # Tool/file events are not user messages -> the session title gate ignores
    # them entirely (existing activity/step behavior is unchanged).
    stream, fake = _stream()
    _e, updates = stream.push(_event(0, tool="edit"))
    assert [u for u in updates if u.kind == "session"] == []
    assert fake.contexts == []  # session titler not invoked on a tool event


# ─── session-title API refinement queueing (fix a: heuristic now, API later) ──


def test_no_refiner_configured_queues_no_refinement():
    # With only the immediate (heuristic) titler, the session title is emitted
    # inline and nothing is queued for off-hot-path refinement.
    titler, _calls = _recording_titler()
    stream = TitleInferencer(model=_FakeTitle(), session_titler=titler).new_stream("S", "copilot")
    _e, updates = stream.push(_msg(0, "Please add retry logic to the HTTP client with backoff"))
    assert len([u for u in updates if u.kind == "session"]) == 1
    assert stream.take_session_refinement() is None


def test_refiner_queues_raw_text_once_and_emits_heuristic_now():
    # When an API refiner is configured the HEURISTIC title is still emitted
    # immediately (never blocking on the refiner), and the raw request text is
    # queued exactly once for the pipeline to upgrade off the hot path.
    titler, hcalls = _recording_titler()
    refiner_calls: list[str] = []

    def refiner(text: str) -> str:
        refiner_calls.append(text)
        return f"Refined {len(refiner_calls)}"

    stream = TitleInferencer(
        model=_FakeTitle(), session_titler=titler, session_refiner=refiner
    ).new_stream("S", "copilot")

    text = "Please add retry logic to the HTTP client with backoff"
    _e, updates = stream.push(_msg(0, text))
    sess = [u for u in updates if u.kind == "session"]
    assert len(sess) == 1
    assert sess[0].title == "Session 1"  # the immediate heuristic, not the refiner
    assert hcalls == [text]  # heuristic ran inline
    assert refiner_calls == []  # refiner is NOT invoked on the hot path
    # The raw request text is available once for off-hot-path refinement.
    assert stream.take_session_refinement() == text
    assert stream.take_session_refinement() is None  # popped, not re-queued

    # Set-once: a later substantive message neither re-titles nor re-queues.
    _e2, updates2 = stream.push(_msg(1, "Also add unit tests for the limiter please"))
    assert [u for u in updates2 if u.kind == "session"] == []
    assert stream.take_session_refinement() is None


def test_non_substantive_message_queues_no_refinement():
    # A message that never titles the session must not queue a refinement either.
    titler, _calls = _recording_titler()

    def refiner(text: str) -> str:  # pragma: no cover - must never run
        raise AssertionError("refiner queued for a non-substantive message")

    stream = TitleInferencer(
        model=_FakeTitle(), session_titler=titler, session_refiner=refiner
    ).new_stream("S", "copilot")
    _e, updates = stream.push(_msg(0, "hi"))
    assert [u for u in updates if u.kind == "session"] == []
    assert stream.take_session_refinement() is None


# ─── real packaged model ─────────────────────────────────────────────────────

_HAS_DEPS = (
    importlib.util.find_spec("onnxruntime") is not None
    and importlib.util.find_spec("tokenizers") is not None
)
_HAS_MODEL = _span_dir() is not None


@pytest.mark.skipif(not (_HAS_DEPS and _HAS_MODEL), reason="span model artifacts absent")
def test_real_model_produces_distinct_nonempty_titles():
    stream = TitleInferencer().new_stream("S", "copilot")
    intent = {"arguments": {"intent": "Adding retry logic to the HTTP client"}}
    stream.push(_event(0, tool="report_intent", payload=intent))
    stream.push(_event(1, tool="edit", payload={"arguments": {"path": "client.py"}}))
    stream.push(
        _event(
            2, tool="shell", boundary="step-boundary", payload={"arguments": {"command": "pytest"}}
        )
    )
    _e3, updates = stream.push(
        _event(
            3,
            tool="report_intent",
            boundary="activity-boundary",
            payload={"arguments": {"intent": "Refactoring auth"}},
        )
    )
    activity = [u for u in updates if u.kind == "activity"]
    steps = [u for u in updates if u.kind == "step"]
    assert activity and all(isinstance(u.title, str) and u.title for u in activity)
    act_title = activity[0].title
    assert all(u.title != act_title for u in steps)
