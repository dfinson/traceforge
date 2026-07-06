"""Base abstractions for configurable ingestion sources."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import TracebackType

from traceforge.types import IngestionMode


@dataclass(slots=True)
class RawRecord:
    """A complete raw record emitted by a source transport."""

    payload: str
    source_name: str
    mode: IngestionMode
    sequence: int | None = None
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class Source(ABC):
    """Async source that yields complete logical records."""

    name: str

    @abstractmethod
    async def __aenter__(self) -> "Source":
        """Allocate any resources required by the source."""

    @abstractmethod
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Release any resources held by the source."""

    @abstractmethod
    def __aiter__(self) -> AsyncIterator[RawRecord]:
        """Yield complete logical records from the source."""
