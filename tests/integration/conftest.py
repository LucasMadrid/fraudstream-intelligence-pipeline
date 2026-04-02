"""Integration test fixtures for the enrichment pipeline.

Markers:
  integration — requires Docker (opt-in: pytest -m integration)

Usage:
  pytest -m integration tests/integration/
"""

import os
import time

import pytest

# Disable Ryuk (testcontainers reaper) — required on macOS Docker Desktop
# where the Docker socket is not at /var/run/docker.sock and cannot be
# bind-mounted into containers.
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")


# ---------------------------------------------------------------------------
# Kafka container
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def kafka_container():
    """Start a single-node Kafka (KRaft) container for the test session."""
    from testcontainers.kafka import KafkaContainer

    with KafkaContainer("confluentinc/cp-kafka:7.6.1") as kafka:
        yield kafka


@pytest.fixture(scope="session")
def kafka_bootstrap(kafka_container):
    """Return the bootstrap-servers string for the test Kafka container."""
    return kafka_container.get_bootstrap_server()


# ---------------------------------------------------------------------------
# Schema Registry mock
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def schema_registry_mock():
    """Start a mock Schema Registry container for the test session."""
    try:
        from testcontainers.core.container import DockerContainer

        class SchemaRegistryContainer(DockerContainer):
            def __init__(self, bootstrap_servers: str) -> None:
                super().__init__("confluentinc/cp-schema-registry:7.6.1")
                self.with_env("SCHEMA_REGISTRY_HOST_NAME", "schema-registry")
                self.with_env("SCHEMA_REGISTRY_KAFKASTORE_BOOTSTRAP_SERVERS", bootstrap_servers)
                self.with_env("SCHEMA_REGISTRY_LISTENERS", "http://0.0.0.0:8081")
                self.with_exposed_ports(8081)

            def get_url(self) -> str:
                host = self.get_container_host_ip()
                port = self.get_exposed_port(8081)
                return f"http://{host}:{port}"

        return SchemaRegistryContainer
    except ImportError:
        pytest.skip("testcontainers not installed")


# ---------------------------------------------------------------------------
# Flink mini-cluster
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def flink_minicluster():
    """Return a PyFlink MiniCluster context manager for integration tests.

    Usage:
        with flink_minicluster as mc:
            env = StreamExecutionEnvironment.get_execution_environment()
            ...
    """
    try:
        from pyflink.datastream import StreamExecutionEnvironment

        class _MiniCluster:
            def __enter__(self):
                self.env = StreamExecutionEnvironment.get_execution_environment()
                self.env.set_parallelism(1)
                return self.env

            def __exit__(self, *_):
                pass

        return _MiniCluster()
    except ImportError:
        pytest.skip("pyflink not installed — run: pip install -e '.[processing]'")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def wait_for_messages(consumer, *, count: int, timeout_s: float = 30.0) -> list:
    """Poll a Kafka consumer until ``count`` messages are received or timeout."""
    messages = []
    deadline = time.time() + timeout_s
    while len(messages) < count and time.time() < deadline:
        msg = consumer.poll(timeout=1.0)
        if msg is not None and not msg.error():
            messages.append(msg)
    return messages
