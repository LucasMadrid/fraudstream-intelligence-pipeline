# Tasks: Stateful Stream Processor — Transaction Feature Enrichment

**Input**: Design documents from `/specs/002-flink-stream-processor/`
**Branch**: `002-flink-stream-processor` | **Date**: 2026-03-31
**Prerequisites**: plan.md ✅, spec.md ✅, research.md ✅, data-model.md ✅, contracts/ ✅, quickstart.md ✅

**Tests**: Included — Constitution §VIII mandates 80% unit test coverage (non-negotiable gate). Property-based tests (Hypothesis) required for velocity window invariants per plan.md.

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1–US4)
- Exact file paths are included in all descriptions

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project scaffolding, infrastructure additions, and dependency registration. No business logic — purely structural.

- [X] T001 Create Python package skeleton: `pipelines/processing/__init__.py`, `pipelines/processing/operators/__init__.py`, `pipelines/processing/schemas/`, `pipelines/processing/shared/` (empty `__init__.py` files)
- [X] T002 [P] Add `[processing]` extras to `pyproject.toml`: `apache-flink>=1.19`, `confluent-kafka[schema-registry]>=2.3`, `fastavro>=1.9`, `geoip2>=4.8`, `prometheus-client>=0.20`, `opentelemetry-sdk>=1.24`, `testcontainers[kafka]>=4`, `hypothesis>=6`, `maxminddb-c`
- [X] T003 [P] Create `infra/flink/flink-conf.yaml` with mandatory settings: `python.fn-execution.bundle.size: 1000`, `python.fn-execution.bundle.time: 15`, `state.backend: rocksdb`, `state.backend.incremental: true`, `state.checkpoints.num-retained: 3`, `state.backend.rocksdb.memory.managed: true`, `state.backend.rocksdb.memory.fixed-per-slot: 512mb`, `state.backend.rocksdb.timer-service.factory: ROCKSDB`, `state.checkpoints.dir: s3://flink-checkpoints/002-stream-processor`, `taskmanager.memory.process.size: 4g`
- [X] T004 [P] Update `infra/docker-compose.yml` to add `flink-jobmanager` (port 8082), `flink-taskmanager`, and `minio` (ports 9000/9001) services with correct volume mounts for GeoIP DB and Flink config
- [X] T005 [P] Create `infra/geoip/.gitkeep` and add `infra/geoip/GeoLite2-City.mmdb` to `.gitignore`
- [X] T006 [P] Copy `specs/002-flink-stream-processor/contracts/enriched-txn-v1.avsc` and `processing-dlq-v1.avsc` into `pipelines/processing/schemas/`

**Checkpoint**: Infrastructure in place — dependencies installable, Docker stack launchable, package importable.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core shared components that ALL user stories depend on. No story work begins until this phase is complete.

**⚠️ CRITICAL**: No user story implementation can start until this phase is complete.

