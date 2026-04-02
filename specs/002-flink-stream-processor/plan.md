# Implementation Plan: Stateful Stream Processor — Transaction Feature Enrichment

**Branch**: `002-flink-stream-processor` | **Date**: 2026-04-01 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `specs/002-flink-stream-processor/spec.md`

> **Note — "from the checklist":** This plan was produced after running four requirement-quality
> checklists against the source documents. 52 of 58 checklist items were confirmed gaps. The most
> structurally risky gaps are surfaced in the relevant sections below and are designated as
> open-requirements work that must be resolved before the feature is considered production-complete.
> The checklists themselves are at `specs/002-flink-stream-processor/checklists/`.

---

## Summary

The processor consumes raw Avro-encoded transaction events from `txn.api`, enriches each event with
four rolling-window velocity aggregates (1m / 5m / 1h / 24h), GeoLite2-derived geolocation context,
and per-device fingerprint signals; then publishes the enriched record to `txn.enriched` for the
downstream fraud-scoring engine. State is maintained in RocksDB across the cluster and survives
failures via incremental checkpoints to S3/MinIO with exactly-once end-to-end semantics (FR-015).

**Technical approach (all unknowns resolved — see [research.md](research.md)):**
- **Runtime:** PyFlink 1.19 DataStream API, Python 3.11; Arrow batching (bundle.size=1000,
  bundle.time=15ms) reduces Python–JVM bridge overhead to ~10–50 µs amortised — within the 20ms
  Constitution enrichment budget slice.
- **Velocity pattern:** `KeyedProcessFunction` + `MapState<minute_bucket, (count, sum)>` — O(1)
  write per event, O(1440) bucket scan for 24h aggregates; no pane copies.
- **State backend:** `EmbeddedRocksDBStateBackend(incremental=True)` with processing-time TTL
  14d fallback + event-time timer at event_time+7d for FR-011-compliant eviction.
- **Exactly-once:** `CheckpointingMode.EXACTLY_ONCE` + `KafkaSink(DeliveryGuarantee.EXACTLY_ONCE)`
  + `isolation.level=read_committed` on BOTH source and all downstream consumers.
- **GeoIP:** `geoip2.database.Reader` opened once per TaskManager slot + `maxminddb-c` C extension
  (5–20 µs/lookup) + LRU cache on /24 subnet prefix (maxsize=10,000).
- **Watermark:** `forBoundedOutOfOrderness(10s)`, 30s allowed lateness, DLQ for beyond-window
  events.
- **Dedup:** `ValueState<Long>` keyed on `transaction_id`, 48h processing-time TTL.
- **Checkpoint storage:** MinIO (local dev, S3-compatible) → S3 (cloud); `num-retained ≥ 3` to
  preserve incremental SST chain integrity.

---

## Technical Context

**Language/Version**: Python 3.11 (established — aligns with ML/data team tooling; validated in
research.md §1 for PyFlink 1.19 compatibility)

**Primary Dependencies**:
- `apache-flink==1.19` (PyFlink DataStream API — stream runtime, state management, checkpointing)
- `confluent-kafka[schema-registry]` (Schema Registry client — bootstrap only; Flink's built-in
  Kafka connector handles consumer/producer at runtime)
- `fastavro` (Avro serialisation/deserialisation for schema validation and DLQ record production)
- `geoip2` + `maxminddb-c` (GeoLite2 lookup — C extension mandatory for latency budget)
- `opentelemetry-sdk` + `opentelemetry-exporter-otlp` (OTel trace propagation across pipeline
  stages)
- `prometheus-client` (custom metric emission from Python operators)
- `hypothesis` (property-based tests for velocity window invariants)
- `pytest` + `pytest-cov` (unit and integration tests)

**Storage**:
- **Flink state:** `EmbeddedRocksDBStateBackend` — velocity buckets, device profiles, dedup keys
- **Checkpoint storage:** MinIO `s3://flink-checkpoints/002-stream-processor` (local dev); AWS S3
  same path pattern (cloud); `num-retained: 3` minimum; incremental SST snapshots
- **GeoLite2 MMDB:** volume-mounted at `/opt/flink/geoip/GeoLite2-City.mmdb`; not bundled in image
  (licence compliance — CHK015 ✅); updated via `make update-geoip`

**Testing**: `pytest` (unit + integration); Flink mini-cluster mode for integration tests;
`hypothesis` for property-based velocity window invariants; load tests in `tests/load/`

> **Open gap — CHK013:** It is not confirmed whether Flink mini-cluster mode supports the
> two-phase commit Kafka sink required by FR-015 exactly-once semantics. This must be validated
> before integration tests are considered authoritative for FR-015.

