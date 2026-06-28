"""Business logic layer for ticket operations."""

from __future__ import annotations

from app.models import Ticket, TicketCreate, TicketListResponse
from app.repository import TicketRepository


class TicketService:
    """Orchestrates ticket operations between routes and the repository."""

    def __init__(self, repository: TicketRepository) -> None:
        self._repository = repository

    def list_tickets(self, *, include_archived: bool = False, search: str | None = None) -> TicketListResponse:
        """Return tickets filtered by archive status and optional search term."""
        tickets = self._repository.list_tickets()

        if search:
            lowered = search.lower()
            tickets = [
                ticket
                for ticket in tickets
                if lowered in ticket.title.lower() or lowered in ticket.description.lower()
            ]

        if not include_archived:
            tickets = [t for t in tickets if not t.archived]

        return TicketListResponse(items=tickets, total=len(tickets))

    def create_ticket(self, payload: TicketCreate) -> Ticket:
        """Validate and persist a new ticket from the given payload."""
        ticket = Ticket(
            id=0,
            title=payload.title.strip(),
            description=payload.description.strip(),
            customer_email=payload.customer_email.strip(),
            priority=payload.priority,
        )
        return self._repository.create_ticket(ticket)

    def archive_ticket(self, ticket_id: int) -> Ticket | None:
        """Archive a ticket by ID. Returns the updated ticket or None."""
        return self._repository.archive_ticket(ticket_id)
