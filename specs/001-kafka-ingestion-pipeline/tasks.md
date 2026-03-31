# Tasks: Kafka Ingestion Pipeline — API Channel

**Input**: Design documents from `/specs/001-kafka-ingestion-pipeline/`
**Branch**: `001-kafka-ingestion-pipeline` | **Date**: 2026-03-30

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story. No test tasks are generated (not requested in spec).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no shared dependencies)
- **[Story]**: Which user story this task belongs to (US1–US5)

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project scaffolding, tooling, and local stack — no business logic yet.

- [x] T001 Create full directory structure: `pipelines/ingestion/api/`, `pipelines/ingestion/shared/pii_masker/`, `pipelines/ingestion/schemas/`, `tests/unit/`, `tests/integration/`, `tests/contract/`, `infra/kafka/`, `monitoring/dashboards/`, `monitoring/alerts/`
- [x] T002 Create `pyproject.toml` at repo root with Python 3.11, dependencies: `confluent-kafka>=2.3`, `fastavro>=1.9`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-grpc`, `prometheus-client`, dev extras: `pytest`, `pytest-cov`, `testcontainers[kafka]`, `hypothesis`, `ruff`
- [x] T003 [P] Create `ruff.toml` at repo root: `line-length = 100`, `target-version = "py311"`, select `["E","F","I","UP"]`
- [x] T004 [P] Create `infra/docker-compose.yml` with services: `broker` (Kafka KRaft, port 9092/29092), `schema-registry` (Confluent, port 8081), `prometheus` (port 9090), `grafana` (port 3000); include healthchecks for all services
- [x] T005 [P] Copy Avro schemas from `specs/001-kafka-ingestion-pipeline/contracts/` to `pipelines/ingestion/schemas/`: `txn_api_v1.avsc`, `dlq_envelope_v1.avsc`
- [x] T006 [P] Create `infra/kafka/topics.sh`: creates `txn.api` (12 partitions, RF=3, retention 220752000000ms, `min.insync.replicas=2`) and `txn.api.dlq` (3 partitions, RF=3, retention 604800000ms)

**Checkpoint**: `docker compose up -d` succeeds; `curl http://localhost:8081/subjects` returns `[]`; topic script runs without error.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core infrastructure shared by all user stories — Schema Registry client, producer config, observability scaffolding.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [x] T007 Implement `SchemaRegistryClient` wrapper in `pipelines/ingestion/shared/schema_registry.py`: wraps `confluent_kafka.schema_registry.SchemaRegistryClient`; adds `connect_with_retry(url, retries=5, base_delay=1.0)` with exponential backoff (1s/2s/4s/8s/16s then fatal `RuntimeError`); caches schema ID after first fetch; exposes `get_serializer(subject, schema_str) -> AvroSerializer` with `auto.register.schemas=False`
- [x] T008 Implement `ProducerConfig` dataclass in `pipelines/ingestion/api/config.py`: reads from env vars (`KAFKA_BOOTSTRAP_SERVERS`, `SCHEMA_REGISTRY_URL`, `KAFKA_ACKS=all`, `KAFKA_LINGER_MS=1`, `SCHEMA_REGISTRY_RETRIES=5`, `MASKING_IPV4_PREFIX=24`, `MASKING_IPV6_PREFIX=64`, `PROMETHEUS_PORT=8001`, `LOG_LEVEL=INFO`); builds librdkafka config dict with `enable.idempotence=True`, `acks=all`, `linger.ms=1`, `queue.buffering.max.ms=1`, `batch.size=65536`, `max.in.flight.requests.per.connection=5`, `retries=2147483647`, `delivery.timeout.ms=5000`, `compression.type=none`
- [x] T009 [P] Set up Prometheus metrics registry in `pipelines/ingestion/api/metrics.py`: define `PUBLISH_LATENCY = Histogram("producer_publish_latency_ms", ..., buckets=[1,2,5,10,20,50])`, `EVENTS_TOTAL = Counter("producer_events_total", ..., labelnames=["topic","status"])`, `ERRORS_TOTAL = Counter("producer_errors_total", ..., labelnames=["topic","error_type"])`, `DLQ_DEPTH = Gauge("dlq_depth", ..., labelnames=["topic"])`; start HTTP server on `PROMETHEUS_PORT` at module import
- [x] T010 [P] Set up OpenTelemetry tracer in `pipelines/ingestion/api/telemetry.py`: initialize `TracerProvider` with OTLP exporter (endpoint from `OTEL_EXPORTER_OTLP_ENDPOINT` env var, optional); export `get_tracer() -> Tracer` returning a no-op tracer if endpoint not configured; span name convention: `"api.producer.publish"`

