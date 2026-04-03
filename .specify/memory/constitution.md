# FraudStream Constitution

## Core Principles

### I. Stream-First (NON-NEGOTIABLE)
Every data flow is designed as an unbounded stream, never as a batch job retrofitted with streaming.  
- All transaction events must be published to Kafka topics before any processing occurs  
- No component may read directly from a source system; the broker is the single entry point  
- Kafka topics are the system of record for raw events — downstream stores are derived views  
- Local and cloud topologies must be functionally equivalent; Docker Compose mirrors the cloud stack 1:1

### II. Sub-100ms Decision Budget
The end-to-end latency from event ingestion to block/allow response must not exceed 100ms at p99.  
- Ingestion → Kafka: < 10ms  
- Kafka → feature enrichment: < 20ms  
- Hot store lookup (Redis): < 5ms  
- ML model inference: < 30ms  
- Decision → response: < 15ms  
- Any component that cannot meet its budget slice is a blocker, not a warning  
- Latency SLOs are enforced via Prometheus alerts; breaches page on-call immediately

### III. Schema Contract Enforcement (NON-NEGOTIABLE)
All events crossing a topic boundary must be validated against a registered schema.  
- Avro or Protobuf schemas are the source of truth; JSON payloads are forbidden in production topics  
- Schema Registry is mandatory in both local and cloud environments  
- Breaking schema changes require a new topic version (`txn.web.v2`); old consumers must be migrated before the old topic is retired  
- Every field in the transaction event schema must have a `nullable: false` justification or an explicit nullability rationale documented in the schema

### IV. Channel Isolation
Each transaction source (POS, web, mobile, API) is treated as an independent channel with its own topic, enrichment job, and threshold configuration.  
- Topics: `txn.pos`, `txn.web`, `txn.mobile`, `txn.api` — no unified raw topic  
- Channel-specific features (device fingerprint for mobile, 3DS result for web) are enriched in the channel's own Flink job  
- Fraud thresholds and model versions are configured per channel; a single global threshold is prohibited  
- Cross-channel aggregations (e.g., velocity across all channels for one account) are computed in a dedicated enrichment step downstream of channel topics

### V. Defense in Depth — Rules Before Models
The scoring pipeline always applies a deterministic rule engine before invoking the ML model.  
- Rules are fast, auditable, and explainable; they catch known fraud patterns with zero inference cost  
- The ML model scores only events that pass the rule engine (no obvious fraud) or are flagged for soft review  
- If the ML serving layer is unavailable, the rule engine alone issues the decision — the pipeline never stalls or passes events silently  
- Rule changes require a unit test proving the new rule fires on a crafted fraudulent event and does not fire on a crafted legitimate event

### VI. Immutable Event Log
Raw events are never mutated or deleted after they reach the event store.  
- Apache Iceberg tables are append-only; `UPDATE` and `DELETE` operations on raw event tables are prohibited  
- All enrichment and decisions are stored in separate derived tables, joined by `transaction_id`  
- The event store is the source of truth for model retraining, audit, and dispute resolution  
- Retention policy: raw events retained for 7 years (regulatory minimum); hot store (Redis) TTL is 24 hours

### VII. PII Minimization at the Edge
Sensitive fields are masked at the Kafka producer, before the event enters any pipeline component.  
- Card numbers: store only last 4 digits + BIN (first 6); full PANs are never written to any topic or store  
- IP addresses: truncate to /24 subnet for storage; full IP used only transiently during enrichment  
- No component downstream of the producer may reconstruct a full PAN or full IP  
- PII masking logic lives in a shared library, versioned and tested independently of producers

### VIII. Observability as a First-Class Concern
Every pipeline component emits structured logs, metrics, and traces; silent failures are forbidden.  
- Metrics: throughput (events/sec), latency (p50/p95/p99), fraud rate, block rate, DLQ depth  
- Logs: structured JSON, minimum fields — `transaction_id`, `component`, `timestamp`, `level`, `message`  
- Traces: distributed trace spanning ingestion → enrichment → scoring → decision (OpenTelemetry)  
- A dead letter queue (DLQ) topic exists for each pipeline stage; DLQ depth > 0 triggers an alert within 60 seconds