**Target Platform**: Linux x86-64, Docker Compose (local dev); Kubernetes with Flink Operator
(cloud production); Python 3.11 is the only supported runtime

**Project Type**: Stream-processing worker (long-lived Flink job submitted to a Flink cluster)

**Performance Goals**:
- End-to-end enrichment latency: < 50 ms p99 at 5,000 TPS/node (SC-001)
- Enrichment budget slice (Constitution §II): < 20 ms from Kafka consumer receipt to enriched
  record write acknowledged (FR-012)
- Throughput: ≥ 5,000 TPS/node without source lag increase (SC-002)
- Recovery time: < 60 s with zero data loss after single-node failure (SC-003)
- State storage: < 4 GB/node for 1M active accounts (SC-005)

**Constraints**:
- Arrow batching mandatory (`bundle.size=1000`, `bundle.time=15ms`) — without it Python–JVM
  overhead exceeds the 20ms budget at 5k TPS
- `num-retained ≥ 3` for incremental checkpoint chain integrity — deleting intermediate checkpoints
  breaks recovery
- All downstream consumers of `txn.enriched` MUST set `isolation.level=read_committed` for
  exactly-once semantics to hold at the consumer boundary
- `transaction.timeout.ms` on the Flink Kafka sink MUST be > checkpoint interval + checkpoint
  timeout (i.e., > 90,000 ms); configured at 900,000 ms (15 min); broker default is sufficient
- `taskmanager.memory.process.size: 4g`; `taskmanager.memory.managed.fraction` MUST be tuned to
  leave adequate JVM heap alongside RocksDB off-heap usage (CHK006 — currently unset)
- Flink parallelism settings MUST be sized to meet SC-002 5,000 TPS/node target (CHK007 —
  currently no parallelism config in `flink-conf.yaml`)

**Scale/Scope**:
- 1M active accounts; worst-case velocity state ≈ 4 GB/node (data-model.md §10)
- Typical state < 500 MB/node (typical 10% bucket fill + device profiles + dedup window)
- Minimum 2 processing nodes in production (FR-007)
- 48h dedup window; 7d velocity TTL; 30s allowed lateness; 10s max out-of-orderness

---

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | Assessment |
|-----------|--------|-----------|
| **I — Stream-First** | ✅ PASS | Stateful stream processing via Flink DataStream API; no batch fallback path |
| **II — Sub-100ms Decision Budget (enrichment < 20ms)** | ✅ PASS | Arrow batching reduces amortised per-record overhead to ~10–50 µs; 15ms bundle timeout bounds worst-case latency well within 20ms; validated in research.md §1 |
| **III — Schema Contract Enforcement** | ⚠️ PARTIAL | Avro schemas defined for both output topics (`enriched-txn-v1.avsc`, `processing-dlq-v1.avsc`). **Open gaps:** `geo_network_class` and `error_type` are free `string` types instead of Avro `enum` — invalid values can be written without schema rejection (CHK001, CHK002 — data-contracts checklist). Schema Registry compatibility mode not registered (CHK004). Enum conversion is a pre-production requirement. |
| **IV — Channel Isolation** | ✅ PASS | Processor reads exclusively from `txn.api`; writes to `txn.enriched` and `txn.processing.dlq`; no cross-channel state sharing |
| **V — Defense in Depth** | ⚠️ PARTIAL | DLQ routing for schema failures and late events defined (FR-010, FR-014). **Open gaps:** Kafka ACLs, TLS-in-transit, Schema Registry auth, MinIO access controls, secrets management, and PCI-DSS scope documentation are all confirmed open gaps (CHK001–CHK014 in security checklist). These are production security requirements; local dev defaults are insufficient. |
| **VI — Immutable Event Log** | ✅ PASS | Input events consumed read-only; output appended to `txn.enriched`; late-event correction appends a new record — no retraction; DLQ is append-only |
| **VII — PII Minimization** | ✅ PASS | Processor receives already-masked fields from 001 (subnet, BIN/last4 masked); no additional masking required. **Note:** `card_bin` and `card_last4` in `enriched-txn-v1.avsc` are PCI-DSS in scope — consumer ACLs for `txn.enriched` are an open gap (CHK009 security checklist) |
| **VIII — Observability as First Class** | ❌ FAIL | FR-012 names three metrics (processing lag, enrichment latency p50/p99, checkpoint duration). All 15 observability checklist items are confirmed gaps — no alerting thresholds, measurement boundaries, trace context propagation spec, or structured logging redaction list are documented. Silent failures are not permitted by the constitution. **This must be resolved before FR-012 is considered implementation-complete.** |

