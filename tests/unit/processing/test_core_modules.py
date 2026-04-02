"""Unit tests for config, avro_serde, enricher, logging_config, metrics, telemetry."""

import threading
import time
from decimal import Decimal
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# ProcessorConfig
# ---------------------------------------------------------------------------
class TestProcessorConfig:
    def test_defaults_from_env(self):
        from pipelines.processing.config import ProcessorConfig

        cfg = ProcessorConfig()
        assert cfg.input_topic == "txn.api"
        assert cfg.output_topic == "txn.enriched"
        assert cfg.dlq_topic == "txn.processing.dlq"
        assert cfg.checkpoint_interval_ms == 60_000
        assert cfg.parallelism == 1
        assert cfg.watermark_ooo_seconds == 10
        assert cfg.allowed_lateness_seconds == 30

    def test_env_var_override(self, monkeypatch):
        monkeypatch.setenv("INPUT_TOPIC", "txn.api.test")
        monkeypatch.setenv("PARALLELISM", "4")
        from importlib import reload

        import pipelines.processing.config as mod

        reload(mod)
        cfg = mod.ProcessorConfig()
        assert cfg.input_topic == "txn.api.test"
        assert cfg.parallelism == 4


# ---------------------------------------------------------------------------
# Avro serde
# ---------------------------------------------------------------------------
class TestSchemaValidationError:
    def test_raised_on_short_payload(self):
        from pipelines.processing.shared.avro_serde import (
            SchemaValidationError,
            deserialise_raw_transaction,
        )

        with pytest.raises(SchemaValidationError, match="too short"):
            deserialise_raw_transaction(b"\x00\x00")

    def test_raised_on_wrong_magic_byte(self):
        from pipelines.processing.shared.avro_serde import (
            SchemaValidationError,
            deserialise_raw_transaction,
        )

        with pytest.raises(SchemaValidationError, match="magic byte"):
            deserialise_raw_transaction(b"\x01\x00\x00\x00\x01" + b"x" * 20)

    def test_raised_on_garbage_payload(self):
        from pipelines.processing.shared.avro_serde import (
            SchemaValidationError,
            deserialise_raw_transaction,
        )

        # Valid magic + schema_id but garbage Avro payload
        with pytest.raises(SchemaValidationError):
            deserialise_raw_transaction(b"\x00\x00\x00\x00\x01" + b"\xff" * 30)

    def test_schema_id_mismatch_raises(self):
        from pipelines.processing.shared.avro_serde import (
            SchemaValidationError,
            deserialise_raw_transaction,
        )

        with pytest.raises(SchemaValidationError, match="mismatch"):
            deserialise_raw_transaction(
                b"\x00\x00\x00\x00\x05" + b"\xff" * 10,
                expected_schema_id=1,
            )


class TestSchemaRegistryClient:
    def test_get_schema_id_calls_correct_url(self):
        from unittest.mock import patch as mock_patch

        from pipelines.processing.shared.avro_serde import SchemaRegistryClient

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": 42}
        mock_resp.raise_for_status = MagicMock()

        with mock_patch("requests.get", return_value=mock_resp) as mock_get:
            client = SchemaRegistryClient("http://localhost:8081")
            schema_id = client.get_schema_id("txn.enriched-value")

        assert schema_id == 42
        mock_get.assert_called_once_with(
            "http://localhost:8081/subjects/txn.enriched-value/versions/latest",
            timeout=10,
        )


