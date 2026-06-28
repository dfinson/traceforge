"""In-memory ticket repository providing CRUD persistence."""

from __future__ import annotations

from app.models import Ticket, TicketPriority, TicketStatus


class TicketRepository:
    """Stores tickets in memory with seed data for demonstration purposes."""
    def __init__(self) -> None:
        self._tickets: list[Ticket] = [
            Ticket(
                id=1,
                title="Checkout button fails on Safari",
                description="Customers report the purchase CTA does nothing on Safari 17.",
                customer_email="ops@northwind.example",
                priority=TicketPriority.high,
                status=TicketStatus.investigating,
            ),
            Ticket(
                id=2,
                title="Exported CSV omits assignee",
                description="Analytics exports should include the current owner column.",
                customer_email="bi@acme.example",
                priority=TicketPriority.medium,
                status=TicketStatus.open,
            ),
            Ticket(
                id=3,
                title="Password reset email arrives twice",
                description="Duplicate notifications are confusing support contacts.",
                customer_email="support@globex.example",
                priority=TicketPriority.medium,
                status=TicketStatus.open,
            ),
            Ticket(
                id=4,
                title="Deprecated webhook endpoint still listed in docs",
                description="Archive this issue after the docs refresh lands.",
                customer_email="docs@initech.example",
                priority=TicketPriority.low,
                status=TicketStatus.archived,
            ),
        ]
        self._next_id = 5

    def list_tickets(self) -> list[Ticket]:
        """Return defensive copies of all stored tickets."""
        return [ticket.model_copy() for ticket in self._tickets]

    def create_ticket(self, ticket: Ticket) -> Ticket:
        """Persist a new ticket, assigning a unique ID."""
        created = ticket.model_copy(update={"id": self._next_id})
        self._next_id += 1
        self._tickets.append(created)
        return created.model_copy()

    def _get_ticket(self, ticket_id: int) -> Ticket | None:
        """Look up a ticket by ID. Returns the live reference or None."""
        for ticket in self._tickets:
            if ticket.id == ticket_id:
                return ticket
        return None

    def get_ticket(self, ticket_id: int) -> Ticket | None:
        """Look up a ticket by ID. Returns a defensive copy or None."""
        ticket = self._get_ticket(ticket_id)
        return ticket.model_copy() if ticket is not None else None

    def archive_ticket(self, ticket_id: int) -> Ticket | None:
        """Mark a ticket as archived. Returns a copy or None if not found."""
        ticket = self._get_ticket(ticket_id)
        if ticket is None:
            return None
        ticket.status = TicketStatus.archived
        return ticket.model_copy()
