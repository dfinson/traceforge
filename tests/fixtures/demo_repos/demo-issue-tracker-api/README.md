# Demo Issue Tracker API

Small FastAPI service used for CodePlane demos and screenshots.

## What It Is

This repo simulates a lightweight support ticket backend with a few realistic flaws intentionally left in place so agent runs have something useful to do.

## API Overview

The API provides ticket management endpoints plus demo endpoints:

- **`GET /health`** - Health check for monitoring
- **`GET /hello`** - Simple greeting endpoint
- **`GET /joke`** - Returns a programming joke
- **`GET /tickets`** - List tickets with optional search and archive filtering
- **`POST /tickets`** - Create a new ticket
- **`PATCH /tickets/{ticket_id}/archive`** - Archive a ticket

For detailed endpoint documentation, see [API_REFERENCE.md](API_REFERENCE.md).

## Suggested CodePlane Demo Tasks

- Add input validation to the create ticket endpoint and cover it with tests.
- Make search match customer email as well as title and description.
- Tighten error handling around ticket archival.

## Run Locally

```bash
uv sync
uv run uvicorn app.main:app --reload
```

The API will be available at `http://localhost:8000`.

## Test

```bash
uv run pytest
```
