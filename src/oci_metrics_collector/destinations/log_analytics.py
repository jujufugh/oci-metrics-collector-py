"""
OCI Log Analytics destination writer.

Ships enriched metric records to OCI Log Analytics using the
upload_log_events_file API. Records are formatted as JSON events
grouped by instance for clean entity correlation.
"""

import io
import json
import logging
from typing import Dict, List, Optional

import oci

from ..collector import _build_oci_config
from ..config import CollectorConfig
from ..enricher import EnrichedMetricRecord

logger = logging.getLogger(__name__)

# Maximum payload size for upload_log_events_file (2 MB)
MAX_PAYLOAD_BYTES = 2 * 1024 * 1024


class LogAnalyticsWriter:
    """
    Writes enriched metric records to OCI Log Analytics.

    Uses upload_log_events_file() to push metrics as structured JSON
    log events, grouped by compute instance.
    """

    def __init__(self, config: CollectorConfig):
        self._config = config
        self._client = self._create_client()

    def _create_client(self):
        """Create an OCI LogAnalyticsClient with the appropriate auth."""
        oci_config, signer = _build_oci_config(self._config)
        if signer:
            return oci.log_analytics.LogAnalyticsClient(
                config={}, signer=signer
            )
        return oci.log_analytics.LogAnalyticsClient(oci_config)

    def _record_to_log_line(self, record: EnrichedMetricRecord) -> str:
        """
        Convert an EnrichedMetricRecord to a JSON string for a logRecord entry.
        """
        data = {
            "collection_time": record.collection_time.isoformat()
            if record.collection_time
            else None,
            "instance_id": record.instance_id,
            "instance_name": record.instance_name,
            "compartment_id": record.compartment_id,
            "availability_domain": record.availability_domain,
            "fault_domain": record.fault_domain,
            "shape": record.shape,
            "lifecycle_state": record.lifecycle_state,
            "cpu_allocated_ocpus": record.cpu_allocated_ocpus,
            "memory_allocated_gbs": record.memory_allocated_gbs,
            "cpu_utilization_pct": record.cpu_utilization_pct,
            "memory_utilization_pct": record.memory_utilization_pct,
            "cpu_usage_ocpus": record.cpu_usage_ocpus,
            "memory_used_gbs": record.memory_used_gbs,
            "statistic_type": record.statistic_type,
        }
        return json.dumps(data)

    def _build_payload(
        self, records: List[EnrichedMetricRecord]
    ) -> dict:
        """
        Build the JSON payload for upload_log_events_file.

        Groups records by instance_id for clean entity correlation.
        If entity_mapping is configured, maps instance OCIDs to
        Log Analytics entity OCIDs.
        """
        la_config = self._config.log_analytics
        entity_mapping = la_config.entity_mapping or {}

        # Group records by instance
        by_instance: Dict[str, List[EnrichedMetricRecord]] = {}
        for record in records:
            by_instance.setdefault(record.instance_id, []).append(record)

        log_events = []
        for instance_id, instance_records in by_instance.items():
            log_records = [
                self._record_to_log_line(r) for r in instance_records
            ]

            event: Dict = {
                "logSourceName": la_config.log_source_name,
                "logRecords": log_records,
            }

            # Map to Log Analytics entity if configured
            entity_id = entity_mapping.get(instance_id)
            if entity_id:
                event["entityId"] = entity_id

            log_events.append(event)

        payload = {
            "metadata": {
                "collector": "oci-metrics-collector-py",
                "version": "1.0.0",
            },
            "logEvents": log_events,
        }

        return payload

    def _upload_payload(self, payload: dict) -> bool:
        """Upload a single JSON payload to Log Analytics."""
        la_config = self._config.log_analytics

        payload_json = json.dumps(payload)
        payload_bytes = payload_json.encode("utf-8")

        if len(payload_bytes) > MAX_PAYLOAD_BYTES:
            logger.warning(
                "Payload size (%d bytes) exceeds 2MB limit — "
                "splitting is required",
                len(payload_bytes),
            )
            return self._upload_chunked(payload)

        payload_stream = io.BytesIO(payload_bytes)

        try:
            response = self._client.upload_log_events_file(
                namespace_name=la_config.namespace,
                log_group_id=la_config.log_group_id,
                upload_log_events_file_body=payload_stream,
                payload_type="JSON",
            )
            logger.info(
                "Log Analytics upload: status=%d, opc-request-id=%s",
                response.status,
                response.headers.get("opc-request-id", "N/A"),
            )
            return True
        except oci.exceptions.ServiceError as e:
            logger.error(
                "Log Analytics upload failed: %s (status=%d, code=%s)",
                e.message,
                e.status,
                e.code,
            )
            return False

    def _upload_chunked(self, payload: dict) -> bool:
        """
        Split a large payload into smaller chunks that fit within
        the 2MB limit, and upload each chunk separately.
        """
        log_events = payload.get("logEvents", [])
        metadata = payload.get("metadata", {})

        success = True
        chunk: List[dict] = []
        chunk_size = 0
        overhead = len(json.dumps({"metadata": metadata, "logEvents": []}).encode())

        for event in log_events:
            event_size = len(json.dumps(event).encode("utf-8"))

            # If adding this event would exceed the limit, flush current chunk
            if chunk and (chunk_size + event_size + overhead > MAX_PAYLOAD_BYTES):
                chunk_payload = {
                    "metadata": metadata,
                    "logEvents": chunk,
                }
                if not self._upload_payload(chunk_payload):
                    success = False
                chunk = []
                chunk_size = 0

            chunk.append(event)
            chunk_size += event_size

        # Flush remaining
        if chunk:
            chunk_payload = {
                "metadata": metadata,
                "logEvents": chunk,
            }
            payload_json = json.dumps(chunk_payload)
            payload_stream = io.BytesIO(payload_json.encode("utf-8"))

            try:
                la_config = self._config.log_analytics
                response = self._client.upload_log_events_file(
                    namespace_name=la_config.namespace,
                    log_group_id=la_config.log_group_id,
                    upload_log_events_file_body=payload_stream,
                    payload_type="JSON",
                )
                logger.info(
                    "Log Analytics chunk upload: status=%d",
                    response.status,
                )
            except oci.exceptions.ServiceError as e:
                logger.error(
                    "Log Analytics chunk upload failed: %s (status=%d)",
                    e.message,
                    e.status,
                )
                success = False

        return success

    def write(self, records: List[EnrichedMetricRecord]) -> int:
        """
        Write enriched metric records to OCI Log Analytics.

        Args:
            records: List of enriched metric records to ship.

        Returns:
            Number of records written (0 if upload failed).
        """
        if not records:
            logger.info("No records to upload to Log Analytics")
            return 0

        payload = self._build_payload(records)

        if self._upload_payload(payload):
            logger.info("Uploaded %d records to Log Analytics", len(records))
            return len(records)
        return 0

    def test_connection(self) -> bool:
        """
        Test connectivity to OCI Log Analytics by querying the namespace.
        """
        try:
            la_config = self._config.log_analytics
            response = self._client.get_namespace(
                namespace_name=la_config.namespace,
            )
            logger.info(
                "Log Analytics connection test: OK (namespace=%s)",
                response.data.namespace_name,
            )
            return True
        except oci.exceptions.ServiceError as e:
            logger.error(
                "Log Analytics connection test FAILED: %s (status=%d)",
                e.message,
                e.status,
            )
            return False
