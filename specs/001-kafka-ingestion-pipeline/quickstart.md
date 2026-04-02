# Quickstart: Kafka Ingestion Pipeline — API Channel

**Branch**: `001-kafka-ingestion-pipeline` | **Date**: 2026-03-30

Get the API channel producer running locally in under 5 minutes.

---

## Prerequisites

- Docker & Docker Compose
- Python 3.11+
- `make` (optional but recommended)

---

## 1. Start the Local Stack

```bash
cd infra/
docker compose up -d kafka schema-registry prometheus grafana
```

Wait for Schema Registry to be healthy:

```bash
docker compose ps          # all services should show "healthy" or "running"
curl http://localhost:8081/subjects   # should return [] on first run
```

---

## 2. Create Topics

```bash
bash infra/kafka/topics.sh
```

This creates:
- `txn.api` — 12 partitions, replication factor 3, 7-year retention
- `txn.api.dlq` — 3 partitions, 7-day retention

Verify:

```bash
docker exec broker kafka-topics --bootstrap-server localhost:9092 --list
# txn.api
# txn.api.dlq
```

---

## 3. Register the Avro Schema

```bash
curl -X POST http://localhost:8081/subjects/txn.api-value/versions \
  -H "Content-Type: application/vnd.schemaregistry.v1+json" \
  -d "{\"schema\": $(cat pipelines/ingestion/schemas/txn_api_v1.avsc | jq -Rs .)}"
```

Verify:

```bash
curl http://localhost:8081/subjects/txn.api-value/versions
# [1]
```

Set compatibility:

```bash
curl -X PUT http://localhost:8081/config/txn.api-value \
  -H "Content-Type: application/vnd.schemaregistry.v1+json" \
  -d '{"compatibility": "BACKWARD_TRANSITIVE"}'
```

---

## 4. Install Python Dependencies

```bash
cd pipelines/ingestion/
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"   # installs confluent-kafka, fastavro, opentelemetry-sdk, prometheus-client, pytest, testcontainers
```

---

## 5. Run the Producer

```bash
# Set required environment variables
export KAFKA_BOOTSTRAP_SERVERS=localhost:9092
export SCHEMA_REGISTRY_URL=http://localhost:8081
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317   # optional for local

# Start the producer service
python -m pipelines.ingestion.api.producer
```

Expected output:

```
INFO  component=api-producer event=startup schema_registry=http://localhost:8081 status=connected
INFO  component=api-producer event=schema_cached subject=txn.api-value schema_id=1
INFO  component=api-producer event=ready listening on 0.0.0.0:8080
```

---

## 6. Send a Test Transaction

```bash
curl -X POST http://localhost:8080/v1/transactions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <test-token>" \
  -d '{
    "account_id": "acc_001",
    "merchant_id": "mch_xyz",
    "amount": "49.99",
    "currency": "USD",
    "card_number": "4111111111111111",
    "caller_ip": "203.0.113.45",
    "api_key_id": "key_abc123",
    "oauth_scope": "transactions:write",
    "event_time": 1743350400000
  }'
```

Expected response:

```json
{
  "transaction_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "status": "accepted",
  "latency_ms": 6
}
```

---

## 7. Verify the Event in Kafka

```bash
docker exec schema-registry \
  kafka-avro-console-consumer \
  --bootstrap-server broker:9092 \
  --topic txn.api \
  --from-beginning \
  --max-messages 1 \
  --property schema.registry.url=http://schema-registry:8081
```

Verify:
- `card_bin`: `"411111"` (first 6)
- `card_last4`: `"1111"` (last 4)
- No 16-digit sequence present anywhere in the output
- `caller_ip_subnet`: `"203.0.113.0"` (truncated to /24)
- `channel`: `"API"`

---

## 8. Run Tests

```bash
# Unit tests (no Docker required)
pytest tests/unit/ -v

# Integration tests (requires running Docker stack from step 1)
pytest tests/integration/ -v --timeout=60

# Contract tests (Schema Registry required)
pytest tests/contract/ -v
```

All tests should pass. Minimum coverage gate: **80%**.

```bash
pytest tests/unit/ --cov=pipelines/ingestion --cov-report=term-missing --cov-fail-under=80
```

---

## 9. Check Metrics & Dashboards

- **Prometheus**: http://localhost:9090
  - Query: `producer_publish_latency_ms_bucket{topic="txn.api"}`
  - Query: `producer_events_total{topic="txn.api"}`
  - Query: `dlq_depth{topic="txn.api.dlq"}`

- **Grafana**: http://localhost:3000 (admin/admin)
  - Dashboard: **API Ingestion Pipeline** (imported from `monitoring/dashboards/api-ingestion.json`)

---

## 10. Simulate a DLQ Event

Force a serialisation error by sending a payload with an invalid PAN:

```bash
curl -X POST http://localhost:8080/v1/transactions \
  -H "Content-Type: application/json" \
  -d '{
    "account_id": "acc_001",
    "merchant_id": "mch_xyz",
    "amount": "10.00",
    "currency": "USD",
    "card_number": "1234567890123456",
    "caller_ip": "203.0.113.1",
    "api_key_id": "key_abc",
    "oauth_scope": "transactions:write",
    "event_time": 1743350400000
  }'
```

Expected response: `400 Bad Request` with `{"error": "InvalidPANError", "detail": "Luhn check failed"}`.

Verify DLQ:

```bash
docker exec schema-registry \
  kafka-console-consumer \
  --bootstrap-server broker:9092 \
  --topic txn.api.dlq \
  --from-beginning \
  --max-messages 1
```

---

## Environment Variables Reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `KAFKA_BOOTSTRAP_SERVERS` | Yes | — | Comma-separated broker list |
| `SCHEMA_REGISTRY_URL` | Yes | — | Confluent Schema Registry URL |
| `SCHEMA_REGISTRY_RETRIES` | No | `5` | Startup retry attempts |
| `KAFKA_ACKS` | No | `all` | Producer acks setting |
| `KAFKA_LINGER_MS` | No | `1` | Batch linger time |
| `MASKING_IPV4_PREFIX` | No | `24` | CIDR prefix for IPv4 truncation |
| `MASKING_IPV6_PREFIX` | No | `64` | CIDR prefix for IPv6 truncation |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | No | — | OpenTelemetry collector endpoint |
| `PROMETHEUS_PORT` | No | `8001` | Port for Prometheus metrics scrape |
| `LOG_LEVEL` | No | `INFO` | Structured log level |

---

## Troubleshooting

**`Schema Registry unreachable after 5 attempts`**
→ Ensure `docker compose up schema-registry` is healthy before starting the producer. Check `SCHEMA_REGISTRY_URL`.

**`Luhn check failed` on all cards**
→ Use test PANs from https://www.paypalobjects.com/en_AU/vhelp/paypalmanager_help/credit_card_numbers.htm (e.g. `4111111111111111` Visa, `378282246310005` Amex).

**p99 latency > 10ms in load test**
→ Verify `linger.ms=1` and `queue.buffering.max.ms=1` are both set. Check broker co-location (cross-AZ adds 5–15ms). Check GC pauses in Python process.

**DLQ not receiving events**
→ Confirm `txn.api.dlq` topic exists (`kafka-topics --list`). Check DLQ producer config (`acks=1`, not `all`) to avoid DLQ writes blocking on broker issues.
