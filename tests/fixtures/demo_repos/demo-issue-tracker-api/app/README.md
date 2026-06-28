# App Package

This package contains the core application code for the Demo Issue Tracker API.

## Modules

| Module | Purpose |
|--------|---------|
| `main.py` | FastAPI application instance, route definitions, and dependency wiring |
| `models.py` | Pydantic domain models and request/response schemas |
| `repository.py` | In-memory data store with CRUD operations for tickets |
| `service.py` | Business logic layer sitting between routes and the repository |

## Architecture

```
Routes (main.py)
  └─▶ Service (service.py)
        └─▶ Repository (repository.py)
              └─▶ Models (models.py)
```

The application follows a layered architecture: HTTP handlers delegate to the
service layer, which orchestrates domain logic and calls through to the
repository for persistence.
