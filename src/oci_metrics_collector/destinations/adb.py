"""
Autonomous Database destination writer.

Writes enriched metric records to an Oracle Autonomous Database
using python-oracledb (Thin mode with mTLS wallet).
"""

import logging
from typing import List

import oracledb

from ..config import CollectorConfig
from ..enricher import EnrichedMetricRecord

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DDL — Auto-create the target table if it does not exist.
# ---------------------------------------------------------------------------
CREATE_TABLE_DDL = """
DECLARE
    table_exists NUMBER;
BEGIN
    SELECT COUNT(*) INTO table_exists
    FROM user_tables
    WHERE table_name = :table_name;

    IF table_exists = 0 THEN
        EXECUTE IMMEDIATE '
            CREATE TABLE {table_name} (
                COLLECTION_TIME        TIMESTAMP WITH TIME ZONE,
                INSTANCE_ID            VARCHAR2(255),
                INSTANCE_NAME          VARCHAR2(255),
                COMPARTMENT_ID         VARCHAR2(255),
                AVAILABILITY_DOMAIN    VARCHAR2(100),
                FAULT_DOMAIN           VARCHAR2(100),
                SHAPE                  VARCHAR2(100),
                LIFECYCLE_STATE        VARCHAR2(50),
                CPU_ALLOCATED_OCPUS    NUMBER,
                MEMORY_ALLOCATED_GBS   NUMBER,
                CPU_UTILIZATION_PCT    NUMBER,
                MEMORY_UTILIZATION_PCT NUMBER,
                CPU_USAGE_OCPUS        NUMBER,
                MEMORY_USED_GBS        NUMBER,
                STATISTIC_TYPE         VARCHAR2(20),
                CONSTRAINT pk_{table_name} PRIMARY KEY
                    (COLLECTION_TIME, INSTANCE_ID, STATISTIC_TYPE)
            )
        ';
    END IF;
END;
"""

# ---------------------------------------------------------------------------
# MERGE (upsert) statement to handle duplicate key conflicts gracefully.
# ---------------------------------------------------------------------------
MERGE_SQL = """
MERGE INTO {table_name} tgt
USING (
    SELECT
        :collection_time       AS collection_time,
        :instance_id           AS instance_id,
        :instance_name         AS instance_name,
        :compartment_id        AS compartment_id,
        :availability_domain   AS availability_domain,
        :fault_domain          AS fault_domain,
        :shape                 AS shape,
        :lifecycle_state       AS lifecycle_state,
        :cpu_allocated_ocpus   AS cpu_allocated_ocpus,
        :memory_allocated_gbs  AS memory_allocated_gbs,
        :cpu_utilization_pct   AS cpu_utilization_pct,
        :memory_utilization_pct AS memory_utilization_pct,
        :cpu_usage_ocpus       AS cpu_usage_ocpus,
        :memory_used_gbs       AS memory_used_gbs,
        :statistic_type        AS statistic_type
    FROM dual
) src
ON (
    tgt.COLLECTION_TIME = src.collection_time
    AND tgt.INSTANCE_ID = src.instance_id
    AND tgt.STATISTIC_TYPE = src.statistic_type
)
WHEN MATCHED THEN UPDATE SET
    tgt.INSTANCE_NAME          = src.instance_name,
    tgt.COMPARTMENT_ID         = src.compartment_id,
    tgt.AVAILABILITY_DOMAIN    = src.availability_domain,
    tgt.FAULT_DOMAIN           = src.fault_domain,
    tgt.SHAPE                  = src.shape,
    tgt.LIFECYCLE_STATE        = src.lifecycle_state,
    tgt.CPU_ALLOCATED_OCPUS    = src.cpu_allocated_ocpus,
    tgt.MEMORY_ALLOCATED_GBS   = src.memory_allocated_gbs,
    tgt.CPU_UTILIZATION_PCT    = src.cpu_utilization_pct,
    tgt.MEMORY_UTILIZATION_PCT = src.memory_utilization_pct,
    tgt.CPU_USAGE_OCPUS        = src.cpu_usage_ocpus,
    tgt.MEMORY_USED_GBS        = src.memory_used_gbs
WHEN NOT MATCHED THEN INSERT (
    COLLECTION_TIME, INSTANCE_ID, INSTANCE_NAME,
    COMPARTMENT_ID, AVAILABILITY_DOMAIN, FAULT_DOMAIN,
    SHAPE, LIFECYCLE_STATE,
    CPU_ALLOCATED_OCPUS, MEMORY_ALLOCATED_GBS,
    CPU_UTILIZATION_PCT, MEMORY_UTILIZATION_PCT,
    CPU_USAGE_OCPUS, MEMORY_USED_GBS,
    STATISTIC_TYPE
) VALUES (
    src.collection_time, src.instance_id, src.instance_name,
    src.compartment_id, src.availability_domain, src.fault_domain,
    src.shape, src.lifecycle_state,
    src.cpu_allocated_ocpus, src.memory_allocated_gbs,
    src.cpu_utilization_pct, src.memory_utilization_pct,
    src.cpu_usage_ocpus, src.memory_used_gbs,
    src.statistic_type
)
"""