**Constitution verdict**: Feature may proceed with two **open violations** (Principle III partial,
Principle VIII fail) subject to the complexity justification below. Both must be resolved before the
feature can be declared production-complete.

---

## Project Structure

### Documentation (this feature)

```text
specs/002-flink-stream-processor/
├── plan.md              # This file
├── spec.md              # Feature specification
├── research.md          # All 8 unknowns resolved (PyFlink, exactly-once, velocity, TTL, RocksDB,
│                        #   GeoIP, watermarks, dedup)
├── data-model.md        # Entity definitions, state stores, window boundaries, storage estimates
├── quickstart.md        # Local dev setup: Docker Compose + MinIO + GeoLite2 download
├── contracts/
│   ├── enriched-txn-v1.avsc      # Output schema: txn.enriched
│   └── processing-dlq-v1.avsc   # DLQ schema: txn.processing.dlq
├── checklists/
│   ├── plan.md          # Plan quality — 3/15 pass, 12 gaps
│   ├── observability.md # Observability requirements — 0/15 pass, 15 gaps (ALL OPEN)
│   ├── data-contracts.md # Contract quality — 2/13 pass, 11 gaps
│   └── security.md      # Security requirements — 1/15 pass, 14 gaps
└── tasks.md             # Phase 2 output (/speckit.tasks — NOT created here)
```

### Source Code

```text
pipelines/
└── processing/                          # This feature's root
    ├── __init__.py
    ├── job.py                           # Flink job entrypoint: env setup, source/sink wiring,
    │                                    #   checkpointing config, parallelism, Arrow batching
    ├── config.py                        # ProcessorConfig dataclass (watermark bounds, TTL values,
    │                                    #   bundle size, checkpoint interval — all configurable)
    ├── operators/
    │   ├── __init__.py
    │   ├── velocity.py                  # VelocityProcessFunction (KeyedProcessFunction);
    │   │                                #   MapState<minute_bucket, (count, sum)>; event-time
    │   │                                #   timer for bucket eviction and 7d TTL
    │   ├── geolocation.py               # GeolocationMapFunction (MapFunction); geoip2 Reader +
    │   │                                #   LRU cache; maxminddb-c C extension
    │   ├── device_profile.py            # DeviceProfileProcessFunction (KeyedProcessFunction);
    │   │                                #   ValueState<DeviceProfileState>; 7d event-time TTL
    │   ├── dedup.py                     # DeduplicationProcessFunction (KeyedProcessFunction);
    │   │                                #   ValueState<Long> keyed on transaction_id; 48h TTL
    │   └── enrichment.py                # EnrichmentJoinFunction: joins velocity + geo + device
    │                                    #   outputs into single EnrichedTransaction record
    ├── schemas/
    │   ├── enriched-txn-v1.avsc         # Mirror of specs/…/contracts/enriched-txn-v1.avsc
    │   └── processing-dlq-v1.avsc       # Mirror of specs/…/contracts/processing-dlq-v1.avsc
    │   # AUTHORITATIVE SOURCE: specs/002-flink-stream-processor/contracts/ (CHK010 — open gap:
    │   # no sync mechanism defined; pipelines/processing/schemas/ is a deploy-time copy)
    ├── serialization/
    │   ├── __init__.py
    │   ├── avro_deserializer.py         # fastavro-based Avro deserializer for txn_api_v1 input
    │   └── avro_serializer.py           # fastavro-based serializer for enriched-txn-v1 output
    └── metrics.py                       # Prometheus metric registration; OTel span helpers

infra/
├── flink/
│   ├── flink-conf.yaml                  # RocksDB, Arrow batching, checkpoint config
│   └── Dockerfile                       # PyFlink 1.19 + Python 3.11 + geoip2 + maxminddb-c
├── kafka/
│   └── topics.sh                        # Topic provisioning: txn.api, txn.enriched,
│                                        #   txn.processing.dlq (min 4 partitions, 48h retention)
└── docker-compose.yml                   # Flink JobManager + TaskManager + Kafka + Schema Registry
                                         #   + MinIO + Prometheus

tests/
├── unit/
│   └── processing/
│       ├── test_velocity.py             # VelocityProcessFunction unit tests (Flink mini-cluster)
│       ├── test_geolocation.py          # GeolocationMapFunction unit tests (mock mmdb)
│       ├── test_device_profile.py       # DeviceProfileProcessFunction unit tests
│       ├── test_dedup.py                # Deduplication unit tests
│       └── test_velocity_properties.py  # Hypothesis property-based tests (see note below)
├── integration/
│   ├── conftest.py                      # Docker Compose fixture: Flink + Kafka + MinIO
│   └── test_enrichment_pipeline.py      # End-to-end pipeline integration tests
└── load/
    └── test_throughput.py               # 5,000 TPS/node throughput validation (SC-002)
```

