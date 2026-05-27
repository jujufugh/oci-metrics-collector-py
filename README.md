# OCI Metrics Collector

A Python library for collecting OCI compute instance metrics (CPU utilization, memory utilization, allocated resources) and shipping them to **Autonomous Database** (for ML/prediction modeling) and **OCI Log Analytics** (for operational visibility).

Designed as a workaround for environments where **OCI Stack Monitoring is unavailable**.

## Architecture

```
┌──────────────────────┐
│   OCI Monitoring     │
│  (oci_computeagent)  │
│  - CpuUtilization    │
│  - MemoryUtilization │
└──────────┬───────────┘
           │ summarize_metrics_data()
           ▼
┌──────────────────────┐     ┌──────────────────────┐
│    Collector          │────▶│     Enricher          │
│ (collector.py)        │     │  (enricher.py)        │
│                       │     │  + Instance metadata  │
│ Raw metric datapoints │     │  + Shape config       │
│                       │     │  + Derived fields     │
└───────────────────────┘     └──────────┬────────────┘
                                         │
                              ┌──────────┴────────────┐
                              ▼                        ▼
                 ┌──────────────────────┐ ┌──────────────────────┐
                 │  Autonomous Database │ │  OCI Log Analytics   │
                 │  (destinations/adb)  │ │  (destinations/la)   │
                 │                      │ │                      │
                 │  OCI_COMPUTE_METRICS │ │  upload_log_events   │
                 │  table for ML/AI     │ │  for dashboards      │
                 └──────────────────────┘ └──────────────────────┘
```

## Metrics Collected

The collector issues one MQL query per `(metric, statistic)` pair listed in `config.metrics.metric_stats` and merges the results into a single wide row per `(instance, timestamp)`.

| Metric | Statistics | Description |
|---|---|---|
| `CpuUtilization` | mean, p99, p95, max | CPU activity as % of total time |
| `MemoryUtilization` | mean, p99, p95, max | Used memory as % of total |
| `LoadAverage` | p95 | OS-level load average |
| `MemoryAllocationStalls` | p95 | Memory allocation stalls per second |

Plus the following per-instance fields enriched from the Compute API:

| Field | Source | Description |
|---|---|---|
| `cpu_allocated_ocpus` | Compute API | OCPUs allocated to the instance |
| `memory_allocated_gbs` | Compute API | Memory (GB) allocated to the instance |
| `cpu_usage_ocpus` | Derived | `(CpuUtilization mean / 100) × cpu_allocated_ocpus` |
| `memory_used_gbs` | Derived | `(MemoryUtilization mean / 100) × memory_allocated_gbs` |

To add or change `(metric, statistic)` pairs, update **both** `config.yaml` (`metrics.metric_stats`) **and** `enricher.METRIC_STAT_FIELD_MAP` (which maps the pair to a record field / DB column).

## Prerequisites

### 1. OCI Configuration

- An OCI config file (`~/.oci/config`) with valid credentials, **OR**
- Instance Principal auth (for running on OCI compute)

### 2. IAM Policies

For a user / group:

```
Allow group <your-group> to read metrics in compartment <compartment-name>
Allow group <your-group> to read instances in compartment <compartment-name>
Allow group <your-group> to use log-analytics-log-group in compartment <compartment-name>
```

For tenancy-wide collection, grant the principal tenancy-level access and set
`scope.compartment_id` to the tenancy OCID with
`scope.compartment_id_in_subtree: true`:

```
Allow group <your-group> to read metrics in tenancy
Allow group <your-group> to read instances in tenancy
Allow group <your-group> to inspect compartments in tenancy
Allow group <your-group> to use log-analytics-log-group in tenancy
```

For **instance principal** auth (running on an OCI compute instance), create a Dynamic Group that matches the instance, then grant it the same permissions:

```
# Dynamic Group matching rule (pick one)
instance.id = 'ocid1.instance.oc1..YOUR_INSTANCE_OCID'
# or: instance.compartment.id = 'ocid1.compartment.oc1..YOUR_COMPARTMENT_OCID'

# Policies
Allow dynamic-group <dg-name> to read metrics   in compartment <compartment-name>
Allow dynamic-group <dg-name> to read instances in compartment <compartment-name>
# Only if Log Analytics destination is enabled:
Allow dynamic-group <dg-name> to use log-analytics-log-group in compartment <compartment-name>
```

