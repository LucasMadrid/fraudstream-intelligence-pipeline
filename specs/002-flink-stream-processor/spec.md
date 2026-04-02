# Feature Specification: Stateful Stream Processor — Transaction Feature Enrichment

**Feature Branch**: `002-flink-stream-processor`
**Created**: 2026-03-31
**Status**: Draft
**Input**: User description: "lets focus on Processing — a stateful stream processor consumes from Kafka, joins each transaction against historical aggregates, extracts velocity, geolocation, and device fingerprint features, and passes the enriched record to the scoring engine. They should maintain state across the cluster and recovery step, checkpoints for failures"

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Real-Time Transaction Enrichment (Priority: P1)

A transaction arrives from the API ingestion layer. The processing system reads the raw transaction event, enriches it with velocity signals computed from the account's recent history, geolocation context derived from the IP subnet, and device fingerprint signals — then forwards the fully enriched record downstream to the fraud scoring engine without perceptible added latency.

**Why this priority**: This is the core pipeline capability. Without end-to-end enrichment, the scoring engine has no signals to act on. Every other story depends on this path being functional.

**Independent Test**: Can be tested by publishing a synthetic transaction event and asserting the downstream enriched record contains all required feature fields populated with correct values, within the latency budget.

**Acceptance Scenarios**:

1. **Given** a valid transaction event on the ingestion topic, **When** the processor receives it, **Then** the enriched output record contains velocity features, geolocation context, device fingerprint signals, plus all original transaction fields — delivered within 50 ms end-to-end processing time.
2. **Given** a transaction from an account with no prior history, **When** the processor enriches it, **Then** velocity count is 1 (the current transaction is always included in its own window), geolocation resolves from the IP, device fingerprint is populated from available fields, and the record is still forwarded (cold-start is not an error).
3. **Given** a transaction with an unresolvable IP subnet, **When** geolocation lookup fails, **Then** geolocation fields are set to null/unknown and enrichment continues rather than dropping the record.
4. **Given** a transaction where velocity aggregation succeeds but both geolocation lookup and device profile lookup fail, **When** the processor enriches the record, **Then** geo fields and device fields are set to null, velocity features are populated with correct values, and the record is forwarded to the output topic — partial enrichment failure does not drop the record.

---

### User Story 2 — Velocity Feature Extraction (Priority: P2)

The processor maintains rolling-window counters for each account: transaction count and total amount over the last 1 minute, 5 minutes, 1 hour, and 24 hours. These aggregates are updated in real time as each transaction is processed and are immediately available to enrich the next transaction from the same account.

**Why this priority**: Velocity is the primary feature family used by fraud models. Accuracy and recency of velocity signals directly determine model quality.

**Independent Test**: Can be tested by replaying a controlled sequence of transactions for the same account and asserting that velocity counts and amounts at each step reflect only events within the correct rolling window boundaries.

**Acceptance Scenarios**:

1. **Given** an account with 5 transactions in the last 3 minutes, **When** a 6th transaction arrives, **Then** the 5-minute count is 6 and the 1-minute count reflects only those events within the last 60 seconds.
2. **Given** transactions that cross a window boundary (e.g., oldest falls outside the 1-hour window), **When** a new transaction arrives, **Then** expired events are no longer counted and amounts are removed from the running total.
3. **Given** two concurrent transactions for the same account processed in parallel partitions, **When** both are enriched, **Then** the velocity counts are consistent and do not produce duplicate increments.

---

### User Story 3 — Fault-Tolerant State Recovery (Priority: P3)

If the processing job fails or restarts, the system recovers all in-flight state (velocity aggregates, device histories, geolocation cache) from the most recent checkpoint and resumes processing from the correct Kafka offset without manual intervention or data loss.

**Why this priority**: Without reliable recovery, any outage causes permanent state corruption — velocity windows reset to zero and the scoring engine temporarily receives under-enriched records, increasing false-negative rates.

