# Tech Debt

Items that are intentionally deferred. Each entry has an ID, context, and the work
needed to resolve it.

---

## TD-001 — ~~Schema Registry compatibility not enforced in CI~~ CLOSED

**Area**: CI/CD, data contracts
**Introduced**: 2026-04-02 (branch `002-flink-stream-processor`)
**Closed**: 2026-04-02 (branch `002-flink-stream-processor`)

Resolved by adding the `schema-registry-compat` job to `.github/workflows/ci.yml`
(Stage 3b). The job spins up Kafka + Schema Registry as GitHub Actions services,
registers all four Avro schemas (`txn-api-value`, `dlq-envelope-value`,
`txn-enriched-value`, `txn-processing-dlq-value`) with `BACKWARD` compatibility
mode, and fails the build if any registration returns an error. The job is a
required check in `ci-summary`.

No external Schema Registry URL or secret is needed — the CI service is ephemeral.

---

## TD-002 — No secrets manager; credentials passed as env vars / Makefile targets

**Area**: Security, operations
**Introduced**: 2026-04-02 (branch `002-flink-stream-processor`)

`MAXMIND_LICENCE_KEY` and any future Kafka/Schema Registry credentials are passed
via environment variables in the Makefile (`make update-geoip`). There is no secrets
manager (Vault, AWS Secrets Manager, GCP Secret Manager, etc.) and no rotation
policy.

**Required to close**:
- Integrate a secrets manager for production
- Store `MAXMIND_LICENCE_KEY` and Kafka credentials there
- Document rotation procedure and owner (see `specs/002-flink-stream-processor/checklists/cicd.md` CHK007, CHK011)

---

## TD-003 — Deploy jobs (dev / staging / production) not implemented

**Area**: CI/CD, deployment
**Introduced**: 2026-04-02 (branch `002-flink-stream-processor`)

The CI pipeline builds and pushes the `flink-worker` Docker image to GHCR but does
not deploy it anywhere. No cloud Flink clusters exist yet. The three deploy stages
(`deploy-dev`, `deploy-staging`, `deploy-production`) were removed from
`.github/workflows/ci.yml` to avoid meaningless stub jobs.

GitHub Environments (`dev`, `staging`, `production`) and their protection rules are
already configured — they will gate the deploy jobs once clusters are available.

**Required to close**:
- Provision Flink clusters for dev, staging, and production
- Add repository variables / secrets: `FLINK_JM_HOST`, `KAFKA_BOOTSTRAP_SERVERS`,
  `SCHEMA_REGISTRY_URL`, `PROMETHEUS_HOST`
- Restore deploy stages in `ci.yml` using Flink REST API job submission:
  ```
  POST /jars/{jar-id}/run  {"programArgs": "--env <env> --parallelism <n>"}
  ```
- Add smoke-test step: poll `GET /jobs` until status == `RUNNING` (timeout 60s)
- Add observability gate: assert `enrichment_latency_ms` metric returns data within
  30s of job startup (spec CHK015)
- For production: trigger a savepoint on the running job before submitting the new
  version (zero-downtime rolling upgrade)

---

## TD-004 — ~~GeoLite2 update automation not implemented~~ PARTIALLY CLOSED

**Area**: CI/CD, data freshness
**Introduced**: 2026-04-02 (branch `002-flink-stream-processor`)
**Partially closed**: 2026-04-02 (branch `002-flink-stream-processor`)

Resolved items:
- `.github/workflows/geoip-refresh.yml`: weekly cron job that downloads and validates
  the mmdb, uploads as a 14-day workflow artifact (CHK016)
- `tests/unit/processing/test_geolocation.py::test_lru_cache_invalidated_on_reader_reload`:
  asserts cache is discarded when `open_with_reader` is called with a new reader (CHK017)

**Still open** (requires cloud infrastructure):
- Rolling TaskManager restart procedure after DB update — needs production Flink cluster
- Upload mmdb to S3 instead of workflow artifact — needs S3 bucket provisioned (TD-003)

---

## TD-005 — No Dockerfile for the ingestion service

**Area**: CI/CD, containerisation
**Introduced**: 2026-04-02 (branch `002-flink-stream-processor`)

`pipelines/ingestion/` has no `Dockerfile`. The CI pipeline builds and scans the `flink-worker`
image (stage 6) but cannot do the same for the ingestion service. The service is described as an
"HTTP server + background Kafka client" intended for Kubernetes deployment, so containerisation
is required before production.

**Required to close**:
- Add `infra/ingestion/Dockerfile` (Python 3.11 slim base, non-root user, health-check endpoint)
- Add a `build-ingestion-image` CI job mirroring `build-flink-image`: build → Trivy scan → push to GHCR
- Add the new image to the Trivy scan matrix in the `security-scan` job
- Add `build-ingestion-image` to `ci-summary` `needs:` list
