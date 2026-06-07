"""Base adapter interface for parsing raw input into SessionEvents."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from tracemill.types import SessionEvent


class Adapter(ABC):
    @abstractmethod
    def parse(self, raw: bytes | str) -> Iterator[SessionEvent]:
        """Parse raw input and yield zero or more SessionEvents.

        Must never raise — log warnings for unparseable input and continue.
        """
        ...