**Independent Test**: Can be tested by forcing a processor restart mid-stream and asserting that after recovery: (a) the Kafka consumer resumes from the committed offset, (b) velocity counts for an active account match pre-failure values, and (c) no duplicate enriched records are emitted.

**Acceptance Scenarios**:

1. **Given** the processor has been running for 10 minutes and has accumulated velocity state, **When** the job is killed and restarted, **Then** it resumes from the last checkpoint, velocity aggregates match the pre-failure state, and no events within the checkpoint interval are reprocessed twice.
2. **Given** a checkpoint is in progress when a failure occurs, **When** the job restarts, **Then** it falls back to the most recent completed checkpoint and replays only events after that point.
3. **Given** a network partition isolates one processing node, **When** connectivity is restored and the node rejoins, **Then** state is reconciled from the checkpoint without operator intervention.

---

### User Story 4 — Device Fingerprint Feature Extraction (Priority: P4)

The processor tracks per-device transaction history keyed on a stable device identifier extracted from the transaction payload. It records first-seen timestamp, lifetime transaction count, and whether the device has previously been associated with fraudulent activity — and exposes these as features on each enriched record.

**Why this priority**: Device fingerprinting provides a signal orthogonal to velocity (same device, different accounts) that is especially valuable for account-takeover detection. It can be delivered after the core velocity path is operational.

**Independent Test**: Can be tested by replaying transactions that share a device identifier and asserting that the enriched record's device feature fields correctly reflect accumulated history up to each event.

**Acceptance Scenarios**:

1. **Given** a transaction from a device seen for the first time, **When** it is enriched, **Then** `device_first_seen` is set to the current event time, `device_txn_count` is 1, and `device_known_fraud` is false.
2. **Given** a device that has appeared in 20 prior transactions, **When** the 21st transaction arrives, **Then** `device_txn_count` is 21 and `device_first_seen` remains the original timestamp.
3. **Given** a device identifier absent from the transaction payload, **When** the processor enriches the record, **Then** device feature fields are null and processing continues without error.

---

### Edge Cases

