# Feature Specification: Kafka Ingestion Pipeline — API Channel

**Feature Branch**: `001-kafka-ingestion-pipeline`
**Created**: 2026-03-30
**Status**: Draft
**Channel**: API (`txn.api`)

## User Scenarios & Testing *(mandatory)*

### User Story 1 — API client successfully submits a transaction event (Priority: P1)

An API caller submits a payment transaction via the REST/gRPC endpoint. The producer validates the payload against the Avro schema, applies PII masking, and publishes the event to the `txn.api` Kafka topic in under 10ms. The caller receives an acknowledgment with the `transaction_id`.

**Why this priority**: Core capability — without it the entire downstream pipeline has no data.

**Independent Test**: Send a single well-formed API transaction payload, verify it appears in `txn.api` with masked PII and a valid schema, and that producer latency < 10ms.

**Acceptance Scenarios**:

1. **Given** a valid transaction payload with `api_key_id`, `oauth_scope`, `caller_ip_subnet`, and all base fields, **When** the producer publishes to Kafka, **Then** the message is visible in `txn.api` within 10ms, conforms to the registered Avro schema, and `card_number` / `ip_address` are masked.
2. **Given** a valid payload, **When** the event reaches the topic, **Then** `card_last4` and `card_bin` are populated; full PAN is absent from the record.
3. **Given** a Kafka broker is temporarily unavailable, **When** the producer receives no ack, **Then** the event is routed to `txn.api.dlq` and an alert fires within 60 seconds.

---

### User Story 2 — Schema-invalid or malformed payloads are rejected at the edge (Priority: P1)

A caller submits a payload missing required fields or with an unregistered schema version. The producer must reject the event before it reaches the topic, return a structured error, and emit a metric increment.

**Why this priority**: Schema contract enforcement is NON-NEGOTIABLE per the constitution; invalid events must never pollute topics.

**Independent Test**: Submit a payload with a missing `transaction_id` and one with an unregistered schema; verify both are rejected with structured errors and that `schema_validation_errors_total` metric increments.

**Acceptance Scenarios**:

1. **Given** a payload missing `transaction_id`, **When** the producer validates it, **Then** a 400 error is returned with field-level detail and no message is published.
2. **Given** a payload referencing an unregistered schema version, **When** the producer checks Schema Registry, **Then** the event is rejected and `schema_validation_errors_total` counter increments.

---

### User Story 3 — PII masking is enforced before any event leaves the producer (Priority: P1)

Card numbers and IP addresses are masked in the shared PII masking library before the serialized event is handed to the Kafka client.

**Why this priority**: PII minimization at the edge is NON-NEGOTIABLE per the constitution; full PAN and full IP must never appear in any topic.

**Independent Test**: Submit a payload containing a full card number and full IP; inspect the raw Kafka message bytes and confirm full PAN and full IP are absent.

**Acceptance Scenarios**:

1. **Given** a payload with a 16-digit PAN, **When** the PII masking library processes it, **Then** the stored fields are `card_bin` (first 6) and `card_last4` only; no other card field is present.
2. **Given** a payload with a full IPv4 address, **When** masking runs, **Then** only the `/24` subnet is stored (e.g., `203.0.113.0/24`).

---

### User Story 4 — Idempotent producer prevents duplicate events on retry (Priority: P2)

When the Kafka producer retries a publish after a transient failure, the same transaction event must not be written twice to `txn.api`.

**Why this priority**: Idempotency is a pre-production non-negotiable; duplicates cause double-block decisions.

**Independent Test**: Simulate a producer retry by forcing a transient broker error; verify `txn.api` contains exactly one copy of the event identified by `transaction_id`.

**Acceptance Scenarios**:

1. **Given** a publish attempt that times out and is retried, **When** the broker recovers, **Then** exactly one record with that `transaction_id` exists in `txn.api`.

---

### User Story 5 — Dead letter queue captures unprocessable events (Priority: P2)

Any event that cannot be published to `txn.api` (serialization error, repeated broker failure) is routed to `txn.api.dlq` and triggers an alert.

**Why this priority**: Silent drops are forbidden; DLQ depth > 0 must page on-call within 60 seconds.

**Independent Test**: Force a serialization failure; confirm the event appears in `txn.api.dlq` and a Prometheus alert fires within 60 seconds.

