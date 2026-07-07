# TraceForge test suite

Fast unit tests live directly under `tests/`. The **end-to-end (e2e) harness**
lives under `tests/e2e/` and drives real I/O — subprocess CLI invocations, live
source/sink classes, the gate IPC registry, and loopback fake network servers.

## Running tests

All commands use [`uv`](https://docs.astral.sh/uv/); the dev dependency group is
installed with `uv sync --group dev`.

```console
# Everything (unit + e2e), the same set CI runs:
uv run --no-progress python -m pytest -q -p no:cacheprovider

# Only the e2e harness:
uv run --no-progress python -m pytest -q -m e2e

# Skip the slow subprocess-spawning tests:
uv run --no-progress python -m pytest -q -m "not slow"

# Just the loopback-network sink/source tests:
uv run --no-progress python -m pytest -q -m net
```

`--strict-markers` is enabled, so every marker must be declared in
`pyproject.toml` (`[tool.pytest.ini_options] markers`). A typo'd marker fails
collection instead of silently doing nothing.

### Markers

| Marker         | Meaning                                                                              |
| -------------- | ------------------------------------------------------------------------------------ |
| `e2e`          | End-to-end test that drives real I/O (subprocess CLI, live sources/sinks, gate IPC). |
| `slow`         | Spawns a `python -m traceforge` subprocess or otherwise runs longer than a unit test.|
| `net`          | Uses a local **loopback** fake network server — never an external host.              |
| `windows_only` | Only applies on Windows (e.g. the gate's TCP transport); auto-skipped elsewhere.     |

`windows_only` tests are skipped automatically on non-Windows platforms (see the
`pytest_collection_modifyitems` hook in `tests/conftest.py`), so the Linux CI
matrix skips them by design.

## Coverage

`pytest-cov` is in the dev group, so coverage is runnable locally and in CI. There
is intentionally **no `--cov-fail-under` gate** yet:

```console
uv run --no-progress python -m pytest -q --cov=traceforge --cov-report=term-missing
```

## The e2e harness (`tests/e2e/`)

`tests/e2e/conftest.py` provides the shared fixtures; `tests/e2e/fakes/` provides
the reusable fake network backends. `tests/e2e/test_harness_smoke.py` is the
canary — one `@pytest.mark.e2e` test per fixture, each exercising the fixture
through the **real** consumer class it exists to serve.

### Fixtures (`tests/e2e/conftest.py`)

| Fixture              | What it gives you                                                                                                   |
| -------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `tmp_traceforge_home`| An isolated `$HOME`/`%USERPROFILE%` so anything reading `~/.traceforge` is sandboxed. Subprocesses inherit it.       |
| `score_server_url`   | Spawns `traceforge score` on a free loopback port, polls `GET /health`, yields the base URL.                         |
| `watch_daemon`       | Spawns `traceforge watch --frameworks claude --no-score` against an isolated home; waits for the `_default` gate session. Yields a `WatchDaemon` handle (`.home`, `.source_dir`, `.system_db`, `.output_db`, `.is_running()`, `.output`). |
| `gate_socket_lookup` | Callable `(session_id) -> sock_path \| None` reading the `gate_endpoints` table in the sandboxed `system.db`.        |

### Fake network backends (`tests/e2e/fakes/`)

All bind `127.0.0.1:0` (an ephemeral loopback port) and run in a daemon thread —
**loopback only, never an external host** (hence the `net` marker on tests that
use them). Each is exposed both as a fixture and as an importable class.

| Fixture           | Class / helper       | Serves                                                                                                  |
| ----------------- | -------------------- | ------------------------------------------------------------------------------------------------------ |
| `http_poll_server`| `HttpPollServer`     | Mutable body behind an `ETag`; conditional GETs (`If-None-Match`) get `304`. For `HttpPollSource`.      |
| `sse_server`      | `SSEServer`          | `text/event-stream` with `id`/`event`/`retry`; `close_current()` forces a `Last-Event-ID` reconnect. For `SSESource`. |
| `otel_collector`  | `RecordingServer`    | Records OTLP/HTTP POSTs; `.endpoint` (`…/v1/traces`), `.spans()`. For `OtelExporterSink`.               |
| `webhook_receiver`| `RecordingServer`    | Records POSTs; `.fail_next(n)`/`.set_status()` to exercise retries. For `WebhookSink`.                  |
| `fake_s3`         | `fake_s3()` / `FakeS3`| A [moto](https://github.com/getmoto/moto)-backed in-memory S3 bucket; `.bucket`, `.region`, `.list_keys()`, `.read_all()`. For `S3Sink`. |

Import the classes directly when you need finer control than the fixtures give:

```python
from tests.e2e.fakes import HttpPollServer, SSEServer, RecordingServer, fake_s3
```

### Isolation & platform notes

* Everything computes `Path.home()` **inside** functions, so overriding `HOME`/
  `USERPROFILE` (which `tmp_traceforge_home` does) fully redirects `~/.traceforge`
  for both in-process code and spawned subprocesses.
* The gate IPC transport is an `AF_UNIX` socket on POSIX and a loopback TCP address
  (`tcp://127.0.0.1:<port>`) on Windows. Assertions specific to the Windows form
  carry `@pytest.mark.windows_only`.
* CI runs on Linux (Python 3.11/3.12/3.13); `windows_only` tests are developed and
  run locally on Windows and skipped in CI.
