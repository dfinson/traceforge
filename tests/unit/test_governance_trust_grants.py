"""Deterministic tests for the trust-grant primitive (U10).

Covers the general, consumer-agnostic mechanism:
  * ``TrustGrant`` TTL semantics (half-open activeness, inert non-positive TTL).
  * ``TrustGrantStore`` query/prune behaviour.
  * ``active_grant_keys`` / ``waive_by_grants`` overlay helpers.
  * Durable persistence round-trip through ``SessionState`` (grant honored, then
    expires by TTL → escalation no longer waived).

Time is always supplied explicitly — no wall clock is read — so TTL expiry is
fully deterministic.
"""

from datetime import datetime, timedelta, timezone

from traceforge.governance.persistence import SystemStore
from traceforge.governance.results import RecommendedAction
from traceforge.governance.rules import (
    PolicyDecision,
    active_grant_keys,
    waive_by_grants,
)
from traceforge.governance.state import SessionState, TrustGrantStore
from traceforge.governance.types import TrustGrant

T0 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


# ─── TrustGrant TTL semantics ────────────────────────────────────────────────


class TestTrustGrantActiveness:
    def test_active_at_grant_instant(self):
        g = TrustGrant(key="k", granted_at=T0, ttl_seconds=60)
        assert g.is_active(T0) is True

    def test_active_within_window(self):
        g = TrustGrant(key="k", granted_at=T0, ttl_seconds=60)
        assert g.is_active(T0 + timedelta(seconds=59)) is True

    def test_inactive_before_grant(self):
        g = TrustGrant(key="k", granted_at=T0, ttl_seconds=60)
        assert g.is_active(T0 - timedelta(seconds=1)) is False

    def test_expiry_is_half_open(self):
        # Exactly at expires_at the grant is NO longer active.
        g = TrustGrant(key="k", granted_at=T0, ttl_seconds=60)
        assert g.expires_at == T0 + timedelta(seconds=60)
        assert g.is_active(g.expires_at) is False

    def test_inactive_after_expiry(self):
        g = TrustGrant(key="k", granted_at=T0, ttl_seconds=60)
        assert g.is_active(T0 + timedelta(seconds=61)) is False

    def test_zero_ttl_is_inert(self):
        g = TrustGrant(key="k", granted_at=T0, ttl_seconds=0)
        assert g.expires_at == T0
        assert g.is_active(T0) is False

    def test_negative_ttl_is_inert(self):
        g = TrustGrant(key="k", granted_at=T0, ttl_seconds=-5)
        assert g.expires_at == T0
        assert g.is_active(T0) is False

    def test_reason_defaults_empty(self):
        g = TrustGrant(key="k", granted_at=T0, ttl_seconds=1)
        assert g.reason == ""


# ─── TrustGrantStore ─────────────────────────────────────────────────────────


class TestTrustGrantStore:
    def test_empty_store(self):
        store = TrustGrantStore()
        assert len(store) == 0
        assert store.active(T0) == ()
        assert store.active_keys(T0) == frozenset()
        assert store.has_active("k", T0) is False
        assert store.all_grants() == ()

    def test_add_and_query_active(self):
        store = TrustGrantStore()
        store.add(TrustGrant(key="a", granted_at=T0, ttl_seconds=60))
        store.add(TrustGrant(key="b", granted_at=T0, ttl_seconds=10))
        assert len(store) == 2
        # Both active at T0
        assert store.active_keys(T0) == frozenset({"a", "b"})
        # Only 'a' active after b expires
        assert store.active_keys(T0 + timedelta(seconds=30)) == frozenset({"a"})
        # Neither active after both expire
        assert store.active_keys(T0 + timedelta(seconds=120)) == frozenset()

    def test_has_active(self):
        store = TrustGrantStore()
        store.add(TrustGrant(key="a", granted_at=T0, ttl_seconds=60))
        assert store.has_active("a", T0) is True
        assert store.has_active("a", T0 + timedelta(seconds=120)) is False
        assert store.has_active("missing", T0) is False

    def test_all_grants_preserves_insert_order(self):
        store = TrustGrantStore()
        g1 = TrustGrant(key="a", granted_at=T0, ttl_seconds=1)
        g2 = TrustGrant(key="b", granted_at=T0, ttl_seconds=1)
        store.add(g1)
        store.add(g2)
        assert store.all_grants() == (g1, g2)

    def test_prune_drops_only_permanently_expired(self):
        store = TrustGrantStore()
        store.add(TrustGrant(key="expired", granted_at=T0, ttl_seconds=10))
        future = TrustGrant(key="future", granted_at=T0 + timedelta(hours=1), ttl_seconds=10)
        store.add(future)
        removed = store.prune(T0 + timedelta(seconds=30))
        assert removed == 1
        # The not-yet-active future grant is retained.
        assert store.all_grants() == (future,)

    def test_prune_retains_active(self):
        store = TrustGrantStore()
        store.add(TrustGrant(key="a", granted_at=T0, ttl_seconds=600))
        removed = store.prune(T0 + timedelta(seconds=30))
        assert removed == 0
        assert len(store) == 1