### IX. Analytics-First Persistence (NON-NEGOTIABLE)
Every enriched transaction and every fraud decision MUST be durably persisted to the event store and made queryable by downstream analytics, ML training, and audit consumers — the streaming pipeline is not a black box.  
- **Enriched transactions sink**: The stream processor MUST write each `EnrichedTransaction` record to an append-only Iceberg table (`iceberg.enriched_transactions`) in addition to publishing to the Kafka output topic — the write is a mandatory side output, not optional  
- **Fraud decisions sink**: The scoring engine MUST write each `ScoringOutput` record (decision, fraud score, rule triggers, model version, latency) to a separate Iceberg table (`iceberg.fraud_decisions`) keyed by `transaction_id`  
- **Feature materialization**: Computed velocity, geolocation, and device fingerprint features MUST be materialized to the Feast feature store on every enrichment cycle, making them available for both online serving (low-latency lookup during inference) and offline training (point-in-time correct feature snapshots)  
- **Analytics query layer**: An always-on SQL query engine (Trino locally; Athena/BigQuery in cloud) MUST be able to join `enriched_transactions` and `fraud_decisions` on `transaction_id` without requiring custom code — schema evolution in Iceberg tables follows the same Schema Registry versioning discipline as Kafka topics  
- **No analytics-only writes to Kafka**: Iceberg is the analytics source of truth; ad-hoc analytics pipelines MUST NOT re-consume raw Kafka topics as a substitute for a missing persistence layer  
- **Write latency budget**: Iceberg sink writes MUST complete within 5 seconds of the Kafka consumer receipt — this is outside the 100 ms scoring hot path and MUST NOT add latency to the decision response

### X. Analytics Consumer Layer
A dedicated, independent consumer component owns all reporting and interactive analytics — it is strictly read-only relative to the pipeline and MUST NOT influence any scoring or enrichment decision.  

**Data sources (dual-mode):**
- **Real-time feed** — Kafka consumer on `txn.decisions` for live dashboards (sub-second event visibility); consumer group `analytics.dashboard` with `auto.offset.reset = latest`  
- **Historical store** — Trino queries over `iceberg.enriched_transactions` and `iceberg.fraud_decisions` for reports, drill-down, and ML audit

**Reporting tools hierarchy:**
| Layer | Tool | Purpose |
|---|---|---|
| Interactive app | **Streamlit** | Primary UI — live fraud dashboard, historical reports, and (future) rule engine config |
| Operational metrics | Grafana | Infrastructure / SLO dashboards (latency, DLQ, throughput) — unchanged |
| Ad-hoc SQL | Trino | Data team exploration and scheduled report queries |
| BI (optional) | Apache Superset | Self-serve dashboards for non-engineering stakeholders |

**Streamlit app responsibilities (v1):**
- Live transaction feed with decision overlay (real-time Kafka consumer, refreshed via `st.empty()` loop)  
- Fraud rate by channel, merchant, and geo over configurable rolling windows (Trino → Iceberg)  
- Rule trigger frequency leaderboard — which rules fire most, false-positive rate per rule  
- Model version comparison — fraud score distribution per model version over a chosen time range  
- DLQ inspector — browse dead-letter records without direct Kafka access

**Streamlit app responsibilities (v2 — rule engine config UI, future spec):**
- CRUD interface for rule definitions (thresholds, conditions, enabled/disabled)  
- Rule test harness — submit a synthetic transaction and show which rules would fire  
- Rule change audit log — who changed what, when, with before/after diff  
- Config changes MUST be written to a `txn.rules.config` Kafka topic consumed by the rule engine; Streamlit MUST NOT write rule state directly to any store the scoring engine reads

**Constraints:**
- The analytics consumer MUST run as an independent process/service — it MUST NOT be co-located with the scoring engine or stream processor  
- It MUST use a separate Kafka consumer group and MUST NOT affect topic offsets consumed by scoring/processing  
- Streamlit sessions MUST NOT cache PII beyond the minimum required to render the current view; masked values from Iceberg are displayed as-is  
- The analytics service is non-critical-path: its unavailability MUST NOT affect fraud scoring SLOs

---

## Data Contracts

### Transaction Event (minimum viable schema)
```json
{
  "transaction_id": "uuid",
  "account_id":     "string",
  "merchant_id":    "string",
  "amount":         "decimal",
  "currency":       "ISO-4217",
  "timestamp":      "epoch_ms",
  "channel":        "enum[POS, WEB, MOBILE, API]",
  "device_id":      "string",
  "ip_subnet":      "string (masked /24)",
  "geo_lat":        "float",
  "geo_lon":        "float",
  "card_bin":       "string (first 6)",
  "card_last4":     "string"
}
```

### Scoring Output (minimum viable schema)
```json
{
  "transaction_id": "uuid",
  "fraud_score":    "float [0.0–1.0]",
  "decision":       "enum[ALLOW, FLAG, BLOCK]",
  "rule_triggers":  ["string"],
  "model_version":  "string",
  "latency_ms":     "int"
}
```

### Iceberg Analytics Tables (write path — mandatory per Principle IX)