### 3. Autonomous Database (if enabled)

- An ADB instance with a wallet downloaded
- A database user with `CREATE TABLE` and `INSERT` privileges
- Wallet files unzipped to a local directory

### 4. Log Analytics (if enabled)

- Log Analytics service enabled in your tenancy
- A **Log Group** created
- A **custom Log Source** named `OCI Compute Metrics`:
  - Parser type: **JSON**
  - Entity type: **OCI Compute Instance** (optional but recommended)
- Optionally, **Log Analytics Entities** created for your compute instances

## Installation

```bash
# Clone the repository
git clone <repo-url>
cd oci-metrics-collector-py

# Install in development mode
pip install -e ".[dev]"
```

## Configuration

```bash
# Copy the example config
cp config.yaml.example config.yaml

# Edit with your values
vi config.yaml
```

### Environment Variables (for secrets)

```bash
export OCI_METRICS_ADB_PASSWORD="your_db_password"
export OCI_METRICS_ADB_WALLET_PASSWORD="your_wallet_password"
export OCI_METRICS_COMPARTMENT_ID="ocid1.compartment.oc1..xxx"
export OCI_METRICS_COMPARTMENT_ID_IN_SUBTREE="false"
```

### Tenancy-Wide Collection

By default, the collector is scoped to exactly one compartment. To collect
metrics across a tenancy, root the scope at the tenancy OCID and enable subtree
queries:

```yaml
scope:
  compartment_id: "ocid1.tenancy.oc1..xxxx"
  compartment_id_in_subtree: true
```

In this mode, Monitoring requests use `compartment_id_in_subtree=True`.
Enrichment separately enumerates active accessible compartments and lists
compute instances in each compartment so metric rows can still be joined to
instance metadata and the source compartment.

## Usage

### One-Shot Collection

```bash
# Collect metrics once and ship to all enabled destinations
oci-metrics-collector collect --config config.yaml
```

### Continuous Collection (Daemon Mode)

```bash
# Collect every 5 minutes
oci-metrics-collector collect --config config.yaml --continuous

# Override interval to 60 seconds
oci-metrics-collector collect --config config.yaml --continuous --interval 60
```

### Test Connectivity

```bash
# Verify ADB and Log Analytics connectivity
oci-metrics-collector test-connection --config config.yaml
```

### Discover Available Metrics

```bash
# List all metrics in the oci_computeagent namespace
oci-metrics-collector discover --config config.yaml

# List all metrics across all namespaces
oci-metrics-collector discover --config config.yaml --namespace ""
```

## ADB Table Schema

The collector auto-creates this table on first run:

```sql
CREATE TABLE OCI_COMPUTE_METRICS (
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
    CPU_UTILIZATION_PCT          NUMBER,   -- mean
    CPU_UTILIZATION_PCT_P99      NUMBER,
    CPU_UTILIZATION_PCT_P95      NUMBER,
    CPU_UTILIZATION_PCT_MAX      NUMBER,
    MEMORY_UTILIZATION_PCT       NUMBER,   -- mean
    MEMORY_UTILIZATION_PCT_P99   NUMBER,
    MEMORY_UTILIZATION_PCT_P95   NUMBER,
    MEMORY_UTILIZATION_PCT_MAX   NUMBER,
    LOAD_AVERAGE_P95             NUMBER,
    MEMORY_ALLOCATION_STALLS_P95 NUMBER,
    CPU_USAGE_OCPUS              NUMBER,
    MEMORY_USED_GBS              NUMBER,
    STATISTIC_TYPE               VARCHAR2(20),
    PRIMARY KEY (COLLECTION_TIME, INSTANCE_ID, STATISTIC_TYPE)
);
```

Notes on the schema:

