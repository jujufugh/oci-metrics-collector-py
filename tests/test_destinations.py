"""
Unit tests for destination writers (ADB and Log Analytics).
"""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from oci_metrics_collector.config import (
    AdbConfig,
    CollectorConfig,
    LogAnalyticsConfig,
    OciAuthConfig,
    ScopeConfig,
)
from oci_metrics_collector.destinations.log_analytics import LogAnalyticsWriter
from oci_metrics_collector.enricher import EnrichedMetricRecord


def _make_record(
    instance_id="ocid1.instance.oc1..inst1",
    instance_name="web-server-01",
    cpu_pct=45.2,
    mem_pct=72.8,
    cpu_usage=1.808,
    mem_used=46.592,
):
    """Create a sample EnrichedMetricRecord."""
    return EnrichedMetricRecord(
        collection_time=datetime(2026, 4, 13, 10, 0, 0, tzinfo=timezone.utc),
        instance_id=instance_id,
        instance_name=instance_name,
        compartment_id="ocid1.compartment.oc1..test",
        availability_domain="US-ASHBURN-AD-1",
        fault_domain="FAULT-DOMAIN-1",
        shape="VM.Standard.E4.Flex",
        lifecycle_state="RUNNING",
        cpu_allocated_ocpus=4.0,
        memory_allocated_gbs=64.0,
        cpu_utilization_pct=cpu_pct,
        memory_utilization_pct=mem_pct,
        cpu_usage_ocpus=cpu_usage,
        memory_used_gbs=mem_used,
        statistic_type="mean",
    )


class TestLogAnalyticsPayloadFormat:
    """Test the JSON payload structure for Log Analytics upload."""

    @pytest.fixture
    def config(self):
        return CollectorConfig(
            oci=OciAuthConfig(auth_method="config_file"),
            scope=ScopeConfig(compartment_id="ocid1.compartment.oc1..test"),
            log_analytics=LogAnalyticsConfig(
                enabled=True,
                namespace="test_ns",
                log_group_id="ocid1.loganalyticsloggroup.oc1..test",
                log_source_name="OCI Compute Metrics",
                entity_mapping={
                    "ocid1.instance.oc1..inst1": "ocid1.loganalyticsentity.oc1..entity1",
                },
            ),
        )

    @patch("oci_metrics_collector.destinations.log_analytics._build_oci_config")
    def test_payload_structure(self, mock_build_config, config):
        """Verify the JSON payload has the correct top-level structure."""
        mock_build_config.return_value = ({}, None)

        with patch("oci.log_analytics.LogAnalyticsClient"):
            writer = LogAnalyticsWriter(config)

        records = [_make_record()]
        payload = writer._build_payload(records)

        assert "metadata" in payload
        assert "logEvents" in payload
        assert payload["metadata"]["collector"] == "oci-metrics-collector-py"
        assert len(payload["logEvents"]) == 1

    @patch("oci_metrics_collector.destinations.log_analytics._build_oci_config")
    def test_log_event_structure(self, mock_build_config, config):
        """Verify individual logEvent entries."""
        mock_build_config.return_value = ({}, None)

        with patch("oci.log_analytics.LogAnalyticsClient"):
            writer = LogAnalyticsWriter(config)

        records = [_make_record()]
        payload = writer._build_payload(records)

        event = payload["logEvents"][0]
        assert event["logSourceName"] == "OCI Compute Metrics"
        assert event["entityId"] == "ocid1.loganalyticsentity.oc1..entity1"
        assert len(event["logRecords"]) == 1

        # Verify log record is valid JSON
        log_line = json.loads(event["logRecords"][0])
        assert log_line["instance_name"] == "web-server-01"
        assert log_line["cpu_utilization_pct"] == 45.2
        assert log_line["memory_utilization_pct"] == 72.8

    @patch("oci_metrics_collector.destinations.log_analytics._build_oci_config")
    def test_grouping_by_instance(self, mock_build_config, config):
        """Verify records are grouped by instance_id."""
        mock_build_config.return_value = ({}, None)

        with patch("oci.log_analytics.LogAnalyticsClient"):
            writer = LogAnalyticsWriter(config)

        records = [
            _make_record(instance_id="ocid1.instance.oc1..inst1"),
            _make_record(instance_id="ocid1.instance.oc1..inst2",
                         instance_name="db-server-01"),
            _make_record(instance_id="ocid1.instance.oc1..inst1"),
        ]
        payload = writer._build_payload(records)

        # Should have 2 logEvents (grouped by instance)
        assert len(payload["logEvents"]) == 2

        # inst1 should have 2 log records
        inst1_event = next(
            e for e in payload["logEvents"]
            if any("inst1" in lr for lr in e["logRecords"])
        )
        assert len(inst1_event["logRecords"]) == 2

    @patch("oci_metrics_collector.destinations.log_analytics._build_oci_config")
    def test_no_entity_mapping(self, mock_build_config, config):
        """Verify events without entity mapping omit entityId."""
        config.log_analytics.entity_mapping = {}
        mock_build_config.return_value = ({}, None)

        with patch("oci.log_analytics.LogAnalyticsClient"):
            writer = LogAnalyticsWriter(config)

        records = [_make_record()]
        payload = writer._build_payload(records)

        event = payload["logEvents"][0]
        assert "entityId" not in event


class TestAdbRecordConversion:
    """Test the ADB record-to-row conversion."""

    def test_record_fields(self):
        """Verify all expected fields are present in the record."""
        record = _make_record()
        assert record.collection_time is not None
        assert record.instance_id == "ocid1.instance.oc1..inst1"
        assert record.cpu_allocated_ocpus == 4.0
        assert record.memory_allocated_gbs == 64.0
        assert record.cpu_utilization_pct == 45.2
        assert record.memory_utilization_pct == 72.8
        assert record.cpu_usage_ocpus == 1.808
        assert record.memory_used_gbs == 46.592
        assert record.statistic_type == "mean"

    def test_record_with_none_values(self):
        """Verify record handles None derived values."""
        record = EnrichedMetricRecord(
            collection_time=datetime(2026, 4, 13, tzinfo=timezone.utc),
            instance_id="ocid1.instance.oc1..inst1",
            instance_name="test",
            compartment_id="ocid1.compartment.oc1..test",
            availability_domain="AD-1",
            fault_domain=None,
            shape="UNKNOWN",
            lifecycle_state="RUNNING",
            cpu_allocated_ocpus=None,
            memory_allocated_gbs=None,
            cpu_utilization_pct=50.0,
            memory_utilization_pct=None,
            cpu_usage_ocpus=None,
            memory_used_gbs=None,
        )
        # Should not raise
        assert record.cpu_allocated_ocpus is None
        assert record.memory_used_gbs is None
