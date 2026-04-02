# Specification Quality Checklist: FR & SC Uniform Sweep

**Purpose**: Validate completeness, clarity, consistency, and measurability of all 15 Functional Requirements and 8 Success Criteria in the stream processor spec
**Created**: 2026-03-31
**Feature**: [spec.md](../spec.md)
**Scope**: Uniform sweep — FR-001 through FR-015, SC-001 through SC-008

---

## Requirement Completeness

- [x] CHK001 — Is a consumer group identifier or starting-offset behaviour (earliest / committed) specified as a requirement, not just an implementation detail? [Completeness, Gap, Spec §FR-001]
- [x] CHK002 — Is a partition key (or ordering guarantee) specified for enriched records published to `txn.enriched`? Absent this, downstream consumers cannot guarantee per-account ordering. [Completeness, Gap, Spec §FR-006]
- [x] CHK003 — Does FR-004 define **how** the `device_known_fraud` flag is set? The spec says the processor "tracks whether the device has been associated with fraudulent activity" but never specifies the write path (external signal, DLQ feedback, scoring engine event). [Completeness, Ambiguity, Spec §FR-004]
- [x] CHK004 — Does FR-007 require a **minimum node count** as an enforceable requirement, or is the "at least 2 nodes" constraint left only in Assumptions? [Completeness, Consistency, Spec §FR-007]
- [x] CHK005 — Are requirements defined for events on the **failed node's partitions** during a single-node failure? FR-007 only guarantees state safety for accounts on surviving nodes. [Completeness, Edge Case, Spec §FR-007]
- [x] CHK006 — Does FR-010 reference or require a specific DLQ record schema (e.g., `processing-dlq-v1.avsc`)? The FR only mentions "an error reason field" without binding to the contract. [Completeness, Traceability, Spec §FR-010]
- [x] CHK007 — Are **bounds** (minimum, maximum) specified for the configurable velocity TTL in FR-011, or can operators set it to zero/unlimited? [Completeness, Clarity, Spec §FR-011]
- [x] CHK008 — Are bounds specified for the configurable checkpoint interval (FR-008)? A very short interval (e.g., 1s) could saturate checkpoint storage; a very long one violates SC-003. [Completeness, Clarity, Spec §FR-008]
- [x] CHK009 — Is there a requirement for **idempotency keys** or duplicate-detection on enriched output records to support exactly-once verification by downstream consumers? [Completeness, Gap, Spec §FR-015]
- [x] CHK010 — Are the **two edge cases defined only in prose** — back-pressure propagation when output is unavailable, and watermark-stall alerting — covered by any FR? If not, are they intentionally out of scope or missing requirements? [Completeness, Gap, Spec §Edge Cases]

---

## Requirement Clarity

- [x] CHK011 — Is "real time" in FR-001 quantified? SC-001 sets a 50 ms end-to-end budget, but FR-001 itself contains an undefined qualifier. [Clarity, Spec §FR-001]
- [x] CHK012 — Does FR-002 explicitly state that velocity windows are driven by **event time**, or is it left implicit (only stated in FR-013)? Each FR should be self-consistent without requiring cross-reference to pass. [Clarity, Consistency, Spec §FR-002]
- [x] CHK013 — Is "IP classification" in FR-003 defined with its possible discrete values (e.g., RESIDENTIAL, CORPORATE, DATACENTER, TOR)? An undefined enum makes the requirement untestable. [Clarity, Ambiguity, Spec §FR-003]
- [x] CHK014 — Is "resolution confidence" in FR-003 defined with a scale, units, or derivation method? "Confidence" is unquantified and would produce inconsistent implementations. [Clarity, Ambiguity, Spec §FR-003]
- [x] CHK015 — Is the aggregation behaviour for **multi-currency transactions** in FR-002 specified? "Total amount" across transactions in different currencies is ambiguous without a normalization or exclusion rule. [Clarity, Ambiguity, Spec §FR-002]
- [x] CHK016 — Does FR-009 clarify whether recovery resumes at the **offset AT the checkpoint** or **immediately after** the last event included in the checkpoint? This distinction matters for exactly-once correctness. [Clarity, Spec §FR-009]
- [x] CHK017 — Is "enrichment latency" in FR-012 defined with a clear measurement boundary (e.g., from Kafka record receipt to enriched record emit)? Without a boundary, p50/p99 metrics cannot be consistently implemented or compared. [Clarity, Spec §FR-012]
- [x] CHK018 — Is the "allowed-lateness window" in FR-014 measured from the **current watermark** or from the **wall-clock time of arrival**? This determines the computation and the spec does not specify. [Clarity, Ambiguity, Spec §FR-014]
- [x] CHK019 — Does FR-014 specify whether a corrected enriched record **replaces** or **appends** the original on the output topic? Downstream consumers may behave differently depending on record semantics. [Clarity, Ambiguity, Spec §FR-014]
- [x] CHK020 — Is the eviction mechanism for FR-011 TTL specified as **event-time-driven** (timer fires on watermark advance) or **processing-time-driven**? Mixing time domains violates FR-013's prohibition on processing-time windows. [Clarity, Consistency, Spec §FR-011]

---

## Requirement Consistency

