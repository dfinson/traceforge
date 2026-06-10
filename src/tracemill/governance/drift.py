"""Phase-aware behavioral drift detection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tracemill.governance.persistence import SystemStore
    from tracemill.governance.state import SessionStateSnapshot
    from tracemill.governance.types import EnrichmentContext


# Default phase distribution (exploration-heavy in early session)
_DEFAULT_PHASES = ("exploration", "implementation", "testing", "documentation", "deployment")


@dataclass(frozen=True)
class DriftResult:
    """Result of drift analysis."""
    current_phase: str
    baseline_distribution: dict[str, float]  # phase → proportion
    current_distribution: dict[str, float]
    divergence: float  # 0.0–1.0 KL-inspired divergence
    anomaly: bool  # True if divergence exceeds threshold


class DriftDetector:
    """Detects behavioral drift by comparing phase distributions to baseline."""

    def __init__(self, store: "SystemStore", threshold: float = 0.3) -> None:
        self._store = store
        self._threshold = threshold

    def detect(
        self,
        ctx: "EnrichmentContext",
        state_snapshot: "SessionStateSnapshot",
        cap: set[str],
    ) -> DriftResult | None:
        """Compare current session phase window against stored baseline."""
        phase_window = state_snapshot.phase_window
        if len(phase_window) < 5:
            return None  # Not enough data

        # Current distribution from window
        current_dist = self._compute_distribution(phase_window)
        current_phase = phase_window[-1] if phase_window else "unknown"

        # Get baseline
        baseline = self._get_baseline(ctx)
        if not baseline:
            return DriftResult(
                current_phase=current_phase,
                baseline_distribution={},
                current_distribution=current_dist,
                divergence=0.0,
                anomaly=False,
            )

        # Compute divergence
        baseline_dist = baseline["phase_counts"]
        total = baseline["total_events"]
        if total == 0:
            return None

        norm_baseline = {k: v / total for k, v in baseline_dist.items()}
        divergence = self._kl_divergence(current_dist, norm_baseline)
        anomaly = divergence > self._threshold

        if anomaly:
            cap.add("phase_anomaly")

        return DriftResult(
            current_phase=current_phase,
            baseline_distribution=norm_baseline,
            current_distribution=current_dist,
            divergence=divergence,
            anomaly=anomaly,
        )

    def _get_baseline(self, ctx: "EnrichmentContext") -> dict | None:
        """Fetch drift baseline from store."""
        model = getattr(ctx.event, "agent_model", None) or "unknown"
        repo = ctx.project_root or "unknown"
        return self._store.get_drift_baseline(model, repo)

    def _compute_distribution(self, phases: tuple[str, ...] | list[str]) -> dict[str, float]:
        """Compute normalized phase distribution."""
        counts: dict[str, int] = {}
        for p in phases:
            counts[p] = counts.get(p, 0) + 1
        total = len(phases)
        return {k: v / total for k, v in counts.items()}

    def _kl_divergence(self, p: dict[str, float], q: dict[str, float]) -> float:
        """Simplified symmetric KL divergence (Jensen-Shannon style)."""
        import math
        all_keys = set(p.keys()) | set(q.keys())
        eps = 1e-10
        divergence = 0.0
        for k in all_keys:
            pk = p.get(k, eps)
            qk = q.get(k, eps)
            m = (pk + qk) / 2
            if pk > eps:
                divergence += pk * math.log(pk / m)
            if qk > eps:
                divergence += qk * math.log(qk / m)
        return divergence / 2  # Symmetric — bounded [0, ln(2)]
