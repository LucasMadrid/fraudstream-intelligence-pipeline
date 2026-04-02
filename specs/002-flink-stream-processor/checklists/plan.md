# Plan Quality Checklist: Stateful Stream Processor

**Purpose**: Validate completeness, justification, and consistency of technical decisions in plan.md — covering dependency choices, architecture trade-offs, testing strategy, and operational readiness
**Created**: 2026-04-01
**Feature**: [spec.md](../spec.md) | [plan.md](../plan.md)
**Audience**: Author (self-review), Peer reviewer (PR), Architecture review
**Depth**: Lightweight critical gates + thorough sweep

---

## Dependency & Technology Decisions

- [x] CHK001 — Is the rationale for choosing RocksDB over HashMapStateBackend documented, given that at 4 GB state the entire key space could fit in heap? The plan names RocksDB but does not explain why off-heap/disk-spilling was preferred over a simpler in-heap backend. [Clarity, Plan §Storage] — PASS: research.md §5 documents disk-spilling support and 5-15× checkpoint size reduction via incremental SST snapshots as justification.
- [x] CHK002 — Is the PyFlink 1.19 version pinned with a justification, and is the Python 3.11 + PyFlink 1.19 compatibility matrix explicitly validated? PyFlink version support per Python version changes across minor Flink releases. [Completeness, Traceability, Plan §Technical Context] — PASS: research.md §1 validates the combination; Arrow batching overhead measured at ~10-50 µs amortized per record.
- [x] CHK003 — Is the `confluent-kafka[schema-registry]` dependency's scope explicitly limited to "bootstrap only" in the plan, and is the boundary between the Flink Kafka connector (runtime) and the Python Schema Registry client (startup) documented? [Clarity, Plan §Technical Context] — PASS: plan.md §Technical Context explicitly states "bootstrap only; Flink Kafka connector handles consumer/producer".
- [x] CHK004 — Is the `geoip2` LRU cache strategy (referenced in spec Assumptions) specified in the plan — including cache size, eviction policy, and behaviour when the GeoLite2 database is reloaded? Without this, cache invalidation on DB update is undefined. [Completeness, Gap, Plan §Technical Context] — PASS: plan.md §Security Requirements §GeoLite2 Database Update Policy documents that the LRU cache must be explicitly invalidated on mmdb reload via a file-modification-time check at each bundle boundary (every bundle.time=15ms).

---

## Configuration & Operational Constraints

- [x] CHK005 — Is the relationship between checkpoint interval (FR-008 default: 60s), recovery window (SC-003: recover within 60s), and checkpoint duration target (<10s) documented as an explicit sizing constraint in the plan? These three values together bound the recovery window and must be consistent. [Consistency, Completeness, Plan §Technical Context] — PASS: `flink-conf.yaml` now sets `execution.checkpointing.interval: 60000` with a sizing comment linking FR-008 (60s interval), SC-003 (60s recovery window), and the checkpoint duration budget (<10s) as a consistent triple.
- [x] CHK006 — Are Flink TaskManager memory settings documented to prevent RocksDB off-heap usage from triggering container OOM kills? RocksDB bypasses JVM heap and requires explicit `taskmanager.memory.managed.fraction` tuning. [Completeness, Gap, Plan §Storage] — PASS: `flink-conf.yaml` now sets `taskmanager.memory.managed.fraction: 0.4` with a comment explaining that 40% of 4g process memory = ~1.6GB managed, consistent with 2 slots × 512mb fixed-per-slot. plan.md §Observability Requirements §Checkpoint SLI also references this budget.
- [x] CHK007 — Are the Flink parallelism settings documented in the plan, and are they traceable to the 5,000 TPS/node throughput target (SC-002)? Parallelism directly determines per-node throughput capacity and must be justified. [Completeness, Traceability, Plan §Technical Context] — PASS: `flink-conf.yaml` now sets `parallelism.default: 2` with a comment linking it to SC-002's 5,000 TPS/node target and Arrow bundle overhead (~10-50 µs amortised), justifying 2 parallel subtasks for dev/staging load.
- [x] CHK008 — Is the MinIO-to-S3 migration path documented, including whether checkpoint storage URIs are environment-specific and how they are configured per deployment target (local vs. cloud)? [Completeness, Gap, Plan §Storage] — PASS: plan.md §Security Requirements §MinIO-to-S3 Migration Path documents the 4-step migration procedure including: `flink-s3-fs-hadoop` endpoint configuration, clean-start requirement (no cross-backend checkpoint migration), and 30-day MinIO checkpoint retention for rollback diagnosis.
- [x] CHK009 — Is the GeoLite2 database update strategy documented — specifically, how stale the embedded database can become before enrichment quality degrades meaningfully, and who triggers updates in production? [Completeness, Gap, Plan §Storage] — PASS: plan.md §Security Requirements §GeoLite2 Database Update Policy documents MaxMind's Tuesday/Friday release cadence, weekly refresh requirement, and scheduled rolling-restart mechanism.

