"""FastAPI application entry point and route definitions."""

from __future__ import annotations

from datetime import datetime, UTC

from fastapi import FastAPI, HTTPException, Query, status

from app.models import Ticket, TicketCreate, TicketListResponse
from app.repository import TicketRepository
from app.service import TicketService

app = FastAPI(title="Demo Issue Tracker API", version="0.1.0")

repository = TicketRepository()
service = TicketService(repository)


@app.get("/health")
def health() -> dict[str, str]:
    """Return a simple health-check response with the current timestamp."""
    return {"status": "ok", "timestamp": datetime.now(UTC).isoformat()}


@app.get("/hello")
def say_hello() -> dict[str, str]:
    """Return a greeting message."""
    return {"message": "Hello, World!"}


@app.get("/joke")
def get_joke() -> dict[str, str]:
    """Return a random programming joke."""
    return {"joke": "Why did the API go to the bar? To get a REST!"}


@app.get("/tickets", response_model=TicketListResponse)
def list_tickets(
    include_archived: bool = Query(default=False),
    search: str | None = Query(default=None),
) -> TicketListResponse:
    """List tickets with optional filtering by archive status and search term."""
    return service.list_tickets(include_archived=include_archived, search=search)


@app.post("/tickets", response_model=Ticket, status_code=status.HTTP_201_CREATED)
def create_ticket(payload: TicketCreate) -> Ticket:
    """Create a new ticket and return the persisted record."""
    return service.create_ticket(payload)


@app.patch("/tickets/{ticket_id}/archive", response_model=Ticket)
def archive_ticket(ticket_id: int) -> Ticket:
    """Archive a ticket by ID. Returns 404 if the ticket does not exist."""
    archived = service.archive_ticket(ticket_id)
    if archived is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    return archived