- [X] T007 Implement `ProcessorConfig` dataclass in `pipelines/processing/config.py`: `kafka_brokers`, `schema_registry_url`, `input_topic` (default `txn.api`), `output_topic` (default `txn.enriched`), `dlq_topic` (default `txn.processing.dlq`), `checkpoint_dir`, `checkpoint_interval_ms` (default 60000), `geoip_db_path`, `parallelism` (default 1), `watermark_ooo_seconds` (default 10), `allowed_lateness_seconds` (default 30) — all env-var driven via `os.environ.get`
- [X] T008 [P] Implement `DLQSink` in `pipelines/processing/shared/dlq_sink.py`: serialises `ProcessingDLQEnvelope` (from `processing-dlq-v1.avsc`) to Avro bytes with Schema Registry prefix, routes to `txn.processing.dlq`; fields: `dlq_id` (UUID v4), `source_topic`, `source_partition`, `source_offset`, `transaction_id` (nullable), `original_payload_bytes`, `error_type`, `error_message`, `watermark_at_rejection` (nullable), `event_time_at_rejection` (nullable), `failed_at`, `processor_host`, `processor_subtask_index`
- [X] T009 [P] Implement metrics bridge in `pipelines/processing/metrics.py`: Prometheus `Histogram` for `enrichment_latency_ms` (buckets: 1,5,10,20,50,100ms), `Counter` for `dlq_events_total` (labelled by `error_type`), `Gauge` for `consumer_lag_records`, `Gauge` for `last_checkpoint_duration_ms`
- [X] T010 [P] Implement OTel trace propagation in `pipelines/processing/telemetry.py`: extract W3C `traceparent` from Kafka record headers on input, inject into enriched record headers on output; no-op if header absent (trace context is best-effort)
- [X] T011 Implement Avro deserializer for `txn_api_v1` with Schema Registry validation in `pipelines/processing/shared/avro_serde.py`: deserialises Confluent wire-format bytes (magic byte + schema ID + Avro payload) into a `RawTransaction` named-tuple; raises `SchemaValidationError` on deserialization failure (to be routed to DLQ by caller)
- [X] T012 Create integration test fixtures in `tests/integration/conftest.py`: `kafka_container` fixture (testcontainers Kafka), `flink_minicluster` fixture (PyFlink `MiniClusterWithClientResource`), `schema_registry_mock` fixture (register both schemas); `pytest.ini` / `pyproject.toml` marker `integration` for opt-in execution
- [X] T046 [P] Configure structured JSON logging in `pipelines/processing/logging_config.py`: `logging.config.dictConfig` with a JSON formatter that emits minimum fields `transaction_id`, `component`, `timestamp`, `level`, `message` per Constitution §VIII; expose `configure_logging()` function called from `pipelines/processing/job.py` main entry point; `transaction_id` injected via a logging `Filter` that reads from thread-local context set by each operator

**Checkpoint**: Foundation complete — config, DLQ sink, metrics, Avro serde, structured logging, and test fixtures ready. User story implementation can now begin.

---

## Phase 3: User Story 1 — Real-Time Transaction Enrichment (Priority: P1) 🎯 MVP

**Goal**: A single transaction flows end-to-end from `txn.api` through all enrichment operators to `txn.enriched` with all feature fields populated. Velocity uses a working implementation (full correctness edge cases deferred to US2). Geolocation and device fingerprint are complete.

**Independent Test**: Publish one synthetic transaction event; assert the downstream enriched record contains `vel_count_1m=1`, `vel_amount_1m=<amount>`, `geo_country` populated (or null for unresolvable IP), `device_txn_count=1`, `enrichment_latency_ms < 50`, and all original fields forwarded unchanged.

### Tests for User Story 1

- [X] T013 [P] [US1] Write unit tests for `DLQSink` routing: schema-invalid bytes → `SCHEMA_VALIDATION_ERROR` DLQ record with `original_payload_bytes` intact in `tests/unit/processing/test_dlq_sink.py`
- [X] T047 [P] [US1] Write unit tests for `GeolocationMapFunction` in `tests/unit/processing/test_geolocation.py`: cache hit (same subnet string returns same dict without re-querying reader), unresolvable subnet → all-null dict returned without raising, `geoip2` I/O exception → all-null dict returned and error logged, `geo_confidence` field is `float` in [0.0, 1.0] or `None`
- [X] T014 [US1] Write integration test for full enrichment pipeline in `tests/integration/test_enrichment_pipeline.py`: publish one `txn_api_v1` Avro record → consume from `txn.enriched` → assert all 36 fields present, `vel_count_1m=1`, `device_txn_count=1`, `enrichment_latency_ms` field is non-negative

### Implementation for User Story 1

