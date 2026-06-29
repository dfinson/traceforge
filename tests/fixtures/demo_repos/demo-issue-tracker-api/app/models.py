"""Pydantic domain models and request/response schemas for the issue tracker."""

from __future__ import annotations

from datetime import datetime, UTC
from enum import StrEnum

from pydantic import BaseModel, Field, computed_field


class TicketPriority(StrEnum):
    """Allowed priority levels for a ticket."""

    low = "low"
    medium = "medium"
    high = "high"


class TicketStatus(StrEnum):
    """Lifecycle states a ticket can occupy."""

    open = "open"
    investigating = "investigating"
    resolved = "resolved"
    archived = "archived"


class Ticket(BaseModel):
    """Full ticket representation returned by the API."""

    id: int
    title: str
    description: str
    customer_email: str
    priority: TicketPriority = TicketPriority.medium
    status: TicketStatus = TicketStatus.open
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @computed_field
    @property
    def archived(self) -> bool:
        """Derived from status; True when status is archived."""
        return self.status == TicketStatus.archived


class TicketCreate(BaseModel):
    """Request body schema for creating a new ticket."""

    title: str
    description: str
    customer_email: str
    priority: TicketPriority = TicketPriority.medium


class TicketListResponse(BaseModel):
    """Paginated list response wrapping ticket items."""

    items: list[Ticket]
    total: int