**Structure Decision**: Single project — Python 3.11 package at `pipelines/processing/`, with
operators as separate modules to allow isolated unit testing without a live Flink cluster.

**Schema source of truth (CHK010)**: `specs/002-flink-stream-processor/contracts/` is authoritative.
`pipelines/processing/schemas/` is a deploy-time copy. A pre-commit hook or CI step must validate
they are byte-identical; divergence must cause the build to fail. This mechanism is not yet
implemented — it is an open requirement.

---

## Open Requirements from Checklists

The following items were identified as confirmed gaps across all four quality checklists. They are
listed here to make them trackable as implementation requirements, not as optional nice-to-haves.

### Observability (0/15 pass — ALL open)

Key items requiring resolution before FR-012 can be declared complete:

- **Alert thresholds:** Enrichment latency p99 alert between 20ms (budget breach) and 50ms (SLA
  breach) must be defined. DLQ depth alert threshold must be quantified. Watermark stall detection
  threshold must be defined (spec names this as an explicit edge case but no FR captures it).
  Checkpoint failure escalation policy needed (how many consecutive failures trigger alert vs halt).
  RocksDB state-size early-warning threshold for SC-005 4 GB ceiling.
- **Measurement boundaries (CHK008):** `enrichment_latency_ms` in `enriched-txn-v1.avsc` is
  calculated as `enrichment_time − processing_time`. However, `processing_time` is set by the
  **ingestion producer** (feature 001) at API receipt — not by the Flink consumer at Kafka message
  receipt. This means the field includes ingestion-to-Flink queue time and **systematically
  overstates** enrichment-internal latency. FR-012 defines latency as "from Kafka consumer receipt"
  — the field is inconsistent with this definition. **This is a named bug in the contract, not a
  future improvement.** It must be corrected before v1 ships, or the field doc must be updated to
  declare the broader measurement window explicitly.
- **Kafka consumer lag source:** The metric source for processing lag must be specified (Flink
  internal reporter vs Kafka JMX exporter vs external consumer group monitor) — they produce
  different lag values at different scrape frequencies.
- **Prometheus cardinality:** Per-account or per-transaction-id metric labels would cause cardinality
  explosion at 1M+ accounts. A label-dimensionality constraint is required.
- **Structured logging:** Log format, mandatory fields (trace_id, transaction_id), and redaction
  list (card_bin, card_last4, caller_ip_subnet, account_id) must be specified.
- **OTel trace propagation:** Carrier fields (Kafka header keys), span boundaries, and
  cross-pipeline trace correlation with feature 001 must be specified.

### Data Contracts (2/13 pass)

Items requiring schema changes before production:

- **CHK001:** `geo_network_class` must be an Avro `enum` with closed values set
  (`RESIDENTIAL`, `BUSINESS`, `HOSTING`, `MOBILE`, `UNKNOWN`), not a free `string`. Free string
  allows invalid values past schema validation and makes downstream switch logic fragile.
- **CHK002:** `error_type` in `processing-dlq-v1.avsc` must be an Avro `enum`. Same rationale.
- **CHK004:** Schema Registry compatibility mode must be explicitly registered for both
  `txn.enriched-value` and `txn.processing.dlq-value` subjects before any schema evolution occurs.
  Running on the default (BACKWARD) silently is not a documented decision.
- **CHK009:** `device_known_fraud` field is `["null", "boolean"]` with default `null`, but the doc
  comment says "always false." Consumers receiving `null` who coerce to `false` make an incorrect
  fraud signal interpretation. The doc must be corrected to say "always null in v1 — consumers
  MUST treat null as 'not evaluated', not as false."

### Security (1/15 pass)

All items are production prerequisites. Key ones:

- **CHK003 / CHK001:** Kafka TLS and ACLs. Local dev can run PLAINTEXT; production cannot. ACL
  matrix (which service account has produce/consume on which topic) must be documented. Produce
  rights on `txn.api` must be explicitly denied to the processor's service account.
- **CHK004 / CHK005 / CHK007:** MinIO/S3 credentials must not be `minioadmin/minioadmin` in any
  non-local environment. Access must be scoped to the processor service account. Encryption-at-rest
  for checkpoint storage is required (velocity aggregates reveal transaction frequency patterns).
  MaxMind licence key must be stored in a secrets manager, not an env var.
- **CHK009:** `card_bin` and `card_last4` in `enriched-txn-v1.avsc` are PCI-DSS in scope. Consumer
  ACLs for `txn.enriched` must restrict access to authorised fraud-model consumers only.

