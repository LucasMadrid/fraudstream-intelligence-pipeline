# Security Policy

---

## CVE Remediation SLA (CHK007)

### Severity thresholds

| Severity | Action | SLA |
|----------|--------|-----|
| CRITICAL | Block merge; begin remediation immediately | 7 days |
| HIGH | Block merge; schedule remediation | 30 days |
| MEDIUM | Tracked in tech-debt; no merge block | 90 days |
| LOW | Advisory only | Best-effort |

### Owner

The **engineering lead on the current feature branch** is responsible for remediating CVEs
introduced by their changes. If no single owner can be identified, the **platform team**
is the default escalation owner.

### Process

1. Trivy scan fails in `security-scan` CI job (HIGH or CRITICAL found).
2. The PR author investigates: is it in a base image or a Python dependency?
   - **Base image** (e.g., `apache/flink:1.19`): pin to a patched version tag, or
     open a tracking issue if no patched version exists.
   - **Python dep**: bump the affected package in `pyproject.toml`.
3. If remediation is not available within SLA (e.g., upstream has not released a fix):
   - Open a tech-debt entry in `docs/tech-debt.md`.
   - Add a Trivy `.trivyignore` entry with a comment, expiry date, and linked issue.
   - Obtain sign-off from engineering lead before merging.

### Escalation path

```
7 days: Engineering lead contacted, tracking issue opened
14 days: Escalate to engineering manager
30 days: If HIGH/CRITICAL unresolved — block all non-hotfix merges to main
```

---

## CI Credential Injection (CHK008, CHK011)

### Credential inventory

| Secret | Used by | CI injection method | Local dev |
|--------|---------|---------------------|-----------|
| `MAXMIND_LICENCE_KEY` | `make update-geoip`, GeoLite2 download | GitHub Actions secret | Shell env var |
| `KAFKA_BOOTSTRAP_SERVERS` | Flink job (future) | GitHub Actions secret | `localhost:9092` (docker-compose) |
| `SCHEMA_REGISTRY_URL` | Schema Registry client (future) | GitHub Actions secret | `http://localhost:8081` (docker-compose) |
| `FLINK_JM_HOST` | Deploy jobs (future, TD-003) | GitHub Actions secret | Not applicable |
| MinIO root credentials | Checkpoint storage (local dev only) | **Never stored in CI** — hardcoded dev values only | `minioadmin` / `minioadmin` |

### MAXMIND_LICENCE_KEY in CI

The `make update-geoip` target is **for local development only**. It must not be run
in CI pipelines that do not have the `MAXMIND_LICENCE_KEY` secret available.

**CI-safe injection method:**

```yaml
- name: Download GeoLite2 database
  env:
    MAXMIND_LICENCE_KEY: ${{ secrets.MAXMIND_LICENCE_KEY }}
  run: |
    if [ -z "$MAXMIND_LICENCE_KEY" ]; then
      echo "MAXMIND_LICENCE_KEY not set — geo fields will be null in integration tests"
      mkdir -p infra/geoip
    else
      make update-geoip
    fi
```

The graceful fallback (`mkdir -p infra/geoip`) allows CI to proceed without the secret
(e.g., in forked repositories or contributor PRs). The Flink operator returns null geo
fields when no mmdb is present — integration tests must tolerate this.

**To register the secret in a repository:**
1. Go to Settings → Secrets and variables → Actions → New repository secret.
2. Name: `MAXMIND_LICENCE_KEY`
3. Value: your MaxMind licence key (free at maxmind.com/en/geolite2/signup).

### MinIO credentials

The MinIO credentials (`minioadmin` / `minioadmin`) are hardcoded local development
defaults. They **must never be used in production**. Production checkpoint storage will
use an S3-compatible bucket with IAM role credentials (no static credentials in CI).

### Future credentials (TD-002, TD-003)

When cloud Kafka and Flink clusters are provisioned, the following secrets must be
registered before restoring deploy jobs:
- `KAFKA_BOOTSTRAP_SERVERS`
- `SCHEMA_REGISTRY_URL`
- `FLINK_JM_HOST`
- `PROMETHEUS_HOST`

See `docs/tech-debt.md TD-003` for the full list.
