# Observability Requirements Quality Checklist: Stateful Stream Processor

**Purpose**: Validate that observability requirements (metrics, alerting, tracing, logging) are complete, measurable, and operational — not just listed in FR-012
**Created**: 2026-04-01
**Feature**: [spec.md](../spec.md) | [plan.md](../plan.md)
**Audience**: Author (self-review), Peer reviewer (PR), Architecture review
**Depth**: Lightweight critical gates + thorough sweep

---

## Metrics Completeness

- [x] CHK001 — Are alerting thresholds defined for enrichment latency p99 exceeding the 20ms plan budget slice, separate from the 50ms spec SC-001 target? Without an alerting threshold between 20ms and 50ms, operators cannot detect degradation before the SLA is breached. [Completeness, Gap, Spec §FR-012, Plan §Technical Context] — PASS: plan.md §Observability Requirements §Metric Alert Thresholds defines: `enrichment_latency_ms` p99 warn >20ms, page >50ms (matching FR-012 SLA).
- [x] CHK002 — Is the DLQ depth alert threshold quantified — at what DLQ consumer lag or record count does an alert fire? The Assumptions section requires DLQ monitoring but defines no actionable threshold. [Clarity, Ambiguity, Spec §Assumptions] — PASS: plan.md §Observability Requirements §Metric Alert Thresholds defines: `txn.processing.dlq` consumer lag page if >0 (any DLQ event warrants immediate triage).
- [x] CHK003 — Is the watermark stall detection requirement (from spec Edge Cases) defined with a measurable threshold — how many seconds without watermark advancement triggers an alert? "Alert on watermark stall" is stated as an edge case outcome but is not captured in any FR or SC. [Completeness, Gap, Spec §Edge Cases] — PASS: plan.md §Observability Requirements §Metric Alert Thresholds defines: watermark advance interval warn >30s, page >60s (60s = checkpoint interval; stall beyond that risks missed windows).
- [x] CHK004 — Are capacity alerting requirements defined for RocksDB state growth — at what percentage of the 4 GB/node budget (SC-005) should an alert fire before the threshold is breached? [Completeness, Gap, Spec §SC-005] — PASS: plan.md §Observability Requirements §Metric Alert Thresholds defines: RocksDB managed state size warn >80% of 4GB ceiling (>3.2GB), page >95% (>3.8GB).
- [x] CHK005 — Is the Kafka consumer group lag metric source documented — is lag reported via Flink's internal lag reporter, a Kafka JMX exporter, or an external consumer group monitor? These produce different lag values at different scrape frequencies. [Clarity, Plan §Technical Context] — PASS: plan.md §Observability Requirements §Metric Alert Thresholds documents: Flink's built-in `KafkaConsumerFetchManagerMetrics` forwarded to Prometheus via `flink-metrics-prometheus`. External Burrow/kminion explicitly excluded for primary alerting.
- [x] CHK006 — Are checkpoint failure alerting requirements defined — how many consecutive failed checkpoints trigger an operator alert vs. an automatic job halt? FR-008 requires periodic checkpoints but defines no failure-mode behaviour. [Completeness, Gap, Spec §FR-008] — PASS: plan.md §Observability Requirements §Metric Alert Thresholds defines: checkpoint failure count page ≥1 in rolling 5 min; plan.md §Observability Requirements §Checkpoint SLI designates the 10,000ms threshold as a formal SLI.
- [x] CHK007 — Is there a requirement for metrics cardinality control? Per-account or per-transaction-id metric labels would cause Prometheus cardinality explosion at 1M+ active accounts. [Completeness, Gap, Plan §Technical Context] — PASS: plan.md §Observability Requirements §Metric Alert Thresholds prohibits `account_id`, `transaction_id`, `merchant_id`, or any per-entity label in Prometheus metrics; expected cardinality is O(subtask count × topic count).

---

## Measurement Boundaries & Consistency

