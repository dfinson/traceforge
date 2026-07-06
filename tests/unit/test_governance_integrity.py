"""Tests for content-integrity verification wiring (issue #14).

`IntegrityVerifier.record_write` was orphaned: nothing populated the `content_hashes`
baseline, so `check()` always returned None and `integrity_unverified` (and its
`integrity_bonus`) were unreachable. These tests pin the wired behaviour:

* the CHECK runs during side-effect-free labeling (against the *prior* baseline), and
* the RECORD is a deferred write committed by the monitor's finalization transaction,

which yields check-before-record ordering (no baseline-laundering), cross-session
writer attribution, and — critically — a preflight simulation that never records.
"""

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tracemill.classify.config import get_default_engine
from tracemill.classify.core import Classification
from tracemill.cli.factory import create_default_pipeline
from tracemill.config import GovernanceConfig
from tracemill.governance.budget import BudgetTracker
from tracemill.governance.integrity import IntegrityVerifier, IntegrityWrite
from tracemill.governance.labeler import GovernanceLabeler
from tracemill.governance.persistence import SystemStore
from tracemill.governance.pipeline import GovernancePipeline
from tracemill.governance.rules import parse_rules
from tracemill.governance.types import EnrichmentContext, ToolCallEvent

REPO = "acme/widgets"
_TS = "2024-01-01T00:00:00+00:00"


@pytest.fixture
def store(tmp_path):
    s = SystemStore(tmp_path / "integrity.db")
    yield s
    s.close()


@pytest.fixture
def rules():
    rules_path = (
        Path(__file__).parent.parent.parent
        / "src"
        / "tracemill"
        / "classify"
        / "data"
        / "recommendation_rules.yaml"
    )
    return parse_rules(rules_path)


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _write_event(path, content, session_id="sess1", event_id="evt-1", key="key-1"):
    """A ToolCallEvent that writes ``content`` to ``path`` (path+content arg shape)."""
    return ToolCallEvent(
        event_id=event_id,
        session_id=session_id,
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
        source_event_key=key,
        span_id="span-1",
        tool_name="write_file",
        server_namespace=None,
        tool_args_json=json.dumps({"path": path, "content": content}),
        source_event_id=None,
    )


def _ctx(event, effect="mutating"):
    classification = Classification(mechanism="filesystem.write", effect=effect)
    return EnrichmentContext(
        event=event,
        base_classification=classification,
        command_analysis=None,
        session_state=None,
        mcp_profiles=None,
        project_root=None,
        engine="coding",
        drift_baseline=None,
        mcp_profile_key=None,
    )


# ─────────────────────────────────────────────────────────────────────────────
# IntegrityVerifier: check-before-record, matching, mismatch, attribution
# ─────────────────────────────────────────────────────────────────────────────


class TestVerifier:
    def test_first_seen_write_not_flagged_and_yields_prescription(self, store):
        verifier = IntegrityVerifier(store, REPO)
        ctx = _ctx(_write_event("src/a.py", "hello"))

        cap: set[str] = set()
        verifier.check_event(ctx, cap)
        assert "integrity_unverified" not in cap  # untracked path — nothing to compare

        writes = verifier.pending_writes(ctx)
        assert writes == [
            IntegrityWrite(
                repo=REPO,
                path="src/a.py",
                sha256=_sha(b"hello"),
                session_id="sess1",
                timestamp="2024-01-01T00:00:00+00:00",
            )
        ]

    def test_matching_rewrite_is_not_flagged(self, store):
        verifier = IntegrityVerifier(store, REPO)
        verifier.record_write("src/a.py", b"hello", "sess1", _TS)

        cap: set[str] = set()
        verifier.check_event(_ctx(_write_event("src/a.py", "hello")), cap)
        assert "integrity_unverified" not in cap

        check = verifier.check("src/a.py", b"hello")
        assert check is not None and check.matched is True

    def test_mismatched_content_is_flagged(self, store):
        verifier = IntegrityVerifier(store, REPO)
        verifier.record_write("src/a.py", b"hello", "sess1", _TS)

        cap: set[str] = set()
        verifier.check_event(_ctx(_write_event("src/a.py", "tampered")), cap)
        assert "integrity_unverified" in cap

    def test_should_check_gates_pending_writes(self, store):
        verifier = IntegrityVerifier(store, REPO)
        # read_only effect and no filesystem_write capability → not integrity-relevant
        assert (
            verifier.pending_writes(_ctx(_write_event("src/a.py", "x"), effect="read_only")) == []
        )

    def test_cross_session_attribution(self, store):
        """A write by session B on a path baselined by session A is detectable, and
        `last_known_writer` reflects the *prior* writer until B's record commits."""
        writer_a = IntegrityVerifier(store, REPO)
        writer_a.record_write("src/a.py", b"content-A", "session-A", _TS)

        writer_b = IntegrityVerifier(store, REPO)
        drift = writer_b.check("src/a.py", b"content-B")
        assert drift is not None
        assert drift.matched is False
        assert drift.last_known_writer == "session-A"  # attribution to prior writer

        # After session B's write commits, the baseline is re-attributed to B.
        writer_b.record_write("src/a.py", b"content-B", "session-B", _TS)
        row = store.connection.execute(
            "SELECT updated_by_session FROM content_hashes WHERE repo = ? AND file_path = ?",
            (REPO, "src/a.py"),
        ).fetchone()
        assert row[0] == "session-B"


