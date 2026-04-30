"""
OCI Monitoring API metrics collector.

Fetches compute instance metrics (CpuUtilization, MemoryUtilization, etc.)
from the oci_computeagent namespace using MonitoringClient.summarize_metrics_data().
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List

import oci

from .config import CollectorConfig

logger = logging.getLogger(__name__)


@dataclass
class MetricDataPoint:
    """A single metric data point from OCI Monitoring."""
    instance_id: str
    metric_name: str
    timestamp: datetime
    value: float
    statistic: str
    compartment_id: str


def _stat_to_mql(statistic: str) -> str:
    """Map a config statistic name to an OCI MQL aggregation function."""
    s = statistic.lower()
    if s in ("mean", "max", "min", "sum", "count", "rate"):
        return f"{s}()"
    if s.startswith("p") and s[1:].isdigit():
        # p99 → percentile(.99), p95 → percentile(.95), p90 → percentile(.90)
        return f"percentile(.{s[1:]})"
    raise ValueError(f"Unsupported statistic: {statistic!r}")


def _build_oci_config(config: CollectorConfig):
    """Build OCI SDK config and signer based on auth method."""
    if config.oci.auth_method == "instance_principal":
        signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        return {}, signer
    else:
        oci_config = oci.config.from_file(
            file_location=config.oci.config_file,
            profile_name=config.oci.profile,
        )
        oci.config.validate_config(oci_config)
        return oci_config, None


def _create_monitoring_client(config: CollectorConfig):
    """Create an OCI MonitoringClient with the appropriate auth."""
    oci_config, signer = _build_oci_config(config)
    if signer:
        return oci.monitoring.MonitoringClient(
            config={}, signer=signer
        )
    return oci.monitoring.MonitoringClient(oci_config)


def collect_metrics(config: CollectorConfig) -> List[MetricDataPoint]:
    """
    Collect compute metrics from OCI Monitoring for all instances
    in the configured compartment.

    Queries each configured metric (e.g., CpuUtilization, MemoryUtilization)
    using MQL and returns normalized MetricDataPoint objects.

    Args:
        config: The collector configuration.

    Returns:
        List of MetricDataPoint objects, one per instance per metric per timestamp.
    """
    client = _create_monitoring_client(config)
    compartment_id = config.scope.compartment_id
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(minutes=config.metrics.lookback_minutes)

    all_datapoints: List[MetricDataPoint] = []

    for metric_name, stats in config.metrics.metric_stats.items():
        for stat in stats:
            logger.info(
                "Collecting metric: %s (namespace=%s, resolution=%s, stat=%s)",
                metric_name,
                config.metrics.namespace,
                config.metrics.resolution,
                stat,
            )

            query = f"{metric_name}[{config.metrics.resolution}].{_stat_to_mql(stat)}"

            summarize_details = oci.monitoring.models.SummarizeMetricsDataDetails(
                namespace=config.metrics.namespace,
                query=query,
                start_time=start_time.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                end_time=end_time.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                resolution=config.metrics.resolution,
            )

            try:
                response = client.summarize_metrics_data(
                    compartment_id=compartment_id,
                    summarize_metrics_data_details=summarize_details,
                )
            except oci.exceptions.ServiceError as e:
                logger.error(
                    "Failed to collect metric %s.%s: %s (status=%d)",
                    metric_name,
                    stat,
                    e.message,
                    e.status,
                )
                continue

            count_before = len(all_datapoints)
            for metric_data in response.data:
                dimensions = metric_data.dimensions or {}
                instance_id = dimensions.get("resourceId", "unknown")

                for dp in metric_data.aggregated_datapoints:
                    all_datapoints.append(
                        MetricDataPoint(
                            instance_id=instance_id,
                            metric_name=metric_name,
                            timestamp=dp.timestamp,
                            value=dp.value,
                            statistic=stat,
                            compartment_id=compartment_id,
                        )
                    )

            logger.info(
                "Collected %d data points for %s.%s",
                len(all_datapoints) - count_before,
                metric_name,
                stat,
            )

    logger.info("Total data points collected: %d", len(all_datapoints))
    return all_datapoints