**Checkpoint**: `python -c "from pipelines.ingestion.api.config import ProducerConfig; print(ProducerConfig())"` succeeds; `from pipelines.ingestion.api.metrics import PUBLISH_LATENCY` imports without error.

---

## Phase 3: User Story 1 — API Client Submits Transaction (Priority: P1) 🎯 MVP

**Goal**: A valid API transaction payload is received, serialized to Avro against the registered schema, and published to `txn.api` within 10ms p99. Caller receives `transaction_id` + latency acknowledgment.

**Independent Test**: `curl -X POST http://localhost:8080/v1/transactions` with a valid payload → event appears in `txn.api` with correct Avro schema, `channel="API"`, all fields present.

- [x] T011 [P] [US1] Create `MaskedPAN` frozen dataclass in `pipelines/ingestion/shared/pii_masker/masker.py`: fields `bin6: str`, `last4: str`, `length: int`; `display()` method returning `"{bin6}{'*' * (length-10)}{last4}"`; `__repr__` must NOT expose middle digits
- [x] T012 [P] [US1] Create `MaskingConfig` frozen dataclass in `pipelines/ingestion/shared/pii_masker/config.py`: fields `ipv4_prefix: int = 24`, `ipv6_prefix: int = 64`, `reject_invalid_pan: bool = True`; import and expose from `pipelines/ingestion/shared/pii_masker/__init__.py`
- [x] T013 [US1] Implement `_luhn_valid(digits: str) -> bool` and `extract_pan_parts(pan: str) -> tuple[str, str]` in `pipelines/ingestion/shared/pii_masker/validators.py`: normalise separators (`[\s\-]`), validate Luhn before extraction, raise `InvalidPANError(ValueError)` on failure; handle 15 and 16-digit PANs
- [x] T014 [US1] Implement `RawPAN` class in `pipelines/ingestion/shared/pii_masker/masker.py` (depends on T013): `__slots__ = ("_digits",)`, `__repr__` returns `"RawPAN(***)"`, `__str__` returns `"***"`, `__reduce__` raises `TypeError("RawPAN must not be pickled")`; `.mask(cfg) -> MaskedPAN` calls `extract_pan_parts` internally
- [x] T015 [P] [US1] Implement `truncate_ip(ip: str, cfg: MaskingConfig) -> str` in `pipelines/ingestion/shared/pii_masker/masker.py`: use `ipaddress.ip_network(f"{ip}/{prefix}", strict=False).network_address`; IPv4 → `/24`, IPv6 → `/64`; raise `ValueError` for multicast; loopback/private truncated normally
- [x] T016 [US1] Implement `TransactionEventBuilder.build(raw_payload: dict, cfg: MaskingConfig) -> dict` in `pipelines/ingestion/api/producer.py`: creates `RawPAN` from `card_number`, calls `.mask()`, calls `truncate_ip()`, stamps `processing_time`, `channel="API"`, `masking_lib_version`, `schema_version="1"`; returns dict matching `txn-api-v1.avsc` field names; raises `MaskingError` on failure
- [x] T017 [US1] Implement `ProducerService.__init__` and `ProducerService.start()` in `pipelines/ingestion/api/producer.py`: calls `connect_with_retry()`, registers schema with `get_serializer("txn.api-value", schema_str)`, creates `SerializingProducer` with config from `ProducerConfig`, starts Prometheus HTTP server, initializes OTel tracer
- [x] T018 [US1] Implement `ProducerService.publish(raw_payload: dict) -> PublishResult` in `pipelines/ingestion/api/producer.py`: wraps full flow in OTel span `"api.producer.publish"`; records `PUBLISH_LATENCY`; increments `EVENTS_TOTAL`; returns `{"transaction_id": ..., "status": "accepted", "latency_ms": ...}`
- [x] T019 [US1] Implement HTTP entrypoint in `pipelines/ingestion/api/producer.py`: `POST /v1/transactions` handler using stdlib `http.server` or `wsgiref`; parse JSON body; call `ProducerService.publish()`; return JSON response; emit structured log on each request: `{"transaction_id": ..., "component": "api-producer", "timestamp": ..., "level": "INFO", "message": "published"}`
- [x] T020 [US1] Add `__init__.py` files for all packages: `pipelines/__init__.py`, `pipelines/ingestion/__init__.py`, `pipelines/ingestion/api/__init__.py`, `pipelines/ingestion/shared/__init__.py`, `pipelines/ingestion/shared/pii_masker/__init__.py`; export public API from each