- [X] T015 [US1] Implement `VelocityProcessFunction` skeleton in `pipelines/processing/operators/velocity.py`: `MapState` descriptor declared in `open()`, `process_element` increments current minute bucket (`event_time_ms // 60_000`) and returns all 8 velocity fields (correct for single-event case: all counts=1, all amounts=current amount); timer registration stubbed for US2
- [X] T016 [P] [US1] Implement `GeolocationMapFunction` in `pipelines/processing/operators/geolocation.py`: opens `geoip2.database.Reader` once in `open()`, wraps `_lookup(subnet_str)` with `@lru_cache(maxsize=10_000)`, returns dict with `geo_country`, `geo_city`, `geo_network_class` (hardcoded `RESIDENTIAL` for v1), `geo_confidence`; on exception returns all-null dict and logs error (spec edge case 3)
- [X] T017 [US1] Implement `DeviceProcessFunction` skeleton in `pipelines/processing/operators/device.py`: `ValueState<DeviceProfileState>` keyed on `api_key_id`; on first encounter sets `first_seen_ms=event_time`, `txn_count=1`, `known_fraud=False`; on subsequent calls increments `txn_count`; null/empty `api_key_id` → all device fields null (spec US4.3); TTL deferred to US4
- [X] T018 [US1] Implement `EnrichedRecordAssembler` `FlatMapFunction` in `pipelines/processing/operators/enricher.py`: receives `(txn, velocity_dict, geo_dict, device_dict)` tuple, constructs `EnrichedTransactionEvent` dict with all 36 fields, sets `enrichment_time=int(time.time()*1000)`, computes `enrichment_latency_ms=enrichment_time - txn.processing_time`, sets `processor_version` from env var `PROCESSOR_VERSION` (default `002-stream-processor@1.0.0`)
- [X] T019 [US1] Implement `TransactionDedup` `KeyedProcessFunction` in `pipelines/processing/operators/enricher.py`: `ValueState<Long>` keyed on `transaction_id`, `StateTtlConfig` 48h processing time; if state not null: skip enrichment, increment `dedup_skipped_total` counter, yield nothing; if null: set state to `event_time_ms`, yield txn for enrichment
- [X] T020 [US1] Wire Flink job topology in `pipelines/processing/job.py`: `KafkaSource[txn.api]` (with `isolation.level=read_committed`, `enable.auto.commit=false`) → `DeduplicationFilter` (TransactionDedup, keyed by `transaction_id`) → `VelocityEnrichment` (keyed by `account_id`) → `GeolocationEnrichment` → `DeviceFingerprintEnrichment` (keyed by `api_key_id`) → `EnrichedRecordAssembler` → `KafkaSink[txn.enriched]` (`DeliveryGuarantee.EXACTLY_ONCE`, `transactional_id_prefix=flink-enrichment-`, `transaction.timeout.ms=900000`) + DLQ side output from schema-validation errors; `__main__` entry point with `argparse` for all `ProcessorConfig` fields

**Checkpoint**: Full pipeline runs end-to-end. Single transaction in → enriched record out with all fields. Integration test passes.

---

## Phase 4: User Story 2 — Velocity Feature Extraction (Priority: P2)

**Goal**: Rolling-window velocity counters are accurate under window boundary crossings, out-of-order events, late events (within allowed-lateness window), and concurrent account partitioning. State eviction prevents unbounded growth.

**Independent Test**: Replay a controlled sequence of 10 transactions across 25 minutes for one account; assert `vel_count_1m`, `vel_count_5m`, `vel_count_1h`, `vel_count_24h` at each step exactly match the expected sliding window counts. Verify monotone nesting holds for all sequences (Hypothesis property test).

### Tests for User Story 2

- [X] T021 [P] [US2] Write unit tests for `VelocityProcessFunction` window boundaries in `tests/unit/processing/test_velocity.py`: cold start (counts=1, amounts=current), window crossing (oldest bucket falls out of 5m window), monotone nesting assertion for a multi-event sequence
- [X] T022 [P] [US2] Write Hypothesis property test in `tests/unit/processing/test_velocity.py`: `@given(st.lists(velocity_event_strategy, min_size=1, max_size=200))` → for any valid event sequence, assert `vel_count_1m ≤ vel_count_5m ≤ vel_count_1h ≤ vel_count_24h` and `vel_count_* ≥ 1` (current txn always included)

