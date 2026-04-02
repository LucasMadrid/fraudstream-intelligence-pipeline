"""Schema contract tests — validate Avro schemas and boundary rules."""

import json
from pathlib import Path

import fastavro
from fastavro.schema import parse_schema

SCHEMAS_DIR = Path(__file__).parent.parent.parent / "pipelines" / "ingestion" / "schemas"


def _load(name: str) -> dict:
    return json.loads((SCHEMAS_DIR / name).read_text())


def test_txn_api_schema_parses():
    schema = _load("txn_api_v1.avsc")
    parsed = parse_schema(schema)
    assert parsed is not None


def test_dlq_envelope_schema_parses():
    schema = _load("dlq_envelope_v1.avsc")
    parsed = parse_schema(schema)
    assert parsed is not None


def test_txn_api_required_fields_present():
    schema = _load("txn_api_v1.avsc")
    field_names = {f["name"] for f in schema["fields"]}
    required = {
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
        "masking_lib_version",
        "schema_version",
    }
    assert required <= field_names, f"Missing fields: {required - field_names}"


def test_txn_api_amount_is_decimal():
    schema = _load("txn_api_v1.avsc")
    amount_field = next(f for f in schema["fields"] if f["name"] == "amount")
    assert amount_field["type"]["logicalType"] == "decimal"
    assert amount_field["type"]["precision"] == 18
    assert amount_field["type"]["scale"] == 4


def test_txn_api_no_card_number_field():
    """Full card number must never appear as a field in the schema."""
    schema = _load("txn_api_v1.avsc")
    field_names = {f["name"] for f in schema["fields"]}
    assert "card_number" not in field_names
    assert "pan" not in field_names


def test_txn_api_geo_fields_nullable():
    schema = _load("txn_api_v1.avsc")
    for fname in ("geo_lat", "geo_lon"):
        field = next(f for f in schema["fields"] if f["name"] == fname)
        ftype = field["type"]
        assert isinstance(ftype, list) and "null" in ftype, (
            f"{fname} must be nullable (union with null)"
        )


def test_dlq_envelope_has_masking_applied():
    schema = _load("dlq_envelope_v1.avsc")
    field_names = {f["name"] for f in schema["fields"]}
    assert "masking_applied" in field_names


def test_txn_api_validates_sample_record():
    """A valid record should serialize and deserialize without error."""
    import io

    schema = _load("txn_api_v1.avsc")
    parsed = parse_schema(schema)

    # Encode amount as bytes (big-endian decimal, scale=4)
    amount_scaled = 499900  # 49.99 * 10000
    amount_bytes = amount_scaled.to_bytes(3, byteorder="big", signed=True)

    record = {
        "transaction_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
        "account_id": "acc_001",
        "merchant_id": "mch_xyz",
        "amount": amount_bytes,
        "currency": "USD",
        "event_time": 1743350400000,
        "processing_time": 1743350400100,
        "channel": "API",
        "card_bin": "411111",
        "card_last4": "1111",
        "caller_ip_subnet": "203.0.113.0",
        "api_key_id": "key_abc123",
        "oauth_scope": "transactions:write",
        "geo_lat": None,
        "geo_lon": None,
        "masking_lib_version": "1.0.0",
        "schema_version": "1",
    }

    buf = io.BytesIO()
    fastavro.schemaless_writer(buf, parsed, record)
    buf.seek(0)
    decoded = fastavro.schemaless_reader(buf, parsed)
    assert decoded["transaction_id"] == record["transaction_id"]
    assert decoded["card_bin"] == "411111"