### Plan Quality (3/15 pass)

Implementation decisions requiring documentation before coding:

- **CHK006:** `taskmanager.memory.managed.fraction` must be explicitly set in `flink-conf.yaml`.
  RocksDB bypasses JVM heap; without explicit managed memory fraction, TaskManager containers risk
  OOM kills under load. Current `flink-conf.yaml` has `rocksdb.memory.fixed-per-slot: 512mb` and
  `taskmanager.memory.process.size: 4g` but the fraction is absent.
- **CHK007:** Flink parallelism settings must be sized to meet SC-002's 5,000 TPS/node target and
  documented with the sizing rationale. `flink-conf.yaml` currently contains no parallelism setting;
  `docker-compose.yml` sets `numberOfTaskSlots: 2` with no TPS justification.
- **CHK012:** Hypothesis property-based test scenarios must enumerate the three critical velocity
  window invariants explicitly: (a) window boundary correctness, (b) TTL eviction, and (c)
  concurrent partition safety (US2-Scenario 3). A generic "property-based tests" reference is not
  sufficient.
- **CHK014:** An integration test scenario that simulates a mid-processing job kill and validates
  FR-015 recovery must be implemented: (a) no duplicate enriched output records, (b) velocity state
  matches pre-failure values after recovery, (c) consumer offset resumes from correct checkpoint.
- **CHK015:** Savepoint compatibility across job versions must be validated before any job upgrade.
  State schema changes between versions can break incremental checkpoint chains. A rollback runbook
  must exist before the first production deployment.

---

## Observability Requirements

All observability requirements are formal — any gap here constitutes a "silent failure" violation of the constitution.

### Metric Alert Thresholds

| Metric | Warn | Page | Source |
|--------|------|------|--------|
| `enrichment_latency_ms` p99 | > 20 ms | > 50 ms | FR-012; 50 ms p99 is the contractual SLA |
| `txn.processing.dlq` consumer lag (record count) | — | > 0 | Any DLQ event warrants immediate triage |
| Watermark advance interval (per Kafka partition) | > 30 s | > 60 s | 60 s = checkpoint interval; stall beyond that risks missed windows |
| RocksDB managed state size (all slots combined) | > 80 % of 4 GB ceiling (> 3.2 GB) | > 95 % (> 3.8 GB) | 4 GB = 4 g TaskManager × 40 % managed fraction; 2 slots × 512 MB fixed-per-slot |
| Last checkpoint duration | > 8 000 ms | > 10 000 ms | SC-003 checkpoint budget < 10 s |
| Checkpoint failure count (rolling 5 min) | — | ≥ 1 | Any checkpoint failure must trigger review before the next interval |
| Kafka consumer lag (input `txn.api`, partitions combined) | > 5 000 records | > 20 000 records | SC-002 5 000 TPS/node; >1 s lag at max rate = warn |

**Consumer lag measurement source**: Use Flink's built-in Kafka metrics reporter (`KafkaConsumerFetchManagerMetrics`) forwarded to Prometheus via `flink-metrics-prometheus`. Do NOT rely on external Burrow/kminion for primary alerting — Flink's internal reporter has per-subtask resolution required for per-partition watermark stall detection.

**Cardinality constraint**: Prometheus metric labels MUST NOT include `account_id`, `transaction_id`, `merchant_id`, or any per-entity identifier. Maximum expected label cardinality is O(subtask count × topic count) — currently O(10). Violating this with per-account labels at 1 M+ accounts would destroy the TSDB.

### Checkpoint SLI

`flink_jobmanager_job_lastCheckpointDuration < 10 000 ms` is a formal SLI for SC-003 (60 s recovery window). This is a hard requirement, not a guideline. If any checkpoint exceeds 10 000 ms, root-cause the RocksDB write stall or network throughput issue before continuing.

### Structured Logging Specification

All log lines MUST be JSON-formatted. Mandatory fields for every log record:

```json
{
  "timestamp": "<ISO-8601 UTC>",
  "level": "<DEBUG|INFO|WARN|ERROR>",
  "pipeline_stage": "<DESERIALIZE|VELOCITY|GEO|DEVICE|SERIALIZE>",
  "processor_version": "<002-stream-processor@semver>",
  "subtask_index": <int>,
  "transaction_id": "<uuid or null>",
  "account_id": "<masked: last 4 chars only, or null>",
  "message": "<human-readable summary>"
}
```

