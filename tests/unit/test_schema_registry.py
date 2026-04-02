"""Unit tests for the Schema Registry wrapper."""

from unittest.mock import patch

import pytest

from pipelines.ingestion.shared.schema_registry import connect_with_retry


def test_connect_raises_after_retries():
    """connect_with_retry should raise RuntimeError after all retries fail."""
    with patch("pipelines.ingestion.shared.schema_registry._ConfluentSRClient") as MockClient:
        instance = MockClient.return_value
        instance.get_subjects.side_effect = Exception("connection refused")
        with pytest.raises(RuntimeError, match="unreachable"):
            connect_with_retry("http://localhost:8081", retries=2, base_delay=0.0)


def test_connect_succeeds_on_second_attempt():
    """connect_with_retry should succeed if a retry succeeds."""
    with patch("pipelines.ingestion.shared.schema_registry._ConfluentSRClient") as MockClient:
        instance = MockClient.return_value
        instance.get_subjects.side_effect = [Exception("timeout"), []]
        wrapper = connect_with_retry("http://localhost:8081", retries=3, base_delay=0.0)
        assert wrapper is not None
