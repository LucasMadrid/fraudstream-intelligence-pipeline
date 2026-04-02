# CI/CD Pipeline Quality Checklist: Stateful Stream Processor

**Purpose**: Validate completeness, clarity, and consistency of CI/CD requirements across plan.md, spec.md, and security/observability sections — covering build gates, security scanning, test coverage enforcement, secrets handling, and deployment promotion criteria
**Created**: 2026-04-02
**Feature**: [spec.md](../spec.md) | [plan.md](../plan.md)
**Audience**: Author (self-review), Peer reviewer (PR), DevOps/Platform team
**Depth**: Standard

---

## Test Coverage & Quality Gates

- [x] CHK001 — Is the 80% coverage gate formally documented as a blocking CI requirement (not just a local `--cov-fail-under` flag), with a reference to the constitution requirement that mandates it? Without formal documentation, the gate can be silently bypassed in a CI config change. [Completeness, Gap, Plan §Testing Strategy] — `--cov-fail-under=80` in `unit-tests` job; job is a required check in `ci-summary` (`.github/workflows/ci.yml`)
- [x] CHK002 — Is the Python version (3.11) explicitly pinned in the CI runner environment specification? The session history shows `python` defaulted to 3.9 in the local environment; CI must not reproduce this ambiguity. [Clarity, Gap] — `python-version: '3.11'` pinned via `actions/setup-python@v5` in every CI job
- [x] CHK003 — Are code quality gate semantics (lint-fail-on-error vs. lint-warn-only) formally specified for the `ruff check` step? "Code quality" without a defined failure mode is untestable as a gate. [Clarity, Gap] — `ruff check .` exits non-zero on violations; documented inline in ci.yml; job has no `continue-on-error`
- [x] CHK004 — Is the scope of the test suite for CI formally defined — specifically, whether integration tests (`tests/integration/`) and load tests (`tests/load/`) are gated, advisory-only, or excluded from the main pipeline? Running load tests on every PR would be prohibitively slow; the policy must be documented. [Completeness, Gap] — Integration tests: advisory on PR (`continue-on-error: true`), blocking on main. Load tests: excluded from pipeline entirely.

---

## Security Scanning Requirements

- [x] CHK005 — Are the exact Docker images in scope for CVE scanning explicitly listed in the CI/CD requirements? plan.md §Security Requirements names `flink:1.19-scala_2.12` and the custom PyFlink worker image — but the requirements do not specify whether the base image is re-scanned on every build or only on base-image updates. [Clarity, Plan §Security Requirements §Docker Image Security Scanning] — `confluentinc/cp-kafka:7.6.1`, `confluentinc/cp-schema-registry:7.6.1`, `apache/flink:1.19-scala_2.12`, and the built `flink-worker` image are all explicitly scanned on every CI run via Trivy
- [x] CHK006 — Is the CVE severity threshold for build promotion formally documented as a blocking requirement (HIGH or CRITICAL = block) versus advisory? plan.md §Security Requirements states this policy but it appears in narrative prose, not as a formally numbered FR or gate requirement. [Completeness, Plan §Security Requirements §Docker Image Security Scanning] — `--severity HIGH,CRITICAL --exit-code 1` enforced in all Trivy scan steps; "Check for blocking vulnerabilities" step exits 1 if any image is vulnerable
- [x] CHK007 — Is the 30-day remediation window for HIGH/CRITICAL CVEs documented as a formal SLA with an owner and escalation path? Without an owner, the policy is unenforceable. [Clarity, Gap, Plan §Security Requirements §Docker Image Security Scanning] — `docs/security-policy.md §CVE Remediation SLA`: CRITICAL 7 days, HIGH 30 days; owner = feature branch lead / platform team fallback; 14/30-day escalation path to engineering manager
- [x] CHK008 — Is the `make update-geoip` target explicitly documented as prohibited from CI/CD pipelines (only local dev), and is the alternative injection method for `MAXMIND_LICENCE_KEY` in CI formally specified? plan.md §Security Requirements §Secrets Management states "must never be replicated in CI/CD pipelines" but does not specify the CI-safe alternative. [Gap, Plan §Security Requirements §Secrets Management] — `docs/security-policy.md §MAXMIND_LICENCE_KEY in CI`: GitHub Actions secret injection documented; graceful fallback if secret absent; `integration-tests` job wired with download step

---

## Schema Integrity Gate

- [x] CHK009 — Is the CI diff check between `specs/002-flink-stream-processor/contracts/` and `pipelines/processing/schemas/` formally specified as a blocking gate (not just a documented convention)? plan.md §Security Requirements §Schema Source of Truth describes the policy and states divergence is "detected by diffing the two directories in CI" — but the requirement does not specify whether a diff causes a hard CI failure or a warning. [Clarity, Plan §Security Requirements §Schema Source of Truth] — `schema-integrity` job diffs all four schema pairs and exits 1 on any divergence; job is a required check in `ci-summary`
- [x] CHK010 — Is the Schema Registry BACKWARD compatibility enforcement requirement documented as a pre-deployment CI gate? plan.md §Security Requirements §Schema Registry Authentication requires BACKWARD mode registration "before the first production deployment" but does not specify how or where this is validated in the pipeline. [Gap, Plan §Security Requirements §Schema Registry Authentication] — `schema-registry-compat` job: ephemeral Kafka + SR services in CI; registers all 4 subjects with `BACKWARD` mode; hard fail on violation; required check in `ci-summary`

---

## Secrets & Credentials in CI