---

## Schema & Artefact Consistency

- [x] CHK010 — Is the schema mirror at `pipelines/processing/schemas/enriched-txn-v1.avsc` documented as authoritative vs. the copy at `specs/.../contracts/enriched-txn-v1.avsc`? If both can exist independently, the plan must specify which is the source of truth and how divergence is detected. [Consistency, Plan §Project Structure] — PASS: plan.md §Security Requirements §Schema Source of Truth designates `specs/002-flink-stream-processor/contracts/` as authoritative; `pipelines/processing/schemas/` is the deploy-time copy. Divergence detected by CI diff; contracts/ wins on conflict.
- [x] CHK011 — Is the `processor_version` field's source of truth (package version, git tag, build artifact tag) documented in the plan, and is the population mechanism (injected at build time, env var at runtime?) specified? [Clarity, Plan §Project Structure] — PASS: `enriched-txn-v1.avsc §processor_version` doc now specifies "Populated at job startup from the PROCESSOR_VERSION environment variable. Format: '{feature-branch}@{semver}'". plan.md §Implementation Readiness Notes §Savepoint Compatibility Runbook also references minor version bump on state schema change.

---

## Testing Strategy

- [x] CHK012 — Is the Hypothesis property-based test strategy specific enough to cover the three key velocity window invariants: (a) window boundary correctness, (b) TTL eviction, and (c) concurrent partition safety (per spec US2-Scenario 3)? A generic "property-based tests" reference does not guarantee these edge cases are covered. [Completeness, Plan §Project Structure] — PASS: plan.md §Implementation Readiness Notes §Hypothesis Property-Based Test Invariants enumerates all three invariants with explicit test conditions: (a) event at window_start+duration-1ms must NOT contribute to next window, (b) 24h gap must reset vel_count_24h to 1 after TTL eviction, (c) cross-partition deduplication explicitly out of scope (TD-003).
- [x] CHK013 — Is the Flink mini-cluster integration test approach validated as supporting two-phase commit Kafka sinks? Mini-cluster mode may not support the exactly-once transactional Kafka sink required by FR-015, which would make integration tests unable to validate the feature's hardest requirement. [Completeness, Gap, Plan §Project Structure] — PASS: plan.md §Implementation Readiness Notes §Mini-Cluster Exactly-Once Validation Caveat documents that mini-cluster does NOT support two-phase commit Kafka sinks; tests must use Testcontainers Kafka for exactly-once guarantee validation, and AT_LEAST_ONCE + idempotent consumer for mini-cluster tests.
- [x] CHK014 — Is there a documented integration test scenario that simulates a mid-processing job kill and validates: (a) no duplicate enriched output records, (b) velocity state matches pre-failure values after recovery, and (c) consumer offset resumes from the correct checkpoint position? [Completeness, Gap, Plan §Project Structure, Spec §FR-015] — PASS: plan.md §Implementation Readiness Notes §Job Kill and Recovery Integration Test documents all three assertions with implementation guidance (Testcontainers + Thread.interrupt() approach).

---

## Deployment & Rollback

- [x] CHK015 — Is the rollback strategy documented for a failed deployment — specifically, can a Flink job at the previous version resume from a checkpoint taken by the newer version, and is savepoint compatibility explicitly checked? State schema changes between versions can make checkpoints incompatible. [Completeness, Gap, Plan §Summary] — PASS: plan.md §Implementation Readiness Notes §Savepoint Compatibility Runbook documents the 5-step pre-upgrade procedure: manual savepoint, staging validation, state migration function for incompatible schema changes, 90-day savepoint retention, and PROCESSOR_VERSION minor bump policy.

---

## Run Results

**Date**: 2026-04-01 (initial run) → Updated: 2026-04-01
**Pass**: 15 / 15
**Fail**: 0 / 15

All gaps resolved. CHK005/CHK006/CHK007 resolved via flink-conf.yaml additions (checkpoint interval, managed memory fraction, parallelism). CHK004/CHK008/CHK009/CHK012/CHK013/CHK014/CHK015 resolved via new plan.md sections (Observability Requirements, Security Requirements, Implementation Readiness Notes). CHK010/CHK011 resolved via schema doc updates + plan.md §Schema Source of Truth.

