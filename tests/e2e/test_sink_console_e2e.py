"""End-to-end tests for :class:`traceforge.sinks.console.ConsoleSink` (issue #83).

Injects a real stream and reads back exactly what the sink prints, asserting the
stable human-facing format: the governance-action filter decides which events
surface, the risk score and argument preview are rendered, closed segments come
out as indented TOC lines, and ANSI coloring is present only when enabled against
a TTY. This is the sink's actual ``print`` path, captured verbatim.
"""

from __future__ import annotations

import io
import sys

import pytest

from tests.conftest import make_event
from tests.e2e._sink_governance import governed_event
from traceforge.sinks.console import ConsoleSink
from traceforge.types import TitleUpdate


class _TtyStream(io.StringIO):
    """A StringIO that claims to be a TTY so ``ConsoleSink`` enables color."""

    def isatty(self) -> bool:
        return True


@pytest.mark.e2e
async def test_console_deny_event_rendered_with_risk_and_args() -> None:
    buf = io.StringIO()
    sink = ConsoleSink(color=False, stream=buf)
    await sink.on_event(governed_event("deny", tool_name="rm", arguments="-rf /tmp/x", score=90))

    out = buf.getvalue()
    assert "DENY" in out
    assert "rm" in out
    assert "[risk:90]" in out
    assert "-rf /tmp/x" in out
    assert "\033[" not in out  # color=False -> no ANSI


@pytest.mark.e2e
async def test_console_warn_event_rendered() -> None:
    buf = io.StringIO()
    sink = ConsoleSink(color=False, stream=buf)
    await sink.on_event(governed_event("warn", tool_name="edit"))
    assert "WARN" in buf.getvalue()


@pytest.mark.e2e
async def test_console_allow_is_filtered_by_default() -> None:
    buf = io.StringIO()
    sink = ConsoleSink(color=False, stream=buf)  # default filter: warn/deny/escalate
    await sink.on_event(governed_event("allow"))
    assert buf.getvalue() == ""


@pytest.mark.e2e
async def test_console_event_without_classification_is_suppressed() -> None:
    buf = io.StringIO()
    sink = ConsoleSink(color=False, stream=buf)
    await sink.on_event(make_event(session_id="plain"))  # no metadata/classification
    assert buf.getvalue() == ""


@pytest.mark.e2e
async def test_console_title_updates_render_as_toc_lines() -> None:
    buf = io.StringIO()
    sink = ConsoleSink(color=False, stream=buf)
    await sink.on_title_update(
        TitleUpdate(session_id="s", segment_id="a", kind="activity", title="Fix the bug")
    )
    await sink.on_title_update(
        TitleUpdate(session_id="s", segment_id="b", kind="step", title="Write a test")
    )

    lines = buf.getvalue().splitlines()
    assert lines[0] == "ACTIVITY Fix the bug"  # activity: no indent
    assert lines[1] == "  STEP Write a test"  # nested kind: indented


@pytest.mark.e2e
async def test_console_color_emits_ansi_on_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    tty = _TtyStream()
    monkeypatch.setattr(sys, "stderr", tty)  # color gate consults sys.stderr.isatty()
    sink = ConsoleSink(color=True)  # stream defaults to the patched sys.stderr
    await sink.on_event(governed_event("deny"))
    assert "\033[" in tty.getvalue()
