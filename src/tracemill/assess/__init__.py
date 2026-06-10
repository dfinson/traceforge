"""Assessment API — synchronous scoring interface for gate integrations."""

from tracemill.assess.assessor import assess_event
from tracemill.assess.types import AssessmentResult, GovernanceAssessment

__all__ = [
    "AssessmentResult",
    "GovernanceAssessment",
    "assess_event",
]