# ─── active_grant_keys / waive_by_grants overlay helpers ─────────────────────


class TestActiveGrantKeys:
    def test_collects_active_only(self):
        grants = [
            TrustGrant(key="live", granted_at=T0, ttl_seconds=100),
            TrustGrant(key="dead", granted_at=T0, ttl_seconds=1),
        ]
        assert active_grant_keys(grants, T0 + timedelta(seconds=50)) == frozenset({"live"})

    def test_empty_iterable(self):
        assert active_grant_keys([], T0) == frozenset()

    def test_naive_vs_aware_mismatch_is_conservative(self):
        # A naive grant timestamp compared with an aware ``now`` raises TypeError
        # internally; the grant simply does not count as active (safe default,
        # since grants only ever waive an escalation).
        naive = TrustGrant(key="k", granted_at=T0.replace(tzinfo=None), ttl_seconds=100)
        assert active_grant_keys([naive], T0 + timedelta(seconds=1)) == frozenset()


class TestWaiveByGrants:
    def test_waives_escalate_when_key_matches(self):
        d = PolicyDecision(action=RecommendedAction.ESCALATE, reason_code="protected_path")
        assert waive_by_grants(d, frozenset({"protected_path"})) is None

    def test_waives_deny_when_key_matches(self):
        d = PolicyDecision(action=RecommendedAction.DENY, reason_code="cost_ceiling")
        assert waive_by_grants(d, frozenset({"cost_ceiling"})) is None

    def test_does_not_waive_unmatched_reason(self):
        d = PolicyDecision(action=RecommendedAction.ESCALATE, reason_code="protected_path")
        assert waive_by_grants(d, frozenset({"other"})) == d

    def test_does_not_waive_warn(self):
        d = PolicyDecision(action=RecommendedAction.WARN, reason_code="k")
        assert waive_by_grants(d, frozenset({"k"})) == d

    def test_does_not_waive_allow(self):
        d = PolicyDecision(action=RecommendedAction.ALLOW, reason_code="k")
        assert waive_by_grants(d, frozenset({"k"})) == d

    def test_none_decision_passes_through(self):
        assert waive_by_grants(None, frozenset({"k"})) is None


# ─── Durable persistence round-trip (grant honored → expires → denied) ───────


class TestGrantPersistence:
    def _store(self, tmp_path):
        return SystemStore(tmp_path / "grants.db")

    def test_grant_round_trips_through_db(self, tmp_path):
        store = self._store(tmp_path)
        try:
            s1 = SessionState(session_id="sess")
            s1.attach_db(store.connection)
            s1.trust_grants.add(TrustGrant(key="protected_path", granted_at=T0, ttl_seconds=3600))
            s1.persist()

            s2 = SessionState.load_from_db("sess", store.connection)
            grants = s2.trust_grants.all_grants()
            assert len(grants) == 1
            assert grants[0].key == "protected_path"
            assert grants[0].granted_at == T0
            assert grants[0].ttl_seconds == 3600.0
        finally:
            store.close()

    def test_grant_honored_then_expires_by_ttl(self, tmp_path):
        """The same persisted grant is active early, inert after its TTL."""
        store = self._store(tmp_path)
        try:
            s1 = SessionState(session_id="sess")
            s1.attach_db(store.connection)
            s1.trust_grants.add(TrustGrant(key="protected_path", granted_at=T0, ttl_seconds=60))
            s1.persist()

            s2 = SessionState.load_from_db("sess", store.connection)
            snap = s2.snapshot()

            # Honored: within TTL the escalation for 'protected_path' is waived.
            keys_early = active_grant_keys(snap.trust_grants, T0 + timedelta(seconds=30))
            decision = PolicyDecision(
                action=RecommendedAction.ESCALATE, reason_code="protected_path"
            )
            assert waive_by_grants(decision, keys_early) is None

            # Expired: past the TTL the same escalation stands.
            keys_late = active_grant_keys(snap.trust_grants, T0 + timedelta(seconds=61))
            assert waive_by_grants(decision, keys_late) == decision
        finally:
            store.close()

    def test_snapshot_defaults_to_no_grants(self, tmp_path):
        store = self._store(tmp_path)
        try:
            s = SessionState.load_from_db("never-granted", store.connection)
            assert s.snapshot().trust_grants == ()
        finally:
            store.close()
