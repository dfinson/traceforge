"""Tests for governance persistence layer."""

import pytest

from tracemill.governance.persistence import SystemStore


@pytest.fixture
def store(tmp_path):
    db_path = tmp_path / "test_system.db"
    s = SystemStore(db_path)
    yield s
    s.close()


class TestSystemStore:
    def test_creates_db_and_tables(self, store):
        tables = store.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = {t[0] for t in tables}
        assert "session_state" in table_names
        assert "processed_events" in table_names
        assert "mcp_fingerprints" in table_names
        assert "drift_baselines" in table_names
        assert "content_hashes" in table_names
        assert "session_summaries" in table_names

    def test_is_duplicate_returns_none_for_new(self, store):
        assert store.is_duplicate("key1") is None

    def test_record_and_check_duplicate(self, store):
        store.record_processed("key1", "sess1", '{"test": true}', "2024-01-01T00:00:00Z")
        result = store.is_duplicate("key1")
        assert result == '{"test": true}'

    def test_duplicate_check_cached(self, store):
        store.record_processed("key2", "sess1", '{"cached": 1}', "2024-01-01T00:00:00Z")
        # Second call should use cache
        assert store.is_duplicate("key2") == '{"cached": 1}'

    def test_mcp_profile_upsert_and_get(self, store):
        profile = {
            "description_hash": "abc123",
            "schema_hash": "def456",
            "registered_effect": "mutating",
            "registered_role": None,
            "registered_capabilities": None,
            "registered_scope": None,
            "clearance": "internal",
            "first_seen": "2024-01-01T00:00:00Z",
            "last_seen": "2024-01-01T00:00:00Z",
        }
        store.upsert_mcp_profile("server1", "tool1", profile)
        result = store.get_mcp_profile("server1", "tool1")
        assert result is not None
        assert result["description_hash"] == "abc123"
        assert result["clearance"] == "internal"

    def test_mcp_profile_not_found(self, store):
        assert store.get_mcp_profile("none", "none") is None

    def test_content_hash_store_and_get(self, store):
        store.store_content_hash("myrepo", "file.py", "sha256abc", "sess1", "2024-01-01T00:00:00Z")
        assert store.get_content_hash("myrepo", "file.py") == "sha256abc"

    def test_content_hash_not_found(self, store):
        assert store.get_content_hash("repo", "missing.py") is None

    def test_drift_baseline_not_found(self, store):
        assert store.get_drift_baseline("gpt-4", "myrepo") is None

    def test_cache_eviction(self, store):
        # Fill cache beyond limit
        for i in range(11_000):
            store._processed_cache[f"key_{i}"] = f"val_{i}"
        store._evict_cache()
        assert len(store._processed_cache) <= 10_000