- What happens when the input Kafka topic is unavailable at startup? The processor must not begin writing enriched output until it has successfully consumed its first record or confirmed topic connectivity.
- How does the system handle malformed or schema-invalid events on the input topic? Invalid events must be routed to a dead-letter queue without blocking the main processing path.
- What happens if an IP subnet maps to multiple geographic regions? The system should select the most specific match available and record resolution confidence.
- How does state grow unbounded over time? Velocity state keyed on inactive accounts must expire after a configurable idle TTL (default: 7 days) to prevent unbounded storage growth.
- What happens when the checkpoint storage backend is temporarily unavailable? The processor must continue processing but alert on checkpoint failures and fall back to the previous successful checkpoint on restart.
- How does the system behave when the downstream output is full or unavailable? Back-pressure must propagate upstream rather than silently dropping enriched records.
- What happens when event timestamps are out of order (late-arriving events)? Events behind the watermark but within the allowed-lateness window must trigger a corrective aggregate update and re-emit the corrected enriched record. Events beyond the allowed-lateness window must be routed to the dead-letter queue — never silently discarded.
- What happens when all partitions stall and the watermark stops advancing? The processor must detect watermark stalls and alert; windows must not be held open indefinitely waiting for a watermark that never arrives.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The processor MUST consume transaction events from the ingestion Kafka topic continuously using a stable, named consumer group, and maintain consumption offset as durable, recoverable state. On first start (no prior checkpoint), the consumer group MUST begin from the earliest available offset on the input topic; on recovery it MUST resume from the offset stored in the most recent completed checkpoint. Continuous means the processor maintains an open consumer loop with no scheduled idle periods; the end-to-end latency bound is defined by SC-001.
- **FR-002**: The processor MUST compute velocity features per account over four rolling time windows — 1 minute, 5 minutes, 1 hour, and 24 hours — capturing both transaction count and total amount for each window, driven by event time (consistent with FR-013). Amount aggregation is performed in the native currency of each transaction; cross-currency normalization is deferred to v2 (see Tech Debt §TD-001).
- **FR-003**: The processor MUST resolve geolocation context (country, city, and IP classification) from the IP subnet field present on each transaction event. IP classification MUST be one of: `RESIDENTIAL`, `BUSINESS`, `HOSTING`, `MOBILE`, `UNKNOWN`. Resolution confidence is a float in [0.0, 1.0]: 1.0 = exact city-level match, 0.5 = country-level match only, 0.0 = failed lookup; exposed as `geo_confidence` in the enriched record.
- **FR-004**: The processor MUST extract and accumulate device fingerprint features (first-seen timestamp, lifetime transaction count, fraud association flag) keyed on the device identifier from the transaction payload. In v1, `api_key_id` (the API channel caller identifier present in `txn_api_v1`) serves as the device proxy identifier; a dedicated `device_id` field is deferred to schema v2 (see `data-model.md §5`). In v1, `device_known_fraud` is always `false`; the flag is reserved in the schema for forward compatibility and will be populated via a scoring-engine feedback loop defined in a future spec (see Tech Debt §TD-002).
- **FR-005**: The processor MUST join each incoming transaction with its current velocity, geolocation, and device feature values and produce a single enriched record containing all original fields plus all extracted feature fields.
- **FR-006**: The processor MUST publish enriched records to a dedicated Kafka output topic (e.g., `txn.enriched`) from which the scoring engine independently consumes, using `account_id` as the Kafka message key to guarantee per-account ordering on the output topic. The processor is not responsible for scoring decisions; it publishes enriched records and moves on. The scoring engine subsequently emits its decision to a separate Kafka topic consumed by action handlers (block response, webhook alert, monitoring dashboard).
- **FR-007**: The processor MUST be deployed with a minimum of two processing nodes in production environments and MUST distribute and replicate state across all nodes such that the failure of any single node does not result in state loss for accounts being processed by surviving nodes. When a node fails, its partitions MUST be redistributed among surviving nodes and resume processing from the most recent completed checkpoint — no partition may remain unprocessed during a single-node failure.
- **FR-008**: The processor MUST take periodic snapshots of all keyed state and consumer offsets to durable external storage at a configurable interval (default: 60 seconds).
- **FR-009**: On restart after failure, the processor MUST automatically restore all state from the most recent completed snapshot and resume consuming from the source offset **stored within** that checkpoint (inclusive) — with no manual operator steps required.
- **FR-010**: Events that fail schema validation or cannot be deserialized MUST be forwarded to a dead-letter queue conforming to the `processing-dlq-v1.avsc` schema defined in `contracts/`, and MUST NOT block processing of subsequent valid events.
- **FR-011**: Velocity state for accounts that have been idle longer than a configurable TTL (default: 7 days) MUST be automatically evicted to bound state storage growth. Eviction MUST be event-time-driven — triggered when the account's last-seen event-time bucket advances past the TTL threshold — never by processing-time timers (consistent with FR-013).
- **FR-012**: The processor MUST expose processing lag (offset lag from source topic), enrichment latency (p50/p99), and checkpoint duration as observable metrics. Enrichment latency is measured from Kafka consumer receipt of the raw record to the enriched record write acknowledged by the output topic broker.
- **FR-013**: The processor MUST advance a per-partition event-time watermark derived from transaction event timestamps, using a configurable maximum out-of-orderness bound (default: 10 seconds). All window computations MUST be driven by event time, not processing time.
- **FR-014**: The processor MUST handle late-arriving events — those whose event timestamp falls behind the current global watermark (the minimum across all per-partition watermarks, consistent with FR-013) — within a configurable allowed-lateness window (default: 30 seconds), measured from that global watermark. Late events within this window MUST trigger a corrective update to the affected rolling aggregates and a re-emission of the corrected enriched record. The corrected record is **appended** to the output topic with the same `transaction_id` and an updated `enrichment_time`; the original record is not retracted. Downstream consumers MUST treat the latest record per `transaction_id` as authoritative. Late events beyond the allowed-lateness window MUST be forwarded to the dead-letter queue with reason `LATE_EVENT_BEYOND_ALLOWED_LATENESS`.
- **FR-015**: The processor MUST guarantee exactly-once processing semantics end-to-end: each input event is reflected in the velocity aggregates exactly once and produces exactly one enriched output record, even under failure and recovery. This requires atomic coordination between state snapshots, input offset commits, and output topic writes. In the event of a partial failure — where the state snapshot has completed but the output topic write has not been confirmed — the processor MUST reprocess the affected event from the last completed checkpoint on recovery, restoring coherence between velocity state and enriched output. Silent divergence (state advanced, output missing) is not an acceptable recovery outcome. `transaction_id` serves as the idempotency key for enriched output records; downstream consumers MUST treat the latest record per `transaction_id` as authoritative (consistent with FR-014 late-event append semantics).