- `COLLECTION_TIME` is plain `TIMESTAMP` (not `TIMESTAMP WITH TIME ZONE`) because Oracle ADB doesn't allow timezone-aware timestamps in a primary key (ORA-02329). Values are stored as UTC.
- One row per `(instance, timestamp)` holds **all** statistics — the percentile / max columns are populated alongside the mean. The plain `CPU_UTILIZATION_PCT` and `MEMORY_UTILIZATION_PCT` columns hold the **mean** (kept for backward compatibility with older collectors).
- New rows are written with `STATISTIC_TYPE = 'aggregate'`. Pre-existing rows from older versions of this collector keep their original `STATISTIC_TYPE` value (e.g. `'mean'`) and have `NULL` in the new percentile / max columns. The PK is unchanged.
- On startup, the writer queries `all_tab_columns` and runs `ALTER TABLE ADD` for any of the extended NUMBER columns (`*_P99`, `*_P95`, `*_MAX`, `LOAD_AVERAGE_P95`, `MEMORY_ALLOCATION_STALLS_P95`) that are missing — historical data is preserved with `NULL`s in the new columns.

### Writing to a different schema (e.g. `OCIRA_DEV`)

Set `adb.table_name` in `config.yaml` to a schema-qualified name:

```yaml
adb:
  table_name: "OCIRA_DEV.OCI_COMPUTE_METRICS"
```

The ADMIN user (or any user with `CREATE ANY TABLE` / `INSERT ANY TABLE`) can create and write the table in another schema.

### Example Queries for ML/Prediction

```sql
-- Average CPU utilization by instance over the last 7 days
SELECT instance_name, shape,
       ROUND(AVG(cpu_utilization_pct), 2) AS avg_cpu_pct,
       ROUND(AVG(memory_utilization_pct), 2) AS avg_mem_pct,
       ROUND(AVG(cpu_usage_ocpus), 2) AS avg_cpu_used,
       ROUND(AVG(memory_used_gbs), 2) AS avg_mem_used_gb
FROM oci_compute_metrics
WHERE collection_time >= SYSTIMESTAMP - INTERVAL '7' DAY
GROUP BY instance_name, shape
ORDER BY avg_cpu_pct DESC;

-- Identify underutilized instances (< 10% CPU, < 20% memory)
SELECT instance_name, shape,
       cpu_allocated_ocpus,
       ROUND(AVG(cpu_utilization_pct), 2) AS avg_cpu_pct,
       memory_allocated_gbs,
       ROUND(AVG(memory_utilization_pct), 2) AS avg_mem_pct
FROM oci_compute_metrics
WHERE collection_time >= SYSTIMESTAMP - INTERVAL '30' DAY
GROUP BY instance_name, shape, cpu_allocated_ocpus, memory_allocated_gbs
HAVING AVG(cpu_utilization_pct) < 10 AND AVG(memory_utilization_pct) < 20
ORDER BY cpu_allocated_ocpus DESC;
```

## Log Analytics Integration

Once data flows into Log Analytics, use **Log Explorer** queries like:

```
'Log Source' = 'OCI Compute Metrics'
| stats avg(cpu_utilization_pct) as avg_cpu,
        avg(memory_utilization_pct) as avg_mem
  by instance_name
| sort -avg_cpu
```

## Deploy on OCI Compute (Instance Principal + systemd)

This is the recommended setup for running the collector continuously against your own tenancy from an OCI compute instance. It uses instance principal auth (no API keys on disk) and systemd to keep the collector alive.

### 1. Create Dynamic Group + IAM policies

