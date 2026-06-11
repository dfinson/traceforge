"""Assessment API — synchronous scoring interface for gate integrations.

Returns SessionMeta — the same shape sinks receive in the standard pipeline.
"""

from tracemill.assess.assessor import assess, assess_event

__all__ = [
    "assess",
    "assess_event",
]
