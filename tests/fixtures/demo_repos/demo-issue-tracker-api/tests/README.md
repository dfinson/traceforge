# Tests

Integration and unit tests for the Demo Issue Tracker API.

## Running Tests

```bash
uv run pytest
```

## Structure

- `test_api.py` — End-to-end HTTP tests exercising all API endpoints via
  FastAPI's `TestClient`.

## Conventions

- Tests use the FastAPI `TestClient` for synchronous request simulation.
- Each test function targets a single behaviour or endpoint scenario.
- No external services or databases are required; the repository uses an
  in-memory store.
