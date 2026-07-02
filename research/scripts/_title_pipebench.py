"""Measure the *live* pipeline footprint with titling ON vs OFF on a real session.

Reconstructs a real multi-thousand-event Copilot session from the labelling
corpus (full enrichment preserved via ``metadata_json``) and replays it through
the production :class:`tracemill.pipeline.EventPipeline` with the real phase +
boundary models, then once more with ``enable_title=True``. The delta is the
true cost of titling in situ: peak working set, CPU time, end-to-end wall time,
per-event push latency, and per-segment titling latency.

Peak RSS uses the Win32 ``PeakWorkingSetSize`` counter (exact, no polling, no
extra deps). Run each mode in its own process for a clean per-mode peak:

    cd <repo root>
    $env:CUDA_VISIBLE_DEVICES="-1"
    .venv\\Scripts\\python.exe -u -m scripts._title_pipebench <session.parquet> off
    .venv\\Scripts\\python.exe -u -m scripts._title_pipebench <session.parquet> on
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import sys
import time
from ctypes import wintypes
from datetime import datetime, timezone

import pyarrow.parquet as pq

from tracemill.pipeline import EventPipeline
from tracemill.sinks.base import StorageSink
from tracemill.title.inferencer import TitleInferencer
from tracemill.types import EventMetadata, SessionEvent


class _PMC(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


_kernel32 = ctypes.windll.kernel32
_psapi = ctypes.windll.psapi
_kernel32.GetCurrentProcess.restype = wintypes.HANDLE
_psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
_psapi.GetProcessMemoryInfo.restype = wintypes.BOOL


def _mem() -> tuple[float, float]:
    """(current RSS, peak RSS) in MB."""
    c = _PMC()
    c.cb = ctypes.sizeof(_PMC)
    if not _psapi.GetProcessMemoryInfo(_kernel32.GetCurrentProcess(), ctypes.byref(c), c.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return c.WorkingSetSize / 1e6, c.PeakWorkingSetSize / 1e6


class NullSink(StorageSink):
    async def on_event(self, event):
        pass

    async def on_span(self, span):
        pass

    async def on_usage(self, usage):
        pass

    async def flush(self):
        pass

    async def close(self):
        pass


def load_events(path: str) -> list[SessionEvent]:
    t = pq.read_table(path).to_pylist()
    t.sort(key=lambda r: r["seq"])
    out: list[SessionEvent] = []
    for r in t:
        payload = json.loads(r["payload_json"]) if r.get("payload_json") else {}
        mj = r.get("metadata_json")
        md = EventMetadata.model_validate(json.loads(mj)) if mj else EventMetadata()
        # Drop any previously-stamped structuring so the pipeline produces it fresh.
        md = md.model_copy(
            update={"phase": None, "boundary": None, "activity_title": None, "step_title": None}
        )
        ts = r["timestamp_ns"]
        if not isinstance(ts, datetime):
            ts = datetime.fromtimestamp(ts / 1e9, tz=timezone.utc)
        out.append(
            SessionEvent(
                id=r["event_id"],
                kind=r["kind"],
                session_id=r["session_id"],
                timestamp=ts,
                payload=payload,
                metadata=md,
            )
        )
    return out


class TimedTitler(TitleInferencer):
    """Wraps the real titler to count + time every model invocation."""

    def __init__(self):
        # TITLE_SERVE_DIR points the served artifact at a custom int8 ONNX dir
        # (encoder/decoder/tokenizer) without touching the packaged tiny model,
        # so alternate-capacity models can be footprint-benched on the real path.
        super().__init__(model_dir=os.environ.get("TITLE_SERVE_DIR") or None)
        self.calls = 0
        self.secs = 0.0

    def _title(self, rows):
        t0 = time.perf_counter()
        try:
            return super()._title(rows)
        finally:
            self.calls += 1
            self.secs += time.perf_counter() - t0

    def _title_distinct(self, rows, used):
        t0 = time.perf_counter()
        try:
            return super()._title_distinct(rows, used)
        finally:
            self.calls += 1
            self.secs += time.perf_counter() - t0


async def run(events: list[SessionEvent], title: bool) -> dict[str, float]:
    rss0, _ = _mem()
    titler = TimedTitler() if title else None
    pipe = EventPipeline(
        sinks=[NullSink()],
        enable_phase=True,
        enable_boundary=True,
        title_inferencer=titler,
        enable_title=False,
    )

    # Heartbeat probe: a coroutine that wants to tick every 10 ms. Whatever gap
    # it actually sees beyond that is the event loop being blocked by synchronous
    # work in the pipeline. This is the metric the to_thread offload targets.
    stalls: list[float] = []
    hb_stop = False

    async def heartbeat() -> None:
        last = time.perf_counter()
        while not hb_stop:
            await asyncio.sleep(0.010)
            now = time.perf_counter()
            stalls.append((now - last - 0.010) * 1e3)
            last = now

    hb_task = asyncio.ensure_future(heartbeat())

    push_ms: list[float] = []
    cpu0, wall0 = time.process_time(), time.perf_counter()
    for ev in events:
        t0 = time.perf_counter()
        await pipe.push(ev)
        push_ms.append((time.perf_counter() - t0) * 1e3)
    t_flush0 = time.perf_counter()
    await pipe.flush()
    flush_s = time.perf_counter() - t_flush0
    wall = time.perf_counter() - wall0
    cpu = time.process_time() - cpu0

    hb_stop = True
    await hb_task

    rss1, peak = _mem()
    push_ms.sort()
    n = len(push_ms)
    p50 = push_ms[n // 2]
    p95 = push_ms[int(n * 0.95)]
    p999 = push_ms[min(n - 1, int(n * 0.999))]
    max_stall = max(stalls) if stalls else 0.0

    print(f"\n=== titling {'ON' if title else 'OFF'} | {n} events ===")
    print(f"wall total        : {wall:8.2f} s   (flush {flush_s:.2f} s)")
    print(f"CPU time          : {cpu:8.2f} s   -> {100 * cpu / wall:5.1f}% of one core")
    print(f"throughput        : {n / wall:8.0f} events/s")
    print(f"push latency  p50  : {p50:8.2f} ms")
    print(f"push latency  p95  : {p95:8.2f} ms")
    print(f"push latency  p99.9: {p999:8.2f} ms")
    print(f"loop max stall    : {max_stall:8.2f} ms   (10 ms heartbeat; high = event loop blocked)")
    print(f"RSS before run    : {rss0:8.1f} MB")
    print(f"RSS after  run    : {rss1:8.1f} MB")
    print(f"RSS PEAK          : {peak:8.1f} MB")
    if titler is not None:
        print(f"titler segments   : {titler.calls}")
        print(
            f"titler model time : {titler.secs:8.2f} s   "
            f"({1e3 * titler.secs / max(titler.calls, 1):.0f} ms/segment, "
            f"{100 * titler.secs / wall:.0f}% of wall)"
        )

    metrics = {
        "events": n,
        "wall_s": wall,
        "cpu_s": cpu,
        "cpu_core_pct": 100 * cpu / wall,
        "throughput_eps": n / wall,
        "push_p50_ms": p50,
        "push_p95_ms": p95,
        "push_p999_ms": p999,
        "loop_max_stall_ms": max_stall,
        "rss_before_mb": rss0,
        "rss_after_mb": rss1,
        "rss_peak_mb": peak,
    }
    if titler is not None:
        metrics["titler_segments"] = titler.calls
        metrics["titler_model_s"] = titler.secs
    return metrics


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    path = sys.argv[1]
    mode = sys.argv[2] if len(sys.argv) > 2 else "on"
    events = load_events(path)
    print(f"loaded {len(events)} events from {path}")
    metrics = asyncio.run(run(events, title=(mode == "on")))

    # Log AFTER measurement so the mlflow import never contaminates the RSS probe.
    import mlflow
    from tracemill_research.mlflow_utils import log_yaml_params, start_run
    from tracemill_research.paths import EXPERIMENTS_DIR

    yaml_path = EXPERIMENTS_DIR / "titler-live-footprint.yaml"
    with start_run("titler-live-footprint-v1", run_name=f"titling-{mode}", tags={"titling": mode}):
        log_yaml_params(yaml_path)
        mlflow.log_param("titling", mode)
        mlflow.log_param("events", metrics["events"])
        mlflow.log_param("source_session", os.path.basename(path))
        for k, v in metrics.items():
            mlflow.log_metric(k, float(v))


if __name__ == "__main__":
    main()
