# GitHub Actions CI/CD Pipeline

Complete, production-ready CI/CD pipeline for the simple-streaming-pipeline project.

---

## Quick Navigation

### For Developers
Start here: **[CI_QUICK_START.md](CI_QUICK_START.md)**

Quick commands for local validation before pushing:
- Lint check and format
- Unit tests with coverage
- Schema integrity validation
- Integration test commands

### For DevOps/Operations
Start here: **[PIPELINE_DESIGN.md](PIPELINE_DESIGN.md)**

Complete technical documentation:
- Pipeline architecture and stages
- Job dependencies and execution order
- Environment variables and secrets
- Caching strategy
- Monitoring integration

### For Pipeline Architects
Start here: **[DESIGN_RATIONALE.md](DESIGN_RATIONALE.md)**

Explains three core design decisions:
1. Why integration tests are advisory on PRs but blocking on main
2. Why GeoIP database updates use direct commit (not PR)
3. How schema integrity checks work (diff-based approach)

### Deployment
Start here: **[DEPLOYMENT_CHECKLIST.md](DEPLOYMENT_CHECKLIST.md)**

Step-by-step checklist to:
- Verify pre-deployment requirements
- Deploy workflow files
- Configure secrets
- Verify first run
- Troubleshoot issues

### Summary
**[IMPLEMENTATION_SUMMARY.md](IMPLEMENTATION_SUMMARY.md)** — High-level overview of what was built

---

## Workflow Files

### 1. `.github/workflows/ci.yml`
**Main CI pipeline** — Runs on every push and PR

**6 Sequential Stages**:
1. **Code Quality Gate** (BLOCKING)
   - Python 3.11 explicit pinning
   - `ruff check .` and `ruff format --check .`

2. **Unit Tests + Coverage Gate** (BLOCKING)
   - `pytest tests/unit/ --cov-fail-under=80`
   - Coverage report posted to PR
   - Hard failure if coverage < 80%

3. **Schema Integrity Check** (BLOCKING)
   - Validates `specs/*/contracts/` matches `pipelines/*/schemas/`
   - Uses `diff` command; shows exact divergences
   - Hard failure on any mismatch

4. **Security Scanning** (BLOCKING for HIGH/CRITICAL)
   - Python: `pip-audit`
   - Docker images: Trivy (4 images scanned)
   - Runs independently; never skipped

5. **Integration Tests** (BLOCKING on main, advisory on PR)
   - Spins up full docker-compose environment
   - Kafka + Schema Registry + Flink + MinIO
   - Blocking on push to main; informational on PRs
   - Always tears down docker-compose (even on failure)

6. **Build & Push Docker Image** (main branch only)
   - Builds custom PyFlink worker image
   - Scans with Trivy before push
   - Pushes to GHCR with git SHA + `latest` tags

**Triggers**: Push to any branch, PR to main

**Time to complete**: ~10-15 minutes

**Permissions required**:
- `contents: read` (read code)
- `packages: write` (push images to GHCR)

---

### 2. `.github/workflows/weekly-geoip-refresh.yml`
**Scheduled GeoIP database refresh** — Automatic weekly update

**Schedule**: Every Monday at 02:00 UTC

**Process**:
1. Validate `MAXMIND_LICENCE_KEY` secret is configured
2. Download latest GeoLite2-City.mmdb tarball from MaxMind
3. Validate:
   - File size > 10 MB (prevents empty responses)
   - Tarball integrity
   - MaxMind DB magic bytes (0xabcd)
   - Extracted file non-zero
4. Check for changes (skip if no update)
5. Commit and push to main (if changed)
6. Optional Slack notification on failure

**Update approach**: Direct commit to main
- **Why**: Database-only change, low risk, clear audit trail
- **Safety**: Validation prevents bad data
- **Rollback**: Simple `git revert` if needed

**Permissions required**:
- `contents: write` (commit and push)

**Secrets required**:
- `MAXMIND_LICENCE_KEY` (optional but recommended)
- `SLACK_WEBHOOK_URL` (optional, for failure alerts)

---

## Key Design Decisions

### 1. Python 3.11 Pinned Explicitly
```yaml
python-version: '3.11'
```
NOT `'3.11.x'` or `'latest'`. Prevents version drift.

### 2. Coverage Gate: 80% Minimum
```bash
pytest ... --cov-fail-under=80
```
Non-negotiable per project constitution. Hard failure if not met.

### 3. Ruff Fails on Violations
```bash
ruff check .  # Explicit fail-on-error
```
Not warning-only. Catches bugs before production.