**Redaction rules** (defensive — fields are already masked upstream but processors must never re-expose):
- Full PAN digits MUST NOT appear in any log field.
- Full IP addresses MUST NOT appear — only `caller_ip_subnet` (/24) is permissible.
- OAuth tokens, API keys, and raw `api_key_id` values MUST NOT appear in log messages (only in structured fields where explicitly required for correlation).
- `error_message` in DLQ records MUST be sanitised before logging — truncate at 512 chars, strip any value resembling a 16-digit PAN sequence.

### Distributed Tracing

**W3C traceparent propagation** (OTel standard):
1. The ingestion producer (feature 001) writes a `traceparent` W3C header into each Kafka message's headers when publishing to `txn.api`.
2. The Flink processor reads `traceparent` from the consumed Kafka message header at the source operator.
3. A child span is created for the enrichment operation using the extracted parent context.
4. The traceparent is forwarded in the enriched record's Kafka message headers when publishing to `txn.enriched`, enabling the scoring engine (feature 003) to continue the same trace.
5. If `traceparent` is absent in the source message, the processor starts a new root span (backward compatibility with producers that predate OTel instrumentation).

**Correlation fields**: `transaction_id` is the primary join key across all pipeline stages. Any dashboard or alert that requires cross-topic trace correlation must join on `transaction_id`, not on trace ID alone.

### Dashboards

Minimum required dashboard panels:
1. Enrichment latency histogram (p50 / p95 / p99) — alert line at 50 ms.
2. DLQ rate (records/min to `txn.processing.dlq`) — alert line at 0 (any record = page).
3. Watermark lag per partition (current time − watermark) — alert line at 60 s.
4. RocksDB managed state size per TaskManager.
5. Checkpoint duration trend (rolling 60 min) — alert line at 10 s.
6. Kafka consumer lag per topic/partition.
7. Arrow bundle efficiency (records per bundle, bundles per second) — informational.

---

## Security Requirements

All security requirements below apply per environment. "Local dev" = docker-compose environment. "Production" = any environment with real transaction data (staging, pre-prod, prod).

### Kafka ACL Matrix

| Principal | Topic | Permission | Environment |
|-----------|-------|------------|-------------|
| `ingestion-producer` | `txn.api` | WRITE | all |
| `flink-processor` | `txn.api` | READ | all |
| `flink-processor` | `txn.enriched` | WRITE | all |
| `flink-processor` | `txn.processing.dlq` | WRITE | all |
| `scoring-engine` | `txn.enriched` | READ | all |
| `ops-dlq-consumer` | `txn.processing.dlq` | READ | all |
| `*` (all others) | `txn.enriched` | DENY | production |
| `*` (all others) | `txn.processing.dlq` | DENY | production |

**Transport security**:
- Local dev: PLAINTEXT is acceptable (no real PII; local network only).
- Staging and production: SASL_SSL with TLS ≥ 1.2 is mandatory. Kafka brokers must not accept PLAINTEXT connections.

### Schema Registry Authentication

- Local dev: unauthenticated Schema Registry is acceptable.
- Production: HTTP Basic Auth or mTLS must be enforced. The processor's Schema Registry client must supply credentials from the secrets manager (not hardcoded in config).
- **Compatibility mode requirement**: Both `txn.enriched` and `txn.processing.dlq` subjects MUST be registered with `BACKWARD` compatibility mode before the first production deployment. `BACKWARD` allows adding optional fields (with defaults) without breaking existing consumers — the correct mode for append-only schema evolution.

### Checkpoint Storage Access (MinIO / S3)

- Local dev: `minioadmin/minioadmin` credentials are acceptable for the docker-compose MinIO instance.
- Production: IAM role-based access only — no static credentials. The Flink TaskManager pods must be assigned an IAM role (AWS) or Workload Identity (GCP) scoped exclusively to `s3://flink-checkpoints/002-stream-processor/`.
- Encryption-at-rest: Required for checkpoint storage in production. RocksDB SST files written to S3 contain velocity aggregate state (transaction counts and amounts per account) which reveals transaction frequency patterns and constitutes sensitive financial data.
- Bucket policy: The checkpoint bucket must deny public access and deny cross-account access.

### Secrets Management

- `MAXMIND_LICENCE_KEY`: In production, this key must be injected via a secrets manager (HashiCorp Vault, AWS Secrets Manager, or Kubernetes Secret with external-secrets-operator) — not as a plain environment variable in a manifest or docker-compose file.
- Rotation policy: MaxMind keys do not auto-expire. Keys must be rotated on personnel change (offboarding of team members with access) or on suspected compromise. Document the rotation procedure in the ops runbook.
- The `make update-geoip` target in the Makefile passes `MAXMIND_LICENCE_KEY` as a direct env var — this is local dev only and must never be replicated in CI/CD pipelines.