# ─────────────────────────────────────────────────────────────────────────────
# Labeler: integrity_bonus surfaces on drift
# ─────────────────────────────────────────────────────────────────────────────


class TestLabelerBonus:
    def test_mismatch_surfaces_integrity_bonus(self, store):
        verifier = IntegrityVerifier(store, REPO)
        verifier.record_write("src/a.py", b"original", "sess1", _TS)
        labeler = GovernanceLabeler(integrity_verifier=verifier)

        result = labeler.label(_ctx(_write_event("src/a.py", "tampered")))

        assert "integrity_unverified" in result.classification.capability
        assert result.risk_modifiers.integrity_bonus == 10
        # ... and the drift event still re-baselines (deferred) to what was written.
        assert len(result.integrity_deferred_writes) == 1
        assert result.integrity_deferred_writes[0].sha256 == _sha(b"tampered")

    def test_matching_write_has_no_bonus(self, store):
        verifier = IntegrityVerifier(store, REPO)
        verifier.record_write("src/a.py", b"same", "sess1", _TS)
        labeler = GovernanceLabeler(integrity_verifier=verifier)

        result = labeler.label(_ctx(_write_event("src/a.py", "same")))

        assert "integrity_unverified" not in result.classification.capability
        assert result.risk_modifiers.integrity_bonus == 0

    def test_first_seen_has_no_bonus_but_defers_record(self, store):
        verifier = IntegrityVerifier(store, REPO)
        labeler = GovernanceLabeler(integrity_verifier=verifier)

        result = labeler.label(_ctx(_write_event("src/new.py", "brand-new")))

        assert "integrity_unverified" not in result.classification.capability
        assert result.risk_modifiers.integrity_bonus == 0
        assert len(result.integrity_deferred_writes) == 1


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end through the mutating pipeline vs. read-only preflight
# ─────────────────────────────────────────────────────────────────────────────


def _pipeline(store, rules):
    verifier = IntegrityVerifier(store, REPO)
    labeler = GovernanceLabeler(integrity_verifier=verifier)
    return GovernancePipeline(
        store=store,
        labeler=labeler,
        budget_tracker=BudgetTracker(),
        rules=rules,
        engine=get_default_engine(),
    )


class TestEndToEnd:
    def test_process_event_records_baseline_then_detects_drift(self, store, rules):
        pipeline = _pipeline(store, rules)

        # First write establishes the baseline (proves record_write is wired live).
        pipeline.process_event(
            _ctx(_write_event("src/a.py", "v1", session_id="A", event_id="e1", key="k1"))
        )
        assert store.get_content_hash(REPO, "src/a.py") == _sha(b"v1")

        # A later, different-session write with different content drifts from the baseline.
        meta2 = pipeline.process_event(
            _ctx(_write_event("src/a.py", "v2", session_id="B", event_id="e2", key="k2"))
        )
        assert "integrity_unverified" in meta2.classification.capability
        # ... and the baseline is re-recorded to what was actually written.
        assert store.get_content_hash(REPO, "src/a.py") == _sha(b"v2")

    def test_preflight_does_not_launder_baseline(self, store, rules):
        """The read-only preflight/simulation path must never populate the baseline."""
        pipeline = _pipeline(store, rules)

        pipeline.preflight_event(
            _ctx(_write_event("src/a.py", "speculative", event_id="e1", key="k1"))
        )

        assert store.get_content_hash(REPO, "src/a.py") is None


# ─────────────────────────────────────────────────────────────────────────────
# Default composition roots: integrity is live by default with NO manual injection
# ─────────────────────────────────────────────────────────────────────────────