### Key Entities

- **RawTransaction**: The event consumed from the ingestion topic — contains masked card BIN/last4, masked IP subnet, account identifier, device identifier, merchant, amount, currency, channel, and event timestamp.
- **VelocityAggregate**: Per-account rolling state — transaction counts and total amounts for 1m, 5m, 1h, and 24h windows. Keyed by `account_id`. Expires after idle TTL.
- **GeolocationContext**: Derived from the IP subnet — country code, city, network classification, and resolution confidence. May be cached per subnet prefix to avoid redundant lookups.
- **DeviceProfile**: Per-device accumulated state — first-seen timestamp, lifetime transaction count, fraud association flag. Keyed by `api_key_id` in v1 (see FR-004 and TD-002 for v2 migration to `device_id`). Expires after idle TTL.
- **EnrichedTransaction**: The output record — all RawTransaction fields plus the full velocity feature set, geolocation context, and device profile fields. Forwarded to the scoring engine.
- **ProcessorCheckpoint**: A consistent snapshot of all keyed state stores and consumer offsets at a point in time, persisted to durable storage. Used exclusively for recovery.
- **DeadLetterRecord**: A malformed or unprocessable event — includes raw bytes, error reason, and source topic/partition/offset for reprocessing.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: End-to-end enrichment latency (event ingested to enriched record available downstream) is under 50 ms at p99 under normal operating load (5,000 TPS/node, consistent with SC-002).
- **SC-002**: The processor sustains throughput of at least 5,000 transactions per second per processing node without increasing lag on the source topic.
- **SC-003**: After a job failure and automatic restart, the processor recovers and resumes processing within 60 seconds with zero data loss — defined as: (a) no enriched output records dropped (every input event that was processed before the failure produces exactly one enriched record on the output topic after recovery, consistent with FR-015), and (b) no velocity state increments lost (every input event is reflected in the velocity aggregates exactly once — not zero times and not twice). These are distinct failure modes: an output-channel loss means the state advanced but the record was not written; a state-channel loss means the event was consumed but the aggregate was never incremented.
- **SC-004**: Velocity feature values have less than 0.1% error rate for single-partition per-account event sequences, measured by replaying the same event sequence and comparing per-account feature snapshots over any 24-hour window. For accounts whose transactions span multiple partitions, the correctness guarantee is that counts and amounts are exact (no double-counting, no omissions); per-event ordering within a time bucket is non-deterministic and excluded from the error-rate measurement.
- **SC-005**: State storage grows sub-linearly with active account count — velocity state for 1 million active accounts occupies less than 4 GB per processing node.
- **SC-006**: 100% of events that fail deserialization appear in the dead-letter queue within 5 seconds of failure — none are silently dropped.
- **SC-007**: The processor achieves exactly-once delivery of enriched records — no event is counted twice in velocity aggregates and no duplicate enriched record appears on the output topic under any failure or recovery scenario.
- **SC-008**: Late-arriving events within the allowed-lateness window produce a corrected enriched record on the output topic within 5 seconds of their arrival. Events beyond the window are observable in the dead-letter queue within 5 seconds — none are silently dropped.

## Assumptions

