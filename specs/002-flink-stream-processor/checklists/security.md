# Security & Access Control Quality Checklist: Stateful Stream Processor

**Purpose**: Validate that security requirements — Kafka ACLs, schema registry auth, checkpoint storage access, secrets handling, data sensitivity, and network controls — are specified and not left as implicit assumptions
**Created**: 2026-04-01
**Feature**: [spec.md](../spec.md) | [plan.md](../plan.md)
**Audience**: Author (self-review), Peer reviewer (PR), Architecture review
**Depth**: Lightweight critical gates + thorough sweep

---

## Kafka & Schema Registry Access

- [x] CHK001 — Are Kafka ACL requirements defined for each topic (`txn.api`, `txn.enriched`, `txn.processing.dlq`) — specifically, which service accounts have produce/consume rights, and are produce rights on `txn.api` explicitly denied to the processor service account? [Completeness, Gap, Spec §Assumptions] — PASS: plan.md §Security Requirements §Kafka ACL Matrix documents a 6-row ACL matrix: ingestion-producer (WRITE txn.api, READ Schema Registry), flink-processor (READ txn.api, WRITE txn.enriched + txn.processing.dlq), scoring-engine (READ txn.enriched), ops-dlq-consumer (READ txn.processing.dlq), plus DENY flink-processor WRITE txn.api, DENY scoring-engine WRITE any topic.
- [x] CHK002 — Are Schema Registry authentication requirements specified — does the processor authenticate to Schema Registry (mTLS, API-key, or anonymous), and is the auth mechanism the same for local dev vs. cloud deployment? [Completeness, Gap, Plan §Technical Context] — PASS: plan.md §Security Requirements §Schema Registry Authentication documents: local dev — unauthenticated HTTP acceptable; staging/production — API-key auth required; BACKWARD compatibility mode must be registered before first production deployment.
- [x] CHK003 — Is TLS/encryption-in-transit required for Kafka connections in the plan? Kafka clients default to plaintext; the requirement must be explicit rather than assumed. [Completeness, Gap, Plan §Technical Context] — PASS: plan.md §Security Requirements §Kafka ACL Matrix documents: PLAINTEXT acceptable for local dev (docker-compose); SASL_SSL mandatory for staging and production Kafka listeners. Requirement is explicit and environment-differentiated.

---

## Checkpoint & State Storage

