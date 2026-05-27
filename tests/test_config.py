"""
Unit tests for configuration loading and validation.
"""

import pytest

from oci_metrics_collector.config import iter_collection_scopes, load_config


def _write_config(path, tenancies_block):
    path.write_text(
        "\n".join(
            [
                "tenancies:",
                *tenancies_block,
            ]
        )
    )


def test_load_config_accepts_tenancy_sources(tmp_path):
    """The collector is configured by first-class tenancy sources."""
    config_path = tmp_path / "config.yaml"
    _write_config(
        config_path,
        [
            '  - name: "parent"',
            '    tenancy_id: "ocid1.tenancy.oc1..parent"',
            '    regions: ["us-ashburn-1", "us-phoenix-1"]',
            '    compartment_strategy: "tenancy_subtree"',
        ],
    )

    config = load_config(str(config_path))

    assert len(config.tenancies) == 1
    assert config.tenancies[0].name == "parent"
    assert config.tenancies[0].regions == ["us-ashburn-1", "us-phoenix-1"]


def test_iter_collection_scopes_expands_tenancy_regions(tmp_path):
    """Each tenancy region becomes an explicit collection scope."""
    config_path = tmp_path / "config.yaml"
    _write_config(
        config_path,
        [
            '  - name: "parent"',
            '    tenancy_id: "ocid1.tenancy.oc1..parent"',
            '    regions: "us-ashburn-1, us-phoenix-1"',
            '    compartment_strategy: "tenancy_subtree"',
        ],
    )

    scopes = list(iter_collection_scopes(load_config(str(config_path))))

    assert [s.region for s in scopes] == ["us-ashburn-1", "us-phoenix-1"]
    assert all(s.compartment_id == "ocid1.tenancy.oc1..parent" for s in scopes)
    assert all(s.compartment_id_in_subtree is True for s in scopes)


def test_load_config_rejects_missing_tenancies(tmp_path):
    """The old single-scope config model is no longer accepted."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "scope:",
                '  compartment_id: "ocid1.tenancy.oc1..test"',
                "  compartment_id_in_subtree: true",
            ]
        )
    )

    with pytest.raises(ValueError, match="tenancies is required"):
        load_config(str(config_path))


def test_load_config_rejects_invalid_tenancy_ocid(tmp_path):
    """Tenancy sources must be rooted at tenancy OCIDs."""
    config_path = tmp_path / "config.yaml"
    _write_config(
        config_path,
        [
            '  - name: "child"',
            '    tenancy_id: "ocid1.compartment.oc1..not-tenancy"',
            '    regions: ["us-ashburn-1"]',
        ],
    )

    with pytest.raises(ValueError, match="tenancy OCID"):
        load_config(str(config_path))


def test_load_config_rejects_empty_regions(tmp_path):
    """A tenancy source must name at least one collection region."""
    config_path = tmp_path / "config.yaml"
    _write_config(
        config_path,
        [
            '  - name: "parent"',
            '    tenancy_id: "ocid1.tenancy.oc1..parent"',
            "    regions: []",
        ],
    )

    with pytest.raises(ValueError, match="regions"):
        load_config(str(config_path))


def test_load_config_rejects_unknown_compartment_strategy(tmp_path):
    """Only explicit supported compartment strategies are accepted."""
    config_path = tmp_path / "config.yaml"
    _write_config(
        config_path,
        [
            '  - name: "parent"',
            '    tenancy_id: "ocid1.tenancy.oc1..parent"',
            '    regions: ["us-ashburn-1"]',
            '    compartment_strategy: "single_compartment"',
        ],
    )

    with pytest.raises(ValueError, match="compartment_strategy"):
        load_config(str(config_path))