### PCI-DSS Consumer Controls

- `txn.enriched` topic contains `card_bin` (first 6 PAN digits) and `card_last4` (last 4 PAN digits). Both fields are PCI-DSS in scope.
- Any consumer of `txn.enriched` must hold PCI-DSS authorisation. Consumer service accounts must be listed in the PCI-DSS scope boundary document.
- Consumer ACLs for `txn.enriched` must be reviewed quarterly as part of PCI-DSS access review.
- `txn.processing.dlq` also contains `original_payload_bytes` which encodes the full (PII-masked) raw transaction including `card_bin` and `card_last4`. DLQ consumers are also PCI-DSS in scope.

### GeoLite2 Database Update Policy

- Local dev: Manual update via `make update-geoip` (downloads from MaxMind, mounts read-only into the processor container).
- Production update frequency: GeoLite2 databases are updated by MaxMind every Tuesday and Friday. The processor should refresh the GeoLite2 database weekly. A scheduled job (cron or CI pipeline) should run `update-geoip` and trigger a rolling restart of the Flink TaskManagers.
- **LRU cache invalidation on reload**: The GeoLite2 lookup uses an in-process LRU cache keyed by `/24` subnet. When the database file is refreshed (new mmdb file mounted), the LRU cache must be explicitly invalidated — a file-modification-time check at each bundle boundary (every `bundle.time=15ms`) is acceptable. Without invalidation, the processor continues serving stale geo lookups from the old database file until the next TaskManager restart.

### Schema Source of Truth

`specs/002-flink-stream-processor/contracts/` is the authoritative source for all Avro schemas. `pipelines/processing/schemas/` is a deploy-time copy; it must be kept byte-for-byte identical to the corresponding contracts/ file. Any PR that modifies a schema must update both paths simultaneously. Divergence is detected by diffing the two directories in CI. If the two copies differ, the contracts/ version wins.

### Prometheus Scrape Endpoint Authentication