**Acceptance Scenarios**:

1. **Given** a serialization error, **When** the producer cannot publish, **Then** the raw payload is written to `txn.api.dlq` with an `error_type` header.
2. **Given** `txn.api.dlq` depth > 0, **When** 60 seconds elapse, **Then** a Prometheus alert fires and the on-call is notified.

---

### Edge Cases

- What happens when Schema Registry is unreachable at producer startup?
- How does the producer handle a `transaction_id` collision (duplicate submission)?
- What if `oauth_scope` is present but does not include the required transaction-write scope?
- How are Kafka topic partitions assigned — by `account_id` hash or round-robin?
- What is the producer behavior during a Kafka leader election?

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The producer MUST publish validated transaction events to the `txn.api` Kafka topic within 10ms of receipt (p99).
- **FR-002**: The producer MUST validate every event against the registered Avro schema in Schema Registry before publishing; events failing validation MUST be rejected and never reach the topic.
- **FR-003**: The PII masking library MUST strip the full PAN (retain `card_bin` + `card_last4`) and truncate IP to `/24` subnet before serialisation.
- **FR-004**: The producer MUST be idempotent — retries on the same `transaction_id` MUST NOT produce duplicate records in `txn.api`.
- **FR-005**: Unprocessable events MUST be routed to `txn.api.dlq` with an `error_type` header.
- **FR-006**: The producer MUST emit structured JSON logs with fields: `transaction_id`, `component`, `timestamp`, `level`, `message`.
- **FR-007**: The producer MUST expose Prometheus metrics: `producer_publish_latency_ms` (histogram), `producer_events_total` (counter), `producer_errors_total` (counter), `dlq_depth` (gauge).
- **FR-008**: The producer MUST include an OpenTelemetry trace span covering the full ingestion path (receive → mask → serialize → publish).
- **FR-009**: The `txn.api` topic MUST use Avro or Protobuf schemas; JSON payloads are forbidden in production topics.
- **FR-010**: API channel-specific fields (`api_key_id`, `caller_ip_subnet`, `oauth_scope`) MUST be present in the schema and validated as non-nullable with documented justification.
- **FR-011**: Topic partitioning MUST be deterministic by `account_id` to enable downstream stateful aggregations.
- **FR-012**: Schema Registry MUST be reachable at producer startup; startup MUST fail fast if unreachable rather than defaulting to schema-less mode.

### Key Entities

- **TransactionEvent (API)**: Base transaction fields + `api_key_id`, `caller_ip_subnet` (masked /24), `oauth_scope`. Immutable after publication.
- **PIIMasker**: Shared library encapsulating masking logic; versioned and independently testable.
- **SchemaRegistryClient**: Wrapper around Schema Registry for schema fetch, cache, and validation.
- **DeadLetterProducer**: Secondary Kafka producer writing to `txn.api.dlq` with error metadata headers.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Producer publish latency p99 < 10ms under 2× expected peak load (load test required pre-production).
- **SC-002**: Zero full PANs or full IP addresses present in `txn.api` topic — verified by automated integration test scanning raw bytes.
- **SC-003**: 100% of schema-invalid payloads rejected at producer boundary — verified by contract test suite.
- **SC-004**: DLQ alert fires within 60 seconds of first unprocessable event — verified by chaos test.
- **SC-005**: Idempotent producer: 0 duplicate `transaction_id` records in `txn.api` under simulated retry conditions.
- **SC-006**: Unit test coverage ≥ 80% across all producer and PII masking components.
- **SC-007**: OpenTelemetry trace present and complete for every event published successfully.

## Assumptions

- Schema Registry is available at a known endpoint in both local (Docker) and cloud environments.
- The base transaction schema (defined in the constitution) is already registered or will be registered as part of this feature.
- OAuth scope validation is the caller's responsibility upstream; the producer records `oauth_scope` as-is but does not enforce authorization decisions.
- Partition count for `txn.api` is pre-configured by infrastructure; the producer uses `account_id` as the partition key.
- The PII masking library is a new shared library created as part of this feature (no existing one exists).
- Load target: up to 10,000 API transactions per second at peak.
- Language choice for the producer is Python 3.11 with `confluent-kafka-python` (aligns with ML/data team tooling) — to be confirmed in research phase.
