"""DLQ Kafka sink — routes unprocessable events to txn.processing.dlq."""

from __future__ import annotations

import io
import socket
import time
import uuid

import fastavro

# Avro schema for ProcessingDLQEnvelope (processing-dlq-v1.avsc)
_DLQ_SCHEMA = {
    "type": "record",
    "name": "ProcessingDLQEnvelope",
    "namespace": "com.fraudstream.processing.dlq",
    "fields": [
        {"name": "dlq_id", "type": "string"},
        {"name": "source_topic", "type": "string"},
        {"name": "source_partition", "type": "int"},
        {"name": "source_offset", "type": "long"},
        {"name": "transaction_id", "type": ["null", "string"], "default": None},
        {"name": "original_payload_bytes", "type": "bytes"},
        {"name": "error_type", "type": "string"},
        {"name": "error_message", "type": "string"},
        {
            "name": "watermark_at_rejection",
            "type": ["null", {"type": "long", "logicalType": "timestamp-millis"}],
            "default": None,
        },
        {
            "name": "event_time_at_rejection",
            "type": ["null", {"type": "long", "logicalType": "timestamp-millis"}],
            "default": None,
        },
        {
            "name": "failed_at",
            "type": {"type": "long", "logicalType": "timestamp-millis"},
        },
        {"name": "processor_host", "type": "string"},
        {"name": "processor_subtask_index", "type": "int"},
    ],
}

_PARSED_DLQ_SCHEMA = fastavro.parse_schema(_DLQ_SCHEMA)
_PROCESSOR_HOST = socket.gethostname()

# Confluent Schema Registry wire format magic byte
_CONFLUENT_MAGIC = b"\x00"


def build_dlq_record(
    *,
    source_topic: str,
    source_partition: int,
    source_offset: int,
    original_payload_bytes: bytes,
    error_type: str,
    error_message: str,
    transaction_id: str | None = None,
    watermark_at_rejection: int | None = None,
    event_time_at_rejection: int | None = None,
    subtask_index: int = 0,
) -> dict:
    """Build a ProcessingDLQEnvelope dict ready for Avro serialisation."""
    return {
        "dlq_id": str(uuid.uuid4()),
        "source_topic": source_topic,
        "source_partition": source_partition,
        "source_offset": source_offset,
        "transaction_id": transaction_id,
        "original_payload_bytes": original_payload_bytes,
        "error_type": error_type,
        "error_message": error_message,
        "watermark_at_rejection": watermark_at_rejection,
        "event_time_at_rejection": event_time_at_rejection,
        "failed_at": int(time.time() * 1000),
        "processor_host": _PROCESSOR_HOST,
        "processor_subtask_index": subtask_index,
    }


def serialise_dlq_record(record: dict, schema_id: int = 0) -> bytes:
    """Serialise a DLQ record to Confluent wire-format Avro bytes.

    Format: magic byte (0x00) + 4-byte big-endian schema ID + Avro payload.
    """
    buf = io.BytesIO()
    fastavro.schemaless_writer(buf, _PARSED_DLQ_SCHEMA, record)
    avro_bytes = buf.getvalue()

    # Confluent wire format prefix
    prefix = _CONFLUENT_MAGIC + schema_id.to_bytes(4, "big")
    return prefix + avro_bytes