- Local dev: unauthenticated Prometheus scrape is acceptable (docker-compose internal network).
- Production: The Prometheus scrape endpoint (`:9249` on TaskManager pods, or the Pushgateway equivalent) must be protected by network policy (only the Prometheus server's service account may reach it) or by HTTP Basic Auth. Exposing processing-lag metrics to arbitrary network-adjacent services leaks account transaction rate patterns.

### Log Redaction Requirements

This requirement lives in the Observability Requirements section above. Cross-reference: the field redaction list (full PAN, full IP, OAuth tokens, raw `api_key_id`) is a security requirement as much as an observability convention. Violation of redaction rules in logs constitutes a PCI-DSS audit finding.

### Audit Logging for Checkpoint Storage

An S3 server-access log (or equivalent) must be enabled on the checkpoint bucket in production to record all GetObject (read) operations. This supports forensic review when a specific checkpoint is replayed to reconstruct historical velocity state — identifying who triggered the replay, from which host, and which checkpoint version was accessed.

### Secrets Rotation Procedures

Rotation policy for each credential class:

| Credential | Storage | Rotation Trigger | Live Refresh? |
|------------|---------|-----------------|---------------|
| Kafka SASL credentials | Secrets manager | Annually or personnel change | No — rolling restart required; schedule during low-traffic window |
| Schema Registry API key | Secrets manager | Annually or personnel change | No — restart required |
| S3 access key (static, non-IAM) | N/A — IAM role preferred | N/A | N/A |
| MaxMind licence key | Secrets manager | Personnel change / compromise | No — restart required to reload mmdb |
| MinIO credentials (local dev) | docker-compose.yml | N/A — local only | N/A |

If the Flink job must be restarted to pick up rotated credentials, the restart must be coordinated with a savepoint take-and-restore cycle to avoid losing in-flight velocity state.

### Docker Image Security Scanning

All Docker images used in the Flink deployment must be scanned for CVEs before promotion to staging or production:
- `flink:1.19-scala_2.12` (base image for JobManager and TaskManager)
- Custom PyFlink worker image (built from `infra/flink/Dockerfile`)

Scanning must be integrated into CI (e.g., `trivy image`, `grype`, or AWS ECR image scanning) with a policy of blocking promotion on HIGH or CRITICAL severity CVEs. Base image updates must be tracked and applied within 30 days of a published fix for a HIGH/CRITICAL CVE.

### MinIO-to-S3 Migration Path

When migrating from local MinIO to production S3:
1. Update `state.checkpoints.dir` in `flink-conf.yaml` from `s3://flink-checkpoints/…` (MinIO) to the production S3 URI.
2. Flink's `flink-s3-fs-hadoop` plugin handles both MinIO and AWS S3 via the same `s3://` scheme — no code changes required, only endpoint configuration (`s3.endpoint` in `flink-conf.yaml` or Hadoop `fs.s3a.endpoint` in `core-site.xml`).
3. Existing checkpoints cannot be migrated across storage backends. The first production deployment must start from a clean state (no checkpoint), not from a MinIO checkpoint.
4. After migration, retain the last 3 MinIO checkpoints for 30 days before deletion (per `state.checkpoints.num-retained: 3`) to allow rollback diagnosis.

---

## Implementation Readiness Notes

### Hypothesis Property-Based Test Invariants (CHK012)

Three velocity window invariants that Hypothesis property tests MUST enumerate explicitly:

1. **Window boundary correctness**: Given a sequence of events where event `n` has `event_time = window_start + duration - 1ms` and event `n+1` has `event_time = window_start + duration`, event `n+1` MUST NOT contribute to the same 1-minute window as event `n`. `vel_count_1m` for event `n+1` must reset to 1.

2. **TTL eviction correctness**: Given an account with no transactions for `> 24h` (event time), the velocity state for that account must be evicted. A transaction arriving after the 24h gap must produce `vel_count_24h = 1` (not the accumulated prior count). RocksDB timer-based TTL must fire within one checkpoint interval after expiry.

3. **Concurrent partition safety**: Given the same `account_id` assigned to two different Kafka partitions (key-based routing violation, or hash collision under re-partitioning), velocity aggregates from both partitions MUST NOT be merged. Each partition's Flink subtask maintains independent state — cross-partition deduplication is not in scope for v1 (TD-003, deferred).

### Mini-Cluster Exactly-Once Validation Caveat (CHK013)

Flink's mini-cluster (used in integration tests) does NOT support the two-phase-commit Kafka sink in the same way as a full cluster. Specifically:
- `DeliveryGuarantee.EXACTLY_ONCE` requires a Kafka transaction coordinator reachable from the TaskManager — mini-cluster tests must use an embedded Kafka (e.g., Testcontainers) to validate this.
- Tests that validate exactly-once delivery MUST use `DeliveryGuarantee.AT_LEAST_ONCE` + idempotent consumer logic OR a full Testcontainers Kafka setup.
- Document this caveat in the integration test setup: mini-cluster tests validate processing correctness (enrichment output values, DLQ routing); exactly-once sink guarantees require the Testcontainers Kafka path.

### Job Kill and Recovery Integration Test (CHK014)

An integration test that simulates a mid-processing job kill must cover three recovery assertions:
1. **No duplicate enriched records**: After recovery from checkpoint, no `transaction_id` appears in `txn.enriched` more than once for events processed before the kill.
2. **Velocity state consistency**: After recovery, velocity aggregates (`vel_count_*`, `vel_amount_*`) match the state at the last successful checkpoint — not the state at kill time.
3. **Consumer offset resume**: The Flink source operator resumes consuming from the Kafka offset stored in the checkpoint, not from the latest Kafka offset. Verify by comparing consumed offsets post-recovery against the checkpoint metadata.

Test implementation: use Testcontainers (Kafka + Flink mini-cluster), inject a `Thread.interrupt()` or kill the JobManager process after committing checkpoint N, verify the above assertions by replaying events from the checkpoint offset.

### Savepoint Compatibility Runbook (CHK015)

Before any job upgrade that changes state schema (new fields in `VelocityState`, new operators, changed window sizes):
1. Take a manual savepoint: `flink savepoint <job-id> s3://flink-checkpoints/savepoints/`.
2. Validate savepoint compatibility: run the new job version against the savepoint in a staging environment with `--fromSavepoint <path>`.
3. If state schema is incompatible (e.g., `ListState` → `MapState` for velocity), implement a state migration function using `StateDescriptor` versioning or write a one-off migration job.
4. Keep the savepoint for 90 days post-upgrade as a rollback point.
5. Document the state schema version in `PROCESSOR_VERSION` env var (e.g., bump minor version on state schema change).

---

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|--------------------------------------|
| Constitution §VIII gap (observability) | All 15 observability items are open gaps in source documents — no thresholds, boundaries, or logging specs defined anywhere | Proceeding without alerting thresholds means operators cannot detect degradation before SLA breach; "add later" is explicitly prohibited by the constitution's "silent failures forbidden" clause |
| Constitution §III partial (free string types) | `geo_network_class` and `error_type` are `string` instead of `enum` in the Avro schemas | Avro `enum` types require a schema evolution with consumer coordination; this is a v1 gap that was shipped, not a deliberate design choice. The constitution requires schema contract enforcement — this must be resolved before production. |
