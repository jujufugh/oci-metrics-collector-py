"""
Configuration management for OCI Metrics Collector.

Loads settings from a YAML config file with environment variable
overrides for sensitive values (passwords, keys).
"""

import os
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)


@dataclass
class OciAuthConfig:
    """OCI authentication settings."""
    auth_method: str = "config_file"
    config_file: str = "~/.oci/config"
    profile: str = "DEFAULT"


@dataclass
class ScopeConfig:
    """Target compartment/tenancy scope."""
    compartment_id: str = ""
    tenancy_id: Optional[str] = None
    compartment_id_in_subtree: bool = False


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
    scope: ScopeConfig = field(default_factory=ScopeConfig)
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
        OCI_METRICS_COMPARTMENT_ID       → scope.compartment_id
        OCI_METRICS_COMPARTMENT_ID_IN_SUBTREE
                                         → scope.compartment_id_in_subtree
        OCI_METRICS_LA_NAMESPACE         → log_analytics.namespace
        OCI_METRICS_LA_LOG_GROUP_ID      → log_analytics.log_group_id
    """
    env_map = {
        "OCI_METRICS_ADB_USER": ("adb", "user"),
        "OCI_METRICS_ADB_PASSWORD": ("adb", "password"),
        "OCI_METRICS_ADB_WALLET_PASSWORD": ("adb", "wallet_password"),
        "OCI_METRICS_COMPARTMENT_ID": ("scope", "compartment_id"),
        "OCI_METRICS_LA_NAMESPACE": ("log_analytics", "namespace"),
        "OCI_METRICS_LA_LOG_GROUP_ID": ("log_analytics", "log_group_id"),
    }
    for env_var, (section, key) in env_map.items():
        value = os.environ.get(env_var)
        if value:
            setattr(getattr(config, section), key, value)
            logger.debug("Config override from env var: %s", env_var)

    subtree = os.environ.get("OCI_METRICS_COMPARTMENT_ID_IN_SUBTREE")
    if subtree:
        config.scope.compartment_id_in_subtree = _parse_bool_env(
            "OCI_METRICS_COMPARTMENT_ID_IN_SUBTREE",
            subtree,
        )
        logger.debug(
            "Config override from env var: OCI_METRICS_COMPARTMENT_ID_IN_SUBTREE"
        )


def _parse_bool_env(name: str, value: str) -> bool:
    """Parse a boolean environment variable value."""
    return _parse_bool_value(name, value)


def _parse_bool_value(name: str, value) -> bool:
    """Parse a bool or string boolean config value."""
    if isinstance(value, bool):
        return value
    if not isinstance(value, str):
        raise ValueError(
            f"{name} must be a boolean value "
            "(true/false, yes/no, on/off, or 1/0)."
        )

    normalized = value.strip().lower()
    if normalized in ("1", "true", "yes", "y", "on"):
        return True
    if normalized in ("0", "false", "no", "n", "off"):
        return False
    raise ValueError(
        f"{name} must be a boolean value "
        "(true/false, yes/no, on/off, or 1/0)."
    )


def _dict_to_dataclass(data: dict, cls):
    """Safely convert a dictionary to a dataclass, ignoring unknown keys."""
    import dataclasses
    valid_keys = {f.name for f in dataclasses.fields(cls)}
    filtered = {k: v for k, v in data.items() if k in valid_keys}
    return cls(**filtered)


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
        scope=_dict_to_dataclass(raw.get("scope", {}), ScopeConfig),
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
    config.scope.compartment_id_in_subtree = _parse_bool_value(
        "scope.compartment_id_in_subtree",
        config.scope.compartment_id_in_subtree,
    )

    # Validate required fields
    if not config.scope.compartment_id:
        raise ValueError(
            "scope.compartment_id is required. Set it in config.yaml "
            "or via OCI_METRICS_COMPARTMENT_ID env var."
        )

    if (
        config.scope.compartment_id_in_subtree
        and not config.scope.compartment_id.startswith("ocid1.tenancy.")
    ):
        raise ValueError(
            "scope.compartment_id_in_subtree requires scope.compartment_id "
            "to be the tenancy OCID. OCI Monitoring only supports subtree "
            "metric queries from the tenancy root."
        )

    logger.info("Configuration loaded from: %s", path)
    return config