**Checkpoint**: Follow quickstart.md steps 1–6. `curl POST /v1/transactions` with valid payload → 200 response with `transaction_id`. `kafka-avro-console-consumer` on `txn.api` shows event with `card_bin`, `card_last4`, `caller_ip_subnet`.

---

## Phase 4: User Story 2 — Schema-Invalid Payloads Rejected (Priority: P1)

**Goal**: Missing required fields or unregistered schema version → structured 400 error; `schema_validation_errors_total` counter increments; no message published to `txn.api`.

**Independent Test**: POST with missing `transaction_id` → 400 with field-level error detail; `schema_validation_errors_total` metric increments; `kafka-console-consumer` on `txn.api` shows no new messages.

- [x] T021 [US2] Implement `validate_required_fields(payload: dict) -> list[str]` in `pipelines/ingestion/api/producer.py`: checks all non-nullable fields from `txn-api-v1.avsc` are present and non-empty; returns list of missing field names; raises `ValidationError` if list non-empty
- [x] T022 [US2] Implement `validate_field_values(payload: dict) -> list[str]` in `pipelines/ingestion/api/producer.py`: `amount > 0`, `currency` is 3-char string, `event_time` within ±5 minutes of `processing_time`, `channel` in `{"POS","WEB","MOBILE","API"}`, `api_key_id` non-empty, `oauth_scope` non-empty; returns list of validation error messages
- [x] T023 [US2] Wire validation into HTTP handler in `pipelines/ingestion/api/producer.py` (depends on T021, T022): call `validate_required_fields` then `validate_field_values` before `TransactionEventBuilder.build()`; on failure return `{"error": "ValidationError", "fields": [...]}` with HTTP 400; increment `ERRORS_TOTAL` counter with `error_type="ValidationError"` and `schema_validation_errors_total` counter
- [x] T024 [US2] Add `schema_validation_errors_total` Counter to `pipelines/ingestion/api/metrics.py` with `labelnames=["topic","error_type"]`; emit structured error log `{"level": "WARNING", "message": "validation_failed", "fields": [...], ...}`

**Checkpoint**: POST with missing `transaction_id` → 400 `{"error": "ValidationError", "fields": ["transaction_id"]}`. POST with `amount: -1` → 400. Valid payload still publishes successfully. `curl http://localhost:8001/metrics | grep schema_validation_errors_total` shows incremented counter.

---

## Phase 5: User Story 3 — PII Masking Enforced at Producer Boundary (Priority: P1)

**Goal**: Full PAN and full IP are absent from every raw byte in `txn.api`. Verified by integration test scanning raw message bytes.

**Independent Test**: Submit payload with full 16-digit PAN and full IPv4; consume raw `msg.value()` bytes from `txn.api`; assert no 15/16-digit sequence and no untruncated IP present.