### Implementation for User Story 2

- [X] T023 [US2] Implement full `VelocityProcessFunction.process_element` in `pipelines/processing/operators/velocity.py`: scan all `MapState` buckets where `bucket_key >= cutoff_bucket` for each window (1m=1 bucket, 5m=5 buckets, 1h=60 buckets, 24h=1440 buckets max), accumulate `(count, Decimal(amount_sum))` per window; register event-time timer at `event_time_ms + (24*3600+41)*1000` for bucket eviction; implement `on_timer` to delete buckets older than `(timestamp - (24*3600+41)*1000) // 60_000`
- [X] T024 [US2] Implement 7-day idle TTL event-time timer in `pipelines/processing/operators/velocity.py`: in `process_element`, register timer at `event_time_ms + 7*24*3600*1000`; in `on_timer`, if timer is the 7d idle timer (not the 24h eviction timer), call `self._buckets.clear()` to evict all state for inactive account
- [X] T025 [US2] Add `StateTtlConfig` processing-time 14-day fallback for `vel_buckets` `MapState` in `pipelines/processing/operators/velocity.py` (`StateTtlConfig.new_builder(Time.days(14)).set_update_type(StateTtlConfig.UpdateType.OnCreateAndWrite).build()`)
- [X] T026 [US2] Implement `WatermarkStrategy` in `pipelines/processing/job.py`: `WatermarkStrategy.for_bounded_out_of_orderness(Duration.of_seconds(config.watermark_ooo_seconds)).with_timestamp_assigner(lambda event, ts: event.event_time)`; apply immediately after `KafkaSource` before `DeduplicationFilter`
- [X] T027 [US2] Implement late-event handling in `VelocityProcessFunction` in `pipelines/processing/operators/velocity.py`: events within `allowed_lateness_seconds` (ctx.timestamp() >= watermark - allowed_lateness) → update bucket, recompute all windows, re-emit corrected enriched record; events beyond allowed lateness → route to DLQ side output with `error_type=LATE_EVENT_BEYOND_ALLOWED_LATENESS`, `watermark_at_rejection`, `event_time_at_rejection` populated

**Checkpoint**: Velocity counts accurate for controlled event sequences. Hypothesis tests pass. Late event re-emission observable on `txn.enriched`. Beyond-window events appear on DLQ.

---

## Phase 5: User Story 3 — Fault-Tolerant State Recovery (Priority: P3)

**Goal**: Job restarts from last checkpoint with zero state loss and zero duplicate output records. Recovery completes within 60 seconds (SC-003).

**Independent Test**: (a) Run pipeline, accumulate velocity state for 5 accounts, force-kill TaskManager, restart; assert `vel_count_24h` for each account matches pre-failure value after recovery. (b) Assert no duplicate `transaction_id` appears on `txn.enriched` across the restart boundary.

### Tests for User Story 3

- [X] T028 [US3] Write integration test for fault recovery in `tests/integration/test_enrichment_pipeline.py`: publish 5 txns, wait for checkpoint, kill TaskManager via container API, restart, publish 1 more txn, assert velocity state consistent (count=6), assert no duplicates on `txn.enriched` (exactly-once)

### Implementation for User Story 3

