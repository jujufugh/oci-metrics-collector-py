"""
Main orchestration pipeline.

Coordinates the Collect → Enrich → Ship workflow with error handling,
retries, and structured logging.
"""

import logging
import signal
import sys
import time
from typing import Optional

from .collector import collect_metrics
from .config import CollectorConfig
from .destinations.adb import AdbWriter
from .destinations.log_analytics import LogAnalyticsWriter
from .enricher import enrich_metrics

logger = logging.getLogger(__name__)


class MetricsOrchestrator:
    """
    Orchestrates the metrics collection pipeline:
    1. Collect raw metrics from OCI Monitoring
    2. Enrich with instance metadata + derived fields
    3. Ship to enabled destinations (ADB, Log Analytics)
    """

    def __init__(self, config: CollectorConfig):
        self._config = config
        self._adb_writer: Optional[AdbWriter] = None
        self._la_writer: Optional[LogAnalyticsWriter] = None
        self._running = True

        # Initialize destination writers based on config
        if config.adb.enabled:
            self._adb_writer = AdbWriter(config)
        if config.log_analytics.enabled:
            self._la_writer = LogAnalyticsWriter(config)

    def _setup_signal_handlers(self) -> None:
        """Register signal handlers for graceful shutdown."""

        def _handler(signum, frame):
            sig_name = signal.Signals(signum).name
            logger.info("Received %s — shutting down gracefully...", sig_name)
            self._running = False

        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)

    def run_once(self) -> dict:
        """
        Run a single collection cycle.

        Returns:
            Summary dict with counts of records collected, enriched,
            and written to each destination.
        """
        summary = {
            "datapoints_collected": 0,
            "records_enriched": 0,
            "adb_records_written": 0,
            "la_records_written": 0,
            "errors": [],
        }

        # ---------------------------------------------------------------
        # Step 1: Collect metrics
        # ---------------------------------------------------------------
        logger.info("=" * 60)
        logger.info("STEP 1: Collecting metrics from OCI Monitoring")
        logger.info("=" * 60)

        try:
            datapoints = collect_metrics(self._config)
            summary["datapoints_collected"] = len(datapoints)
        except Exception as e:
            msg = f"Metric collection failed: {e}"
            logger.error(msg)
            summary["errors"].append(msg)
            return summary

        if not datapoints:
            logger.warning("No data points collected — nothing to process")
            return summary

        # ---------------------------------------------------------------
        # Step 2: Enrich with instance metadata
        # ---------------------------------------------------------------
        logger.info("=" * 60)
        logger.info("STEP 2: Enriching metrics with instance metadata")
        logger.info("=" * 60)

        try:
            enriched = enrich_metrics(self._config, datapoints)
            summary["records_enriched"] = len(enriched)
        except Exception as e:
            msg = f"Metric enrichment failed: {e}"
            logger.error(msg)
            summary["errors"].append(msg)
            return summary

        if not enriched:
            logger.warning("No enriched records — nothing to ship")
            return summary

        # ---------------------------------------------------------------
        # Step 3a: Ship to Autonomous Database
        # ---------------------------------------------------------------
        if self._adb_writer:
            logger.info("=" * 60)
            logger.info("STEP 3a: Writing to Autonomous Database")
            logger.info("=" * 60)

            retries = 0
            while retries <= self._config.orchestration.max_retries:
                try:
                    count = self._adb_writer.write(enriched)
                    summary["adb_records_written"] = count
                    break
                except Exception as e:
                    retries += 1
                    if retries > self._config.orchestration.max_retries:
                        msg = f"ADB write failed after {retries} retries: {e}"
                        logger.error(msg)
                        summary["errors"].append(msg)
                    else:
                        wait = 2**retries
                        logger.warning(
                            "ADB write failed (attempt %d/%d): %s — "
                            "retrying in %ds",
                            retries,
                            self._config.orchestration.max_retries,
                            e,
                            wait,
                        )
                        time.sleep(wait)

        # ---------------------------------------------------------------
        # Step 3b: Ship to Log Analytics
        # ---------------------------------------------------------------
        if self._la_writer:
            logger.info("=" * 60)
            logger.info("STEP 3b: Uploading to OCI Log Analytics")
            logger.info("=" * 60)

            retries = 0
            while retries <= self._config.orchestration.max_retries:
                try:
                    count = self._la_writer.write(enriched)
                    summary["la_records_written"] = count
                    break
                except Exception as e:
                    retries += 1
                    if retries > self._config.orchestration.max_retries:
                        msg = (
                            f"Log Analytics upload failed after "
                            f"{retries} retries: {e}"
                        )
                        logger.error(msg)
                        summary["errors"].append(msg)
                    else:
                        wait = 2**retries
                        logger.warning(
                            "LA upload failed (attempt %d/%d): %s — "
                            "retrying in %ds",
                            retries,
                            self._config.orchestration.max_retries,
                            e,
                            wait,
                        )
                        time.sleep(wait)

        # ---------------------------------------------------------------
        # Summary
        # ---------------------------------------------------------------
        logger.info("=" * 60)
        logger.info("COLLECTION CYCLE COMPLETE")
        logger.info("  Data points collected:    %d", summary["datapoints_collected"])
        logger.info("  Records enriched:         %d", summary["records_enriched"])
        logger.info("  ADB records written:      %d", summary["adb_records_written"])
        logger.info("  LA records uploaded:       %d", summary["la_records_written"])
        if summary["errors"]:
            logger.warning("  Errors: %d", len(summary["errors"]))
            for err in summary["errors"]:
                logger.warning("    - %s", err)
        logger.info("=" * 60)

        return summary

    def run_continuous(self) -> None:
        """
        Run the collection pipeline in a continuous loop.

        Collects metrics at the configured interval and handles
        graceful shutdown on SIGINT/SIGTERM.
        """
        self._setup_signal_handlers()
        interval = self._config.orchestration.interval_seconds

        logger.info(
            "Starting continuous collection (interval=%ds). "
            "Press Ctrl+C to stop.",
            interval,
        )

        cycle = 0
        while self._running:
            cycle += 1
            logger.info("--- Collection cycle #%d ---", cycle)

            try:
                self.run_once()
            except Exception as e:
                logger.error("Unhandled error in cycle #%d: %s", cycle, e)

            if self._running:
                logger.info("Sleeping %ds until next cycle...", interval)
                # Use small sleep increments so we can respond to signals
                for _ in range(interval):
                    if not self._running:
                        break
                    time.sleep(1)

        logger.info("Continuous collection stopped after %d cycles", cycle)
        self.close()

    def test_connections(self) -> dict:
        """
        Test connectivity to all enabled destinations.

        Returns:
            Dict with destination names as keys and bool status as values.
        """
        results = {}

        if self._adb_writer:
            results["adb"] = self._adb_writer.test_connection()
        else:
            results["adb"] = None  # Disabled

        if self._la_writer:
            results["log_analytics"] = self._la_writer.test_connection()
        else:
            results["log_analytics"] = None  # Disabled

        return results

    def close(self) -> None:
        """Clean up resources."""
        if self._adb_writer:
            self._adb_writer.close()
        logger.info("Orchestrator resources cleaned up")