- [x] T025 [US3] Ensure `RawPAN` is created and discarded entirely within `TransactionEventBuilder.build()` in `pipelines/ingestion/api/producer.py`: the `dict` returned by `build()` must contain only `card_bin` and `card_last4`; the key `card_number` must not appear in the returned dict; add an assertion to enforce this
- [x] T026 [US3] Add PII leak guard assertion in `ProducerService.publish()` in `pipelines/ingestion/api/producer.py`: after `TransactionEventBuilder.build()` and before `producer.produce()`, assert that `"card_number"` key is absent from the event dict and that no value in the dict matches `re.fullmatch(r"\d{15,19}", str(v))`; raise `MaskingError` on breach
- [x] T027 [US3] Write integration test `test_no_pii_in_raw_bytes` in `tests/integration/test_api_ingestion.py`: use `testcontainers` Kafka + Schema Registry; publish 10 transactions with real Luhn-valid PANs and real IPv4; consume `msg.value()` bytes; assert no `\d{16}` or `\d{15}` sequence (word-boundary regex); assert all `caller_ip_subnet` values end in `.0`
- [x] T028 [P] [US3] Write Hypothesis property tests in `tests/unit/test_pii_masker.py`: (a) `masked.display()` never contains middle digits of original PAN; (b) `bin6` always equals first 6 of normalised digits; (c) `last4` always equals last 4; (d) garbage input never silently returns a masked form; (e) `truncate_ip` output for IPv4 always ends in `.0`

**Checkpoint**: `pytest tests/unit/test_pii_masker.py -v` all pass. `pytest tests/integration/test_api_ingestion.py::test_no_pii_in_raw_bytes -v` passes. Grep Kafka topic raw dump for any 16-digit run → zero matches.

---

## Phase 6: User Story 4 — Idempotent Producer (Priority: P2)

**Goal**: Kafka producer retries on transient failure do not produce duplicate records in `txn.api` for the same `transaction_id`.

**Independent Test**: Force a transient broker error mid-publish; verify `txn.api` contains exactly one record with that `transaction_id` after recovery.

- [x] T029 [US4] Verify `enable.idempotence=True`, `max.in.flight.requests.per.connection=5`, `retries=2147483647` are set in `ProducerConfig` in `pipelines/ingestion/api/config.py`; add an integration assertion that `producer.list_topics()` succeeds only after idempotent session is established
- [x] T030 [US4] Write integration test `test_idempotent_no_duplicate_on_retry` in `tests/integration/test_api_ingestion.py`: use `testcontainers`; publish same `transaction_id` twice (simulating retry by calling `publish()` twice with identical payload); consume all messages from `txn.api` for that partition; assert exactly one record with that `transaction_id`; note that this tests application-layer idempotency guarding (broker-level dedup is validated by `enable.idempotence` config only)

**Checkpoint**: `pytest tests/integration/test_api_ingestion.py::test_idempotent_no_duplicate_on_retry -v` passes.

---

## Phase 7: User Story 5 — Dead Letter Queue (Priority: P2)

**Goal**: Events that cannot be published are routed to `txn.api.dlq` with `error_reason` header; `dlq_depth > 0` alert fires within 60s.

**Independent Test**: Force serialisation error (invalid PAN that passes request validation but fails masking); verify `txn.api.dlq` receives a message with `error_type="MaskingError"`; verify `dlq_depth` gauge > 0.

