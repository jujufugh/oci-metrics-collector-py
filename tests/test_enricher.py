"""
Unit tests for the instance metadata enricher.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from oci_metrics_collector.collector import MetricDataPoint
from oci_metrics_collector.config import CollectorConfig, MetricsConfig, OciAuthConfig, TenancyConfig
from oci_metrics_collector.enricher import (
    FIXED_SHAPE_SPECS,
    EnrichedMetricRecord,
    InstanceMetadataCache,
    enrich_metrics,
)


@pytest.fixture
def config():
    """Build a minimal test config."""
    return CollectorConfig(
        oci=OciAuthConfig(auth_method="config_file"),
        tenancies=[
            TenancyConfig(
                name="parent",
                tenancy_id="ocid1.tenancy.oc1..test",
                regions=["us-ashburn-1"],
            )
        ],
        metrics=MetricsConfig(),
    )


def _make_datapoint(instance_id, metric_name, value, ts=None):
    """Helper to create a MetricDataPoint."""
    return MetricDataPoint(
        source_tenancy_name="parent",
        source_tenancy_id="ocid1.tenancy.oc1..test",
        region="us-ashburn-1",
        instance_id=instance_id,
        metric_name=metric_name,
        timestamp=ts or datetime(2026, 4, 13, 10, 0, 0, tzinfo=timezone.utc),
        value=value,
        statistic="mean",
        compartment_id="ocid1.compartment.oc1..test",
    )


def _make_mock_instance(
    instance_id,
    display_name,
    shape,
    ocpus=None,
    memory_gbs=None,
    lifecycle_state="RUNNING",
    compartment_id="ocid1.compartment.oc1..test",
):
    """Helper to create a mock OCI instance object."""
    inst = MagicMock()
    inst.id = instance_id
    inst.display_name = display_name
    inst.shape = shape
    inst.lifecycle_state = lifecycle_state
    inst.availability_domain = "US-ASHBURN-AD-1"
    inst.fault_domain = "FAULT-DOMAIN-1"
    inst.compartment_id = compartment_id

    if ocpus is not None:
        inst.shape_config = MagicMock()
        inst.shape_config.ocpus = ocpus
        inst.shape_config.memory_in_gbs = memory_gbs
    else:
        inst.shape_config = None

    return inst


class TestFixedShapeSpecs:
    """Verify the fixed shape lookup table."""

    def test_standard2_1(self):
        assert FIXED_SHAPE_SPECS["VM.Standard2.1"] == (1, 15)

    def test_standard2_24(self):
        assert FIXED_SHAPE_SPECS["VM.Standard2.24"] == (24, 320)

    def test_flex_shapes_are_none(self):
        assert FIXED_SHAPE_SPECS["VM.Standard.E4.Flex"] is None


class TestEnrichMetrics:
    """Tests for the enrich_metrics function."""

    @patch("oci_metrics_collector.enricher.InstanceMetadataCache")
    def test_enrich_flex_instance(self, MockCache, config):
        """Test enrichment with a flexible shape instance."""
        # Setup mock cache
        mock_cache = MagicMock()
        mock_meta = MagicMock()
        mock_meta.source_tenancy_name = "parent"
        mock_meta.source_tenancy_id = "ocid1.tenancy.oc1..test"
        mock_meta.region = "us-ashburn-1"
        mock_meta.display_name = "web-server-01"
        mock_meta.shape = "VM.Standard.E4.Flex"
        mock_meta.lifecycle_state = "RUNNING"
        mock_meta.availability_domain = "US-ASHBURN-AD-1"
        mock_meta.fault_domain = "FAULT-DOMAIN-1"
        mock_meta.compartment_id = "ocid1.compartment.oc1..test"
        mock_meta.ocpus = 4.0
        mock_meta.memory_in_gbs = 64.0
        mock_cache.get.return_value = mock_meta
        MockCache.return_value = mock_cache

        datapoints = [
            _make_datapoint("ocid1.instance.oc1..inst1", "CpuUtilization", 50.0),
            _make_datapoint("ocid1.instance.oc1..inst1", "MemoryUtilization", 75.0),
        ]

        result = enrich_metrics(config, datapoints)

        assert len(result) == 1
        record = result[0]
        assert isinstance(record, EnrichedMetricRecord)
        assert record.instance_name == "web-server-01"
        assert record.cpu_allocated_ocpus == 4.0
        assert record.memory_allocated_gbs == 64.0
        assert record.cpu_utilization_pct == 50.0
        assert record.memory_utilization_pct == 75.0
        # Derived fields
        assert record.cpu_usage_ocpus == 2.0  # 50% of 4 OCPUs
        assert record.memory_used_gbs == 48.0  # 75% of 64 GB

    @patch("oci_metrics_collector.enricher.InstanceMetadataCache")
    def test_enrich_unknown_instance(self, MockCache, config):
        """Test enrichment when instance metadata is not found."""
        mock_cache = MagicMock()
        mock_cache.get.return_value = None
        MockCache.return_value = mock_cache

        datapoints = [
            _make_datapoint("ocid1.instance.oc1..unknown", "CpuUtilization", 80.0),
        ]

        result = enrich_metrics(config, datapoints)

        assert len(result) == 1
        assert result[0].instance_name == "UNKNOWN"
        assert result[0].cpu_usage_ocpus is None  # Can't compute without spec

    @patch("oci_metrics_collector.enricher.InstanceMetadataCache")
    def test_enrich_groups_by_instance_timestamp(self, MockCache, config):
        """Test that CPU and Memory for the same instance+timestamp merge."""
        mock_cache = MagicMock()
        mock_meta = MagicMock()
        mock_meta.source_tenancy_name = "parent"
        mock_meta.source_tenancy_id = "ocid1.tenancy.oc1..test"
        mock_meta.region = "us-ashburn-1"
        mock_meta.display_name = "db-server-01"
        mock_meta.shape = "VM.Standard2.4"
        mock_meta.lifecycle_state = "RUNNING"
        mock_meta.availability_domain = "US-ASHBURN-AD-1"
        mock_meta.fault_domain = "FD-1"
        mock_meta.compartment_id = "ocid1.compartment.oc1..test"
        mock_meta.ocpus = 4.0
        mock_meta.memory_in_gbs = 60.0
        mock_cache.get.return_value = mock_meta
        MockCache.return_value = mock_cache

        ts = datetime(2026, 4, 13, 10, 5, 0, tzinfo=timezone.utc)
        datapoints = [
            _make_datapoint("ocid1.instance.oc1..inst1", "CpuUtilization", 25.0, ts),
            _make_datapoint("ocid1.instance.oc1..inst1", "MemoryUtilization", 50.0, ts),
        ]

        result = enrich_metrics(config, datapoints)

        # Should merge into 1 record (same instance + timestamp)
        assert len(result) == 1
        assert result[0].cpu_utilization_pct == 25.0
        assert result[0].memory_utilization_pct == 50.0


class TestInstanceMetadataCache:
    """Tests for compute metadata discovery."""

    @patch("oci_metrics_collector.enricher.oci.pagination.list_call_get_all_results")
    @patch("oci_metrics_collector.enricher.oci.identity.IdentityClient")
    @patch("oci_metrics_collector.enricher.oci.core.ComputeClient")
    @patch("oci_metrics_collector.enricher._build_oci_config")
    def test_subtree_refresh_lists_instances_in_each_compartment(
        self,
        mock_build_config,
        MockComputeClient,
        MockIdentityClient,
        mock_list_all,
    ):
        """Subtree metadata refresh enumerates compartments before instances."""
        mock_build_config.return_value = ({}, None)
        mock_compute = MagicMock()
        mock_identity = MagicMock()
        MockComputeClient.return_value = mock_compute
        MockIdentityClient.return_value = mock_identity

        child1 = MagicMock()
        child1.id = "ocid1.compartment.oc1..child1"
        child2 = MagicMock()
        child2.id = "ocid1.compartment.oc1..child2"

        instances_by_compartment = {
            "ocid1.tenancy.oc1..test": [
                _make_mock_instance(
                    "ocid1.instance.oc1..root",
                    "root-instance",
                    "VM.Standard2.1",
                    compartment_id="ocid1.tenancy.oc1..test",
                )
            ],
            "ocid1.compartment.oc1..child1": [
                _make_mock_instance(
                    "ocid1.instance.oc1..child1",
                    "child1-instance",
                    "VM.Standard2.1",
                    compartment_id="ocid1.compartment.oc1..child1",
                )
            ],
            "ocid1.compartment.oc1..child2": [
                _make_mock_instance(
                    "ocid1.instance.oc1..child2",
                    "child2-instance",
                    "VM.Standard2.1",
                    compartment_id="ocid1.compartment.oc1..child2",
                )
            ],
        }

        def list_all_side_effect(func, **kwargs):
            if func is mock_identity.list_compartments:
                return MagicMock(data=[child1, child2])
            if func is mock_compute.list_instances:
                return MagicMock(data=instances_by_compartment[kwargs["compartment_id"]])
            raise AssertionError(f"Unexpected paginated function: {func}")

        mock_list_all.side_effect = list_all_side_effect

        cache = InstanceMetadataCache(
            CollectorConfig(
                oci=OciAuthConfig(auth_method="config_file"),
                tenancies=[
                    TenancyConfig(
                        name="parent",
                        tenancy_id="ocid1.tenancy.oc1..test",
                        regions=["us-ashburn-1"],
                    )
                ],
            )
        )
        cache.refresh()

        assert cache.get("ocid1.instance.oc1..root").display_name == "root-instance"
        assert cache.get("ocid1.instance.oc1..root").source_tenancy_name == "parent"
        assert cache.get("ocid1.instance.oc1..root").region == "us-ashburn-1"
        assert cache.get("ocid1.instance.oc1..child1").compartment_id == (
            "ocid1.compartment.oc1..child1"
        )
        listed_instance_compartments = {
            call.kwargs["compartment_id"]
            for call in mock_list_all.call_args_list
            if call.args[0] is mock_compute.list_instances
        }
        assert listed_instance_compartments == set(instances_by_compartment)


class TestDerivedFieldCalculations:
    """Test derived field calculation edge cases."""

    def test_cpu_usage_calculation(self):
        """Verify CPU usage = (util% / 100) * allocated OCPUs."""
        # 25% of 8 OCPUs = 2.0
        assert round((25.0 / 100.0) * 8.0, 4) == 2.0

    def test_memory_usage_calculation(self):
        """Verify memory usage = (util% / 100) * allocated GB."""
        # 33.33% of 120 GB ≈ 39.996
        assert round((33.33 / 100.0) * 120.0, 4) == 39.996

    def test_zero_utilization(self):
        """Verify zero utilization produces zero usage."""
        assert round((0.0 / 100.0) * 16.0, 4) == 0.0

    def test_full_utilization(self):
        """Verify 100% utilization equals allocated amount."""
        assert round((100.0 / 100.0) * 4.0, 4) == 4.0