- [X] T029 [US3] Configure `EmbeddedRocksDBStateBackend(incremental=True)` in `pipelines/processing/job.py` and add `ExternalizedCheckpointCleanup.RETAIN_ON_CANCELLATION` to checkpoint config
- [X] T030 [US3] Configure `CheckpointConfig` in `pipelines/processing/job.py`: `enable_checkpointing(config.checkpoint_interval_ms)`, `set_checkpointing_mode(CheckpointingMode.EXACTLY_ONCE)`, `set_min_pause_between_checkpoints(10_000)`, `set_checkpoint_timeout(30_000)`, `set_max_concurrent_checkpoints(1)`
- [X] T031 [US3] Configure S3/MinIO filesystem for checkpoint storage in `pipelines/processing/job.py`: set Hadoop S3A or Flink S3 config (`s3.endpoint`, `s3.path.style.access: true`, `s3.access-key`, `s3.secret-key` from env vars) and set `env.get_checkpoint_config().set_checkpoint_storage(FileSystemCheckpointStorage(config.checkpoint_dir))`
- [X] T032 [P] [US3] Add watermark stall alert: register Flink `CheckpointListener` in `pipelines/processing/job.py` that increments `checkpoint_failures_total` counter; add `last_watermark_advance_epoch` gauge updated in `VelocityProcessFunction.process_element`; document Prometheus alert rule (watermark not advanced in 60s) in `infra/flink/alerts.yaml`

**Checkpoint**: Job survives TaskManager restart. State and offsets restored. No duplicates. Checkpoint duration < 10s for typical state size.

---

## Phase 6: User Story 4 — Device Fingerprint Feature Extraction (Priority: P4)

**Goal**: Per-device profile (first-seen, lifetime count, fraud flag) accumulated correctly and exposed on enriched records. State expires after idle TTL.

**Independent Test**: Replay 3 transactions sharing `api_key_id=key-abc`; assert `device_txn_count` increments 1→2→3, `device_first_seen` is immutable after first transaction, `device_known_fraud=false`. Replay 1 transaction with null `api_key_id`; assert all device fields are null and no error.

### Tests for User Story 4

- [X] T034 [P] [US4] Write unit tests for `DeviceProcessFunction` in `tests/unit/processing/test_device.py`: first-seen scenario (count=1, known_fraud=false), count accumulation (N transactions → count=N, first_seen unchanged), null `api_key_id` → all-null device fields, `device_first_seen` monotone invariant (`device_first_seen ≤ current event_time`)

### Implementation for User Story 4

- [X] T035 [US4] Implement full `DeviceProcessFunction` in `pipelines/processing/operators/device.py`: `ValueState<DeviceProfileState>` using `@dataclass(frozen=True) DeviceProfileState(first_seen_ms, txn_count, known_fraud, last_seen_ms)`; on first event: init state; on subsequent: increment `txn_count`, update `last_seen_ms`; `known_fraud` update deferred (always `False` in v1 — documented in schema); yield `(txn, device_dict)` with `device_first_seen`, `device_txn_count`, `device_known_fraud`
- [X] T036 [US4] Implement 7d event-time idle TTL + 14d `StateTtlConfig` fallback for `DeviceProfileState` in `pipelines/processing/operators/device.py` (same pattern as velocity: register `register_event_time_timer(event_time_ms + 7*24*3600*1000)`; `on_timer` clears state)
- [X] T037 [US4] Handle null/empty `api_key_id` in `DeviceProcessFunction` in `pipelines/processing/operators/device.py`: guard at top of `process_element`; if `not txn.api_key_id`: yield `(txn, {"device_first_seen": None, "device_txn_count": None, "device_known_fraud": None})`; do not read/write state

**Checkpoint**: Device fingerprint fields appear on all enriched records. Null device-id path exercises cleanly. TTL timer registered.

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Tie together observability, topic/schema registration automation, latency validation, and coverage gate.