- [x] T031 [US5] Implement `DLQProducer` in `pipelines/ingestion/shared/dlq_producer.py`: separate `Producer` instance (not `SerializingProducer`); config: `acks=1`, `linger.ms=5`; exposes `send_to_dlq(source_topic, original_payload, error_type, error_message, masking_applied: bool)` which serialises `DLQEnvelope` dict to JSON and calls `producer.produce(dlq_topic, value=..., headers={"error_type": error_type})`; calls `producer.poll(0)` after; never calls `flush()` inside a callback
- [x] T032 [US5] Wire inline DLQ routing for pre-serialisation errors in `pipelines/ingestion/api/producer.py` (depends on T031): catch `MaskingError`, `InvalidPANError`, and `SerializationError` in `ProducerService.publish()`; call `dlq_producer.send_to_dlq(..., masking_applied=False/True)` based on where failure occurred; increment `ERRORS_TOTAL` and `DLQ_DEPTH`; re-raise as HTTP 500 to caller
- [x] T033 [US5] Wire async DLQ routing in `delivery_callback` in `pipelines/ingestion/api/producer.py`: if `err is not None`, call `dlq_producer.send_to_dlq(source_topic=msg.topic(), ..., error_type="BrokerError", masking_applied=True)`; increment `DLQ_DEPTH` gauge; emit structured error log; never call `flush()` inside the callback
- [x] T034 [P] [US5] Create Prometheus alert rule in `monitoring/alerts/api-ingestion.yml`: alert `DLQDepthNonZero` — `expr: dlq_depth{topic="txn.api.dlq"} > 0`, `for: 0m` (fire immediately), `labels: {severity: page}`, `annotations: {summary: "txn.api.dlq has unprocessable events"}`. Alert `ProducerLatencyHigh` — `expr: histogram_quantile(0.99, producer_publish_latency_ms_bucket{topic="txn.api"}) > 10`, `for: 1m`
- [x] T035 [US5] Write integration test `test_dlq_receives_masking_error` in `tests/integration/test_api_ingestion.py`: submit payload with syntactically valid `card_number` that fails Luhn check; verify `txn.api` has no new message; verify `txn.api.dlq` receives a message with `error_type` header = `"InvalidPANError"`; verify `dlq_depth` gauge incremented

**Checkpoint**: DLQ flow end-to-end: invalid PAN → `txn.api.dlq` message visible. `curl http://localhost:8001/metrics | grep dlq_depth` shows `dlq_depth{topic="txn.api.dlq"} 1`.

---

## Phase 8: Polish & Cross-Cutting Concerns

**Purpose**: Observability completeness, coverage gate, and final validation.

- [x] T036 [P] Complete OpenTelemetry span wiring in `pipelines/ingestion/api/producer.py`: span `"api.producer.publish"` must span the full flow (receive → mask → serialize → produce); add span attributes: `transaction_id`, `channel`, `topic`, `schema_version`; add span event on DLQ routing: `"dlq.routed"` with `error_type` attribute
- [x] T037 Audit all log statements across `pipelines/ingestion/` to ensure every `logging.info/warning/error` call includes mandatory structured fields: `transaction_id`, `component`, `timestamp`, `level`, `message`; use a `structlog` formatter or a custom `logging.Formatter` that enforces JSON output with these keys
- [x] T038 [P] Create Grafana dashboard JSON in `monitoring/dashboards/api-ingestion.json`: panels for (a) `producer_publish_latency_ms` p50/p95/p99, (b) `producer_events_total` rate, (c) `producer_errors_total` rate by `error_type`, (d) `dlq_depth` by topic; import via `monitoring/dashboards/import.sh`
- [x] T039 Run coverage gate: `pytest tests/unit/ tests/contract/ --cov=pipelines/ingestion --cov-report=term-missing --cov-fail-under=80`; fix any gaps to reach 80% minimum
- [x] T040 [P] Run `ruff check pipelines/ tests/` and fix all lint errors; run `ruff format pipelines/ tests/ --check`
- [x] T041 [P] End-to-end validation: follow all 10 steps in `specs/001-kafka-ingestion-pipeline/quickstart.md`; confirm each expected output matches

**Checkpoint**: `pytest --cov --cov-fail-under=80` passes. `ruff check .` clean. Quickstart steps 1–10 all produce expected output. Prometheus alert rules load without error (`promtool check rules monitoring/alerts/api-ingestion.yml`).

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — can start immediately
- **Foundational (Phase 2)**: Depends on Phase 1 completion — **BLOCKS all user stories**
- **US1 Phase 3**: Depends on Phase 2 — core producer pipeline
- **US2 Phase 4**: Depends on Phase 3 (needs `ProducerService` and HTTP handler from US1)
- **US3 Phase 5**: Depends on Phase 3 (masking is already wired; this phase adds the guard assertion and tests)
- **US4 Phase 6**: Depends on Phase 2 (`ProducerConfig` must exist); independent of US2/US3
- **US5 Phase 7**: Depends on Phase 3 (`ProducerService.publish()` must exist for DLQ wiring)
- **Polish (Phase 8)**: Depends on all prior phases