class TestDefaultInjection:
    """Proves the orphan is actually closed in production: pipelines built the standard
    way — with NO hand-injected verifier — record the baseline on a first write and flag
    a mismatched re-write with the +10 integrity bonus. Integrity is on by default at
    both composition roots (`create_default_pipeline` and `GovernancePipeline.create`/
    `from_config`) and opt-out via `integrity_verification: false`. `GovernancePipeline.
    create`/`from_config` fall back to a repo key of ``"unknown"`` (matching drift.py)
    when no project_root is configured, whereas the CLI factory `create_default_pipeline`
    defaults the key to the resolved cwd so per-repo baselines in the shared persistent
    store never collide."""

    def test_default_factory_wires_integrity_live(self, store):
        # cli/factory.py path (CLI + Score API). No manual IntegrityVerifier.
        pipeline = create_default_pipeline(store, project_root=REPO)

        # First-seen write: nothing to compare against → no flag, no bonus …
        meta0 = pipeline.process_event(
            _ctx(_write_event("src/a.py", "v1", session_id="A", event_id="e0", key="k0"))
        )
        assert "integrity_unverified" not in meta0.classification.capability
        assert "integrity_unverified" not in meta0.risk_assessment.factors
        # … but the baseline is recorded live (record path is actually reached).
        assert store.get_content_hash(REPO, "src/a.py") == _sha(b"v1")

        # Mismatched re-write on the tracked path → integrity_unverified surfaces,
        # raising the final risk by exactly the +10 integrity bonus.
        meta_drift = pipeline.process_event(
            _ctx(_write_event("src/a.py", "v2", session_id="B", event_id="e1", key="k1"))
        )
        assert "integrity_unverified" in meta_drift.classification.capability
        assert "integrity_unverified" in meta_drift.risk_assessment.factors
        assert meta_drift.risk_assessment.score - meta0.risk_assessment.score == 10
        # Drift re-baselines (deferred) to what was actually written.
        assert store.get_content_hash(REPO, "src/a.py") == _sha(b"v2")

    def test_cli_factory_keys_baseline_by_cwd_not_unknown(self, store, monkeypatch, tmp_path):
        """The CLI builds its pipeline with NO explicit project_root — exactly
        ``create_default_pipeline(store)`` as in watch/score/replay — against the shared,
        persistent ``~/.tracemill/system.db``. That store used to namespace every repo's
        per-file baseline under the literal ``"unknown"``, so two different repositories
        both writing the relative path ``src/main.py`` collided and produced false
        integrity signals. The factory now defaults the repo key to the resolved cwd, so
        each repository gets its own, non-colliding namespace."""
        repo_a = tmp_path / "repo_a"
        repo_b = tmp_path / "repo_b"
        repo_a.mkdir()
        repo_b.mkdir()

        # ── Repo A: run the CLI-style factory from inside repo_a, no project_root ──
        monkeypatch.chdir(repo_a)
        key_a = os.path.abspath(os.getcwd())
        pipeline_a = create_default_pipeline(store)
        pipeline_a.process_event(
            _ctx(_write_event("src/main.py", "content-A", session_id="A", event_id="a1", key="ka"))
        )

        # ── Repo B: SAME relative path, DIFFERENT content, from inside repo_b ──
        monkeypatch.chdir(repo_b)
        key_b = os.path.abspath(os.getcwd())
        pipeline_b = create_default_pipeline(store)
        pipeline_b.process_event(
            _ctx(_write_event("src/main.py", "content-B", session_id="B", event_id="b1", key="kb"))
        )

        assert key_a != key_b  # distinct repos → distinct keys, no "unknown" collision
        assert "unknown" not in (key_a, key_b)

        # Each repo's baseline is namespaced by its own cwd (its real identity) …
        assert store.get_content_hash(key_a, "src/main.py") == _sha(b"content-A")
        assert store.get_content_hash(key_b, "src/main.py") == _sha(b"content-B")
        # … and nothing landed in the colliding "unknown" bucket that caused the bug.
        assert store.get_content_hash("unknown", "src/main.py") is None

    def test_zero_config_pipeline_wires_integrity_live(self):
        # GovernancePipeline.create() with default config (the from_config root, which
        # delegates here). No project_root → repo key "unknown"; still live by default.
        pipeline = GovernancePipeline.create()

        meta0 = pipeline.process_event(
            _ctx(_write_event("src/a.py", "v1", session_id="A", event_id="e0", key="k0"))
        )
        assert "integrity_unverified" not in meta0.classification.capability

        meta_drift = pipeline.process_event(
            _ctx(_write_event("src/a.py", "v2", session_id="B", event_id="e1", key="k1"))
        )
        assert "integrity_unverified" in meta_drift.classification.capability
        assert "integrity_unverified" in meta_drift.risk_assessment.factors
        assert meta_drift.risk_assessment.score - meta0.risk_assessment.score == 10

    def test_integrity_verification_disabled_is_noop(self):
        # Opt-out: integrity_verification=False → no verifier → no labels even on drift.
        pipeline = GovernancePipeline.create(GovernanceConfig(integrity_verification=False))

        pipeline.process_event(
            _ctx(_write_event("src/a.py", "v1", session_id="A", event_id="e0", key="k0"))
        )
        meta_drift = pipeline.process_event(
            _ctx(_write_event("src/a.py", "v2", session_id="B", event_id="e1", key="k1"))
        )
        assert "integrity_unverified" not in meta_drift.classification.capability
        assert "integrity_unverified" not in meta_drift.risk_assessment.factors