- [X] T038 [P] Add `txn.enriched` and `txn.processing.dlq` topic creation commands to `infra/kafka/topics.sh` (partitions=3, replication-factor=1 for dev, cleanup.policy=delete, retention.ms=604800000; `txn.enriched` adds compression.type=lz4)
- [X] T039 [P] Add Schema Registry registration commands for `enriched-txn-v1` and `processing-dlq-v1` to `infra/kafka/topics.sh` (or `infra/kafka/register-schemas.sh`) using `curl -X POST` pattern from `quickstart.md §4`
- [X] T040 [P] Add `--processor-version` arg to `pipelines/processing/job.py` `argparse` and propagate as `PROCESSOR_VERSION` env var; add `schema_version` field default `"1"` injection in `EnrichedRecordAssembler`
- [X] T041 Add enrichment latency assertion to integration test in `tests/integration/test_enrichment_pipeline.py`: publish 100 transactions, collect `enrichment_latency_ms` from enriched records, assert `p99 < 50` (SC-001); add comment that local Docker single-node may exceed budget — test marked with `pytest.mark.slow`
- [X] T042 Run full test suite and verify 80% coverage gate: `pytest --cov=pipelines/processing --cov-fail-under=80 tests/unit/processing/`; fix any gaps in `test_velocity.py`, `test_geolocation.py`, `test_device.py`, `test_dlq_sink.py` to reach threshold
- [X] T043 [P] Add `make update-geoip` target to `Makefile` (or `infra/geoip/download.sh`) using MaxMind permalink pattern from `quickstart.md §1`
- [X] T044 [P] Add DLQ depth Prometheus alert rule to `infra/flink/alerts.yaml`: alert fires if `dlq_events_total` counter increments (i.e., rate > 0 over 60s window) — constitution Non-Negotiable requires DLQ depth > 0 is observable within 60s; test alert expression fires correctly in local Prometheus
- [X] T045 Write throughput benchmark in `tests/load/test_throughput.py`: publish 5,000 transactions in one second to `txn.api`, measure lag on `txn.enriched`, assert lag clears within 5 seconds (validating SC-002 ≥5,000 TPS/node); mark `pytest.mark.perf` for opt-in execution; document in `quickstart.md` how to run against a local single-node stack
- [X] T048 [P] Add enrichment latency Prometheus alert rule to `infra/flink/alerts.yaml`: alert fires when `histogram_quantile(0.99, enrichment_latency_ms_bucket)` exceeds 20ms over a 5-minute window; Constitution §II: "Any component that cannot meet its budget slice is a blocker, not a warning; breaches page on-call immediately"; test alert expression fires on a synthetic metric in local Prometheus
- [X] T049 Write memory profiling test in `tests/load/test_memory.py`: spin up a single PyFlink `MiniCluster`, drive 1,000,000 distinct `account_id` values through the velocity operator, measure JVM heap and off-heap state backend size via Flink's REST metrics API (`/jobs/{id}/vertices/{vid}/metrics?get=Status.JVM.Memory.Heap.Used`), assert total state size ≤ 4 GB; mark `pytest.mark.perf` for opt-in execution; validates SC-005 (<4 GB/1M accounts). Add the Flink REST endpoint and expected ceiling to `quickstart.md §Observability`.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately. All T001–T006 with [P] can run in parallel after T001 creates the directory structure.
- **Foundational (Phase 2)**: Depends on Phase 1 completion. T008–T010, T046 [P] can run in parallel after T007. T011 depends on T007. T012 depends on T007.
- **User Story 1 (Phase 3)**: Depends on Phase 2 completion. T013, T047, T014 [P] can run in parallel. T015–T019 [P] can run in parallel with each other (separate files). T020 depends on T015–T019.
- **User Story 2 (Phase 4)**: Depends on Phase 3 completion. T021–T022 [P] can run in parallel. T023–T027 in sequence (T027 depends on T023 and T026).
- **User Story 3 (Phase 5)**: Depends on Phase 4 completion (checkpointing velocity state requires full velocity). T029–T031 in sequence. T032 [P] after T029.
- **User Story 4 (Phase 6)**: Depends on Phase 3 completion (can run in parallel with US2/US3 if staffed). T034 [P] independent. T035–T037 sequential.
- **Polish (Phase 7)**: Depends on all user stories complete. T038–T040, T043–T044, T048 [P]. T041 requires full pipeline. T042 after T041. T045, T049 require full pipeline (same as T041).