**`iceberg.enriched_transactions`** — written by the stream processor as a side output
```json
{
  "transaction_id":    "uuid        (partition key)",
  "account_id":        "string",
  "merchant_id":       "string",
  "amount":            "decimal",
  "currency":          "ISO-4217",
  "event_timestamp":   "epoch_ms   (event time — used for Iceberg partitioning by day)",
  "enrichment_time":   "epoch_ms   (processing time — when sink write occurred)",
  "channel":           "enum[POS, WEB, MOBILE, API]",
  "vel_count_1m":      "int",
  "vel_count_5m":      "int",
  "vel_count_1h":      "int",
  "vel_count_24h":     "int",
  "vel_amount_1m":     "decimal",
  "vel_amount_5m":     "decimal",
  "vel_amount_1h":     "decimal",
  "vel_amount_24h":    "decimal",
  "geo_country":       "string",
  "geo_city":          "string",
  "geo_network_class": "enum[RESIDENTIAL, BUSINESS, HOSTING, MOBILE, UNKNOWN]",
  "geo_confidence":    "float [0.0–1.0]",
  "device_first_seen": "epoch_ms",
  "device_txn_count":  "int",
  "device_known_fraud":"bool",
  "schema_version":    "string"
}
```
Partitioned by `event_timestamp` (daily). `transaction_id` is the deduplication key.

**`iceberg.fraud_decisions`** — written by the scoring engine
```json
{
  "transaction_id":  "uuid        (join key to enriched_transactions)",
  "decision":        "enum[ALLOW, FLAG, BLOCK]",
  "fraud_score":     "float [0.0–1.0]",
  "rule_triggers":   "array<string>",
  "model_version":   "string",
  "decision_time":   "epoch_ms   (used for Iceberg partitioning by day)",
  "latency_ms":      "int",
  "schema_version":  "string"
}
```
Partitioned by `decision_time` (daily). `transaction_id` is the deduplication key.

---

### Channel-Specific Extra Fields
| Channel | Required extra fields |
|---|---|
| POS     | `terminal_id`, `entry_mode` (chip/swipe/tap), `mcc` |
| WEB     | `session_id`, `user_agent`, `billing_eq_shipping` (bool), `3ds_result` |
| MOBILE  | `app_version`, `os_version`, `rooted_flag` (bool), `gps_accuracy_m` |
| API     | `api_key_id`, `caller_ip_subnet`, `oauth_scope` |

---

## Technology Stack

### Local (development / CI)
| Role | Tool |
|---|---|
| Message broker | Apache Kafka (Docker) |
| Schema registry | Confluent Schema Registry (Docker) |
| Stream processing | Apache Flink (local mode) or Kafka Streams |
| Hot store | Redis (Docker) |
| Event store | MinIO + Apache Iceberg |
| Feature store | Feast (local SQLite backend) |
| Analytics query engine | Trino (Docker) — queries Iceberg tables on MinIO |
| Analytics UI | Streamlit (Docker) — live dashboard + historical reports |
| BI (optional) | Apache Superset (Docker) — self-serve SQL dashboards |
| ML serving | MLflow local server / ONNX Runtime |
| Orchestration | Docker Compose + Makefile |
| Observability | Prometheus + Grafana (Docker) |

### Cloud (staging / production)
| Role | Tool |
|---|---|
| Message broker | Confluent Cloud · AWS MSK · GCP Pub/Sub |
| Stream processing | Flink on Kubernetes · AWS Kinesis Data Analytics |
| Hot store | AWS ElastiCache (Redis) · Aerospike |
| Event store | S3/GCS + Apache Iceberg · Delta Lake |
| Feature store | Feast on K8s · Hopsworks · Tecton |
| Analytics query engine | AWS Athena · GCP BigQuery · Trino on K8s |
| Analytics UI | Streamlit on K8s (or managed container) |
| BI (optional) | Apache Superset on K8s · AWS QuickSight |
| ML serving | SageMaker · Vertex AI · Seldon |
| Orchestration | Kubernetes · Terraform · Helm |
| Observability | Datadog · Grafana Cloud · AWS CloudWatch |

### Prohibited technology choices
- Batch-only orchestrators (Airflow DAGs) for the scoring path — streaming pipeline only  
- Mutable OLTP databases (Postgres, MySQL) as the primary event store  
- Synchronous HTTP calls between pipeline components during the scoring hot path  
- Any ML framework that cannot serve inference in < 30ms at p99 on target hardware

---

## Repository Structure