### User Story Dependencies

- **US1 (P1)**: Blocks US2, US3, US5 (they all extend `ProducerService`)
- **US2 (P1)**: Depends on US1; independent of US3
- **US3 (P1)**: Depends on US1; independent of US2 (different concerns: validation vs PII guard)
- **US4 (P2)**: Depends on Phase 2 only; can be done in parallel with US1–US3
- **US5 (P2)**: Depends on US1; can be done after US1 independently of US2/US3

### Within Each User Story

- Models/dataclasses before services (e.g., T011+T012 before T013)
- Config/infrastructure before business logic (T007+T008 before T017)
- Business logic before HTTP entrypoint (T016+T018 before T019)
- Implementation before integration test verification

### Parallel Opportunities

- T003, T004, T005, T006 — all Phase 1 setup, different files
- T009, T010 — metrics and telemetry scaffolding, independent
- T011, T012, T015 — PII masker dataclasses and IP masking, independent files
- T021, T022 — validation functions, can be written in parallel
- T028 — Hypothesis unit tests, independent of integration work
- T031 — DLQ producer, independent of main producer wiring
- T034 — alert rules file, independent of application code
- T036, T037, T038, T040, T041 — polish tasks, all different concerns

---

## Parallel Examples

### Phase 1 — All setup tasks in parallel

```
Task T003: Create ruff.toml
Task T004: Create docker-compose.yml
Task T005: Copy Avro schemas to pipelines/ingestion/schemas/
Task T006: Create infra/kafka/topics.sh
```

### Phase 2 — Metrics + telemetry in parallel with schema client

```
Task T007: SchemaRegistryClient wrapper
Task T009: Prometheus metrics registry    ← parallel
Task T010: OpenTelemetry tracer           ← parallel
```

### Phase 3 (US1) — PII masker components in parallel

```
Task T011: MaskedPAN dataclass
Task T012: MaskingConfig dataclass         ← parallel
Task T015: truncate_ip() function          ← parallel
```

### US4 — Can run alongside US2 and US3

```
Developer A: US2 (validation rejection)
Developer B: US4 (idempotency config + test)   ← parallel
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup
2. Complete Phase 2: Foundational (CRITICAL — blocks all stories)
3. Complete Phase 3: User Story 1 (T011–T020)
4. **STOP and VALIDATE**: `curl POST /v1/transactions` → event in `txn.api` with masked PII
5. Demo or deploy the MVP

### Incremental Delivery

1. Setup + Foundational → foundation ready (T001–T010)
2. US1 → happy-path producer working → **MVP**
3. US2 → add rejection boundary → production-safe validation
4. US3 → add PII guard assertion + property tests → audit-ready
5. US4 → idempotency config + test → retry-safe
6. US5 → DLQ + alerts → ops-ready
7. Polish → coverage gate, dashboards, full quickstart validation

### Single-Developer Sequence

```
T001 → T002 → T003,T004,T005,T006 (parallel) →
T007 → T008 → T009,T010 (parallel) →
T011,T012,T015 (parallel) → T013 → T014 → T016 → T017 → T018 → T019 → T020 →
T021,T022 (parallel) → T023 → T024 →
T025 → T026 → T027 → T028 →
T029 → T030 →
T031 → T032 → T033 → T034,T035 (parallel) →
T036,T037,T038,T040,T041 (parallel) → T039
```

---

## Notes

- `[P]` tasks operate on different files with no shared incomplete dependencies
- `[Story]` label maps each task to its user story for traceability
- Each user story phase ends with an explicit **Checkpoint** that verifies independent functionality
- Run `ruff check .` and `pytest` after each logical group before moving to the next task
- Commit after each checkpoint, not after every individual task
- The DLQ producer (`T031`) must always be a **separate instance** from the main producer — never reuse the same `Producer` for DLQ writes