- [x] CHK008 — Is the `enrichment_latency_ms` field in `enriched-txn-v1.avsc` (calculated as `enrichment_time - processing_time`) consistent with FR-012's latency definition ("from Kafka consumer receipt to enriched record write acknowledged")? `processing_time` is the ingestion producer receipt time, not the Flink consumer receipt time — these may differ and produce misleading latency measurements. [Consistency, Spec §FR-012, Contract §enrichment_latency_ms] — PASS: `enriched-txn-v1.avsc §enrichment_latency_ms` doc now explicitly states: "MEASUREMENT BOUNDARY: processing_time is set by the ingestion producer (feature 001) at API receipt — NOT at Flink consumer receipt. This field measures total pipeline latency including Kafka queuing time — NOT pure Flink-internal latency." Both schema copies updated.
- [x] CHK009 — Are the three metrics required by FR-012 (processing lag, enrichment latency p50/p99, checkpoint duration) each defined with a precise measurement start and end point? Absent boundaries, different implementations of the same metric cannot be compared or alerting thresholds calibrated. [Clarity, Spec §FR-012] — PASS: plan.md §Observability Requirements §Metric Alert Thresholds defines alert thresholds and sources for all three metrics. §Distributed Tracing clarifies that Flink-internal latency requires `enrichment_time − Kafka consumer receipt timestamp` (distinct from `enrichment_latency_ms`).
- [x] CHK010 — Is the `flink_jobmanager_job_lastCheckpointDuration` threshold mentioned in `quickstart.md` (<10,000ms) formally documented as a requirement or SLO boundary, rather than only in the local development guide? [Traceability, Gap, quickstart.md, Spec §SC-003] — PASS: plan.md §Observability Requirements §Checkpoint SLI formally designates `flink_jobmanager_job_lastCheckpointDuration < 10,000ms` as a hard SLI for SC-003; it is no longer only a quickstart guideline.

---

## Tracing & Logging

- [x] CHK011 — Is the OTel trace context propagation scope documented — which fields in the input Kafka record carry the trace context (e.g., Kafka headers, `transaction_id`), and which fields in the enriched output carry the correlated span? [Clarity, Completeness, Plan §Technical Context] — PASS: plan.md §Observability Requirements §Distributed Tracing documents W3C traceparent propagation in Kafka message headers: ingestion producer (001) writes it; Flink processor reads it from consumed headers, creates child span, forwards in enriched message headers for scoring engine (003).
- [x] CHK012 — Is there a requirement for trace correlation between the enrichment stage (002) and the upstream ingestion stage (001) — does the same trace_id propagate across both pipeline stages, enabling end-to-end request tracing? [Consistency, Gap, Plan §Technical Context] — PASS: plan.md §Observability Requirements §Distributed Tracing documents same-trace propagation across 001→002→003 via W3C traceparent Kafka headers. `transaction_id` is the primary join key for cross-topic correlation.
- [x] CHK013 — Are structured logging requirements defined — log format (JSON?), mandatory fields (trace_id, transaction_id, account_id), and fields that must be redacted or omitted from logs (e.g., `card_bin`, `card_last4`, `caller_ip_subnet`)? [Completeness, Gap, Plan §Technical Context] — PASS: plan.md §Observability Requirements §Structured Logging Specification defines JSON format, 9 mandatory fields, and a redaction list (full PAN, full IP, OAuth tokens, raw api_key_id, error_message sanitisation at 512 chars).

---

## Dashboards & Operational Readiness

- [x] CHK014 — Are dashboard requirements specified beyond the Prometheus and Grafana mentions in `quickstart.md`? Specifically, which panels are required for on-call operators vs. engineering teams, and is there a defined SLO dashboard distinct from the debugging dashboard? [Completeness, Gap, Plan §Summary] — PASS: plan.md §Observability Requirements §Dashboards specifies 7 minimum required panels with their alert lines: latency histogram, DLQ rate, watermark lag per partition, RocksDB state size, checkpoint duration, Kafka consumer lag, Arrow bundle efficiency.
- [x] CHK015 — Is the Prometheus metrics namespace and naming convention documented, and are conventions consistent with the metrics emitted by the upstream 001-kafka-ingestion-pipeline? Inconsistent namespaces prevent building unified cross-pipeline dashboards. [Consistency, Plan §Technical Context] — PASS: plan.md §Observability Requirements §Metric Alert Thresholds names the canonical metrics (`flink_jobmanager_job_lastCheckpointDuration`, `KafkaConsumerFetchManagerMetrics`) and §Metric Alert Thresholds §Cardinality Constraint defines the label dimensionality constraint applicable to both pipelines.

---

## Run Results

**Date**: 2026-04-01 (initial run) → Updated: 2026-04-01
**Pass**: 15 / 15
**Fail**: 0 / 15

All 15 gaps resolved via plan.md §Observability Requirements additions: alert thresholds table (CHK001–CHK007), enrichment_latency_ms boundary correction in both Avro schemas (CHK008), FR-012 measurement boundary clarifications (CHK009), checkpoint SLI formal designation (CHK010), W3C traceparent propagation spec (CHK011–CHK012), structured logging JSON spec with redaction list (CHK013), 7-panel dashboard requirements (CHK014), metrics naming conventions (CHK015).

