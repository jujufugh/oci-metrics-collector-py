"""
Unit tests for the OCI Monitoring metrics collector.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from oci_metrics_collector.collector import MetricDataPoint, _stat_to_mql, collect_metrics
from oci_metrics_collector.config import CollectorConfig, MetricsConfig, OciAuthConfig, ScopeConfig


@pytest.fixture
def config():
    """Build a minimal test config."""
    return CollectorConfig(
        oci=OciAuthConfig(auth_method="config_file"),
        scope=ScopeConfig(compartment_id="ocid1.compartment.oc1..test"),
        metrics=MetricsConfig(
            namespace="oci_computeagent",
            metric_stats={
                "CpuUtilization": ["mean"],
                "MemoryUtilization": ["mean"],
            },
            resolution="5m",
            lookback_minutes=10,
        ),
    )


class TestStatToMql:
    """Tests for the MQL statistic mapping."""

    def test_simple_aggregations(self):
        assert _stat_to_mql("mean") == "mean()"
        assert _stat_to_mql("max") == "max()"
        assert _stat_to_mql("min") == "min()"

    def test_percentiles(self):
        assert _stat_to_mql("p99") == "percentile(.99)"
        assert _stat_to_mql("p95") == "percentile(.95)"
        assert _stat_to_mql("p90") == "percentile(.90)"

    def test_unknown_raises(self):
        with pytest.raises(ValueError):
            _stat_to_mql("median")


class TestMetricDataPoint:
    """Tests for the MetricDataPoint dataclass."""

    def test_create_datapoint(self):
        dp = MetricDataPoint(
            instance_id="ocid1.instance.oc1..test",
            metric_name="CpuUtilization",
            timestamp=datetime(2026, 4, 13, 10, 0, 0, tzinfo=timezone.utc),
            value=45.5,
            statistic="mean",
            compartment_id="ocid1.compartment.oc1..test",
        )
        assert dp.instance_id == "ocid1.instance.oc1..test"
        assert dp.metric_name == "CpuUtilization"
        assert dp.value == 45.5

    def test_datapoint_equality(self):
        kwargs = dict(
            instance_id="ocid1.instance.oc1..test",
            metric_name="CpuUtilization",
            timestamp=datetime(2026, 4, 13, 10, 0, 0, tzinfo=timezone.utc),
            value=45.5,
            statistic="mean",
            compartment_id="ocid1.compartment.oc1..test",
        )
        assert MetricDataPoint(**kwargs) == MetricDataPoint(**kwargs)


class TestCollectMetrics:
    """Tests for the collect_metrics function."""

    @patch("oci_metrics_collector.collector._create_monitoring_client")
    def test_collect_returns_datapoints(self, mock_create_client, config):
        """Test that collect_metrics returns properly structured data points."""
        # Mock the OCI response
        mock_dp = MagicMock()
        mock_dp.timestamp = datetime(2026, 4, 13, 10, 0, 0, tzinfo=timezone.utc)
        mock_dp.value = 45.5

        mock_metric = MagicMock()
        mock_metric.dimensions = {"resourceId": "ocid1.instance.oc1..inst1"}
        mock_metric.aggregated_datapoints = [mock_dp]

        mock_response = MagicMock()
        mock_response.data = [mock_metric]

        mock_client = MagicMock()
        mock_client.summarize_metrics_data.return_value = mock_response
        mock_create_client.return_value = mock_client

        result = collect_metrics(config)

        # Should have datapoints for each metric name
        assert len(result) >= 1
        assert all(isinstance(dp, MetricDataPoint) for dp in result)
        assert result[0].instance_id == "ocid1.instance.oc1..inst1"
        assert result[0].value == 45.5

    @patch("oci_metrics_collector.collector._create_monitoring_client")
    def test_collect_empty_response(self, mock_create_client, config):
        """Test that an empty response returns no data points."""
        mock_response = MagicMock()
        mock_response.data = []

        mock_client = MagicMock()
        mock_client.summarize_metrics_data.return_value = mock_response
        mock_create_client.return_value = mock_client

        result = collect_metrics(config)
        assert result == []

    @patch("oci_metrics_collector.collector._create_monitoring_client")
    def test_collect_multiple_instances(self, mock_create_client, config):
        """Test collection across multiple instances."""
        mock_dp1 = MagicMock()
        mock_dp1.timestamp = datetime(2026, 4, 13, 10, 0, 0, tzinfo=timezone.utc)
        mock_dp1.value = 30.0

        mock_dp2 = MagicMock()
        mock_dp2.timestamp = datetime(2026, 4, 13, 10, 0, 0, tzinfo=timezone.utc)
        mock_dp2.value = 70.0

        mock_metric1 = MagicMock()
        mock_metric1.dimensions = {"resourceId": "ocid1.instance.oc1..inst1"}
        mock_metric1.aggregated_datapoints = [mock_dp1]

        mock_metric2 = MagicMock()
        mock_metric2.dimensions = {"resourceId": "ocid1.instance.oc1..inst2"}
        mock_metric2.aggregated_datapoints = [mock_dp2]

        mock_response = MagicMock()
        mock_response.data = [mock_metric1, mock_metric2]

        mock_client = MagicMock()
        mock_client.summarize_metrics_data.return_value = mock_response
        mock_create_client.return_value = mock_client

        result = collect_metrics(config)

        # 2 instances × 2 metrics = 4 data points (both CpuUtil + MemUtil)
        instance_ids = {dp.instance_id for dp in result}
        assert "ocid1.instance.oc1..inst1" in instance_ids
        assert "ocid1.instance.oc1..inst2" in instance_ids

    @patch("oci_metrics_collector.collector._create_monitoring_client")
    def test_collect_handles_api_error(self, mock_create_client, config):
        """Test that API errors are handled gracefully."""
        import oci

        mock_client = MagicMock()
        mock_client.summarize_metrics_data.side_effect = oci.exceptions.ServiceError(
            status=429,
            code="TooManyRequests",
            headers={},
            message="Rate limit exceeded",
        )
        mock_create_client.return_value = mock_client

        result = collect_metrics(config)
        # Should return empty list, not raise
        assert result == []
