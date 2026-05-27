"""
Instance metadata enrichment.

Enriches raw metric data points with compute instance details
(display name, shape, OCPUs, memory allocation) and computes
derived fields like memory_used_gbs and cpu_usage_ocpus.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Set

import oci

from .collector import MetricDataPoint, _build_oci_config
from .config import CollectorConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lookup table for common fixed (non-flex) OCI compute shapes.
# Maps shape name → (ocpus, memory_in_gbs).
# Flex shapes are dynamically sized and read from the instance API.
# ---------------------------------------------------------------------------
FIXED_SHAPE_SPECS: Dict[str, tuple] = {
    # Standard shapes
    "VM.Standard2.1": (1, 15),
    "VM.Standard2.2": (2, 30),
    "VM.Standard2.4": (4, 60),
    "VM.Standard2.8": (8, 120),
    "VM.Standard2.16": (16, 240),
    "VM.Standard2.24": (24, 320),
    "VM.Standard.E2.1": (1, 8),
    "VM.Standard.E2.2": (2, 16),
    "VM.Standard.E2.4": (4, 32),
    "VM.Standard.E2.8": (8, 64),
    "VM.Standard3.Flex": None,  # Flex — handled dynamically
    "VM.Standard.E3.Flex": None,
    "VM.Standard.E4.Flex": None,
    "VM.Standard.E5.Flex": None,
    "VM.Standard.A1.Flex": None,
    "VM.Optimized3.Flex": None,
    # Dense I/O
    "VM.DenseIO2.8": (8, 120),
    "VM.DenseIO2.16": (16, 240),
    "VM.DenseIO2.24": (24, 320),
    # GPU
    "VM.GPU2.1": (12, 72),
    "VM.GPU3.1": (6, 90),
    "VM.GPU3.2": (12, 180),
    "VM.GPU3.4": (24, 360),
    "BM.GPU4.8": (64, 2048),
    # Bare Metal
    "BM.Standard2.52": (52, 768),
    "BM.Standard.E2.64": (64, 512),
    "BM.Standard.E3.128": (128, 2048),
    "BM.Standard.E4.128": (128, 2048),
}


@dataclass
class InstanceMetadata:
    """Cached metadata for a single compute instance."""
    instance_id: str
    display_name: str
    shape: str
    lifecycle_state: str
    availability_domain: str
    fault_domain: Optional[str]
    compartment_id: str
    ocpus: Optional[float] = None
    memory_in_gbs: Optional[float] = None


@dataclass
class EnrichedMetricRecord:
    """A metric data point enriched with instance metadata and derived fields."""
    # Timing
    collection_time: datetime

    # Instance identity
    instance_id: str
    instance_name: str
    compartment_id: str
    availability_domain: str
    fault_domain: Optional[str]
    shape: str
    lifecycle_state: str

    # Allocated resources
    cpu_allocated_ocpus: Optional[float]
    memory_allocated_gbs: Optional[float]

    # CPU utilization (one column per statistic; mean kept in original column)
    cpu_utilization_pct: Optional[float] = None
    cpu_utilization_pct_p99: Optional[float] = None
    cpu_utilization_pct_p95: Optional[float] = None
    cpu_utilization_pct_max: Optional[float] = None

    # Memory utilization
    memory_utilization_pct: Optional[float] = None
    memory_utilization_pct_p99: Optional[float] = None
    memory_utilization_pct_p95: Optional[float] = None
    memory_utilization_pct_max: Optional[float] = None

    # Additional compute-agent metrics
    load_average_p95: Optional[float] = None
    memory_allocation_stalls_p95: Optional[float] = None

    # Derived usage (computed from mean values)
    cpu_usage_ocpus: Optional[float] = None
    memory_used_gbs: Optional[float] = None

    # Collection metadata
    statistic_type: str = "aggregate"


# (metric_name, statistic) → EnrichedMetricRecord field name.
# Add entries here when adding new metrics or statistics.
METRIC_STAT_FIELD_MAP: Dict[tuple, str] = {
    ("CpuUtilization", "mean"): "cpu_utilization_pct",
    ("CpuUtilization", "p99"): "cpu_utilization_pct_p99",
    ("CpuUtilization", "p95"): "cpu_utilization_pct_p95",
    ("CpuUtilization", "max"): "cpu_utilization_pct_max",
    ("MemoryUtilization", "mean"): "memory_utilization_pct",
    ("MemoryUtilization", "p99"): "memory_utilization_pct_p99",
    ("MemoryUtilization", "p95"): "memory_utilization_pct_p95",
    ("MemoryUtilization", "max"): "memory_utilization_pct_max",
    ("LoadAverage", "p95"): "load_average_p95",
    ("MemoryAllocationStalls", "p95"): "memory_allocation_stalls_p95",
}


class InstanceMetadataCache:
    """
    Caches compute instance metadata from OCI.

    Fetches instance details using ComputeClient.list_instances()
    and provides O(1) lookup by instance OCID.
    """

    def __init__(self, config: CollectorConfig):
        self._config = config
        self._cache: Dict[str, InstanceMetadata] = {}
        self._client = self._create_compute_client()
        self._identity_client = None

    def _create_compute_client(self):
        """Create an OCI ComputeClient with the appropriate auth."""
        oci_config, signer = _build_oci_config(self._config)
        if signer:
            return oci.core.ComputeClient(config={}, signer=signer)
        return oci.core.ComputeClient(oci_config)

    def _create_identity_client(self):
        """Create an OCI IdentityClient with the appropriate auth."""
        oci_config, signer = _build_oci_config(self._config)
        if signer:
            return oci.identity.IdentityClient(config={}, signer=signer)
        return oci.identity.IdentityClient(oci_config)

    def _get_instance_compartment_ids(self) -> List[str]:
        """Return the compartments whose instances should be cached."""
        compartment_id = self._config.scope.compartment_id
        if not self._config.scope.compartment_id_in_subtree:
            return [compartment_id]

        if self._identity_client is None:
            self._identity_client = self._create_identity_client()

        logger.info("Listing active compartments under tenancy: %s", compartment_id)
        compartments = oci.pagination.list_call_get_all_results(
            self._identity_client.list_compartments,
            compartment_id=compartment_id,
            compartment_id_in_subtree=True,
            access_level="ACCESSIBLE",
            lifecycle_state="ACTIVE",
        ).data

        compartment_ids: Set[str] = {compartment_id}
        compartment_ids.update(comp.id for comp in compartments)
        return sorted(compartment_ids)

    def _cache_instance(self, inst) -> None:
        """Cache one OCI compute instance response object."""
        if inst.lifecycle_state == "TERMINATED":
            return

        # Determine OCPUs and memory
        ocpus = None
        memory_gbs = None

        if inst.shape_config:
            # Flex shapes: read from shape_config
            ocpus = inst.shape_config.ocpus
            memory_gbs = inst.shape_config.memory_in_gbs
        else:
            # Fixed shapes: lookup table
            specs = FIXED_SHAPE_SPECS.get(inst.shape)
            if specs:
                ocpus, memory_gbs = specs
            else:
                logger.warning(
                    "Unknown shape '%s' for instance %s - "
                    "allocated resources will be None",
                    inst.shape,
                    inst.display_name,
                )

        self._cache[inst.id] = InstanceMetadata(
            instance_id=inst.id,
            display_name=inst.display_name,
            shape=inst.shape,
            lifecycle_state=inst.lifecycle_state,
            availability_domain=inst.availability_domain,
            fault_domain=getattr(inst, "fault_domain", None),
            compartment_id=inst.compartment_id,
            ocpus=ocpus,
            memory_in_gbs=memory_gbs,
        )

    def refresh(self) -> None:
        """
        Refresh the instance metadata cache by listing all instances
        in the configured compartment or tenancy subtree.
        """
        self._cache.clear()
        compartment_ids = self._get_instance_compartment_ids()
        logger.info(
            "Refreshing instance metadata from %d compartment(s)",
            len(compartment_ids),
        )

        for compartment_id in compartment_ids:
            logger.debug("Listing instances in compartment: %s", compartment_id)
            try:
                instances = oci.pagination.list_call_get_all_results(
                    self._client.list_instances,
                    compartment_id=compartment_id,
                ).data
            except oci.exceptions.ServiceError as e:
                logger.warning(
                    "Failed to list instances in compartment %s: %s (status=%d)",
                    compartment_id,
                    e.message,
                    e.status,
                )
                continue

            for inst in instances:
                self._cache_instance(inst)

        logger.info("Cached metadata for %d instances", len(self._cache))

    def get(self, instance_id: str) -> Optional[InstanceMetadata]:
        """Look up cached metadata for an instance."""
        return self._cache.get(instance_id)


def enrich_metrics(
    config: CollectorConfig,
    datapoints: List[MetricDataPoint],
) -> List[EnrichedMetricRecord]:
    """
    Enrich raw metric data points with instance metadata and
    compute derived fields.

    Groups data points by (instance_id, timestamp) to merge
    CpuUtilization and MemoryUtilization into a single record.

    Args:
        config: The collector configuration.
        datapoints: Raw metric data points from the collector.

    Returns:
        List of EnrichedMetricRecord objects.
    """
    # Build and refresh the metadata cache
    cache = InstanceMetadataCache(config)
    cache.refresh()

    # Group data points by (instance_id, timestamp) — one wide record per group.
    # Each datapoint contributes a value to the field its (metric, stat) maps to.
    grouped: Dict[tuple, Dict[str, float]] = {}
    for dp in datapoints:
        field_name = METRIC_STAT_FIELD_MAP.get((dp.metric_name, dp.statistic))
        if field_name is None:
            logger.warning(
                "No EnrichedMetricRecord field mapped for (%s, %s) — dropping datapoint",
                dp.metric_name,
                dp.statistic,
            )
            continue
        key = (dp.instance_id, dp.timestamp, dp.compartment_id)
        if key not in grouped:
            grouped[key] = {}
        grouped[key][field_name] = dp.value

    enriched: List[EnrichedMetricRecord] = []

    for (instance_id, timestamp, compartment_id), values in grouped.items():
        meta = cache.get(instance_id)
        if not meta:
            logger.warning(
                "No metadata found for instance %s — skipping enrichment "
                "(instance may have been terminated)",
                instance_id,
            )
            meta = InstanceMetadata(
                instance_id=instance_id,
                display_name="UNKNOWN",
                shape="UNKNOWN",
                lifecycle_state="UNKNOWN",
                availability_domain="UNKNOWN",
                fault_domain=None,
                compartment_id=compartment_id,
            )

        record = EnrichedMetricRecord(
            collection_time=timestamp,
            instance_id=instance_id,
            instance_name=meta.display_name,
            compartment_id=meta.compartment_id,
            availability_domain=meta.availability_domain,
            fault_domain=meta.fault_domain,
            shape=meta.shape,
            lifecycle_state=meta.lifecycle_state,
            cpu_allocated_ocpus=meta.ocpus,
            memory_allocated_gbs=meta.memory_in_gbs,
        )
        for field_name, value in values.items():
            setattr(record, field_name, value)

        # Derived usage from the mean values
        if record.cpu_utilization_pct is not None and meta.ocpus is not None:
            record.cpu_usage_ocpus = round(
                (record.cpu_utilization_pct / 100.0) * meta.ocpus, 4
            )
        if record.memory_utilization_pct is not None and meta.memory_in_gbs is not None:
            record.memory_used_gbs = round(
                (record.memory_utilization_pct / 100.0) * meta.memory_in_gbs, 4
            )

        enriched.append(record)

    logger.info("Enriched %d records from %d data points", len(enriched), len(datapoints))
    return enriched
