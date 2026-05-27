"""
Configuration management for OCI Metrics Collector.

Loads settings from a YAML config file with environment variable
overrides for sensitive values (passwords, keys).
"""

import os
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List

import yaml

logger = logging.getLogger(__name__)

SUPPORTED_COMPARTMENT_STRATEGIES = {"tenancy_subtree"}


@dataclass
class OciAuthConfig:
    """OCI authentication settings."""
    auth_method: str = "config_file"
    config_file: str = "~/.oci/config"
    profile: str = "DEFAULT"


@dataclass
class TenancyConfig:
    """OCI tenancy source scope."""
    name: str = ""
    tenancy_id: str = ""
    regions: List[str] = field(default_factory=list)
    compartment_strategy: str = "tenancy_subtree"


@dataclass(frozen=True)
class CollectionScope:
    """Resolved collection scope for a tenancy and region."""
    source_tenancy_name: str
    source_tenancy_id: str
    region: str
    compartment_id: str
    compartment_id_in_subtree: bool
    compartment_strategy: str


@dataclass
class MetricsConfig:
    """Metrics collection parameters."""
    namespace: str = "oci_computeagent"
    metric_stats: Dict[str, List[str]] = field(
        default_factory=lambda: {
            "CpuUtilization": ["mean"],
            "MemoryUtilization": ["mean"],
        }
    )
    resolution: str = "5m"
    lookback_minutes: int = 10


@dataclass
class AdbConfig:
    """Autonomous Database destination settings."""
    enabled: bool = True
    wallet_dir: str = ""
    dsn: str = ""
    user: str = "ADMIN"
    password: str = ""
    wallet_password: str = ""
    table_name: str = "OCI_COMPUTE_METRICS"


@dataclass
class LogAnalyticsConfig:
    """OCI Log Analytics destination settings."""
    enabled: bool = True
    namespace: str = ""
    log_group_id: str = ""
    log_source_name: str = "OCI Compute Metrics"
    entity_mapping: Dict[str, str] = field(default_factory=dict)


@dataclass
class OrchestrationConfig:
    """Orchestration and runtime settings."""
    interval_seconds: int = 300
    max_retries: int = 3
    log_level: str = "INFO"


@dataclass
class CollectorConfig:
    """Root configuration object for the OCI Metrics Collector."""
    oci: OciAuthConfig = field(default_factory=OciAuthConfig)
    tenancies: List[TenancyConfig] = field(default_factory=list)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)
    adb: AdbConfig = field(default_factory=AdbConfig)
    log_analytics: LogAnalyticsConfig = field(default_factory=LogAnalyticsConfig)
    orchestration: OrchestrationConfig = field(default_factory=OrchestrationConfig)


def _apply_env_overrides(config: CollectorConfig) -> None:
    """
    Override config values with environment variables for sensitive data.

    Supported env vars:
        OCI_METRICS_ADB_USER            → adb.user
        OCI_METRICS_ADB_PASSWORD         → adb.password
        OCI_METRICS_ADB_WALLET_PASSWORD  → adb.wallet_password
        OCI_METRICS_LA_NAMESPACE         → log_analytics.namespace
        OCI_METRICS_LA_LOG_GROUP_ID      → log_analytics.log_group_id
    """
    env_map = {
        "OCI_METRICS_ADB_USER": ("adb", "user"),
        "OCI_METRICS_ADB_PASSWORD": ("adb", "password"),
        "OCI_METRICS_ADB_WALLET_PASSWORD": ("adb", "wallet_password"),
        "OCI_METRICS_LA_NAMESPACE": ("log_analytics", "namespace"),
        "OCI_METRICS_LA_LOG_GROUP_ID": ("log_analytics", "log_group_id"),
    }
    for env_var, (section, key) in env_map.items():
        value = os.environ.get(env_var)
        if value:
            setattr(getattr(config, section), key, value)
            logger.debug("Config override from env var: %s", env_var)


def _dict_to_dataclass(data: dict, cls):
    """Safely convert a dictionary to a dataclass, ignoring unknown keys."""
    import dataclasses
    valid_keys = {f.name for f in dataclasses.fields(cls)}
    filtered = {k: v for k, v in data.items() if k in valid_keys}
    return cls(**filtered)


