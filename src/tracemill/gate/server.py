"""Gate IPC server — listens on a unix socket (or named pipe on Windows) for gate requests.

The server runs in a background thread inside the Pipeline process. When a gate
request arrives (from `tracemill gate --stdin`), it:
  1. Deserializes the event JSON
  2. Calls pipeline.score_tool_call(payload)
  3. Fires the tool_preflight_gate callback
  4. Returns the Verdict as JSON
"""

from __future__ import annotations

import atexit
import json
import os
import socket
import struct
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from tracemill.governance.pipeline import GovernancePipeline
    from tracemill.governance.results import SessionMeta
    from tracemill.sdk.verdict import Verdict


class GateServer:
    """IPC server for cross-process gating."""

    def __init__(
        self,
        pipeline: "GovernancePipeline",
        tool_preflight_gate: "Callable[[dict, SessionMeta], Verdict | bool | None]",
        sock_path: str | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._tool_preflight_gate = tool_preflight_gate
        self._sock_path = sock_path or self._default_sock_path()
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    @staticmethod
    def _default_sock_path() -> str:
        gates_dir = Path.home() / ".tracemill" / "gates"
        gates_dir.mkdir(parents=True, exist_ok=True)
        return str(gates_dir / f"{os.getpid()}.sock")

    @property
    def sock_path(self) -> str:
        return self._sock_path

    def start(self) -> None:
        """Start the IPC server in a background daemon thread."""
        if self._running:
            return

        # Clean up stale socket file
        if os.path.exists(self._sock_path):
            os.unlink(self._sock_path)

        if sys.platform == "win32":
            # Windows: use TCP on localhost with a random port
            self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server.bind(("127.0.0.1", 0))
            # Store actual port in sock_path for registry
            _, port = self._server.getsockname()
            self._sock_path = f"tcp://127.0.0.1:{port}"
        else:
            self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._server.bind(self._sock_path)

        self._server.listen(16)
        self._server.settimeout(1.0)  # allow periodic check of _running
        self._running = True

        self._thread = threading.Thread(target=self._serve_loop, daemon=True, name="tracemill-gate")
        self._thread.start()

        atexit.register(self.stop)

    def stop(self) -> None:
        """Stop the IPC server and clean up."""
        self._running = False
        if self._server:
            try:
                self._server.close()
            except OSError:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        # Clean up socket file
        if not self._sock_path.startswith("tcp://") and os.path.exists(self._sock_path):
            try:
                os.unlink(self._sock_path)
            except OSError:
                pass

    def register_session(self, session_id: str) -> None:
        """Register a session_id → this server's socket in the registry."""
        from tracemill.gate.registry import register_session
        register_session(session_id, self._sock_path)

    def _serve_loop(self) -> None:
        while self._running:
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()

    def _handle_conn(self, conn: socket.socket) -> None:
        """Handle a single gate request."""
        try:
            conn.settimeout(10.0)
            data = self._recv_all(conn)
            if not data:
                return

            request = json.loads(data)
            payload = request.get("payload", request)
            response = self._process_gate_request(payload)
            resp_bytes = json.dumps(response).encode("utf-8")
            conn.sendall(struct.pack("!I", len(resp_bytes)) + resp_bytes)
        except Exception:
            try:
                err = json.dumps({"decision": "deny", "reason": "internal error"}).encode()
                conn.sendall(struct.pack("!I", len(err)) + err)
            except OSError:
                pass
        finally:
            conn.close()

    def _process_gate_request(self, payload: dict) -> dict:
        """Score and gate a tool call, returning verdict as dict."""
        from tracemill.sdk.verdict import interpret_callback_result

        meta = self._pipeline.score_tool_call(payload)
        if self._tool_preflight_gate is not None:
            verdict = interpret_callback_result(self._tool_preflight_gate(payload, meta))
        else:
            # No callback = allow everything
            from tracemill.sdk.verdict import Verdict
            verdict = Verdict.allow()

        return {
            "decision": verdict.decision.value,
            "reason": verdict.reason,
            "score": meta.risk_assessment.score if meta.risk_assessment else None,
            "level": meta.risk_assessment.level if meta.risk_assessment else None,
        }

    @staticmethod
    def _recv_all(conn: socket.socket) -> bytes:
        """Read a length-prefixed message (4-byte big-endian length + payload)."""
        header = b""
        while len(header) < 4:
            chunk = conn.recv(4 - len(header))
            if not chunk:
                return b""
            header += chunk
        length = struct.unpack("!I", header)[0]
        if length > 10 * 1024 * 1024:  # 10MB sanity limit
            return b""
        data = b""
        while len(data) < length:
            chunk = conn.recv(min(length - len(data), 65536))
            if not chunk:
                return b""
            data += chunk
        return data