### User Story Dependencies

- **US1 (P1)**: Starts after Phase 2. No dependency on other stories.
- **US2 (P2)**: Starts after US1 (refines velocity operator started in US1).
- **US3 (P3)**: Starts after US2 (checkpoint correctness depends on accurate velocity state).
- **US4 (P4)**: Starts after US1 (device operator skeleton exists from Phase 3). Can overlap with US2/US3.

### Within Each User Story

- Tests written before implementation (fail-first)
- `open()` / state descriptor before `process_element`
- Operator implementation before `job.py` wiring
- Unit tests before integration tests

---

## Parallel Execution Examples

### Phase 1 — after T001

```
T002  Add [processing] extras to pyproject.toml
T003  Create infra/flink/flink-conf.yaml
T004  Update infra/docker-compose.yml
T005  Create infra/geoip/.gitkeep
T006  Copy Avro schemas to pipelines/processing/schemas/
```

### Phase 2 — after T007

```
T008  DLQ Kafka sink (pipelines/processing/shared/dlq_sink.py)
T009  Metrics bridge (pipelines/processing/metrics.py)
T010  OTel telemetry (pipelines/processing/telemetry.py)
```

### Phase 3 (US1) — tests and operator skeletons

```
T013  Unit test: DLQ routing
T014  Integration test: end-to-end enrichment

T015  VelocityProcessFunction skeleton (velocity.py)
T016  GeolocationMapFunction (geolocation.py)
T017  DeviceProcessFunction skeleton (device.py)
T018  EnrichedRecordAssembler (enricher.py)
T019  TransactionDedup (enricher.py)
```

### Phase 4 (US2) — after T023

```
T021  Unit tests: window boundary correctness
T022  Hypothesis property tests
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup (T001–T006)
2. Complete Phase 2: Foundational (T007–T012)
3. Complete Phase 3: User Story 1 (T013–T020)
4. **STOP and VALIDATE**: `python3 -m pipelines.processing.job` submits job, single transaction produces enriched output within latency budget
5. Demo: enriched record on `txn.enriched` with all 36 fields

### Incremental Delivery

1. Phases 1–2 → Foundation ready
2. Phase 3 (US1) → Working end-to-end pipeline (MVP)
3. Phase 4 (US2) → Velocity window accuracy guaranteed (property tests pass)
4. Phase 5 (US3) → Production-grade fault tolerance
5. Phase 6 (US4) → Device fingerprint signals live
6. Phase 7 → Observability, latency validation, coverage gate ✅

### Parallel Team Strategy (if staffed)

- Complete Phases 1–2 together
- US2 and US4 can proceed in parallel after US1 is complete:
  - Developer A: US2 (velocity correctness, watermarks)
  - Developer B: US4 (device fingerprint full implementation)
  - Developer C: US3 (checkpoint/recovery)

---

## Notes

- `[P]` tasks operate on different files and have no unfinished dependencies — safe to parallelise
- Each user story phase has an **Independent Test** that can be run before the next story begins
- The velocity operator in Phase 3 (T015) is a working skeleton (correct for single-event case) — Phase 4 (T023) replaces it with the full `MapState` bucket implementation
- `device_known_fraud` is always `False` in v1 (fraud label feedback loop not wired) — documented in `enriched-txn-v1.avsc`; no placeholder or TODO needed
- `maxminddb-c` C extension is mandatory for GeoIP lookup to meet the 5–20 µs/lookup budget (pure Python is 0.1–0.5 ms — 10–100× over budget at record-level)
- Do not increase `parallelism` on a live deployment without draining first — Kafka transactional IDs are keyed by subtask index and reuse causes broker rejection until timeout expires (see research.md §2)