- The input Kafka topic is the `txn.api` topic produced by feature 001 (API ingestion), with events in Avro format conforming to the `txn_api_v1` schema.
- Transaction events already carry a masked IP address (subnet-level) and a device identifier field; no additional PII masking is required in the processing layer.
- A geolocation database (e.g., embedded GeoLite2 or equivalent) is available to the processor at deployment time; per-event calls to an external paid geolocation API are out of scope.
- "Historical aggregates" are maintained entirely within the processor's own state — there is no external pre-computed batch feature store to join against in v1.
- The cluster runs with at least 2 processing nodes for high availability; single-node deployments are supported for local development only.
- Exactly-once processing is a hard requirement (FR-015), not a nice-to-have. The output Kafka topic and the checkpoint/offset commit mechanism must be configured to support atomic, transactional writes.
- The "tool choices" in the user description refer to Apache Flink as the stream processing runtime; this spec describes the WHAT independently of implementation technology, but planning should validate Flink's fit given the project's existing Python tooling.
- Geolocation lookup results may be cached in-process for subnet prefixes that appear frequently; cache invalidation strategy is deferred to planning.
- The dead-letter queue topic (`txn.processing.dlq`) MUST be provisioned with at least 4 partitions and a minimum retention period of 48 hours. Under normal operation DLQ volume is a small fraction of total TPS; the 48-hour retention is sized to allow a DLQ consumer outage of up to one business day without message loss. SC-006's 5-second SLA is only achievable if the topic is not full; operators MUST monitor DLQ consumer lag and alert before retention is exhausted. Exact partition and retention sizing for the cloud deployment is deferred to infrastructure planning.

## Tech Debt

Known limitations accepted for v1 MVP. Each item must be addressed before the feature it affects can be considered production-complete.

- **TD-001** *(FR-002)*: Velocity `vel_amount_*` fields aggregate in the native currency of each transaction. Accounts with multi-currency activity will have amount features that are economically meaningless (e.g., USD + EUR summed). Cross-currency normalization (e.g., convert to a base currency at enrichment time using a reference rate) is deferred to v2. Fraud models consuming these features must be aware of this limitation and treat amount features as unreliable for multi-currency merchants.

- **TD-002** *(FR-004)*: `device_known_fraud` is hardcoded to `false` in v1. There is no feedback loop from the scoring engine or any external signal to set this flag. The field is present in the enriched schema for forward compatibility only. A future spec must define the write path (e.g., scoring engine emits fraud decisions to a `txn.fraud.decisions` topic that the processor consumes to update device state).

- **TD-003** *(FR-003)*: `geo_network_class` is hardcoded to `RESIDENTIAL` in v1. The GeoLite2-City database does not expose a connection-type field; a separate GeoLite2-Connection-Type database is required for accurate classification (`RESIDENTIAL`, `BUSINESS`, `HOSTING`, `MOBILE`, `UNKNOWN`). Fraud models consuming this field must treat it as unreliable in v1. The fix requires adding the GeoLite2-Connection-Type mmdb to the operator's open path and deriving the enum from `record.connection_type.user_type`.

- **TD-004** *(Contract §schema_version)*: The `schema_version` field is a `string` with default `"1"` and a comment indicating it signals a topic rename at v2. No formal upgrade procedure, compatibility-mode alignment, or v2 trigger criteria are defined. Before any v2 work begins, a schema evolution spec must define: (a) what constitutes a v2-triggering change, (b) whether v2 ships as `txn.enriched.v2` (topic rename) or a new Schema Registry subject version, (c) how consumers migrate across the topic boundary without a processing gap, and (d) how `schema_version` value is incremented at job startup.

- **TD-005** *(Contract §namespace)*: The Avro namespace `com.fraudstream.processing.enrichment` is not designated as stable in any document. No governance rule currently treats a namespace change as a breaking change. Before any refactor that touches the namespace (e.g., package reorganisation), a decision must be recorded: namespace changes must be treated as breaking changes requiring a Schema Registry subject rename and consumer migration equivalent to a topic-rename event.
