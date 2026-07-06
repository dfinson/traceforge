"""Score API — lightweight HTTP server for preflight tool-call scoring."""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from traceforge.governance.pipeline import GovernancePipeline

logger = logging.getLogger(__name__)


class _ScoreHandler(BaseHTTPRequestHandler):
    """HTTP handler for POST /score endpoint."""

    pipeline: "GovernancePipeline"  # set on class before server starts

    def do_POST(self) -> None:
        if self.path != "/score":
            self._respond(404, {"error": "not found"})
            return

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self._respond(400, {"error": "empty body"})
            return

        try:
            body = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._respond(400, {"error": f"invalid JSON: {exc}"})
            return

        # Required fields
        tool_name = body.get("tool_name")
        arguments = body.get("arguments")
        session_id = body.get("session_id", "anonymous")

        if not tool_name:
            self._respond(400, {"error": "missing required field: tool_name"})
            return
        if arguments is None:
            self._respond(400, {"error": "missing required field: arguments"})
            return

        # Ensure arguments is a dict for tool_input
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {"raw": arguments}

        # Build payload dict matching score_tool_call's expected shape
        score_payload = {
            "tool_name": tool_name,
            "tool_input": arguments,
            "session_id": session_id,
        }
        if body.get("server"):
            score_payload["server"] = body["server"]
        if body.get("effect"):
            score_payload["effect"] = body["effect"]
        if body.get("role"):
            score_payload["role"] = body["role"]

        try:
            result = self.pipeline.score_tool_call(score_payload)
            self._respond(200, _serialize_session_meta(result))
        except Exception as exc:
            logger.exception("Score API error")
            self._respond(500, {"error": str(exc)})

    def do_GET(self) -> None:
        if self.path == "/health":
            self._respond(200, {"status": "ok"})
        else:
            self._respond(404, {"error": "not found"})

    def _respond(self, status: int, body: dict) -> None:
        payload = json.dumps(body, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        logger.debug("ScoreAPI: %s", format % args)


def _serialize_session_meta(meta) -> dict:
    """Serialize SessionMeta or EventTrace to JSON-safe dict."""
    from traceforge.governance.results import SessionMeta
    from traceforge.trace import EventTrace

    if isinstance(meta, EventTrace):
        result: dict = {}
        if meta.risk_score is not None:
            result["risk_assessment"] = {
                "score": meta.risk_score,
                "level": meta.risk_band,
            }
        if meta.suggested_action is not None:
            result["recommendation"] = {
                "action": meta.suggested_action,
                "reason_code": meta.reason,
            }
        if meta.mechanism or meta.effect:
            result["evidence"] = {
                "canonical_tool": meta.canonical_tool,
                "mechanism": meta.mechanism,
                "effect": meta.effect,
                "scope": list(meta.scope) if meta.scope else [],
                "role": list(meta.role) if meta.role else [],
            }
        result["stage"] = meta.stage
        return result

    if isinstance(meta, SessionMeta):
        result = {}
        if meta.risk_assessment is not None:
            result["risk_assessment"] = {
                "score": meta.risk_assessment.score,
                "level": meta.risk_assessment.level,
                "confidence": meta.risk_assessment.confidence,
            }
        if meta.recommendation is not None:
            result["recommendation"] = {
                "action": meta.recommendation.recommended_action.value,
                "reason_code": meta.recommendation.reason_code,
                "canonical_id": meta.recommendation.canonical_id,
            }
        if meta.evidence is not None:
            e = meta.evidence
            result["evidence"] = {
                "canonical_id": e.canonical_id,
                "mechanism": e.mechanism,
                "effect": e.effect,
                "scope": list(e.scope) if e.scope else [],
                "role": list(e.role) if e.role else [],
            }
        return result
    # Fallback
    return {"raw": str(meta)}


class ScoreServer:
    """Wraps ThreadingHTTPServer for the Score API."""

    def __init__(self, pipeline: "GovernancePipeline", listen: str = "localhost:7331") -> None:
        self._pipeline = pipeline
        host, _, port_str = listen.rpartition(":")
        self._host = host or "localhost"
        self._port = int(port_str) if port_str else 7331
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start_background(self) -> None:
        """Start the Score API in a daemon thread."""
        _ScoreHandler.pipeline = self._pipeline
        self._server = ThreadingHTTPServer((self._host, self._port), _ScoreHandler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="traceforge-score-api",
            daemon=True,
        )
        self._thread.start()
        logger.info("Score API listening on %s:%d", self._host, self._port)

    def stop(self) -> None:
        """Shut down the Score API server."""
        if self._server:
            self._server.shutdown()
            self._server = None
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None


# ─── CLI command ─────────────────────────────────────────────────────────────


@click.command()
@click.option(
    "--listen", default="localhost:7331", help="Host:port to bind (default: localhost:7331)."
)
@click.option("--config", "config_path", type=click.Path(exists=True), default=None)
def score(listen: str, config_path: str | None) -> None:
    """Run the Score API server standalone (no file watching)."""
    from pathlib import Path

    from traceforge.cli.factory import create_default_pipeline
    from traceforge.governance.persistence import SystemStore

    db_path = Path.home() / ".traceforge" / "system.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = SystemStore(db_path)

    pipeline = create_default_pipeline(store)

    server = ScoreServer(pipeline, listen=listen)
    click.echo(f"Starting Score API on {listen} ...")
    server.start_background()

    try:
        # Block until Ctrl+C
        threading.Event().wait()
    except KeyboardInterrupt:
        click.echo("\nShutting down...")
        server.stop()
        store.close()