class AdbWriter:
    """
    Writes enriched metric records to Oracle Autonomous Database.

    Uses python-oracledb Thin mode with mTLS wallet for connectivity.
    Automatically creates the target table on first use.
    """

    def __init__(self, config: CollectorConfig):
        self._config = config
        self._pool = None
        self._table_ensured = False

    def _get_pool(self):
        """Create or return an existing connection pool."""
        if self._pool is None:
            adb = self._config.adb
            logger.info(
                "Creating ADB connection pool (dsn=%s, user=%s)",
                adb.dsn,
                adb.user,
            )
            self._pool = oracledb.create_pool(
                user=adb.user,
                password=adb.password,
                dsn=adb.dsn,
                config_dir=adb.wallet_dir,
                wallet_location=adb.wallet_dir,
                wallet_password=adb.wallet_password,
                min=1,
                max=5,
                increment=1,
            )
        return self._pool

    def _ensure_table(self, connection) -> None:
        """Create the metrics table if it does not exist."""
        if self._table_ensured:
            return

        table_name = self._config.adb.table_name
        ddl = CREATE_TABLE_DDL.format(table_name=table_name)

        try:
            with connection.cursor() as cursor:
                cursor.execute(ddl, {"table_name": table_name})
            logger.info("Ensured table exists: %s", table_name)
            self._table_ensured = True
        except oracledb.DatabaseError as e:
            logger.error("Failed to ensure table %s: %s", table_name, e)
            raise

    def _record_to_row(self, record: EnrichedMetricRecord) -> dict:
        """Convert an EnrichedMetricRecord to a dict for bind variables."""
        return {
            "collection_time": record.collection_time,
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

    def write(self, records: List[EnrichedMetricRecord]) -> int:
        """
        Write enriched metric records to ADB.

        Uses MERGE (upsert) to handle duplicate timestamps gracefully.

        Args:
            records: List of enriched metric records to write.

        Returns:
            Number of records written.
        """
        if not records:
            logger.info("No records to write to ADB")
            return 0

        pool = self._get_pool()
        table_name = self._config.adb.table_name
        merge_sql = MERGE_SQL.format(table_name=table_name)

        with pool.acquire() as connection:
            self._ensure_table(connection)

            rows = [self._record_to_row(r) for r in records]

            with connection.cursor() as cursor:
                cursor.executemany(merge_sql, rows)

            connection.commit()

        logger.info(
            "Wrote %d records to ADB table %s",
            len(records),
            table_name,
        )
        return len(records)

    def test_connection(self) -> bool:
        """Test connectivity to the Autonomous Database."""
        try:
            pool = self._get_pool()
            with pool.acquire() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1 FROM DUAL")
                    result = cursor.fetchone()
                    logger.info("ADB connection test: OK (result=%s)", result)
                    return True
        except Exception as e:
            logger.error("ADB connection test FAILED: %s", e)
            return False

    def close(self) -> None:
        """Close the connection pool."""
        if self._pool:
            self._pool.close(force=True)
            self._pool = None
            logger.info("ADB connection pool closed")