# ---------------------------------------------------------------------------
# EnrichedRecordAssembler (pure-Python fallback)
# ---------------------------------------------------------------------------
class TestEnrichedRecordAssembler:
    def _make_txn(self):
        from pipelines.processing.shared.avro_serde import RawTransaction

        return RawTransaction(
            transaction_id="txn-001",
            account_id="acc-123",
            merchant_id="merch-99",
            amount=Decimal("49.99"),
            currency="USD",
            event_time=1_767_225_600_000,
            processing_time=1_767_225_600_010,
            channel="API",
            card_bin="411111",
            card_last4="1234",
            caller_ip_subnet="203.0.113.0",
            api_key_id="key-abc",
            oauth_scope="txn:write",
            geo_lat=None,
            geo_lon=None,
            masking_lib_version="0.1.0",
        )

    def _velocity(self, count=1, amount=Decimal("49.99")):
        return {
            "vel_count_1m": count,
            "vel_amount_1m": amount,
            "vel_count_5m": count,
            "vel_amount_5m": amount,
            "vel_count_1h": count,
            "vel_amount_1h": amount,
            "vel_count_24h": count,
            "vel_amount_24h": amount,
        }

    def _geo(self):
        return {
            "geo_country": "US",
            "geo_city": "New York",
            "geo_network_class": "RESIDENTIAL",
            "geo_confidence": 0.9,
        }

    def _device(self):
        return {
            "device_first_seen": 1_767_225_600_000,
            "device_txn_count": 1,
            "device_known_fraud": False,
        }

    def test_all_36_fields_present(self):
        from pipelines.processing.operators.enricher import EnrichedRecordAssembler

        assembler = EnrichedRecordAssembler()
        record = assembler.assemble(self._make_txn(), self._velocity(), self._geo(), self._device())
        # 36 fields per enriched-txn-v1.avsc
        expected_fields = {
            "transaction_id",
            "account_id",
            "merchant_id",
            "amount",
            "currency",
            "event_time",
            "processing_time",
            "channel",
            "card_bin",
            "card_last4",
            "caller_ip_subnet",
            "api_key_id",
            "oauth_scope",
            "geo_lat",
            "geo_lon",
            "masking_lib_version",
            "vel_count_1m",
            "vel_amount_1m",
            "vel_count_5m",
            "vel_amount_5m",
            "vel_count_1h",
            "vel_amount_1h",
            "vel_count_24h",
            "vel_amount_24h",
            "geo_country",
            "geo_city",
            "geo_network_class",
            "geo_confidence",
            "device_first_seen",
            "device_txn_count",
            "device_known_fraud",
            "enrichment_time",
            "enrichment_latency_ms",
            "processor_version",
            "schema_version",
        }
        assert set(record.keys()) == expected_fields

    def test_enrichment_latency_ms_non_negative(self):
        from pipelines.processing.operators.enricher import EnrichedRecordAssembler

        record = EnrichedRecordAssembler().assemble(
            self._make_txn(), self._velocity(), self._geo(), self._device()
        )
        assert record["enrichment_latency_ms"] >= 0

    def test_schema_version_is_one(self):
        from pipelines.processing.operators.enricher import EnrichedRecordAssembler

        record = EnrichedRecordAssembler().assemble(
            self._make_txn(), self._velocity(), self._geo(), self._device()
        )
        assert record["schema_version"] == "1"

    def test_original_fields_forwarded_unchanged(self):
        from pipelines.processing.operators.enricher import EnrichedRecordAssembler

        txn = self._make_txn()
        record = EnrichedRecordAssembler().assemble(
            txn, self._velocity(), self._geo(), self._device()
        )
        assert record["transaction_id"] == txn.transaction_id
        assert record["account_id"] == txn.account_id
        assert record["amount"] == txn.amount


class TestTransactionDedup:
    def test_first_occurrence_returns_true(self):
        from pipelines.processing.operators.enricher import TransactionDedup

        dedup = TransactionDedup()
        assert dedup.process_element_pure("txn-001", 1_000) is True

    def test_duplicate_returns_false(self):
        from pipelines.processing.operators.enricher import TransactionDedup

        dedup = TransactionDedup()
        dedup.process_element_pure("txn-001", 1_000)
        assert dedup.process_element_pure("txn-001", 2_000) is False

    def test_different_ids_are_independent(self):
        from pipelines.processing.operators.enricher import TransactionDedup

        dedup = TransactionDedup()
        assert dedup.process_element_pure("txn-A", 1_000) is True
        assert dedup.process_element_pure("txn-B", 1_000) is True


