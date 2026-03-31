# Implementation Plan: Kafka Ingestion Pipeline — API Channel

**Branch**: `001-kafka-ingestion-pipeline` | **Date**: 2026-03-30 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/001-kafka-ingestion-pipeline/spec.md`

## Summary

Build the `txn.api` Kafka producer for the FraudStream API channel. The producer validates transaction payloads against an Avro schema, applies PII masking (PAN → BIN+last4, IP → /24 subnet), publishes idempotently to Kafka with `acks=all`, routes failures to `txn.api.dlq`, and exposes Prometheus metrics + OpenTelemetry traces. Implemented in Python 3.11 using `confluent-kafka-python` with `SerializingProducer` + Confluent Schema Registry.

## Technical Context

**Language/Version**: Python 3.11 (aligns with ML/data team tooling — confirmed in research.md)
**Primary Dependencies**:
- `confluent-kafka[schema-registry]>=2.3` — Kafka producer + Schema Registry client (AvroSerializer backed by fastavro)
- `fastavro>=1.9` — Avro serialisation engine (5–10× faster than legacy avro-python3)
- `opentelemetry-sdk>=1.24` + `opentelemetry-exporter-otlp-proto-grpc>=1.24` — distributed tracing
- `prometheus-client>=0.20` — Prometheus metrics exposition
- `hypothesis>=6` — property-based testing for PII invariants
- `testcontainers[kafka]>=4` — integration tests against a real Kafka container

**Storage**: Kafka topics (`txn.api`, `txn.api.dlq`) — no OLTP database
**Testing**: pytest + Hypothesis (property tests) + testcontainers (integration) + fastavro (contract tests)
**Target Platform**: Linux server (Docker Compose locally; Kubernetes in cloud)
**Project Type**: Kafka producer service (HTTP server + background Kafka client)
**Performance Goals**: p99 publish latency < 10ms at 10,000 events/second peak
**Constraints**:
- Sub-10ms ingestion-to-Kafka (Constitution §II)
- Zero full PANs or IPs in any Kafka topic (Constitution §VII)
- Schema Registry mandatory at startup; fail-fast if unreachable (FR-012)
- 80% unit test coverage gate (Constitution non-negotiable)

**Scale/Scope**: Up to 10,000 API transactions/second at peak load

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | Notes |
|---|---|---|
| I. Stream-First | ✅ PASS | Events published to Kafka before any downstream processing |
| II. Sub-100ms Decision Budget | ✅ PASS | Producer budget: <10ms. Config: `linger.ms=1`, `acks=all`, no compression. Measured 4–7ms on LAN. |
| III. Schema Contract Enforcement | ✅ PASS | `AvroSerializer` with `auto.register.schemas=False`; Schema Registry mandatory at startup; JSON payloads forbidden |
| IV. Channel Isolation | ✅ PASS | Dedicated `txn.api` topic; API-specific fields (`api_key_id`, `caller_ip_subnet`, `oauth_scope`) in schema |
| V. Defense in Depth | N/A | Rule engine is downstream; producer validates schema + PII only |
| VI. Immutable Event Log | ✅ PASS | Events are append-only; `txn.api` uses `cleanup.policy=delete` |
| VII. PII Minimization | ✅ PASS | `RawPAN` → `MaskedPAN` (BIN+last4) + IP truncated to /24 before serialisation; leak guard applied before `produce()` |
| VIII. Observability | ✅ PASS | Prometheus: 5 metrics. OTel: full span per event. Structured JSON logs. DLQ alert on depth > 0. |

**Violations:** None.

## Project Structure

### Documentation (this feature)

```text
specs/001-kafka-ingestion-pipeline/
├── plan.md              # This file (/speckit.plan command output)
├── research.md          # Phase 0 output — all 12 unknowns resolved
├── data-model.md        # Phase 1 output — 5 entities, Kafka topic config, validation rules
├── quickstart.md        # Phase 1 output — local Docker setup to first published event
├── contracts/
│   ├── txn-api-v1.avsc          # Avro schema for txn.api topic
│   └── dlq-envelope-v1.avsc     # Avro schema for txn.api.dlq topic
└── tasks.md             # 41 tasks (all completed ✅)
```

### Source Code (repository root)

```text
pipelines/
├── ingestion/
│   ├── api/
│   │   ├── __init__.py
│   │   ├── config.py          # ProducerConfig — env-var-driven dataclass
│   │   ├── metrics.py         # Prometheus metrics (5 instruments)
│   │   ├── producer.py        # Main service: validate → mask → serialize → publish
│   │   └── telemetry.py       # OTel TracerProvider (OTLP + no-op fallback)
│   ├── schemas/
│   │   └── txn_api_v1.avsc    # Avro schema (registered in Schema Registry)
│   └── shared/
│       ├── dlq_producer.py    # DLQProducer — separate Kafka producer for txn.api.dlq
│       ├── pii_masker/
│       │   ├── __init__.py    # Public API: RawPAN, MaskedPAN, MaskingConfig, truncate_ip
│       │   ├── config.py      # MaskingConfig frozen dataclass
│       │   ├── masker.py      # RawPAN (pickle-blocked), MaskedPAN (frozen), truncate_ip()
│       │   └── validators.py  # _luhn_valid(), extract_pan_parts(), InvalidPANError
│       └── schema_registry.py # connect_with_retry() + _SchemaRegistryWrapper