def _load_tenancies(raw: dict) -> List[TenancyConfig]:
    """Load and validate the first-class tenancy source list."""
    tenancy_items = raw.get("tenancies")
    if not isinstance(tenancy_items, list) or not tenancy_items:
        raise ValueError(
            "tenancies is required and must contain at least one tenancy source."
        )

    tenancies: List[TenancyConfig] = []
    for index, item in enumerate(tenancy_items):
        if not isinstance(item, dict):
            raise ValueError(f"tenancies[{index}] must be a mapping.")
        tenancy = _dict_to_dataclass(item, TenancyConfig)
        tenancy.regions = _normalize_regions(
            f"tenancies[{index}].regions",
            tenancy.regions,
        )
        _validate_tenancy(tenancy, index)
        tenancies.append(tenancy)
    return tenancies


def _normalize_regions(name: str, regions) -> List[str]:
    """Normalize a region list from YAML."""
    if isinstance(regions, str):
        regions = [r.strip() for r in regions.split(",") if r.strip()]
    if not isinstance(regions, list):
        raise ValueError(f"{name} must be a non-empty list of region names.")
    normalized = []
    for region in regions:
        if not isinstance(region, str) or not region.strip():
            raise ValueError(f"{name} must contain non-empty region names.")
        normalized.append(region.strip())
    if not normalized:
        raise ValueError(f"{name} must contain at least one region.")
    return normalized


def _validate_tenancy(tenancy: TenancyConfig, index: int) -> None:
    """Validate one tenancy source definition."""
    prefix = f"tenancies[{index}]"
    if not tenancy.name:
        raise ValueError(f"{prefix}.name is required.")
    if not tenancy.tenancy_id:
        raise ValueError(f"{prefix}.tenancy_id is required.")
    if not tenancy.tenancy_id.startswith("ocid1.tenancy."):
        raise ValueError(f"{prefix}.tenancy_id must be a tenancy OCID.")
    if tenancy.compartment_strategy not in SUPPORTED_COMPARTMENT_STRATEGIES:
        raise ValueError(
            f"{prefix}.compartment_strategy must be one of: "
            f"{', '.join(sorted(SUPPORTED_COMPARTMENT_STRATEGIES))}."
        )


def iter_collection_scopes(config: CollectorConfig) -> Iterable[CollectionScope]:
    """Yield resolved collection scopes for all configured tenancies and regions."""
    for tenancy in config.tenancies:
        for region in tenancy.regions:
            if tenancy.compartment_strategy == "tenancy_subtree":
                yield CollectionScope(
                    source_tenancy_name=tenancy.name,
                    source_tenancy_id=tenancy.tenancy_id,
                    region=region,
                    compartment_id=tenancy.tenancy_id,
                    compartment_id_in_subtree=True,
                    compartment_strategy=tenancy.compartment_strategy,
                )


def load_config(config_path: str) -> CollectorConfig:
    """
    Load configuration from a YAML file, applying environment variable
    overrides for sensitive values.

    Args:
        config_path: Path to the YAML configuration file.

    Returns:
        A fully populated CollectorConfig instance.

    Raises:
        FileNotFoundError: If the config file does not exist.
        ValueError: If the config file is malformed or missing required fields.
    """
    path = Path(config_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")

    with open(path, "r") as f:
        raw = yaml.safe_load(f)

    if not raw or not isinstance(raw, dict):
        raise ValueError(f"Invalid configuration file: {path}")

    # Build config from YAML sections
    config = CollectorConfig(
        oci=_dict_to_dataclass(raw.get("oci", {}), OciAuthConfig),
        tenancies=_load_tenancies(raw),
        metrics=_dict_to_dataclass(raw.get("metrics", {}), MetricsConfig),
        adb=_dict_to_dataclass(raw.get("adb", {}), AdbConfig),
        log_analytics=_dict_to_dataclass(
            raw.get("log_analytics", {}), LogAnalyticsConfig
        ),
        orchestration=_dict_to_dataclass(
            raw.get("orchestration", {}), OrchestrationConfig
        ),
    )

    # Apply environment variable overrides
    _apply_env_overrides(config)

    logger.info("Configuration loaded from: %s", path)
    return config
