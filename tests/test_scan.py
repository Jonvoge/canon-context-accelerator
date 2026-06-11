"""
Tests for scripts/scan.py scan engine.

Uses a mock connector to avoid live Fabric calls.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from connectors.base import (
    MeasureMetadata,
    MetadataSnapshot,
    TableMetadata,
)
from scripts.scan import run_scan


def _make_snapshot_with_undocumented_measure() -> MetadataSnapshot:
    """A snapshot containing a measure not defined in Canon."""
    return MetadataSnapshot(
        tables=[
            TableMetadata(name="fact_sales", columns=[]),
        ],
        measures=[
            MeasureMetadata(name="Total Revenue", table="fact_sales", expression="SUM(fact_sales[amount])"),
            MeasureMetadata(
                name="Undocumented Measure XYZ", table="fact_sales", expression="CALCULATE(SUM(fact_sales[qty]))"
            ),
        ],
        relationships=[],
    )


def _make_snapshot_missing_defined_measure() -> MetadataSnapshot:
    """A snapshot where a Canon-defined measure is absent from the model."""
    return MetadataSnapshot(
        tables=[
            TableMetadata(name="fact_sales", columns=[]),
        ],
        measures=[],  # Total Revenue is missing
        relationships=[],
    )


@patch("scripts.scan._build_connector")
def test_undocumented_measure_finding(mock_build: MagicMock, repo_root: Path) -> None:
    """Measures in the model but not in Canon should produce undocumented_measure findings."""
    mock_connector = MagicMock()
    mock_connector.validate_config.return_value = []
    mock_connector.fetch_metadata.return_value = _make_snapshot_with_undocumented_measure()
    mock_connector.profile_dimension.return_value = []
    mock_build.return_value = mock_connector

    result = run_scan(
        domain="retail",
        config_path=str(repo_root / "scan-config.yaml"),
        repo_root=repo_root,
    )

    finding_types = {f.type for f in result.findings}
    assert "undocumented_measure" in finding_types


@patch("scripts.scan._build_connector")
def test_orphaned_definition_finding(mock_build: MagicMock, repo_root: Path) -> None:
    """Metrics defined in Canon but absent from the model should produce orphaned_definition findings."""
    mock_connector = MagicMock()
    mock_connector.validate_config.return_value = []
    mock_connector.fetch_metadata.return_value = _make_snapshot_missing_defined_measure()
    mock_connector.profile_dimension.return_value = []
    mock_build.return_value = mock_connector

    result = run_scan(
        domain="retail",
        config_path=str(repo_root / "scan-config.yaml"),
        repo_root=repo_root,
    )

    finding_types = {f.type for f in result.findings}
    assert "orphaned_definition" in finding_types


@patch("scripts.scan._build_connector")
def test_clean_domain_no_critical_findings(
    mock_build: MagicMock, repo_root: Path, canned_snapshot: MetadataSnapshot
) -> None:
    """When the model matches Canon exactly, no high-severity findings should appear."""
    mock_connector = MagicMock()
    mock_connector.validate_config.return_value = []
    mock_connector.fetch_metadata.return_value = canned_snapshot
    mock_connector.profile_dimension.return_value = []
    mock_build.return_value = mock_connector

    result = run_scan(
        domain="retail",
        config_path=str(repo_root / "scan-config.yaml"),
        repo_root=repo_root,
    )

    high_findings = [f for f in result.findings if f.severity == "high"]
    assert not high_findings, f"Unexpected high findings: {[f.description for f in high_findings]}"