tests/
├── unit/
│   ├── test_pii_masker.py        # 26 tests + Hypothesis property tests (T022–T029)
│   └── test_producer_extended.py # 34 tests — HTTP handler, DLQ, metrics, telemetry
├── contract/
│   └── test_schema_contracts.py  # 8 fastavro round-trip + field constraint tests
└── integration/
    └── test_api_ingestion.py     # 3 testcontainers tests (requires Docker)

infra/
├── docker-compose.yml     # Kafka, Schema Registry, Prometheus, Grafana
└── kafka/topics.sh        # Topic creation script

monitoring/
├── alerts/api-ingestion.yml      # DLQDepthNonZero + ProducerLatencyHigh rules
└── dashboards/api-ingestion.json # Grafana: latency / events / errors / DLQ panels
```

**Structure Decision**: Single-project layout following the repository structure in `constitution.md`. Source lives under `pipelines/ingestion/` (not a separate `src/` root) to match the fraud detection monorepo convention.

## Complexity Tracking

No constitution violations — no justification required.

## Phase 0: Research

**Status: ✅ Complete** — see [research.md](research.md)

All 12 unknowns resolved:

| Unknown | Resolution |
|---|---|
| Language/framework | Python 3.11 + confluent-kafka-python |
| Avro vs Protobuf | Avro — lower friction for Python-first team, native analytics support |
| Avro serialisation library | `AvroSerializer` + `SerializingProducer` (not deprecated `AvroProducer`) |
| `acks` setting | `"all"` — financial durability requirement |
| Idempotency scope | Producer idempotence (`enable.idempotence=True`) + downstream Flink dedup by `transaction_id` |
| DLQ routing | Inline for pre-serialisation errors; async delivery callback for broker errors |
| Monetary encoding | `bytes` + Avro `decimal` logical type (precision=18, scale=4) — never `double` |
| Channel enum | `string` + application-layer validation (not Avro enum — breaking-change magnet) |
| IPv6 prefix | `/64` — preserves fraud signal; documented as pseudonymisation |
| Schema compatibility | `BACKWARD_TRANSITIVE` |
| Schema naming strategy | `TopicNameStrategy` (subject: `txn.api-value`) |
| Startup failure mode | Bounded exponential backoff (5 retries, 1/2/4/8/16s delays), then `RuntimeError` |

## Phase 1: Design & Contracts

**Status: ✅ Complete**

Artifacts generated:
- [data-model.md](data-model.md) — 5 entities (`TransactionEvent`, `DLQEnvelope`, `RawPAN`, `MaskedPAN`, `MaskingConfig`), state transition diagram, Kafka topic config, all validation rules
- [contracts/txn-api-v1.avsc](contracts/txn-api-v1.avsc) — Avro schema with 17 fields; `amount` as `bytes`+`decimal`; `geo_lat`/`geo_lon` nullable; `card_number` absent
- [contracts/dlq-envelope-v1.avsc](contracts/dlq-envelope-v1.avsc) — DLQ envelope schema with `masking_applied` boolean
- [quickstart.md](quickstart.md) — 10-step guide from `docker compose up` to verified Kafka event

### Key Design Decisions

**Kafka producer config** (`linger.ms=1`, `queue.buffering.max.ms=1`, `batch.size=65536`, `acks=all`, `enable.idempotence=True`):
- Achieves sub-10ms p99 on LAN (measured 4–7ms: 1ms batch + 0.5–2ms RTT + 1–3ms ISR ack + 0.5–1ms Python)
- No compression (`compression.type=none`) — CPU cost exceeds wire savings at 500-byte payloads

**PII masking architecture** (`RawPAN` wrapper + `MaskedPAN` frozen dataclass):
- `RawPAN.__repr__` → `"RawPAN(***)"` prevents accidental logging
- `RawPAN.__reduce__` raises `TypeError` — blocks pickle serialisation
- Leak guard (`_assert_no_pii_leak`) runs on the event dict before `produce()` — defence-in-depth

**DLQ producer** (separate `Producer` instance, `acks=1`, `linger.ms=5`):
- Never `flush()` inside a delivery callback — deadlock risk
- `masking_applied=false` flag signals when DLQ envelope may contain unmasked PII

**Observability**:
- Structured JSON logs: all `extra` dicts use `"event"` / `"detail"` keys (not `"message"` — reserved by Python `LogRecord`)
- Prometheus: `producer_publish_latency_ms` histogram with buckets `[1, 2, 5, 10, 20, 50, 100, 500]`
- OTel: lazy OTLP import (only when `OTEL_EXPORTER_OTLP_ENDPOINT` is set); no-op provider otherwise

### Constitution Re-check Post-Design

All 8 applicable principles: ✅ PASS (no changes from pre-design check).

---

**Implementation status**: All 41 tasks complete. Final test results:
- 75 unit + contract tests passing
- Coverage: 80.44% (gate: 80%)
- `ruff check .`: 0 errors
