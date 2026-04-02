COMPOSE := docker compose -f infra/docker-compose.yml
PYTHON  := python3.11

.PHONY: infra-up infra-down infra-clean infra-ps infra-logs \
        topics bootstrap update-geoip \
        flink-job generate consume \
        install test test-unit test-integration

# ── Infrastructure lifecycle ──────────────────────────────────────────────

infra-up:
	$(COMPOSE) up -d
	$(COMPOSE) ps

infra-down:
	$(COMPOSE) down

infra-clean:
	$(COMPOSE) down -v

infra-ps:
	$(COMPOSE) ps

## SERVICE=broker make infra-logs   → tail a single service
## make infra-logs                  → tail all services
infra-logs:
	$(COMPOSE) logs -f $(SERVICE)

# ── Kafka topics + Schema Registry ───────────────────────────────────────

topics:
	@echo "Waiting for Kafka broker..."
	@until docker exec broker kafka-topics --bootstrap-server localhost:9092 --list >/dev/null 2>&1; do \
	    printf '.'; sleep 2; \
	done
	@echo " ready."
	bash infra/kafka/topics.sh

# ── One-shot dev bootstrap ────────────────────────────────────────────────
## Starts infra, waits for health, creates topics and registers all schemas.
## Run once after cloning or after make infra-clean.

bootstrap: infra-up topics
	@echo ""
	@echo "Bootstrap complete. Next steps:"
	@echo "  make update-geoip   (if infra/geoip/GeoLite2-City.mmdb is missing)"
	@echo "  make flink-job      (dedicated terminal)"
	@echo "  make generate       (another terminal)"

# ── GeoIP database ────────────────────────────────────────────────────────
## Requires MAXMIND_LICENCE_KEY env var (free at maxmind.com).
## Re-run after make infra-clean or after cloning.

update-geoip:
	@if [ -z "$$MAXMIND_LICENCE_KEY" ]; then \
	    echo "Error: MAXMIND_LICENCE_KEY is not set. Get a free key at https://www.maxmind.com/en/geolite2/signup"; \
	    exit 1; \
	fi
	@echo "Downloading GeoLite2-City database..."
	@tmpdir=$$(mktemp -d) && \
	  curl -sL \
	    "https://download.maxmind.com/app/geoip_download?edition_id=GeoLite2-City&license_key=$${MAXMIND_LICENCE_KEY}&suffix=tar.gz" \
	    | tar -xz -C $$tmpdir && \
	  find $$tmpdir -name "GeoLite2-City.mmdb" -exec mv {} infra/geoip/GeoLite2-City.mmdb \; && \
	  rm -rf $$tmpdir
	@echo "GeoLite2-City.mmdb downloaded to infra/geoip/"
	@ls -lh infra/geoip/GeoLite2-City.mmdb

# ── Flink enrichment job ─────────────────────────────────────────────────

flink-job:
	$(PYTHON) -m pipelines.processing.job \
	  --kafka-brokers localhost:9092 \
	  --input-topic txn.api \
	  --output-topic txn.enriched \
	  --geoip-db-path infra/geoip/GeoLite2-City.mmdb

# ── Data generation ──────────────────────────────────────────────────────
## COUNT=50 DELAY=200 make generate

generate:
	$(PYTHON) scripts/generate_transactions.py --count $(or $(COUNT),10) --delay $(or $(DELAY),500)

## Tail txn.enriched without producing
consume:
	$(PYTHON) scripts/generate_transactions.py --consume-only

# ── Python dependencies ──────────────────────────────────────────────────

install:
	pip install -e ".[processing]"

# ── Tests ─────────────────────────────────────────────────────────────────

test-unit:
	pytest tests/unit/ --cov=pipelines/processing --cov-fail-under=80 -v

test-integration:
	pytest -m integration tests/integration/ -v

test: test-unit
