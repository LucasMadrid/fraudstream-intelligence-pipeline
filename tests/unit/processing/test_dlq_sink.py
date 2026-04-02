"""Unit tests for DLQ sink serialisation and record construction (T013)."""

import struct

from pipelines.processing.shared.dlq_sink import (
    build_dlq_record,
    serialise_dlq_record,
)


class TestBuildDlqRecord:
    def test_required_fields_present(self):
        record = build_dlq_record(
            source_topic="txn.api",
            source_partition=0,
            source_offset=42,
            original_payload_bytes=b"\x00\x00\x00\x00\x01abc",
            error_type="SCHEMA_VALIDATION_ERROR",
            error_message="Invalid magic byte",
        )
        assert record["source_topic"] == "txn.api"
        assert record["source_partition"] == 0
        assert record["source_offset"] == 42
        assert record["error_type"] == "SCHEMA_VALIDATION_ERROR"
        assert record["error_message"] == "Invalid magic byte"
        assert record["transaction_id"] is None
        assert record["watermark_at_rejection"] is None
        assert record["event_time_at_rejection"] is None

    def test_optional_fields_populated_when_provided(self):
        record = build_dlq_record(
            source_topic="txn.api",
            source_partition=1,
            source_offset=99,
            original_payload_bytes=b"raw",
            error_type="LATE_EVENT_BEYOND_ALLOWED_LATENESS",
            error_message="10s past watermark",
            transaction_id="txn-abc-123",
            watermark_at_rejection=1_700_000_000_000,
            event_time_at_rejection=1_699_999_990_000,
            subtask_index=2,
        )
        assert record["transaction_id"] == "txn-abc-123"
        assert record["watermark_at_rejection"] == 1_700_000_000_000
        assert record["event_time_at_rejection"] == 1_699_999_990_000
        assert record["processor_subtask_index"] == 2

    def test_dlq_id_is_uuid_v4(self):
        import re

        record = build_dlq_record(
            source_topic="txn.api",
            source_partition=0,
            source_offset=0,
            original_payload_bytes=b"",
            error_type="ENRICHMENT_EXCEPTION",
            error_message="unexpected",
        )
        uuid_pattern = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        assert re.match(uuid_pattern, record["dlq_id"]), f"Not a UUID v4: {record['dlq_id']}"

    def test_failed_at_is_recent_epoch_ms(self):
        import time

        before = int(time.time() * 1000)
        record = build_dlq_record(
            source_topic="txn.api",
            source_partition=0,
            source_offset=0,
            original_payload_bytes=b"",
            error_type="SCHEMA_VALIDATION_ERROR",
            error_message="test",
        )
        after = int(time.time() * 1000)
        assert before <= record["failed_at"] <= after


class TestSerialiseDlqRecord:
    def _make_record(self):
        return build_dlq_record(
            source_topic="txn.api",
            source_partition=0,
            source_offset=1,
            original_payload_bytes=b"\x00\x00\x00\x00\x01payload",
            error_type="SCHEMA_VALIDATION_ERROR",
            error_message="test error",
        )

    def test_confluent_wire_format_magic_byte(self):
        record = self._make_record()
        serialised = serialise_dlq_record(record, schema_id=7)
        assert serialised[0:1] == b"\x00", "First byte must be Confluent magic 0x00"

    def test_schema_id_encoded_big_endian(self):
        record = self._make_record()
        schema_id = 42
        serialised = serialise_dlq_record(record, schema_id=schema_id)
        decoded_id = struct.unpack(">I", serialised[1:5])[0]
        assert decoded_id == schema_id

    def test_original_payload_bytes_preserved(self):
        """original_payload_bytes must be round-trippable through Avro serialisation."""
        import io

        import fastavro

        from pipelines.processing.shared.dlq_sink import _PARSED_DLQ_SCHEMA

        record = self._make_record()
        serialised = serialise_dlq_record(record, schema_id=0)
        avro_payload = serialised[5:]  # strip 5-byte Confluent header
        decoded = fastavro.schemaless_reader(io.BytesIO(avro_payload), _PARSED_DLQ_SCHEMA)
        assert decoded["original_payload_bytes"] == record["original_payload_bytes"]

    def test_schema_validation_error_type_roundtrip(self):
        """SCHEMA_VALIDATION_ERROR error_type survives Avro round-trip."""
        import io

        import fastavro

        from pipelines.processing.shared.dlq_sink import _PARSED_DLQ_SCHEMA

        record = self._make_record()
        serialised = serialise_dlq_record(record, schema_id=0)
        decoded = fastavro.schemaless_reader(io.BytesIO(serialised[5:]), _PARSED_DLQ_SCHEMA)
        assert decoded["error_type"] == "SCHEMA_VALIDATION_ERROR"
