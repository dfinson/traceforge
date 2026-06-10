"""Phase-aware behavioral drift detection with transition-specific bonuses."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tracemill.governance.persistence import SystemStore
    from tracemill.governance.state import SessionStateSnapshot
    from tracemill.governance.types import EnrichmentContext


# Suspicious transition bonuses (from → to: bonus)
_TRANSITION_BONUSES: dict[tuple[str, str], int] = {
    ("testing", "implementation"): 10,     # Verification → destructive implementation
    ("verification", "implementation"): 10,
    ("testing", "destructive"): 18,        # Verification → destructive
    ("verification", "destructive"): 18,
    ("exploration", "network"): 15,        # Exploration → network write
    ("exploration", "deployment"): 12,     # Exploration → deployment (skipping impl)
}

# Oscillation threshold
_OSCILLATION_THRESHOLD = 5  # >5 transitions in window = +20 bonus
_OSCILLATION_BONUS = 20
_WARMUP_EVENTS = 5  # Minimum events before drift detection activates


@dataclass(frozen=True)
class DriftAssessment:
    """Full drift assessment per the design spec."""
    phase_window: tuple[str, ...]
    baseline_distribution: tuple[tuple[str, float], ...]  # Sorted pairs (immutable)
    current_phase: str
    anomaly_score: float  # 0.0–1.0 deviation from baseline
    risk_bonus: int  # 0–25 pts added to risk
    transitions: int  # Phase transitions in window
    anomaly: bool  # True if risk_bonus > 0


class DriftDetector:
    """Stateless drift detector. Phase window from session state, baseline from store."""

    def __init__(self, store: "SystemStore", window_size: int = 20, threshold: float = 0.3) -> None:
        self._store = store
        self._window_size = window_size
        self._threshold = threshold

    def detect(
        self,
        ctx: "EnrichmentContext",
        state_snapshot: "SessionStateSnapshot",
        cap: set[str],
    ) -> DriftAssessment | None:
        """Full drift detection: divergence + transition bonuses + oscillation."""
        phase_window = state_snapshot.phase_window
        if len(phase_window) < _WARMUP_EVENTS:
            return None

        current_phase = phase_window[-1] if phase_window else "unknown"

        # Count transitions in window
        transitions = self._count_transitions(phase_window)

        # Compute distribution divergence
        current_dist = self._compute_distribution(phase_window)
        baseline = self._get_baseline(ctx)

        divergence = 0.0
        baseline_tuples: tuple[tuple[str, float], ...] = ()

        if baseline:
            baseline_dist = baseline["phase_counts"]
            total = baseline["total_events"]
            if total > 0:
                norm_baseline = {k: v / total for k, v in baseline_dist.items()}
                baseline_tuples = tuple(sorted(norm_baseline.items()))
                divergence = self._js_divergence(current_dist, norm_baseline)

        # Compute risk bonus from multiple sources
        risk_bonus = 0

        # 1. Transition-specific bonuses
        if len(phase_window) >= 2:
            prev_phase = phase_window[-2]
            transition_key = (prev_phase, current_phase)
            risk_bonus += _TRANSITION_BONUSES.get(transition_key, 0)

        # 2. Oscillation bonus
        if transitions > _OSCILLATION_THRESHOLD:
            risk_bonus += _OSCILLATION_BONUS

        # 3. Divergence-based bonus (scale anomaly_score to 0-15 pts)
        if divergence > self._threshold:
            risk_bonus += min(int(divergence * 25), 15)

        # Cap total at 25
        risk_bonus = min(risk_bonus, 25)

        anomaly = risk_bonus > 0

        if anomaly:
            cap.add("phase_anomaly")

        return DriftAssessment(
            phase_window=phase_window,
            baseline_distribution=baseline_tuples,
            current_phase=current_phase,
            anomaly_score=divergence,
            risk_bonus=risk_bonus,
            transitions=transitions,
            anomaly=anomaly,
        )

    def check_drift(
        self,
        phase_window: tuple[str, ...],
        current_phase: str,
        baseline: tuple[tuple[str, float], ...] | None,
    ) -> DriftAssessment | None:
        """Pure function version — receives pre-loaded baseline."""
        if len(phase_window) < _WARMUP_EVENTS:
            return None

        transitions = self._count_transitions(phase_window)
        current_dist = self._compute_distribution(phase_window)

        divergence = 0.0
        if baseline:
            norm_baseline = dict(baseline)
            divergence = self._js_divergence(current_dist, norm_baseline)

        risk_bonus = 0
        if len(phase_window) >= 2:
            prev_phase = phase_window[-2]
            risk_bonus += _TRANSITION_BONUSES.get((prev_phase, current_phase), 0)
        if transitions > _OSCILLATION_THRESHOLD:
            risk_bonus += _OSCILLATION_BONUS
        if divergence > self._threshold:
            risk_bonus += min(int(divergence * 25), 15)
        risk_bonus = min(risk_bonus, 25)

        return DriftAssessment(
            phase_window=phase_window,
            baseline_distribution=baseline or (),
            current_phase=current_phase,
            anomaly_score=divergence,
            risk_bonus=risk_bonus,
            transitions=transitions,
            anomaly=risk_bonus > 0,
        )

    def _get_baseline(self, ctx: "EnrichmentContext") -> dict | None:
        # Use pre-loaded baseline from context when available
        if ctx.drift_baseline:
            return {"phase_counts": dict(ctx.drift_baseline), "total_events": sum(v for _, v in ctx.drift_baseline)}
        repo = ctx.project_root or "unknown"
        return self._store.get_drift_baseline("unknown", repo)

    def _count_transitions(self, phases: tuple[str, ...]) -> int:
        """Count phase transitions (consecutive different phases) in window."""
        count = 0
        for i in range(1, len(phases)):
            if phases[i] != phases[i - 1]:
                count += 1
        return count

    def _compute_distribution(self, phases: tuple[str, ...] | list[str]) -> dict[str, float]:
        counts: dict[str, int] = {}
        for p in phases:
            counts[p] = counts.get(p, 0) + 1
        total = len(phases)
        return {k: v / total for k, v in counts.items()}

    def _js_divergence(self, p: dict[str, float], q: dict[str, float]) -> float:
        """Jensen-Shannon divergence (symmetric, bounded [0, ln(2)])."""
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
        return divergence / 2