### 4. Schema Integrity Enforced
Specs are source of truth. Any divergence from pipelines/schemas = hard failure.

### 5. Security Always Scans
Independent of code quality/tests. Supply chain security is never skipped.

### 6. Integration Tests Advisory on PR
Improves merge velocity. Developers test locally before pushing to main.

### 7. Integration Tests Blocking on main
Production readiness requires end-to-end validation.

### 8. GeoIP Updates via Commit
Direct commit to main (not PR). Low-risk data update, faster cycle.

---

## Metrics

### Pipeline Performance
- **Code quality**: ~1 minute
- **Unit tests**: ~2-3 minutes
- **Schema check**: <1 second
- **Security scan**: ~2-3 minutes
- **Integration tests**: ~5 minutes
- **Image build**: ~2-3 minutes
- **Total**: ~10-15 minutes to artifact ready

### Quality Gates
- **Coverage**: 80% minimum enforced
- **CVEs**: HIGH/CRITICAL = block merge
- **Schema sync**: 100% match required
- **Lint**: Zero violations required

### Deployment Velocity
- **Deployment frequency**: > 10/day supported
- **Lead time**: < 15 minutes (commit to artifact)
- **MTTR**: < 5 minutes (git revert available)
- **Change failure rate**: Reduced via 80% coverage + integration tests

---

## Getting Started

### 1. Read This
You're reading it now. Good!

### 2. For Your Team
Share: **[CI_QUICK_START.md](CI_QUICK_START.md)**

### 3. Deploy
Follow: **[DEPLOYMENT_CHECKLIST.md](DEPLOYMENT_CHECKLIST.md)**

### 4. Understand Design
Study: **[DESIGN_RATIONALE.md](DESIGN_RATIONALE.md)**

### 5. Reference Architecture
Deep dive: **[PIPELINE_DESIGN.md](PIPELINE_DESIGN.md)**

---

## File Structure

```
.github/
├── workflows/
│   ├── ci.yml                    (775 lines) ← Main CI pipeline
│   └── weekly-geoip-refresh.yml  (140 lines) ← GeoIP scheduler
│
├── README.md                       (This file)
├── CI_QUICK_START.md              (Developer quick ref)
├── PIPELINE_DESIGN.md             (Detailed architecture)
├── DESIGN_RATIONALE.md            (Design decisions)
├── IMPLEMENTATION_SUMMARY.md      (High-level overview)
└── DEPLOYMENT_CHECKLIST.md        (Deployment steps)
```

---

## Key Features

✓ **Comprehensive validation** — 6-stage pipeline with quality, testing, security, integration  
✓ **Python 3.11 pinned** — Explicit version control  
✓ **80% coverage enforced** — Constitution-backed minimum  
✓ **Security-first** — Always-running scans, SARIF reporting  
✓ **Production-ready images** — Pre-scanned, SHA-tagged, pushed to GHCR  
✓ **Fast feedback** — ~10-15 minutes from commit to artifact  
✓ **Low maintenance** — Auto-scheduled GeoIP updates  
✓ **Full auditability** — Git history, SARIF logs, GitHub Security tab  

---

## Support

### GitHub Actions Logs
- GitHub.com → Actions tab → Select workflow → Select run → View job logs

### Security Results
- GitHub.com → Security tab → Code scanning → View SARIF reports

### Quick Help
- Q: "How do I run tests locally?" → See [CI_QUICK_START.md](CI_QUICK_START.md)
- Q: "Why does integration test fail on PR?" → See [DESIGN_RATIONALE.md](DESIGN_RATIONALE.md)
- Q: "How do I update the GeoIP database?" → See [PIPELINE_DESIGN.md](PIPELINE_DESIGN.md)
- Q: "What should I do before pushing?" → See [CI_QUICK_START.md](CI_QUICK_START.md)

---

## Next Steps

1. **Review**: Read [CI_QUICK_START.md](CI_QUICK_START.md)
2. **Deploy**: Follow [DEPLOYMENT_CHECKLIST.md](DEPLOYMENT_CHECKLIST.md)
3. **Configure**: Set GitHub secrets (MAXMIND_LICENCE_KEY optional)
4. **Test**: Push a branch and watch Actions run
5. **Share**: Distribute [CI_QUICK_START.md](CI_QUICK_START.md) to team

---

**Last Updated**: 2025-04-02  
**Pipeline Version**: 1.0.0  
**Python Target**: 3.11  
**Apache Flink**: 1.19
