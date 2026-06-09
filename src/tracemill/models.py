"""Base Pydantic model classes for all tracemill models.

Defines two base classes that encode structural policies:

- ``StrictModel``: Rejects unknown fields (``extra="forbid"``).
  Used for all configuration and mapping models where typos must be caught.

- ``FrozenModel``: Immutable after construction (``frozen=True``).
  Used for domain value objects that flow through the pipeline.

All tracemill models inherit from one of these — never from raw ``BaseModel``.
This eliminates per-class ``model_config`` repetition and enforces consistency.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """Base for configuration / schema models. Rejects unknown fields."""

    model_config = ConfigDict(extra="forbid")


class FrozenModel(BaseModel):
    """Base for immutable domain objects. Cannot be mutated after construction."""

    model_config = ConfigDict(frozen=True)
