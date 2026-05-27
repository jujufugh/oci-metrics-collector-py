"""
Unit tests for the command-line interface.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from oci_metrics_collector.cli import cmd_discover
from oci_metrics_collector.config import CollectorConfig, OciAuthConfig, ScopeConfig


@patch("oci_metrics_collector.cli._create_monitoring_client")
@patch("oci_metrics_collector.cli._setup_logging")
@patch("oci_metrics_collector.cli.load_config")
def test_discover_uses_subtree_scope(
    mock_load_config,
    mock_setup_logging,
    mock_create_client,
):
    """Metric discovery passes the subtree flag through to Monitoring."""
    config = CollectorConfig(
        oci=OciAuthConfig(auth_method="config_file"),
        scope=ScopeConfig(
            compartment_id="ocid1.tenancy.oc1..test",
            compartment_id_in_subtree=True,
        ),
    )
    mock_load_config.return_value = config
    mock_client = MagicMock()
    mock_create_client.return_value = mock_client

    args = SimpleNamespace(config="config.yaml", namespace="oci_computeagent")

    with patch("oci.pagination.list_call_get_all_results") as mock_list_all:
        mock_list_all.return_value = MagicMock(data=[])

        cmd_discover(args)

    call = mock_list_all.call_args
    assert call.args[0] is mock_client.list_metrics
    assert call.kwargs["compartment_id"] == "ocid1.tenancy.oc1..test"
    assert call.kwargs["compartment_id_in_subtree"] is True