Match the compute instance in a Dynamic Group and grant it `read metrics` + `read instances` on the target compartment (see [IAM Policies](#2-iam-policies) above).

### 2. Install on the instance

```bash
git clone <repo-url> /home/opc/oci-metrics-collector-py
cd /home/opc/oci-metrics-collector-py
python3 -m venv venv
source venv/bin/activate
pip install -e .
```

### 3. Configure

```yaml
# config.yaml
oci:
  auth_method: "instance_principal"

scope:
  compartment_id: "ocid1.compartment.oc1..xxxx"   # target compartment
  compartment_id_in_subtree: false                # true only with tenancy OCID

adb:
  enabled: true
  wallet_dir: "/home/opc/wallet_<dbname>"
  dsn: "<dbname>_high"                            # or <dbname>_public_high for public endpoint
  user: "ADMIN"
  table_name: "OCI_COMPUTE_METRICS"               # or SCHEMA.TABLE
```

You can pull the compartment OCID from the instance metadata service:

```bash
curl -s -H "Authorization: Bearer Oracle" http://169.254.169.254/opc/v2/instance/ | jq -r '.compartmentId'
```

### 4. Store DB passwords for systemd

Create `/home/opc/.env` in **systemd EnvironmentFile format** — plain `KEY=VALUE`, **no `export`**, no surrounding quotes:

```ini
# /home/opc/.env
OCI_METRICS_ADB_PASSWORD=your_admin_password
OCI_METRICS_ADB_WALLET_PASSWORD=your_wallet_password
```

```bash
chmod 600 /home/opc/.env
```

> ⚠️ systemd's `EnvironmentFile=` does **not** interpret `export` and treats quotes literally. A file written with `export VAR="..."` will be silently ignored, causing `ORA-01017: invalid credential` at runtime.

### 5. Verify connectivity

```bash
set -a; source /home/opc/.env; set +a
source /home/opc/oci-metrics-collector-py/venv/bin/activate
oci-metrics-collector test-connection --config config.yaml
```

### 6. Install the systemd unit

Create `/etc/systemd/system/oci-metrics-collector.service`:

```ini
[Unit]
Description=OCI Metrics Collector
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=opc
WorkingDirectory=/home/opc/oci-metrics-collector-py
EnvironmentFile=/home/opc/.env
ExecStart=/home/opc/oci-metrics-collector-py/venv/bin/oci-metrics-collector collect --config config.yaml --continuous
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now oci-metrics-collector
sudo systemctl status oci-metrics-collector
sudo journalctl -u oci-metrics-collector -f
```

The service runs `--continuous` (5-minute cycles by default), so it's one long-lived process — don't also add a crontab entry.

### Why systemd, not cron / `/loop` / `/schedule`?

- The collector already has a built-in continuous mode with retries, connection pooling, and SIGTERM-based graceful shutdown. systemd gives it auto-restart on crash, auto-start on reboot, and `journalctl` logs for free.
- A crontab every 5 minutes would spawn a fresh process each cycle — new connection pool, new auth handshake, and risk of overlapping runs.
- Claude Code's `/loop` needs Claude to stay open in the terminal and is meant for dev-time polling.
- Claude Code's `/schedule` (triggers) spins up a full agent per run — overkill for calling one CLI.

## Troubleshooting

Issues encountered during real deployments and how to resolve them:

| Symptom | Cause | Fix |
|---|---|---|
| `ORA-01017: invalid credential` under systemd, but works interactively | `/home/opc/.env` uses shell `export VAR="..."`; systemd `EnvironmentFile=` ignores `export` and treats quotes literally | Rewrite as plain `KEY=VALUE`, no `export`, no quotes. `chmod 600`. Restart service. |
| `[Errno -2] Name or service not known` for the ADB hostname | Wallet uses a private endpoint hostname (e.g. `xxx.adb.<region>.oraclecloud.com`) that your VCN's DNS can't resolve | Either wire up a Private View DNS record for the ADB private endpoint in the VCN, or switch `adb.dsn` to the public alias (`<dbname>_public_high`) and allow the instance IP on the ADB ACL. |
| `ORA-12506: listener refused connection` on the public DSN | ADB Access Control List is blocking the compute instance's egress IP | Add the instance's public/NAT egress IP to the ADB network ACL, or use the private endpoint path. |
| `ORA-02329: Column of data type TIME/TIMESTAMP WITH TIME ZONE cannot be unique or a primary key` | Legacy DDL used `TIMESTAMP WITH TIME ZONE` in the PK | Already fixed in code — `COLLECTION_TIME` is plain `TIMESTAMP`. Drop any pre-existing table created with the old DDL. |
| `ORA-12838: cannot read/modify an object after modifying it in parallel` right after table creation | ADB defaults to parallel DML; MERGE immediately after DDL on the same object fails | Already fixed in code — the session issues `ALTER SESSION DISABLE PARALLEL DML` before the MERGE. |

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Run with coverage
pytest tests/ --cov=oci_metrics_collector --cov-report=term-missing
```

## License

MIT