- [x] CHK021 — Is SC-001's end-to-end latency definition (50 ms) consistent with the plan's tighter p99 budget of 20 ms for the enrichment stage alone? If the budget slice is 20 ms, the spec's 50 ms target requires documenting the remaining 30 ms allocation. [Consistency, Spec §SC-001] — SC-001 kept as standalone end-to-end target (50 ms); budget allocation (10 ms ingestion + 20 ms enrichment + 20 ms buffer) is documented in plan.md §Constitution Check row II, not duplicated in spec.
- [x] CHK022 — Does the spec consistently define what "exactly-once" means for velocity state updates vs. for output record delivery? FR-015 bundles both under one requirement; a partial failure (state updated, output not delivered) should have an explicit behaviour. [Consistency, Ambiguity, Spec §FR-015]
- [x] CHK023 — Are FR-013 (per-partition watermarks) and FR-014 (allowed-lateness window) consistent in their definition of "current watermark"? FR-013 implies per-partition watermarks, but FR-014 refers to "the current watermark" without specifying whether it is per-partition or globally merged. [Consistency, Ambiguity, Spec §FR-013, §FR-014]
- [x] CHK024 — Is FR-004's use of `api_key_id` as a device proxy documented consistently with the v1 schema contract (`enriched-txn-v1.avsc`)? The contract may expose a field named differently. [Consistency, Traceability, Spec §FR-004]

---

## Acceptance Criteria Quality

- [x] CHK025 — Is "under normal operating load" in SC-001 quantified with a specific transaction-per-second value? Without a reference load, the 50 ms target cannot be reproduced across test environments. [Measurability, Ambiguity, Spec §SC-001]
- [x] CHK026 — Is "end-to-end" in SC-001 defined with a precise start point (event timestamp, Kafka write time, consumer receipt time) and end point (enriched record written to output topic, or available to downstream consumer)? [Measurability, Ambiguity, Spec §SC-001]
- [x] CHK027 — Is "zero data loss" in SC-003 defined precisely? Does it mean no enriched output records dropped, or also no velocity state increments lost? These are different failure modes with different test oracles. [Measurability, Ambiguity, Spec §SC-003] — SC-003 now defines both failure modes explicitly: (a) no enriched output records dropped and (b) no velocity state increments lost, with a note distinguishing output-channel loss from state-channel loss.
- [x] CHK028 — Does SC-003's "60 seconds" recovery window specify its start (failure detected, job killed, or checkpoint storage available) and end (first record processed, or lag fully recovered)? [Measurability, Clarity, Spec §SC-003]
- [x] CHK029 — Does SC-004 acknowledge that concurrent transactions for the same account processed in parallel partitions may produce non-deterministic ordering, making "replaying the same event sequence" produce different velocity snapshots? [Measurability, Assumption, Spec §SC-004]
- [x] CHK030 — Is "active account" defined for SC-005 (state < 4 GB / 1M active accounts)? If "active" means any account with state, the threshold is straightforward; if it means "transacted in the last N days", the definition needs to match the TTL in FR-011. [Measurability, Ambiguity, Spec §SC-005]
- [x] CHK031 — Does SC-007 scope "any failure or recovery scenario" to the **documented failure modes** (node failure, checkpoint failure, network partition, late events)? An unconstrained claim is untestable. [Measurability, Clarity, Spec §SC-007]

---

## Scenario Coverage

- [x] CHK032 — Is there an acceptance scenario for **partial enrichment failure** — where velocity succeeds but geolocation or device lookup fails mid-record? FR-005 requires a single joined record but no acceptance scenario covers this combination failure. [Coverage, Gap, Spec §US1]
- [x] CHK033 — Is there an acceptance scenario for **watermark stall** (all partitions idle, no events arrive)? This is described in edge cases but has no corresponding acceptance scenario in any user story. [Coverage, Gap, Spec §Edge Cases]
- [x] CHK034 — Is there an acceptance scenario for the **checkpoint storage unavailable** edge case? The prose says "continue processing but alert", but no user story defines the observable outcomes to assert. [Coverage, Gap, Spec §Edge Cases]
- [x] CHK035 — Is there an acceptance scenario covering the **startup condition** when the input Kafka topic is unavailable? The edge case says the processor must not begin writing enriched output, but no story defines what "not begin writing" means observably. [Coverage, Gap, Spec §Edge Cases]

---

## Non-Functional Requirements

- [x] CHK036 — Are **security requirements** defined for checkpoint storage access? If checkpoint state contains account velocity aggregates, unauthorized read access exposes transaction frequency patterns. [Non-Functional, Gap, Spec §Assumptions]
- [x] CHK037 — Are **capacity requirements** for the dead-letter queue topic specified? SC-006 requires events appear in DLQ within 5 seconds, but if the DLQ consumer is absent or the topic is full, this SLA cannot be met. [Non-Functional, Completeness, Spec §SC-006] — Added to Assumptions: ≥4 partitions, 48-hour retention (one business-day consumer outage tolerance), with monitoring requirement on consumer lag. Principles-based rather than TPS-derived (see trade-off note below).
- [x] CHK038 — Is a **schema evolution** requirement defined for the enriched record? FR-005 joins all features into a single output record, but there is no requirement for how adding new features in v2 affects existing downstream consumers. [Non-Functional, Gap, Spec §FR-005]

---

## Dependencies & Assumptions

- [x] CHK039 — Is the assumption that "historical aggregates are maintained entirely within the processor's own state — no external feature store" explicitly validated against SC-004's 0.1% error rate? If the embedded state becomes corrupted (not a checkpoint failure, but a state bug), there is no external ground truth to reconcile against. [Assumption, Gap, Spec §Assumptions]
- [x] CHK040 — Is the assumption that "transaction events carry a stable device identifier" validated? FR-004 acknowledges `api_key_id` is a proxy, but the spec does not define what happens if `api_key_id` changes across sessions for the same physical device. [Assumption, Ambiguity, Spec §FR-004]