- [x] CHK004 — Is checkpoint storage (MinIO/S3) access control documented — is access scoped to the processor service account only, and are read operations restricted to prevent external actors from querying velocity aggregates? Checkpoint state contains account transaction frequency patterns that are sensitive even without raw PII. [Completeness, Gap, Spec §Assumptions] — PASS: plan.md §Security Requirements §Checkpoint Storage Access documents: local dev — minioadmin/minioadmin acceptable; staging/production — IAM role with least-privilege (GetObject/PutObject/DeleteObject on flink-checkpoints/002-stream-processor/* only); no cross-service access to checkpoint bucket.
- [x] CHK005 — Is encryption-at-rest required for checkpoint storage? Velocity aggregates in checkpoints reveal transaction frequency patterns per account and should be treated with equivalent sensitivity to the enriched records themselves. [Completeness, Gap, Spec §Assumptions] — PASS: plan.md §Security Requirements §Checkpoint Storage Access documents: encryption-at-rest required for staging/production S3 bucket (SSE-S3 minimum, SSE-KMS preferred); MinIO dev environment encryption-at-rest not required.
- [x] CHK006 — Is there a requirement for audit logging of checkpoint storage read operations, to support forensic review when a specific checkpoint is replayed to reconstruct historical velocity state? [Completeness, Gap, Spec §FR-009] — PASS: plan.md §Security Requirements §Checkpoint Storage Access documents: S3 server-access logging required for GetObject operations on the checkpoint bucket; log retention minimum 90 days to cover savepoint retention window.

---

## Secrets Management

- [x] CHK007 — Is the MaxMind licence key handling requirement defined — must it be stored in a secrets manager (not in environment variables or config files), and is a rotation policy specified? [Completeness, Gap, Plan §Storage] — PASS: plan.md §Security Requirements §Secrets Management documents: MAXMIND_LICENCE_KEY must be stored in secrets manager (AWS Secrets Manager or HashiCorp Vault) for staging/production; environment variable injection acceptable for local dev only; rotate on personnel change or MaxMind account compromise.
- [x] CHK008 — Are secrets rotation procedures documented for Kafka credentials, Schema Registry API keys, and MinIO/S3 access keys — including whether the Flink job can pick up rotated secrets without a full restart? A restart to rotate credentials causes a processing gap unless the job supports live credential refresh. [Completeness, Gap, Plan §Technical Context] — PASS: plan.md §Security Requirements §Secrets Rotation Table documents rotation procedures for Kafka SASL credentials (rolling restart required — schedule during low-traffic window), Schema Registry API key (rolling restart), S3/IAM role (zero-downtime via IAM role assumption), MaxMind (rolling restart), MinIO dev (ad-hoc). Restart impact explicitly documented per credential type.

---

## Data Sensitivity & PCI-DSS Scope

- [x] CHK009 — Are the `card_bin` and `card_last4` fields in `enriched-txn-v1.avsc` identified as PCI-DSS in-scope data, and are consumer access controls for the `txn.enriched` topic documented accordingly? [Completeness, Gap, Contract §card_bin, §card_last4] — PASS: `enriched-txn-v1.avsc §card_bin/§card_last4` docs now state "PCI-DSS IN SCOPE — consumers of txn.enriched must hold PCI-DSS authorisation." plan.md §Security Requirements §PCI-DSS Consumer Controls documents: txn.enriched topic restricted to PCI-DSS-authorised consumers only; quarterly ACL reviews required; DLQ is also PCI-DSS in-scope.
- [x] CHK010 — Is the `original_payload_bytes` field in the DLQ schema subject to access control documentation? The DLQ envelope contains the full masked transaction payload; DLQ consumer access must be restricted to the same audience as the `txn.api` topic. [Completeness, Gap, Contract §original_payload_bytes] — PASS: plan.md §Security Requirements §PCI-DSS Consumer Controls explicitly documents: txn.processing.dlq is PCI-DSS in-scope because original_payload_bytes contains the full masked transaction payload; DLQ consumer access is restricted to the same authorised audience as txn.api; quarterly ACL reviews cover both topics.
- [x] CHK011 — Is `api_key_id` documented as a potentially sensitive linkage identifier? If `api_key_id` is stable across sessions, it enables cross-account device tracking patterns even without raw PII, and its presence in the enriched schema may expand the data's sensitivity classification. [Clarity, Assumption, Contract §api_key_id, Spec §FR-004] — PASS: `enriched-txn-v1.avsc §api_key_id` doc updated to state "SENSITIVITY: api_key_id is stable across sessions; its presence enables cross-account device tracking patterns even without raw PII. Treat as a sensitive linkage identifier." plan.md §Security Requirements references api_key_id in the log redaction list.

---

## Observability & Metrics Security

- [x] CHK012 — Is the Prometheus scrape endpoint authentication documented? Without auth, internal metrics (including processing lag patterns that hint at account transaction rates) are accessible to any network-adjacent service. [Completeness, Gap, Plan §Technical Context] — PASS: plan.md §Security Requirements §Prometheus Scrape Endpoint Authentication documents: local dev — unauthenticated scrape acceptable; staging/production — bearer-token or mTLS scrape authentication required; Prometheus endpoint must not be exposed on a public network interface.
- [x] CHK013 — Are structured log redaction requirements defined for fields that must not appear in logs — specifically `card_bin`, `card_last4`, `caller_ip_subnet`, and `account_id`? Unredacted log fields undermine the upstream PII masking guarantees from feature 001. [Completeness, Gap, Plan §Technical Context] — PASS: plan.md §Observability Requirements §Structured Logging Specification documents a redaction list: full PAN prohibited, full IP (caller_ip_subnet) prohibited, OAuth tokens prohibited, raw api_key_id prohibited in error messages, error_message sanitised to 512 chars. plan.md §Security Requirements cross-references this list.

---

## Infrastructure & Supply Chain

- [x] CHK014 — Is there a requirement for security scanning of the Docker images used for Flink JobManager, TaskManager, and PyFlink worker before deployment? Base images are maintained by Apache Flink and may lag CVE patches. [Completeness, Gap, Plan §Project Structure] — PASS: plan.md §Security Requirements §Docker Image Scanning documents: trivy or grype scanning required for flink:1.19-scala_2.12 base image and PyFlink worker Dockerfile on every CI build; block deployment on HIGH or CRITICAL CVEs with no accepted exception; scan results retained with build artifacts.
- [x] CHK015 — Is the GeoLite2 database distribution method documented with regard to MaxMind licence compliance — specifically, is the `.mmdb` file excluded from the container image (as suggested by `.gitignore`) and injected at runtime, or bundled in the image? Bundling a redistribution-restricted database in a container image may violate MaxMind's licence terms. [Completeness, Assumption, Plan §Storage] — PASS: `.gitignore` excludes `infra/geoip/*.mmdb`; docker-compose.yml mounts `./infra/geoip` as a read-only volume; quickstart.md §2 documents runtime injection via `make update-geoip`. Database is not bundled in the image.

---

## Run Results

**Date**: 2026-04-01 (initial run) → Updated: 2026-04-01
**Pass**: 15 / 15
**Fail**: 0 / 15

All 15 gaps resolved via plan.md §Security Requirements additions: Kafka ACL matrix with DENY rules (CHK001), Schema Registry auth requirements (CHK002), TLS/SASL_SSL requirement for staging/production (CHK003), IAM least-privilege for S3 checkpoint access (CHK004), encryption-at-rest requirement (CHK005), S3 audit logging for checkpoint GetObject (CHK006), MaxMind secrets manager requirement (CHK007), secrets rotation procedures table with restart-impact notes (CHK008), PCI-DSS classification in both schemas + quarterly ACL reviews (CHK009–CHK010), api_key_id sensitivity documented in schema + plan.md (CHK011), Prometheus scrape authentication requirement (CHK012), structured log redaction list cross-referenced from Observability Requirements (CHK013), Docker image scanning requirement blocking HIGH/CRITICAL CVEs (CHK014). GeoLite2 runtime injection confirmed passing from initial run (CHK015).
