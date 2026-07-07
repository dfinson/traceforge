"""Shared end-to-end test harness for TraceForge.

This is the Wave-0 scaffolding imported by the downstream E2E stories (#81–#86).
It provides:

* **Isolation** — ``tmp_traceforge_home`` redirects ``$HOME``/``%USERPROFILE%`` to a
  throwaway directory so anything that reads ``Path.home()/.traceforge`` (the Score
  API, watch daemon, gate registry, default sinks) is fully sandboxed.
* **Process fixtures** — ``score_server_url`` and ``watch_daemon`` spawn the real
  ``python -m traceforge`` subprocesses and health-poll them before yielding.
* **Registry access** — ``gate_socket_lookup`` reads the ``gate_endpoints`` table
  in the sandboxed ``system.db``.
* **Fake network backends** — ``http_poll_server``, ``sse_server``, ``fake_s3``,
  ``otel_collector`` and ``webhook_receiver`` (loopback only; see
  ``tests/e2e/fakes/``) so sink/source tests assert real I/O against the actual
  classes.

The unit-level factories (``make_event`` etc.) live in the repo-root
``tests/conftest.py`` and remain available here.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable

import pytest

from tests.e2e.fakes.http_poll import HttpPollServer
from tests.e2e.fakes.recording import RecordingServer
from tests.e2e.fakes.sse import SSEServer

# ─── Isolation ───────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_traceforge_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """An isolated ``$HOME`` so ``~/.traceforge`` never touches the real one.

    Overrides every variable ``os.path.expanduser`` consults (POSIX ``HOME``;
    Windows ``USERPROFILE`` and ``HOMEDRIVE``/``HOMEPATH``) and scrubs the
    framework-detection env vars, so auto-detection only sees what a test sets up.
    Subprocesses spawned by the other fixtures inherit this patched environment.
    """
    home = tmp_path_factory.mktemp("tf-home")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    drive, tail = os.path.splitdrive(str(home))
    if drive:  # Windows: back up USERPROFILE with HOMEDRIVE/HOMEPATH
        monkeypatch.setenv("HOMEDRIVE", drive)
        monkeypatch.setenv("HOMEPATH", tail or "\\")

    # Redirect app-data probes (cline/goose/amazonq detectors) into the sandbox
    # and drop source-specific overrides so detection is deterministic.
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    for var in ("CODEX_HOME", "CONTINUE_GLOBAL_DIR", "TRACEFORGE_CONFIG"):
        monkeypatch.delenv(var, raising=False)

    (home / ".traceforge").mkdir(parents=True, exist_ok=True)
    return home


# ─── Subprocess helpers ──────────────────────────────────────────────────────


class BackgroundProcess:
    """A spawned ``python -m traceforge`` process with drained stdout/stderr."""

    def __init__(self, args: list[str]) -> None:
        self.args = args
        self.proc = subprocess.Popen(
            args,
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._lines: list[str] = []
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.append(line)

    @property
    def output(self) -> str:
        return "".join(self._lines)

    def is_running(self) -> bool:
        return self.proc.poll() is None

    def terminate(self, timeout: float = 10.0) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=timeout)
        self._reader.join(timeout=2.0)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _traceforge_cmd(*args: str) -> list[str]:
    return [sys.executable, "-m", "traceforge", *args]


def _wait_for_http_ok(url: str, proc: BackgroundProcess, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        if not proc.is_running():
            raise RuntimeError(
                f"process exited early (code {proc.proc.returncode}).\n--- output ---\n{proc.output}"
            )
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    resp.read()
                    return
        except Exception as exc:  # connection refused until the server binds
            last_err = exc
        time.sleep(0.2)
    raise TimeoutError(
        f"{url} not ready within {timeout}s (last error: {last_err!r}).\n--- output ---\n{proc.output}"
    )


def _query_gate_endpoint(system_db: Path, session_id: str) -> str | None:
    if not system_db.exists():
        return None
    conn = sqlite3.connect(str(system_db))
    try:
        row = conn.execute(
            "SELECT sock_path FROM gate_endpoints WHERE session_id = ?", (session_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None  # table not created yet
    finally:
        conn.close()
    return row[0] if row else None


def _wait_for_gate_session(
    system_db: Path, session_id: str, proc: BackgroundProcess, timeout: float
) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not proc.is_running():
            raise RuntimeError(
                f"watch exited early (code {proc.proc.returncode}).\n--- output ---\n{proc.output}"
            )
        value = _query_gate_endpoint(system_db, session_id)
        if value is not None:
            return value
        time.sleep(0.25)
    raise TimeoutError(
        f"gate session {session_id!r} not registered within {timeout}s.\n--- output ---\n{proc.output}"
    )


# ─── Process fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def score_server_url(tmp_traceforge_home: Path) -> str:
    """Spawn ``traceforge score`` on a free port and yield its base URL.

    Blocks until ``GET /health`` returns 200. POST scoring requests to
    ``f"{score_server_url}/score"``.
    """
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    proc = BackgroundProcess(_traceforge_cmd("score", "--listen", f"127.0.0.1:{port}"))
    try:
        _wait_for_http_ok(f"{url}/health", proc, timeout=60.0)
        yield url
    finally:
        proc.terminate()


class WatchDaemon:
    """Handle onto a running ``traceforge watch`` daemon.

    Attributes:
        home: the isolated ``$HOME``.
        source_dir: ``~/.claude/projects`` — drop ``*.jsonl`` here to feed the daemon.
        system_db / output_db: the sandboxed ``system.db`` and default sqlite sink.
    """

    def __init__(self, proc: BackgroundProcess, home: Path) -> None:
        self._proc = proc
        self.home = home
        self.traceforge_dir = home / ".traceforge"
        self.system_db = self.traceforge_dir / "system.db"
        self.output_db = self.traceforge_dir / "traceforge.db"
        self.source_dir = home / ".claude" / "projects"

    @property
    def process(self) -> subprocess.Popen:
        return self._proc.proc

    @property
    def output(self) -> str:
        return self._proc.output

    def is_running(self) -> bool:
        return self._proc.is_running()


@pytest.fixture
def watch_daemon(tmp_traceforge_home: Path) -> WatchDaemon:
    """Spawn ``traceforge watch`` against an isolated home and health-poll it.

    Seeds ``~/.claude/projects`` so the ``claude`` framework is detected (the
    daemon exits if nothing is detected), spawns ``watch --frameworks claude
    --no-score`` (``--no-score`` keeps the fixed 7331 port free — use
    ``score_server_url`` for the Score API), then waits until the gate registry
    has a live ``_default`` session before yielding.
    """
    home = tmp_traceforge_home
    (home / ".claude" / "projects").mkdir(parents=True, exist_ok=True)

    proc = BackgroundProcess(
        _traceforge_cmd("watch", "--frameworks", "claude", "--no-score", "--log-level", "DEBUG")
    )
    daemon = WatchDaemon(proc, home)
    try:
        _wait_for_gate_session(daemon.system_db, "_default", proc, timeout=90.0)
        yield daemon
    finally:
        proc.terminate()


@pytest.fixture
def gate_socket_lookup(tmp_traceforge_home: Path) -> Callable[[str], str | None]:
    """Return ``lookup(session_id) -> sock_path | None`` over the sandboxed registry.

    Reads the ``gate_endpoints`` table in ``~/.traceforge/system.db`` directly.
    ``sock_path`` is a unix-socket path on POSIX or ``tcp://127.0.0.1:<port>`` on
    Windows. Returns ``None`` if the db/table/row is absent.
    """
    system_db = tmp_traceforge_home / ".traceforge" / "system.db"

    def lookup(session_id: str) -> str | None:
        return _query_gate_endpoint(system_db, session_id)

    return lookup


# ─── Fake network backends ───────────────────────────────────────────────────


@pytest.fixture
def http_poll_server() -> HttpPollServer:
    """A loopback HTTP endpoint with ETag/304 support (for ``HttpPollSource``)."""
    server = HttpPollServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def sse_server() -> SSEServer:
    """A loopback SSE endpoint with reconnect/resume support (for ``SSESource``)."""
    server = SSEServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def otel_collector() -> RecordingServer:
    """A fake OTLP/HTTP collector; POSTs land in ``.received`` / ``.spans()``.

    ``.endpoint`` is the full ``/v1/traces`` URL to hand to ``OtelExporterSink``.
    """
    server = RecordingServer()
    server.start()
    server.endpoint = f"{server.url}/v1/traces"  # type: ignore[attr-defined]
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def webhook_receiver() -> RecordingServer:
    """A fake webhook endpoint; POST bodies land in ``.received``.

    Use ``.fail_next(n)`` / ``.set_status(code)`` to exercise ``WebhookSink`` retries.
    """
    server = RecordingServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def fake_s3():
    """A moto-backed in-memory S3 bucket (for ``S3Sink``); yields a ``FakeS3``.

    Construct the sink inside the test as ``S3Sink(bucket=fake_s3.bucket,
    region=fake_s3.region)`` — the mock intercepts boto3 at the wire level, so no
    ``endpoint_url`` is needed. Skips if ``moto``/``boto3`` are unavailable.
    """
    pytest.importorskip("boto3")
    pytest.importorskip("moto")
    from tests.e2e.fakes.s3 import fake_s3 as _fake_s3

    with _fake_s3() as handle:
        yield handle
