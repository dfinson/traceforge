"""Base adapter interfaces for parsing raw input into SessionEvents."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import Any

from tracemill.types import SessionEvent

logger = logging.getLogger(__name__)


class Adapter(ABC):
    """Root interface: parse raw bytes/str into SessionEvents."""

    @abstractmethod
    def parse(self, raw: bytes | str) -> Iterator[SessionEvent]:
        """Parse raw input and yield zero or more SessionEvents.

        Must never raise — log warnings for unparseable input and continue.
        """
        ...


class JsonLineAdapter(Adapter):
    """Base for adapters that consume one JSON object per line.

    Handles the common boilerplate:
    - bytes → str decoding
    - JSON parsing with error logging
    - dict validation
    - Delegates to ``parse_dict()`` which subclasses implement

    The parsed dict is automatically attached as ``raw_event`` on every
    SessionEvent yielded by ``parse_dict()``.
    """

    @abstractmethod
    def parse_dict(self, obj: dict[str, Any]) -> Iterator[SessionEvent]:
        """Convert a parsed JSON dict into SessionEvents.

        Subclasses implement framework-specific deserialization here.
        The ``raw_event`` field is set automatically by the base class.
        """
        ...

    def parse(self, raw: bytes | str) -> Iterator[SessionEvent]:
        if isinstance(raw, bytes):
            try:
                text = raw.decode("utf-8")
            except (UnicodeDecodeError, ValueError):
                logger.warning("%s: failed to decode bytes as UTF-8", self.__class__.__name__)
                return
        else:
            text = raw
        text = text.strip()
        if not text:
            return

        try:
            obj = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            logger.warning("%s: failed to parse JSON line", self.__class__.__name__)
            return

        if not isinstance(obj, dict):
            logger.warning("%s: expected JSON object, got %s", self.__class__.__name__, type(obj).__name__)
            return

        try:
            for event in self.parse_dict(obj):
                if event.raw_event is None:
                    yield event.model_copy(update={"raw_event": obj})
                else:
                    yield event
        except Exception as exc:
            logger.warning("%s: parse_dict raised: %s", self.__class__.__name__, exc)
