"""Avro deserialiser for txn_api_v1 events with Schema Registry validation.

Confluent wire format:  0x00 | schema_id (4 bytes BE) | Avro payload
"""

from __future__ import annotations

import io
import struct
from typing import NamedTuple

import fastavro
import requests

# txn_api_v1 Avro schema (mirrors specs/001-kafka-ingestion-pipeline/contracts/txn-api-v1.avsc)
# Kept inline to avoid a runtime file dependency in the Flink TaskManager worker.
_TXN_API_V1_SCHEMA = {
    "type": "record",
    "name": "TxnApiV1",
    "namespace": "com.fraudstream.ingestion",
    "fields": [
        {"name": "transaction_id", "type": "string"},
        {"name": "account_id", "type": "string"},
        {"name": "merchant_id", "type": "string"},
        {
            "name": "amount",
            "type": {"type": "bytes", "logicalType": "decimal", "precision": 18, "scale": 4},
        },
        {"name": "currency", "type": "string"},
        {"name": "event_time", "type": {"type": "long", "logicalType": "timestamp-millis"}},
        {"name": "processing_time", "type": {"type": "long", "logicalType": "timestamp-millis"}},
        {"name": "channel", "type": "string"},
        {"name": "card_bin", "type": "string"},
        {"name": "card_last4", "type": "string"},
        {"name": "caller_ip_subnet", "type": "string"},
        {"name": "api_key_id", "type": "string"},
        {"name": "oauth_scope", "type": "string"},
        {"name": "geo_lat", "type": ["null", "float"], "default": None},
        {"name": "geo_lon", "type": ["null", "float"], "default": None},
        {"name": "masking_lib_version", "type": "string"},
    ],
}

_PARSED_TXN_SCHEMA = fastavro.parse_schema(_TXN_API_V1_SCHEMA)

_CONFLUENT_MAGIC = 0x00
_HEADER_SIZE = 5  # 1 byte magic + 4 bytes schema ID


class SchemaValidationError(Exception):
    """Raised when Avro deserialization or schema validation fails."""


class RawTransaction(NamedTuple):
    transaction_id: str
    account_id: str
    merchant_id: str
    amount: object  # decimal.Decimal
    currency: str
    event_time: int  # epoch ms
    processing_time: int  # epoch ms
    channel: str
    card_bin: str
    card_last4: str
    caller_ip_subnet: str
    api_key_id: str
    oauth_scope: str
    geo_lat: float | None
    geo_lon: float | None
    masking_lib_version: str


def deserialise_raw_transaction(
    payload: bytes,
    *,
    expected_schema_id: int | None = None,
) -> RawTransaction:
    """Deserialise a Confluent wire-format Avro payload into a RawTransaction.

    Raises SchemaValidationError on any failure — caller routes to DLQ.
    """
    try:
        if len(payload) < _HEADER_SIZE:
            raise SchemaValidationError(f"Payload too short: {len(payload)} bytes")

        magic = payload[0]
        if magic != _CONFLUENT_MAGIC:
            raise SchemaValidationError(f"Invalid Confluent magic byte: {magic:#x}")

        schema_id = struct.unpack(">I", payload[1:5])[0]
        if expected_schema_id is not None and schema_id != expected_schema_id:
            raise SchemaValidationError(
                f"Schema ID mismatch: got {schema_id}, expected {expected_schema_id}"
            )

        avro_payload = payload[_HEADER_SIZE:]
        record = fastavro.schemaless_reader(io.BytesIO(avro_payload), _PARSED_TXN_SCHEMA)
        # fastavro returns timestamp-millis as datetime; convert to epoch ms
        import datetime as _dt

        for field in ("event_time", "processing_time"):
            val = record.get(field)
            if isinstance(val, _dt.datetime):
                record[field] = int(val.timestamp() * 1000)
        return RawTransaction(**record)

    except SchemaValidationError:
        raise
    except Exception as exc:
        raise SchemaValidationError(f"Avro deserialization failed: {exc}") from exc


def serialise_enriched_transaction(record: dict, schema_id: int, parsed_schema: object) -> bytes:
    """Serialise an EnrichedTransactionEvent dict to Confluent wire-format Avro bytes."""
    buf = io.BytesIO()
    fastavro.schemaless_writer(buf, parsed_schema, record)
    prefix = b"\x00" + schema_id.to_bytes(4, "big")
    return prefix + buf.getvalue()


class SchemaRegistryClient:
    """Minimal Schema Registry client for schema ID lookup at startup."""

    def __init__(self, url: str) -> None:
        self._url = url.rstrip("/")

    def get_schema_id(self, subject: str) -> int:
        resp = requests.get(
            f"{self._url}/subjects/{subject}/versions/latest",
            timeout=10,
        )
        resp.raise_for_status()
        return int(resp.json()["id"])
