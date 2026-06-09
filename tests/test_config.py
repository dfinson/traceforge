"""Tests for tracemill.config — models, loader, and mapping resolver."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from tracemill.config import (
    FileWatchSourceConfig,
    FilePollSourceConfig,
    HttpPollSourceConfig,
    JsonlSinkConfig,
    MappedJsonAdapterConfig,
    OtelSpanAdapterConfig,
    PipelineConfig,
    ReplaySourceConfig,
    S3SinkConfig,
    SDKConfig,
    SSESourceConfig,
    SqliteSinkConfig,
    TracemillConfig,
    get_config,
    list_available_mappings,
    load_config,
    reset_config,
    resolve_mapping_path,
)


# ─── Model Tests ─────────────────────────────────────────────────────────────


class TestSourceConfigs:
    def test_file_watch_defaults(self):
        cfg = FileWatchSourceConfig(path="/tmp/test.jsonl")
        assert cfg.type == "file_watch"
        assert cfg.start_at == "end"
        assert cfg.encoding == "utf-8"

    def test_file_poll_positive_interval(self):
        with pytest.raises(ValidationError):
            FilePollSourceConfig(path="/tmp/test.jsonl", interval=-1.0)

    def test_http_poll_defaults(self):
        cfg = HttpPollSourceConfig(url="http://localhost:8080/events")
        assert cfg.interval == 30.0
        assert cfg.headers == {}
        assert cfg.max_retries == 3

    def test_http_poll_negative_interval(self):
        with pytest.raises(ValidationError):
            HttpPollSourceConfig(url="http://x", interval=0)

    def test_sse_defaults(self):
        cfg = SSESourceConfig(url="http://localhost:8080/sse")
        assert cfg.reconnect_interval == 3.0

    def test_replay_config(self):
        cfg = ReplaySourceConfig(path="/tmp/capture.jsonl")
        assert cfg.type == "replay"


class TestAdapterConfigs:
    def test_mapped_json_by_name(self):
        cfg = MappedJsonAdapterConfig(mapping="copilot")
        assert cfg.type == "mapped_json"
        assert cfg.mapping_file is None

    def test_mapped_json_by_file(self):
        cfg = MappedJsonAdapterConfig(mapping="custom", mapping_file=Path("/etc/custom.yaml"))
        assert cfg.mapping_file == Path("/etc/custom.yaml")

    def test_otel_span(self):
        cfg = OtelSpanAdapterConfig()
        assert cfg.type == "otel_span"


class TestSinkConfigs:
    def test_sqlite_defaults(self):
        cfg = SqliteSinkConfig(path="/tmp/traces.db")
        assert cfg.journal_mode == "wal"

    def test_jsonl_defaults(self):
        cfg = JsonlSinkConfig(path="/tmp/events.jsonl")
        assert cfg.rotate_size_mb is None

    def test_s3_config(self):
        cfg = S3SinkConfig(bucket="my-bucket", prefix="traces/", region="us-east-1")
        assert cfg.type == "s3"


class TestSDKConfig:
    def test_defaults(self):
        cfg = SDKConfig()
        assert cfg.batch_size == 64
        assert cfg.flush_interval == 5.0
        assert cfg.max_queue_size == 10000

    def test_invalid_batch_size(self):
        with pytest.raises(ValidationError):
            SDKConfig(batch_size=0)


class TestPipelineConfig:
    def test_valid_pipeline(self):
        cfg = PipelineConfig(
            name="test",
            source=FileWatchSourceConfig(path="/tmp/a.jsonl"),
            adapter=MappedJsonAdapterConfig(mapping="copilot"),
            sinks=[SqliteSinkConfig(path="/tmp/db")],
        )
        assert cfg.name == "test"

    def test_empty_name_rejected(self):
        with pytest.raises(ValidationError):
            PipelineConfig(
                name="   ",
                source=FileWatchSourceConfig(path="/tmp/a.jsonl"),
                adapter=MappedJsonAdapterConfig(mapping="x"),
                sinks=[SqliteSinkConfig(path="/tmp/db")],
            )

    def test_empty_sinks_rejected(self):
        with pytest.raises(ValidationError):
            PipelineConfig(
                name="test",
                source=FileWatchSourceConfig(path="/tmp/a.jsonl"),
                adapter=MappedJsonAdapterConfig(mapping="x"),
                sinks=[],
            )

    def test_discriminated_source_types(self):
        """Source type is correctly discriminated from dict."""
        data = {
            "name": "test",
            "source": {"type": "sse", "url": "http://x"},
            "adapter": {"type": "otel_span"},
            "sinks": [{"type": "jsonl", "path": "/tmp/out.jsonl"}],
        }
        cfg = PipelineConfig.model_validate(data)
        assert isinstance(cfg.source, SSESourceConfig)
        assert isinstance(cfg.adapter, OtelSpanAdapterConfig)
        assert isinstance(cfg.sinks[0], JsonlSinkConfig)


class TestTracemillConfig:
    def test_defaults(self):
        cfg = TracemillConfig()
        assert cfg.log_level == "INFO"
        assert cfg.pipelines == []
        assert cfg.sdk.batch_size == 64

    def test_duplicate_pipeline_names_rejected(self):
        pipeline = PipelineConfig(
            name="dupe",
            source=FileWatchSourceConfig(path="/a"),
            adapter=MappedJsonAdapterConfig(mapping="x"),
            sinks=[SqliteSinkConfig(path="/b")],
        )
        with pytest.raises(ValidationError, match="duplicate pipeline names"):
            TracemillConfig(pipelines=[pipeline, pipeline])

    def test_extra_fields_rejected(self):
        with pytest.raises(ValidationError):
            TracemillConfig.model_validate({"unknown_field": "value"})


# ─── Loader Tests ────────────────────────────────────────────────────────────


class TestLoader:
    def setup_method(self):
        reset_config()

    def teardown_method(self):
        reset_config()

    def test_load_defaults(self):
        cfg = load_config()
        assert cfg.log_level == "INFO"
        assert isinstance(cfg, TracemillConfig)

    def test_explicit_overrides(self):
        cfg = load_config(log_level="DEBUG")
        assert cfg.log_level == "DEBUG"

    def test_get_config_singleton(self):
        cfg1 = get_config()
        cfg2 = get_config()
        assert cfg1 is cfg2

    def test_load_from_yaml_file(self, tmp_path):
        config_file = tmp_path / "test-config.yaml"
        config_file.write_text(yaml.dump({"log_level": "WARNING", "sdk": {"batch_size": 128}}))
        os.environ["TRACEMILL_CONFIG"] = str(config_file)
        try:
            cfg = load_config()
            assert cfg.log_level == "WARNING"
            assert cfg.sdk.batch_size == 128
        finally:
            del os.environ["TRACEMILL_CONFIG"]

    def test_env_var_overrides(self, monkeypatch):
        monkeypatch.setenv("TRACEMILL_LOG_LEVEL", "ERROR")
        cfg = load_config()
        assert cfg.log_level == "ERROR"

    def test_nested_env_var_overrides(self, monkeypatch):
        monkeypatch.setenv("TRACEMILL_SDK__BATCH_SIZE", "256")
        cfg = load_config()
        assert cfg.sdk.batch_size == 256

    def test_explicit_overrides_beat_env(self, monkeypatch):
        monkeypatch.setenv("TRACEMILL_LOG_LEVEL", "ERROR")
        cfg = load_config(log_level="DEBUG")
        assert cfg.log_level == "DEBUG"


# ─── Mapping Resolver Tests ──────────────────────────────────────────────────


class TestMappingResolver:
    def test_resolve_bundled_mapping(self):
        path = resolve_mapping_path("copilot")
        assert path is not None
        assert path.name == "copilot.yaml"
        assert path.is_file()

    def test_resolve_nonexistent(self):
        path = resolve_mapping_path("nonexistent_framework_xyz")
        assert path is None

    def test_list_available_includes_bundled(self):
        mappings = list_available_mappings()
        assert "copilot" in mappings
        assert "claude" in mappings

    def test_user_mapping_overrides_bundled(self, tmp_path):
        """User mapping dir takes priority over bundled."""
        user_yaml = tmp_path / "copilot.yaml"
        user_yaml.write_text("framework: copilot\nframework_version: '99.x'\n")

        path = resolve_mapping_path("copilot", extra_dirs=[tmp_path])
        assert path == user_yaml  # user override, not bundled

    def test_list_with_extra_dirs(self, tmp_path):
        custom_yaml = tmp_path / "my_framework.yaml"
        custom_yaml.write_text("framework: my_framework\n")

        mappings = list_available_mappings(extra_dirs=[tmp_path])
        assert "my_framework" in mappings
        assert mappings["my_framework"] == custom_yaml