# ---------------------------------------------------------------------------
# Logging config
# ---------------------------------------------------------------------------
class TestLoggingConfig:
    def test_set_and_get_transaction_id(self):
        from pipelines.processing.logging_config import get_transaction_id, set_transaction_id

        set_transaction_id("txn-xyz")
        assert get_transaction_id() == "txn-xyz"

    def test_none_transaction_id_stores_empty_string(self):
        from pipelines.processing.logging_config import get_transaction_id, set_transaction_id

        set_transaction_id(None)
        assert get_transaction_id() == ""

    def test_thread_isolation(self):
        """Each thread has its own transaction_id."""
        from pipelines.processing.logging_config import get_transaction_id, set_transaction_id

        results: dict = {}

        def worker(txn_id: str, key: str):
            set_transaction_id(txn_id)
            time.sleep(0.01)
            results[key] = get_transaction_id()

        t1 = threading.Thread(target=worker, args=("txn-thread-1", "t1"))
        t2 = threading.Thread(target=worker, args=("txn-thread-2", "t2"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert results["t1"] == "txn-thread-1"
        assert results["t2"] == "txn-thread-2"

    def test_configure_logging_does_not_raise(self):
        from pipelines.processing.logging_config import configure_logging

        configure_logging(level="WARNING")  # must not raise

    def test_transaction_filter_injects_txn_id(self):
        import logging

        from pipelines.processing.logging_config import _TransactionFilter, set_transaction_id

        set_transaction_id("txn-filter-99")
        f = _TransactionFilter()
        record = logging.LogRecord("test", logging.INFO, "", 0, "msg", [], None)
        result = f.filter(record)
        assert result is True
        assert record.transaction_id == "txn-filter-99"  # type: ignore[attr-defined]

    def test_json_formatter_emits_valid_json(self):
        import json
        import logging

        from pipelines.processing.logging_config import _JsonFormatter

        formatter = _JsonFormatter()
        record = logging.LogRecord("test.comp", logging.WARNING, "", 0, "hello", [], None)
        record.transaction_id = "txn-fmt-01"  # type: ignore[attr-defined]
        output = formatter.format(record)
        data = json.loads(output)
        assert data["message"] == "hello"
        assert data["level"] == "WARNING"
        assert data["component"] == "test.comp"
        assert "timestamp" in data

    def test_json_formatter_includes_exception_field(self):
        import json
        import logging
        import sys

        from pipelines.processing.logging_config import _JsonFormatter

        formatter = _JsonFormatter()
        try:
            raise ValueError("boom")
        except ValueError:
            exc_info = sys.exc_info()
        record = logging.LogRecord("test", logging.ERROR, "", 0, "err", [], exc_info)
        record.transaction_id = ""  # type: ignore[attr-defined]
        output = formatter.format(record)
        data = json.loads(output)
        assert "exception" in data
        assert "ValueError" in data["exception"]


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
class TestMetrics:
    def test_metrics_are_importable(self):
        # Verify they are the expected prometheus_client types
        from prometheus_client import Counter, Gauge, Histogram

        from pipelines.processing.metrics import (
            consumer_lag_records,
            dlq_events_total,
            enrichment_latency_ms,
            last_checkpoint_duration_ms,
        )

        assert isinstance(enrichment_latency_ms, Histogram)
        assert isinstance(dlq_events_total, Counter)
        assert isinstance(consumer_lag_records, Gauge)
        assert isinstance(last_checkpoint_duration_ms, Gauge)

    def test_dlq_events_total_labels(self):
        from pipelines.processing.metrics import dlq_events_total

        # Should not raise when labelled by error_type
        dlq_events_total.labels(error_type="SCHEMA_VALIDATION_ERROR").inc(0)

    def test_enrichment_latency_observe(self):
        from pipelines.processing.metrics import enrichment_latency_ms

        enrichment_latency_ms.observe(5.0)  # must not raise


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------
class TestTelemetry:
    def test_extract_returns_empty_dict_on_none_headers(self):
        from pipelines.processing.telemetry import extract_trace_context

        result = extract_trace_context(None)
        assert result == {}

    def test_extract_returns_empty_dict_on_empty_headers(self):
        from pipelines.processing.telemetry import extract_trace_context

        result = extract_trace_context([])
        assert result == {}

    def test_extract_picks_up_traceparent_header(self):
        from pipelines.processing.telemetry import extract_trace_context

        headers = [
            ("traceparent", b"00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"),
            ("other", b"ignored"),
        ]
        result = extract_trace_context(headers)
        assert "traceparent" in result

    def test_inject_returns_list(self):
        from pipelines.processing.telemetry import inject_trace_context

        result = inject_trace_context({})
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Job module — _parse_args and main
# ---------------------------------------------------------------------------
class TestJobParseArgs:
    def test_defaults_are_none(self):
        from pipelines.processing.job import _parse_args

        args = _parse_args([])
        assert args.kafka_brokers is None
        assert args.schema_registry is None
        assert args.input_topic is None
        assert args.parallelism is None

    def test_explicit_broker_and_parallelism(self):
        from pipelines.processing.job import _parse_args

        args = _parse_args(["--kafka-brokers", "localhost:9092", "--parallelism", "4"])
        assert args.kafka_brokers == "localhost:9092"
        assert args.parallelism == 4

    def test_all_overrideable_options(self):
        from pipelines.processing.job import _parse_args

        args = _parse_args(
            [
                "--kafka-brokers",
                "b:9092",
                "--schema-registry",
                "http://sr:8081",
                "--input-topic",
                "txn.api.test",
                "--output-topic",
                "txn.enriched.test",
                "--dlq-topic",
                "txn.dlq.test",
                "--checkpoint-dir",
                "s3://bucket/ckpt",
                "--checkpoint-interval-ms",
                "30000",
                "--geoip-db-path",
                "/tmp/GeoLite2-City.mmdb",
                "--parallelism",
                "2",
                "--watermark-ooo-seconds",
                "5",
                "--allowed-lateness-seconds",
                "15",
                "--processor-version",
                "002@test",
            ]
        )
        assert args.kafka_brokers == "b:9092"
        assert args.input_topic == "txn.api.test"
        assert args.checkpoint_interval_ms == 30000
        assert args.parallelism == 2
        assert args.processor_version == "002@test"


class TestJobMain:
    def test_main_exits_on_import_error(self):
        from unittest.mock import patch as mock_patch

        from pipelines.processing.job import main

        with mock_patch(
            "pipelines.processing.job.build_job",
            side_effect=ImportError("pyflink"),
        ):
            with pytest.raises(SystemExit) as exc_info:
                main([])
        assert exc_info.value.code == 1

    def test_main_executes_env_on_success(self):
        from unittest.mock import patch as mock_patch

        from pipelines.processing.job import main

        mock_env = MagicMock()
        with mock_patch("pipelines.processing.job.build_job", return_value=mock_env):
            main([])
        mock_env.execute.assert_called_once()

    def test_main_passes_broker_override(self):
        from unittest.mock import patch as mock_patch

        from pipelines.processing.job import main

        mock_env = MagicMock()
        with mock_patch("pipelines.processing.job.build_job", return_value=mock_env) as mock_build:
            main(["--kafka-brokers", "localhost:9092", "--parallelism", "2"])
        called_config = mock_build.call_args[0][0]
        assert called_config.kafka_brokers == "localhost:9092"
        assert called_config.parallelism == 2

    def test_main_sets_processor_version_env(self, monkeypatch):
        import os
        from unittest.mock import patch as mock_patch

        from pipelines.processing.job import main

        mock_env = MagicMock()
        with mock_patch("pipelines.processing.job.build_job", return_value=mock_env):
            main(["--processor-version", "002@vtest"])
        assert os.environ.get("PROCESSOR_VERSION") == "002@vtest"
