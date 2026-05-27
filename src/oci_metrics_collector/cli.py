"""
Command-line interface for OCI Metrics Collector.

Provides subcommands for one-shot collection, continuous monitoring,
connectivity testing, and metric discovery.
"""

import argparse
import json
import logging
import sys

from . import __version__
from .collector import _create_monitoring_client
from .config import iter_collection_scopes, load_config
from .orchestrator import MetricsOrchestrator


def _setup_logging(level: str = "INFO") -> None:
    """Configure structured logging to stderr."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )


def cmd_collect(args) -> None:
    """Run the metrics collection pipeline."""
    config = load_config(args.config)
    _setup_logging(config.orchestration.log_level)

    orchestrator = MetricsOrchestrator(config)

    if args.continuous:
        if args.interval:
            config.orchestration.interval_seconds = args.interval
        orchestrator.run_continuous()
    else:
        summary = orchestrator.run_once()
        orchestrator.close()

        # Print summary as JSON for scripting
        print(json.dumps(summary, indent=2, default=str))

        if summary.get("errors"):
            sys.exit(1)


def cmd_test_connection(args) -> None:
    """Test connectivity to all configured destinations."""
    config = load_config(args.config)
    _setup_logging(config.orchestration.log_level)

    orchestrator = MetricsOrchestrator(config)
    results = orchestrator.test_connections()
    orchestrator.close()

    print("\n=== Connection Test Results ===")
    all_ok = True
    for dest, status in results.items():
        if status is None:
            icon = "⏭️"
            label = "DISABLED"
        elif status:
            icon = "✅"
            label = "OK"
        else:
            icon = "❌"
            label = "FAILED"
            all_ok = False
        print(f"  {icon} {dest}: {label}")
    print()

    if not all_ok:
        sys.exit(1)


def cmd_discover(args) -> None:
    """Discover available metrics in configured tenancy-region scopes."""
    config = load_config(args.config)
    _setup_logging(config.orchestration.log_level)

    import oci

    scopes = list(iter_collection_scopes(config))
    print(f"\nDiscovering metrics in {len(scopes)} tenancy-region scope(s)")
    print(f"Namespace filter: {args.namespace or 'all'}\n")

    list_details = oci.monitoring.models.ListMetricsDetails(
        namespace=args.namespace,
    )

    metrics_map = {}
    for scope in scopes:
        client = _create_monitoring_client(config, region=scope.region)
        try:
            response = oci.pagination.list_call_get_all_results(
                client.list_metrics,
                compartment_id=scope.compartment_id,
                list_metrics_details=list_details,
                compartment_id_in_subtree=scope.compartment_id_in_subtree,
            )
        except oci.exceptions.ServiceError as e:
            print(
                f"Error in {scope.source_tenancy_name}/{scope.region}: "
                f"{e.message} (status={e.status})",
                file=sys.stderr,
            )
            sys.exit(1)

        for m in response.data:
            key = (
                scope.source_tenancy_name,
                scope.region,
                m.namespace,
                m.name,
            )
            if key not in metrics_map:
                metrics_map[key] = {
                    "tenancy": scope.source_tenancy_name,
                    "region": scope.region,
                    "namespace": m.namespace,
                    "name": m.name,
                    "dimensions": set(),
                    "resource_count": 0,
                }
            metrics_map[key]["resource_count"] += 1
            for dim_key in (m.dimensions or {}).keys():
                metrics_map[key]["dimensions"].add(dim_key)

    if not metrics_map:
        print("No metrics found.")
        return

    print(
        f"{'Tenancy':<20} {'Region':<18} {'Namespace':<30} "
        f"{'Metric Name':<25} {'Resources':<10} Dimensions"
    )
    print("-" * 140)

    for key in sorted(metrics_map.keys()):
        info = metrics_map[key]
        dims = ", ".join(sorted(info["dimensions"]))
        print(
            f"{info['tenancy']:<20} {info['region']:<18} "
            f"{info['namespace']:<30} {info['name']:<25} "
            f"{info['resource_count']:<10} {dims}"
        )

    print(f"\nTotal: {len(metrics_map)} unique metrics")


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="oci-metrics-collector",
        description=(
            "Collect OCI compute instance metrics and ship to "
            "Autonomous Database and OCI Log Analytics."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        help="Available commands",
    )

    # --- collect ---
    collect_parser = subparsers.add_parser(
        "collect",
        help="Collect metrics and ship to configured destinations",
    )
    collect_parser.add_argument(
        "--config",
        "-c",
        required=True,
        help="Path to config.yaml",
    )
    collect_parser.add_argument(
        "--continuous",
        action="store_true",
        help="Run in continuous/daemon mode",
    )
    collect_parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Override collection interval (seconds)",
    )
    collect_parser.set_defaults(func=cmd_collect)

    # --- test-connection ---
    test_parser = subparsers.add_parser(
        "test-connection",
        help="Test connectivity to all configured destinations",
    )
    test_parser.add_argument(
        "--config",
        "-c",
        required=True,
        help="Path to config.yaml",
    )
    test_parser.set_defaults(func=cmd_test_connection)

    # --- discover ---
    discover_parser = subparsers.add_parser(
        "discover",
        help="Discover available metrics in configured tenancy-region scopes",
    )
    discover_parser.add_argument(
        "--config",
        "-c",
        required=True,
        help="Path to config.yaml",
    )
    discover_parser.add_argument(
        "--namespace",
        "-n",
        default="oci_computeagent",
        help="Metric namespace to filter (default: oci_computeagent)",
    )
    discover_parser.set_defaults(func=cmd_discover)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
