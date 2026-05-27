"""
Unit tests for configuration loading and validation.
"""

import pytest

from oci_metrics_collector.config import load_config


def _write_config(path, scope):
    path.write_text(
        "\n".join(
            [
                "scope:",
                *[f"  {key}: {value}" for key, value in scope.items()],
            ]
        )
    )


def test_load_config_accepts_subtree_scope_with_tenancy_ocid(tmp_path):
    """Subtree collection is valid only when rooted at the tenancy OCID."""
    config_path = tmp_path / "config.yaml"
    _write_config(
        config_path,
        {
            "compartment_id": '"ocid1.tenancy.oc1..test"',
            "compartment_id_in_subtree": "true",
        },
    )

    config = load_config(str(config_path))

    assert config.scope.compartment_id == "ocid1.tenancy.oc1..test"
    assert config.scope.compartment_id_in_subtree is True


def test_load_config_rejects_subtree_scope_with_compartment_ocid(tmp_path):
    """OCI Monitoring subtree metrics require the tenancy OCID as scope root."""
    config_path = tmp_path / "config.yaml"
    _write_config(
        config_path,
        {
            "compartment_id": '"ocid1.compartment.oc1..test"',
            "compartment_id_in_subtree": "true",
        },
    )

    with pytest.raises(ValueError, match="tenancy OCID"):
        load_config(str(config_path))


def test_env_override_parses_subtree_boolean(tmp_path, monkeypatch):
    """Environment overrides can enable subtree collection explicitly."""
    config_path = tmp_path / "config.yaml"
    _write_config(
        config_path,
        {
            "compartment_id": '"ocid1.tenancy.oc1..test"',
            "compartment_id_in_subtree": "false",
        },
    )
    monkeypatch.setenv("OCI_METRICS_COMPARTMENT_ID_IN_SUBTREE", "yes")

    config = load_config(str(config_path))

    assert config.scope.compartment_id_in_subtree is True


def test_load_config_coerces_quoted_subtree_boolean(tmp_path):
    """Quoted YAML booleans are normalized before validation."""
    config_path = tmp_path / "config.yaml"
    _write_config(
        config_path,
        {
            "compartment_id": '"ocid1.compartment.oc1..test"',
            "compartment_id_in_subtree": '"false"',
        },
    )

    config = load_config(str(config_path))

    assert config.scope.compartment_id_in_subtree is False


def test_env_override_rejects_invalid_subtree_boolean(tmp_path, monkeypatch):
    """Invalid boolean env values fail during configuration loading."""
    config_path = tmp_path / "config.yaml"
    _write_config(
        config_path,
        {
            "compartment_id": '"ocid1.tenancy.oc1..test"',
            "compartment_id_in_subtree": "false",
        },
    )
    monkeypatch.setenv("OCI_METRICS_COMPARTMENT_ID_IN_SUBTREE", "maybe")

    with pytest.raises(ValueError, match="must be a boolean"):
        load_config(str(config_path))
