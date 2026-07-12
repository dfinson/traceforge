"""Live activity/step titling stream tests.

The stream is a *streaming enrichment*: every event is stamped with its live
``activity_id``/``step_id`` and emitted immediately (never buffered), and a
closed activity's titles are returned out-of-band as append-only
:class:`~traceforge.types.TitleUpdate` records keyed by those ids.

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
import json
from datetime import datetime, timezone

import pytest

from traceforge.title import SessionTitleStream, TitleInferencer
from traceforge.title._resolve import span_dir as _span_dir
from traceforge.types import EventMetadata, SessionEvent


def _event(i, tool="edit", boundary=None, payload=None, kind="tool.call", metadata=True):
    md = EventMetadata(source_framework="copilot", boundary=boundary) if metadata else None
    body = {"tool_name": tool}
    if payload is None:
        # A realistic tool call acts on a file, giving the span a concrete
        # subject anchor so the span model (not the anchorless heuristic
        # fallback) titles it. Signal-less fixtures pass payload={} to opt out.
        body["arguments"] = {"path": "client.py"}
    else:
        body.update(payload)
    return SessionEvent(
        id=f"e{i}",
        kind=kind,
        session_id="S",
        timestamp=datetime.now(timezone.utc),
        payload=body,
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


# ─── activity/step-title API refinement (packaged now, API upgrade later) ────


class _RecordingActivityRefiner:
    """A deterministic activity refiner recording the spans it was asked to title.

    Stands in for :class:`traceforge.title.naming.ActivityApiProvider` so the
    inferencer/stream can be tested without LiteLLM or a network. Returns an
    :class:`~traceforge.title.naming.ActivityTitles` upgrading the activity and
    each step title.
    """

    def __init__(self, activity="API Activity", steps=None):
        self._activity = activity
        self._steps = steps
        self.spans: list = []

    def __call__(self, span):
        from traceforge.title.naming import ActivityTitles

        self.spans.append(span)
        steps = self._steps
        if steps is None:
            steps = [f"API Step {i}" for i in range(len(span.step_contexts))]
        return ActivityTitles(self._activity, steps)


def test_no_activity_refiner_queues_nothing():
    # Default (strategy=model): no refiner is configured, so closing an activity
    # emits its packaged titles and queues NOTHING for off-hot-path refinement.
    inf = TitleInferencer(model=_FakeTitle())
    assert inf.has_activity_refiner is False
    stream = inf.new_stream("S", "copilot")
    stream.push(_event(0, tool="edit"))
    _e, updates = stream.push(_event(1, boundary="activity-boundary"))
    assert any(u.kind == "activity" for u in updates)  # packaged titles emitted
    assert stream.take_activity_refinements() == []  # nothing queued


def test_activity_refiner_queues_closed_activity_once():
    # With a refiner configured, closing an activity queues exactly one closed
    # activity (ids + rows) for refinement, popped once by the pipeline.
    inf = TitleInferencer(model=_FakeTitle(), activity_refiner=_RecordingActivityRefiner())
    assert inf.has_activity_refiner is True
    stream = inf.new_stream("S", "copilot")
    stream.push(_event(0, tool="edit"))  # activity e0 / step e0
    stream.push(_event(1, tool="shell", boundary="step-boundary"))  # step e1
    stream.push(_event(2, tool="grep", boundary="activity-boundary"))  # closes e0

    queued = stream.take_activity_refinements()
    assert len(queued) == 1
    closed = queued[0]
    assert closed.activity_id == "e0"
    assert [sid for sid, _rows in closed.steps] == ["e0", "e1"]
    # Popped once, not re-queued.
    assert stream.take_activity_refinements() == []


def test_signal_less_activity_is_not_queued():
    # A span with no packaged title (no signal) emits no updates, so there is
    # nothing to upgrade -> it is never queued even with a refiner configured.
    inf = TitleInferencer(model=_FakeTitle(), activity_refiner=_RecordingActivityRefiner())
    stream = inf.new_stream("S", "copilot")
    stream.push(_event(0, tool=None, payload={}))  # no signal
    _e, updates = stream.push(_event(1, tool=None, payload={}, boundary="activity-boundary"))
    assert updates == []
    assert stream.take_activity_refinements() == []


def test_refine_activity_maps_api_titles_to_refinements():
    # refine_activity distils the closed activity + steps, calls the refiner
    # ONCE, and maps the returned titles to per-segment refinements (activity +
    # each step, parented to the activity).
    refiner = _RecordingActivityRefiner()
    inf = TitleInferencer(model=_FakeTitle(), activity_refiner=refiner)
    stream = inf.new_stream("S", "copilot")
    stream.push(_event(0, tool="edit"))
    stream.push(_event(1, tool="shell", boundary="step-boundary"))
    stream.push(_event(2, tool="grep", boundary="activity-boundary"))
    closed = stream.take_activity_refinements()[0]

    refs = inf.refine_activity(closed)
    assert len(refiner.spans) == 1  # exactly one API call for the whole activity
    by = {(r.kind, r.segment_id): r for r in refs}
    assert by[("activity", "e0")].title == "API Activity"
    assert by[("activity", "e0")].parent_id is None
    assert by[("step", "e0")].title == "API Step 0" and by[("step", "e0")].parent_id == "e0"
    assert by[("step", "e1")].title == "API Step 1" and by[("step", "e1")].parent_id == "e0"


def test_refine_activity_drops_step_titles_colliding_with_activity():
    # A step title that collides with the (effective) activity title is dropped
    # so that step keeps its distinct packaged-model title.
    refiner = _RecordingActivityRefiner(activity="Add Auth", steps=["Add Auth", "Write Tests"])
    inf = TitleInferencer(model=_FakeTitle(), activity_refiner=refiner)
    stream = inf.new_stream("S", "copilot")
    stream.push(_event(0, tool="edit"))
    stream.push(_event(1, tool="shell", boundary="step-boundary"))
    stream.push(_event(2, tool="grep", boundary="activity-boundary"))
    closed = stream.take_activity_refinements()[0]

    refs = inf.refine_activity(closed)
    kinds = {(r.kind, r.segment_id): r.title for r in refs}
    assert kinds[("activity", "e0")] == "Add Auth"
    assert ("step", "e0") not in kinds  # collided -> dropped
    assert kinds[("step", "e1")] == "Write Tests"  # distinct -> kept


def test_refine_activity_none_activity_keeps_packaged_and_seeds_distinctness():
    # When the API declines the activity title (None), no activity refinement is
    # produced (the packaged one stands) and step distinctness is seeded off the
    # packaged activity title.
    refiner = _RecordingActivityRefiner(activity=None, steps=["Step One", "Step Two"])
    inf = TitleInferencer(model=_FakeTitle(), activity_refiner=refiner)
    stream = inf.new_stream("S", "copilot")
    stream.push(_event(0, tool="edit"))
    stream.push(_event(1, tool="shell", boundary="step-boundary"))
    stream.push(_event(2, tool="grep", boundary="activity-boundary"))
    closed = stream.take_activity_refinements()[0]

    refs = inf.refine_activity(closed)
    assert all(r.kind == "step" for r in refs)  # no activity refinement
    assert {r.title for r in refs} == {"Step One", "Step Two"}


def test_refine_activity_without_refiner_returns_empty():
    inf = TitleInferencer(model=_FakeTitle())  # no refiner
    stream = inf.new_stream("S", "copilot")
    stream.push(_event(0, tool="edit"))
    updates = stream.flush()
    assert updates  # packaged titles emitted
    # Nothing queued, but refine_activity is still a safe no-op if called.
    from traceforge.title.inferencer import _ClosedActivity

    assert inf.refine_activity(_ClosedActivity("e0", "Title 0", [("e0", [])])) == []


# ─── honest abstention: no subject anchor -> heuristic, never a model guess ───


def _frow(kind="tool.call", tool_name=None, payload=None):
    """A minimal event feature-row (the shape ``distilled_context`` consumes)."""
    return {
        "kind": kind,
        "tool_name": tool_name,
        "payload_json": json.dumps(payload) if payload is not None else None,
        "binaries": [],
        "structure": [],
    }


def test_anchored_span_still_uses_the_model():
    # A span with a real subject anchor (a stated intent) is titled by the span
    # model exactly as before.
    fake = _FakeTitle()
    inf = TitleInferencer(model=fake)
    rows = [
        _frow(
            tool_name="report_intent",
            payload={
                "tool_name": "report_intent",
                "arguments": {"intent": "Adding retry logic to the client"},
            },
        )
    ]
    title = inf._title(rows)
    assert fake.contexts  # the model was consulted
    assert title == "Title 0"


def test_anchorless_span_falls_back_to_heuristic_not_model():
    # Only narration, no intent/files/symbols: the model would hallucinate a
    # subject, so we title extractively from the words actually spoken and never
    # invoke the model.
    fake = _FakeTitle()
    inf = TitleInferencer(model=fake)
    rows = [_frow(kind="message.user", payload={"content": "Please fix the flaky retry behavior"})]
    title = inf._title(rows)
    assert fake.contexts == []  # model NOT consulted (no anchor)
    assert title and "retry" in title.lower()  # grounded in the real words


def test_anchorless_span_without_narration_abstains():
    # Actions only, nothing spoken: no honest title exists, so abstain (return
    # "") rather than let the model invent one.
    fake = _FakeTitle()
    inf = TitleInferencer(model=fake)
    rows = [_frow(tool_name="edit"), _frow(tool_name="shell")]
    assert inf._title(rows) == ""
    assert fake.contexts == []  # the model never guessed


def test_title_distinct_heuristic_fallback_respects_used():
    # The anchorless heuristic fallback still honors parent/sibling distinctness:
    # a second span with identical words abstains instead of repeating the title.
    fake = _FakeTitle()
    inf = TitleInferencer(model=fake)
    rows = [_frow(kind="message.user", payload={"content": "Please fix the flaky retry behavior"})]
    used: set = set()
    first = inf._title_distinct(rows, used)
    assert first and fake.contexts == []
    second = inf._title_distinct(rows, used)
    assert second == ""  # collides with the sibling -> abstain, don't guess


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
