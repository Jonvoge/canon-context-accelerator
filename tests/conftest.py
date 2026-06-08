"""
Shared test fixtures for Canon test suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from connectors.base import (
    ColumnMetadata,
    MeasureMetadata,
    MetadataSnapshot,
    RelationshipMetadata,
    TableMetadata,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def repo_root() -> Path:
    return _REPO_ROOT


@pytest.fixture
def retail_domain_path(repo_root: Path) -> Path:
    return repo_root / "domains" / "retail"


@pytest.fixture
def canned_snapshot() -> MetadataSnapshot:
    """A minimal MetadataSnapshot that matches the retail example domain."""
    return MetadataSnapshot(
        source_ref="fabric_semantic:retail",
        tables=[
            TableMetadata(
                name="Sales",
                source_type="semantic_model",
                measures=[
                    MeasureMetadata(
                        name="Total Revenue",
                        table="Sales",
                        expression="SUM(Sales[Amount])",
                        hidden=False,
                        display_folder="",
                        format_string="#,##0.00",
                    ),
                    MeasureMetadata(
                        name="Total Margin",
                        table="Sales",
                        expression="[Total Revenue] - [Total Cost]",
                        hidden=False,
                        display_folder="",
                        format_string="#,##0.00",
                    ),
                ],
                columns=[
                    ColumnMetadata(name="Order Status", table="Sales", data_type="string"),
                    ColumnMetadata(name="Product Category", table="Sales", data_type="string"),
                    ColumnMetadata(name="Store Region", table="Sales", data_type="string"),
                ],
            )
        ],
        relationships=[
            RelationshipMetadata(
                from_table="Sales",
                from_column="ProductKey",
                to_table="Products",
                to_column="ProductKey",
                cross_filter="Single",
                active=True,
            )
        ],
    )