```
fraud-detection-streaming/
├── infra/
│   ├── docker-compose.yml        # local full stack
│   ├── docker-compose.dev.yml    # lightweight dev override
│   └── terraform/                # cloud modules (VPC, MSK, EKS, S3, ElastiCache)
├── pipelines/
│   ├── ingestion/                # Kafka producers, schema definitions, PII masking lib
│   ├── processing/               # Flink jobs, feature engineering, velocity aggregations
│   └── scoring/                  # rule engine, ML model client, decision publisher
├── models/
│   ├── training/                 # offline training notebooks, feature selection
│   └── serving/                  # MLflow / ONNX serving wrapper, version registry
├── storage/
│   ├── feature_store/            # Feast repo, feature views, data sources, materialization jobs
│   └── lake/                     # Iceberg table definitions, schema migrations, Trino catalog config
├── analytics/
│   ├── app/                      # Streamlit app (pages: live feed, fraud rate, rules, DLQ inspector)
│   │   ├── pages/
│   │   │   ├── 1_live_feed.py    # real-time Kafka consumer dashboard
│   │   │   ├── 2_fraud_rate.py   # historical Trino → Iceberg reports
│   │   │   ├── 3_rule_triggers.py# rule leaderboard and false-positive analysis
│   │   │   ├── 4_model_compare.py# fraud score distribution per model version
│   │   │   └── 5_dlq_inspector.py# dead-letter queue browser
│   │   └── Home.py               # entry point, shared Kafka/Trino client init
│   ├── consumers/                # Kafka consumer wrapper (group: analytics.dashboard)
│   ├── queries/                  # Trino SQL query library (parameterized, versioned)
│   └── reports/                  # scheduled report definitions (cron + Trino queries)
├── monitoring/
│   ├── dashboards/               # Grafana JSON (latency, DLQ depth, throughput — ops metrics only)
│   └── alerts/                   # Prometheus alerting rules
├── tests/
│   ├── unit/                     # per-component unit tests
│   ├── integration/              # testcontainers-based end-to-end tests
│   └── load/                     # k6 / Locust latency benchmarks
└── README.md
```

---

## Non-Negotiables (Pre-Production Checklist)

- [ ] Idempotent consumers — deduplication on `transaction_id` handles Kafka redeliveries without double-blocking
- [ ] Dead letter queue per stage — no event is silently dropped; DLQ depth alert fires in < 60s
- [ ] Model versioning — every deployed model carries a tagged version; previous version stays live for instant rollback
- [ ] Circuit breaker on ML service — rule engine fallback is active and tested before any ML deployment
- [ ] PII masking — full PAN and full IP never reach any topic; verified by automated integration test
- [ ] Latency budget test — p99 < 100ms verified under 2× expected peak load before promotion to production
- [ ] Schema migration plan — every schema change ships with a consumer migration guide and a deprecation date for the old topic
- [ ] Fraud rate baseline — 24-hour rolling mean established in staging; production alert fires on > 3σ deviation
- [ ] Every component should be tested at least 80% code coverage
- [ ] Analytics sink verified — `iceberg.enriched_transactions` and `iceberg.fraud_decisions` tables queryable via Trino/Athena before production promotion; row count must match Kafka topic offset within 0.1%
- [ ] Feast materialization verified — feature values for a test account are retrievable from the online store within 5 seconds of the enrichment event being published to Kafka
- [ ] Point-in-time correctness — offline Feast feature snapshots for a replayed event sequence match the values observed at enrichment time (no future leakage)
- [ ] Analytics consumer isolation verified — `analytics.dashboard` consumer group offset does not interfere with scoring or processing consumer groups under load
- [ ] Streamlit live feed lag — real-time dashboard reflects a new decision within 2 seconds of it being published to `txn.decisions` under normal load
- [ ] Streamlit historical report accuracy — fraud rate figures from Streamlit/Trino match the figures from a direct Iceberg table scan for the same time window (zero discrepancy)
- [ ] Analytics service failure isolation — taking down the Streamlit service and Trino container has zero effect on fraud scoring latency and throughput SLOs

---

## Governance

This constitution supersedes all other architectural decisions, ADRs, and team conventions for the FraudStream project. Any component, PR, or design that conflicts with a Core Principle is blocked until resolved.

**Amendments** require:
1. A written rationale explaining why the principle is insufficient
2. An impact assessment covering affected components and latency budget
3. Approval from at least two senior engineers and the data engineering lead
4. A migration plan with a completion date before the amendment takes effect

All pull requests must include a checklist item confirming compliance with the relevant principle(s). Complexity must be justified — if a simpler approach meets the latency budget and data contract requirements, the simpler approach wins.

---

**Version**: 1.2.0 | **Ratified**: 2026-03-30 | **Last Amended**: 2026-04-03