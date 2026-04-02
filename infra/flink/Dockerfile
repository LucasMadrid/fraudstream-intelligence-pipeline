# Multi-stage Dockerfile for PyFlink 1.19 worker image
# Context: repo root (.)
# Build: docker build -f infra/flink/Dockerfile --build-arg PROCESSOR_VERSION=... .

# ============================================================================
# BUILDER STAGE — compile C extensions and install Python deps into a venv
# ============================================================================
FROM apache/flink:1.19-scala_2.12 AS builder

ARG PROCESSOR_VERSION=002-stream-processor@1.0.0

# Build tools needed for maxminddb-c C extension
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libmaxminddb-dev \
    python3.11-dev \
    && rm -rf /var/lib/apt/lists/*

# Isolated venv so nothing bleeds into the system Python
RUN python3.11 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    VIRTUAL_ENV="/opt/venv"

RUN pip install --upgrade pip setuptools wheel

# Copy source — pyproject.toml first so dependency layer is cached independently
COPY pyproject.toml ./
COPY pipelines/ ./pipelines/

# Install processing deps (non-editable — wheels land in venv site-packages)
# Excludes dev extras (pytest, ruff, hypothesis)
RUN pip install --no-cache-dir ".[processing]"

# ============================================================================
# RUNTIME STAGE — minimal final image, no build tools
# ============================================================================
FROM apache/flink:1.19-scala_2.12

ARG PROCESSOR_VERSION=002-stream-processor@1.0.0
ENV PROCESSOR_VERSION=$PROCESSOR_VERSION \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    VIRTUAL_ENV="/opt/venv"

# Only the runtime .so for maxminddb-c — no build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    libmaxminddb0 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy fully-built venv (includes installed pipeline package + all wheels)
COPY --from=builder --chown=flink:flink /opt/venv /opt/venv

# GeoIP DB is volume-mounted at runtime — never baked into the image
RUN mkdir -p /opt/geoip && chown flink:flink /opt/geoip

WORKDIR /opt/flink

# Smoke-test: verify PyFlink and the pipeline package are importable
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python3.11 -c "import pyflink; from pipelines.processing import job" || exit 1

# apache/flink base already defines a flink user; run as non-root
USER flink
