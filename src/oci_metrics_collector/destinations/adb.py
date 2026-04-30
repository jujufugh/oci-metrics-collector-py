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
# Columns that may be missing on tables created by older versions of this
# collector. _ensure_table() will ALTER TABLE ADD any that aren't already
# present so existing historical rows survive with NULLs in the new columns.
# ---------------------------------------------------------------------------
EXTENDED_NUMBER_COLUMNS = [
    "CPU_UTILIZATION_PCT_P99",
    "CPU_UTILIZATION_PCT_P95",
    "CPU_UTILIZATION_PCT_MAX",
    "MEMORY_UTILIZATION_PCT_P99",
    "MEMORY_UTILIZATION_PCT_P95",
    "MEMORY_UTILIZATION_PCT_MAX",
    "LOAD_AVERAGE_P95",
    "MEMORY_ALLOCATION_STALLS_P95",
]

# ---------------------------------------------------------------------------
# DDL — Auto-create the target table if it does not exist.
# ---------------------------------------------------------------------------
CREATE_TABLE_DDL = """
DECLARE
    table_exists NUMBER;
BEGIN
    SELECT COUNT(*) INTO table_exists
    FROM all_tables
    WHERE owner = :owner AND table_name = :table_name;

    IF table_exists = 0 THEN
        EXECUTE IMMEDIATE '
            CREATE TABLE {full_table_name} (
                COLLECTION_TIME              TIMESTAMP,
                INSTANCE_ID                  VARCHAR2(255),
                INSTANCE_NAME                VARCHAR2(255),
                COMPARTMENT_ID               VARCHAR2(255),
                AVAILABILITY_DOMAIN          VARCHAR2(100),
                FAULT_DOMAIN                 VARCHAR2(100),
                SHAPE                        VARCHAR2(100),
                LIFECYCLE_STATE              VARCHAR2(50),
                CPU_ALLOCATED_OCPUS          NUMBER,
                MEMORY_ALLOCATED_GBS         NUMBER,
                CPU_UTILIZATION_PCT          NUMBER,
                CPU_UTILIZATION_PCT_P99      NUMBER,
                CPU_UTILIZATION_PCT_P95      NUMBER,
                CPU_UTILIZATION_PCT_MAX      NUMBER,
                MEMORY_UTILIZATION_PCT       NUMBER,
                MEMORY_UTILIZATION_PCT_P99   NUMBER,
                MEMORY_UTILIZATION_PCT_P95   NUMBER,
                MEMORY_UTILIZATION_PCT_MAX   NUMBER,
                LOAD_AVERAGE_P95             NUMBER,
                MEMORY_ALLOCATION_STALLS_P95 NUMBER,
                CPU_USAGE_OCPUS              NUMBER,
                MEMORY_USED_GBS              NUMBER,
                STATISTIC_TYPE               VARCHAR2(20),
                CONSTRAINT pk_{short_table_name} PRIMARY KEY
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
        :collection_time                  AS collection_time,
        :instance_id                      AS instance_id,
        :instance_name                    AS instance_name,
        :compartment_id                   AS compartment_id,
        :availability_domain              AS availability_domain,
        :fault_domain                     AS fault_domain,
        :shape                            AS shape,
        :lifecycle_state                  AS lifecycle_state,
        :cpu_allocated_ocpus              AS cpu_allocated_ocpus,
        :memory_allocated_gbs             AS memory_allocated_gbs,
        :cpu_utilization_pct              AS cpu_utilization_pct,
        :cpu_utilization_pct_p99          AS cpu_utilization_pct_p99,
        :cpu_utilization_pct_p95          AS cpu_utilization_pct_p95,
        :cpu_utilization_pct_max          AS cpu_utilization_pct_max,
        :memory_utilization_pct           AS memory_utilization_pct,
        :memory_utilization_pct_p99       AS memory_utilization_pct_p99,
        :memory_utilization_pct_p95       AS memory_utilization_pct_p95,
        :memory_utilization_pct_max       AS memory_utilization_pct_max,
        :load_average_p95                 AS load_average_p95,
        :memory_allocation_stalls_p95     AS memory_allocation_stalls_p95,
        :cpu_usage_ocpus                  AS cpu_usage_ocpus,
        :memory_used_gbs                  AS memory_used_gbs,
        :statistic_type                   AS statistic_type
    FROM dual
) src
ON (
    tgt.COLLECTION_TIME = src.collection_time
    AND tgt.INSTANCE_ID = src.instance_id
    AND tgt.STATISTIC_TYPE = src.statistic_type
)
WHEN MATCHED THEN UPDATE SET
    tgt.INSTANCE_NAME                = src.instance_name,
    tgt.COMPARTMENT_ID               = src.compartment_id,
    tgt.AVAILABILITY_DOMAIN          = src.availability_domain,
    tgt.FAULT_DOMAIN                 = src.fault_domain,
    tgt.SHAPE                        = src.shape,
    tgt.LIFECYCLE_STATE              = src.lifecycle_state,
    tgt.CPU_ALLOCATED_OCPUS          = src.cpu_allocated_ocpus,
    tgt.MEMORY_ALLOCATED_GBS         = src.memory_allocated_gbs,
    tgt.CPU_UTILIZATION_PCT          = src.cpu_utilization_pct,
    tgt.CPU_UTILIZATION_PCT_P99      = src.cpu_utilization_pct_p99,
    tgt.CPU_UTILIZATION_PCT_P95      = src.cpu_utilization_pct_p95,
    tgt.CPU_UTILIZATION_PCT_MAX      = src.cpu_utilization_pct_max,
    tgt.MEMORY_UTILIZATION_PCT       = src.memory_utilization_pct,
    tgt.MEMORY_UTILIZATION_PCT_P99   = src.memory_utilization_pct_p99,
    tgt.MEMORY_UTILIZATION_PCT_P95   = src.memory_utilization_pct_p95,
    tgt.MEMORY_UTILIZATION_PCT_MAX   = src.memory_utilization_pct_max,
    tgt.LOAD_AVERAGE_P95             = src.load_average_p95,
    tgt.MEMORY_ALLOCATION_STALLS_P95 = src.memory_allocation_stalls_p95,
    tgt.CPU_USAGE_OCPUS              = src.cpu_usage_ocpus,
    tgt.MEMORY_USED_GBS              = src.memory_used_gbs
WHEN NOT MATCHED THEN INSERT (
    COLLECTION_TIME, INSTANCE_ID, INSTANCE_NAME,
    COMPARTMENT_ID, AVAILABILITY_DOMAIN, FAULT_DOMAIN,
    SHAPE, LIFECYCLE_STATE,
    CPU_ALLOCATED_OCPUS, MEMORY_ALLOCATED_GBS,
    CPU_UTILIZATION_PCT, CPU_UTILIZATION_PCT_P99,
    CPU_UTILIZATION_PCT_P95, CPU_UTILIZATION_PCT_MAX,
    MEMORY_UTILIZATION_PCT, MEMORY_UTILIZATION_PCT_P99,
    MEMORY_UTILIZATION_PCT_P95, MEMORY_UTILIZATION_PCT_MAX,
    LOAD_AVERAGE_P95, MEMORY_ALLOCATION_STALLS_P95,
    CPU_USAGE_OCPUS, MEMORY_USED_GBS,
    STATISTIC_TYPE
) VALUES (
    src.collection_time, src.instance_id, src.instance_name,
    src.compartment_id, src.availability_domain, src.fault_domain,
    src.shape, src.lifecycle_state,
    src.cpu_allocated_ocpus, src.memory_allocated_gbs,
    src.cpu_utilization_pct, src.cpu_utilization_pct_p99,
    src.cpu_utilization_pct_p95, src.cpu_utilization_pct_max,
    src.memory_utilization_pct, src.memory_utilization_pct_p99,
    src.memory_utilization_pct_p95, src.memory_utilization_pct_max,
    src.load_average_p95, src.memory_allocation_stalls_p95,
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

    def _parse_table_name(self) -> tuple:
        """Parse schema.table_name into (owner, table_name, full_name)."""
        table_name = self._config.adb.table_name
        if "." in table_name:
            owner, short_name = table_name.split(".", 1)
            return owner.upper(), short_name.upper(), table_name
        return self._config.adb.user.upper(), table_name.upper(), table_name

    def _ensure_table(self, connection) -> None:
        """Create the metrics table if missing, and add any new columns to existing tables."""
        if self._table_ensured:
            return

        owner, short_name, full_name = self._parse_table_name()
        ddl = CREATE_TABLE_DDL.format(
            full_table_name=full_name,
            short_table_name=short_name,
        )

        try:
            with connection.cursor() as cursor:
                cursor.execute(ddl, {"owner": owner, "table_name": short_name})
                self._migrate_columns(cursor, owner, short_name, full_name)
            logger.info("Ensured table exists: %s", full_name)
            self._table_ensured = True
        except oracledb.DatabaseError as e:
            logger.error("Failed to ensure table %s: %s", full_name, e)
            raise

    def _migrate_columns(self, cursor, owner: str, short_name: str, full_name: str) -> None:
        """ALTER TABLE ADD any extended NUMBER columns missing from an existing table."""
        cursor.execute(
            """
            SELECT column_name FROM all_tab_columns
            WHERE owner = :owner AND table_name = :table_name
            """,
            {"owner": owner, "table_name": short_name},
        )
        existing = {row[0] for row in cursor.fetchall()}
        missing = [c for c in EXTENDED_NUMBER_COLUMNS if c not in existing]
        if not missing:
            return
        ddl = f"ALTER TABLE {full_name} ADD (" + ", ".join(f"{c} NUMBER" for c in missing) + ")"
        cursor.execute(ddl)
        logger.info("Added %d new column(s) to %s: %s", len(missing), full_name, missing)

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
            "cpu_utilization_pct_p99": record.cpu_utilization_pct_p99,
            "cpu_utilization_pct_p95": record.cpu_utilization_pct_p95,
            "cpu_utilization_pct_max": record.cpu_utilization_pct_max,
            "memory_utilization_pct": record.memory_utilization_pct,
            "memory_utilization_pct_p99": record.memory_utilization_pct_p99,
            "memory_utilization_pct_p95": record.memory_utilization_pct_p95,
            "memory_utilization_pct_max": record.memory_utilization_pct_max,
            "load_average_p95": record.load_average_p95,
            "memory_allocation_stalls_p95": record.memory_allocation_stalls_p95,
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
                cursor.execute("ALTER SESSION DISABLE PARALLEL DML")
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