- [x] CHK011 — Are the injection methods for each credential class documented for the CI environment, distinguishing CI/staging from local dev? plan.md §Security Requirements §Secrets Management lists the production secrets manager requirement but the CI environment is a gap — neither Vault, external-secrets-operator, nor GitHub Actions Secrets is named. [Gap, Plan §Security Requirements §Secrets Management] — `docs/security-policy.md §CI Credential Injection`: credential inventory table with CI injection method vs local dev for each secret
- [x] CHK012 — Is the rotation procedure for credentials that require a pipeline restart formally documented with a CI runbook reference? plan.md §Security Requirements §Secrets Rotation Procedures specifies rotation triggers but the CI pipeline's role in coordinating savepoint + restart is absent. [Gap, Plan §Security Requirements §Secrets Rotation Procedures] — `docs/runbooks/credential-rotation.md`: savepoint → rotate secret → restart from savepoint; CI re-run step; emergency procedure

---

## Deployment Promotion Criteria

- [ ] CHK013 — Are the promotion gates between environments (local → staging → production) formally specified as a numbered list of required checks, rather than implicit "all tests pass"? The plan documents individual requirements but no single document enumerates all the gate criteria a release must satisfy before promotion. [Completeness, Gap] — Tech debt: docs/tech-debt.md TD-003 (no clusters provisioned yet)
- [ ] CHK014 — Is the Flink job submission and smoke-test step in the CI/CD pipeline formally required, or only the unit/integration test suite? A pipeline that deploys a Flink job without verifying the job reaches RUNNING state misses the most common deployment failure mode. [Coverage, Gap] — Tech debt: docs/tech-debt.md TD-003
- [ ] CHK015 — Are the observability validation requirements for a CI deployment smoke test defined — for example, asserting that the `enrichment_latency_ms` metric endpoint is reachable and returning data within N seconds of job startup? plan.md §Observability Requirements defines alert thresholds but not their role as deployment acceptance criteria. [Gap, Plan §Observability Requirements] — Tech debt: docs/tech-debt.md TD-003

---

## GeoLite2 Update Automation

- [x] CHK016 — Is the scheduled CI/CD job for weekly GeoLite2 refresh formally specified — including trigger cadence, the step that performs the rolling TaskManager restart, and the rollback procedure if the new mmdb fails the LRU invalidation check? plan.md §Security Requirements §GeoLite2 Database Update Policy describes the policy narrative but the CI pipeline trigger is not formally defined. [Completeness, Gap, Plan §Security Requirements §GeoLite2 Database Update Policy] — `.github/workflows/geoip-refresh.yml`: weekly `cron: '0 3 * * 1'` + manual trigger; mmdb validated with `maxminddb` spot-check; uploaded as 14-day workflow artifact; S3 production path documented in comments
- [x] CHK017 — Is the LRU cache invalidation requirement on GeoLite2 reload testable in CI? plan.md §Security Requirements §GeoLite2 Database Update Policy specifies "file-modification-time check at each bundle boundary" — but no acceptance criterion defines how this is verified before a GeoLite2 update is promoted. [Measurability, Gap, Plan §Security Requirements §GeoLite2 Database Update Policy] — `tests/unit/processing/test_geolocation.py::TestGeolocationMapFunction::test_lru_cache_invalidated_on_reader_reload`: asserts old cache is discarded and new reader is called after `open_with_reader` reload

---

## Run Results

**Date**: 2026-04-02
**Evaluated against**: `.github/workflows/ci.yml` (post-implementation)

| # | Status | Item |
|---|--------|------|
| CHK001 | ✅ pass | 80% coverage gate — `--cov-fail-under=80`, required in `ci-summary` |
| CHK002 | ✅ pass | Python 3.11 pinned via `actions/setup-python@v5` in all jobs |
| CHK003 | ✅ pass | `ruff check` is fail-on-error (no `continue-on-error`) |
| CHK004 | ✅ pass | Integration tests advisory on PR, blocking on main; load tests excluded |
| CHK005 | ✅ pass | All 3 base images + custom flink-worker image scanned by Trivy every run |
| CHK006 | ✅ pass | `--severity HIGH,CRITICAL --exit-code 1` enforced; blocking step in pipeline |
| CHK007 | ✅ pass | `docs/security-policy.md`: CRITICAL 7d / HIGH 30d SLA; owner + escalation path |
| CHK008 | ✅ pass | `docs/security-policy.md`: CI injection via GitHub Actions secret; graceful fallback |
| CHK009 | ✅ pass | `schema-integrity` job: hard fail on divergence, required check |
| CHK010 | ✅ pass | `schema-registry-compat` job: ephemeral SR in CI; BACKWARD mode; required check |
| CHK011 | ✅ pass | `docs/security-policy.md`: credential inventory table with CI vs local dev injection |
| CHK012 | ✅ pass | `docs/runbooks/credential-rotation.md`: savepoint → rotate → restart procedure |
| CHK013 | ❌ gap | No clusters yet; promotion gates not implemented — TD-003 |
| CHK014 | ❌ gap | Flink job submission not in CI pipeline — TD-003 |
| CHK015 | ❌ gap | Observability acceptance criteria not defined for deployments — TD-003 |
| CHK016 | ✅ pass | `.github/workflows/geoip-refresh.yml`: weekly cron + manual trigger; mmdb validated |
| CHK017 | ✅ pass | `test_lru_cache_invalidated_on_reader_reload`: asserts cache cleared on reader reload |

**Pass: 14 / 17 — Fail: 3 / 17**

All remaining open gaps (CHK013, CHK014, CHK015) are blocked on cloud cluster provisioning — tracked in [docs/tech-debt.md](../../../../docs/tech-debt.md) TD-003.
