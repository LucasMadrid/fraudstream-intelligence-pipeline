# Runbook: Credential Rotation (CHK012)

**Applies to**: All credentials used by the enrichment pipeline at runtime.  
**Triggers**: Scheduled rotation, suspected compromise, team member offboarding.

---

## Credentials in scope

| Credential | Rotation trigger | Max rotation window |
|------------|------------------|---------------------|
| `MAXMIND_LICENCE_KEY` | Annual / account compromise | 48 hours |
| Kafka SASL credentials (future) | 90 days / compromise | 4 hours |
| Schema Registry credentials (future) | 90 days / compromise | 4 hours |
| Flink cluster API token (future) | 90 days / compromise | 4 hours |

---

## Rotation procedure

### Step 1 — Trigger a savepoint on the running Flink job

A Flink job must be stopped cleanly before credentials are rotated so that
exactly-once state is preserved and no in-flight records are lost.

```bash
# Find the running job ID
curl -s http://<FLINK_JM_HOST>:8081/jobs | jq '.jobs[] | select(.status=="RUNNING") | .id'

# Trigger an async savepoint, then cancel
JOB_ID=<job-id-from-above>
SAVEPOINT_DIR="s3://flink-checkpoints/savepoints"

curl -X POST "http://<FLINK_JM_HOST>:8081/jobs/${JOB_ID}/savepoints" \
  -H "Content-Type: application/json" \
  -d "{\"target-directory\": \"${SAVEPOINT_DIR}\", \"cancel-job\": true}"

# Poll until savepoint is completed
curl "http://<FLINK_JM_HOST>:8081/jobs/${JOB_ID}/savepoints/<request-id>"
# status should be "COMPLETED" before proceeding
```

### Step 2 — Rotate the credential

| Credential type | Rotation action |
|-----------------|-----------------|
| GitHub Actions secret | Settings → Secrets → Update value |
| MaxMind licence key | maxmind.com → My Account → Licence Keys → Regenerate |
| Kafka SASL | Rotate in Confluent Cloud or broker ACL config |
| Schema Registry | Rotate API key in Confluent Cloud |

### Step 3 — Update secrets in all environments

For each environment (dev, staging, production):
1. Update the GitHub Actions secret.
2. If using a secrets manager (Vault / AWS Secrets Manager): update the secret version there first, then update the GitHub Actions reference if it points to a static value.

### Step 4 — Restart the Flink job from savepoint

```bash
# Submit job, resuming from the savepoint created in Step 1
SAVEPOINT_PATH="s3://flink-checkpoints/savepoints/<savepoint-dir>"

curl -X POST "http://<FLINK_JM_HOST>:8081/jars/<jar-id>/run" \
  -H "Content-Type: application/json" \
  -d "{
    \"savepointPath\": \"${SAVEPOINT_PATH}\",
    \"allowNonRestoredState\": false,
    \"programArgs\": \"--env production --parallelism 2\"
  }"
```

### Step 5 — Verify

```bash
# Confirm job is RUNNING
curl -s "http://<FLINK_JM_HOST>:8081/jobs/${JOB_ID}" | jq .status

# Check that enriched records are flowing (Kafka consumer lag should decrease)
kafka-consumer-groups --bootstrap-server <KAFKA_BOOTSTRAP_SERVERS> \
  --describe --group flink-enrichment-processor
```

---

## CI pipeline coordination

When a credential used by CI is rotated:

1. Update the GitHub Actions repository secret **before** triggering a new CI run.
2. Re-run the last failed CI workflow (`gh run rerun <run-id>`) to confirm the new
   credential works.
3. If the rotation affects the `schema-registry-compat` job, verify the ephemeral SR
   services start cleanly with `gh run view <run-id> --log`.

---

## Emergency (compromise suspected)

1. Invalidate the credential immediately at the source (MaxMind / Confluent / IAM).
2. Trigger an immediate savepoint + cancel (Step 1 above).
3. Rotate and restart (Steps 2–4 above).
4. Review audit logs for unauthorized use of the compromised credential.
5. File an incident report and update `docs/tech-debt.md` if a process gap was exposed.

---

## Related

- `docs/security-policy.md` — credential inventory and CI injection methods
- `docs/tech-debt.md TD-002` — secrets manager not yet integrated
- `specs/002-flink-stream-processor/checklists/cicd.md CHK012`
