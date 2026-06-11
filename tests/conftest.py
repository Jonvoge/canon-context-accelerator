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
    """A minimal MetadataSnapshot that matches the retail example domain's 15 measures."""
    _all_measures = [
        "Total Revenue",
        "Total Gross Revenue",
        "Total COGS",
        "Gross Profit",
        "Gross Margin %",
        "Total Discount",
        "Discount %",
        "Total Orders",
        "Total Quantity",
        "Avg Order Value",
        "Avg Unit Price",
        "Customer Count",
        "Revenue PY",
        "Revenue YTD",
        "Revenue YoY %",
    ]
    return MetadataSnapshot(
        tables=[
            TableMetadata(
                name="fact_sales",
                columns=[
                    ColumnMetadata(name="order_status", table="fact_sales"),
                    ColumnMetadata(name="product_id", table="fact_sales"),
                ],
            ),
            TableMetadata(
                name="dim_product",
                columns=[ColumnMetadata(name="category", table="dim_product")],
            ),
            TableMetadata(
                name="dim_geography",
                columns=[ColumnMetadata(name="region", table="dim_geography")],
            ),
            TableMetadata(
                name="dim_customer",
                columns=[
                    ColumnMetadata(name="customer_id", table="dim_customer"),
                    ColumnMetadata(name="segment", table="dim_customer"),
                ],
            ),
            TableMetadata(
                name="dim_date",
                columns=[ColumnMetadata(name="date_key", table="dim_date")],
            ),
        ],
        measures=[MeasureMetadata(name=name, table="fact_sales", expression=f"[{name}]") for name in _all_measures],
        relationships=[
            RelationshipMetadata(
                from_table="fact_sales",
                from_column="product_id",
                to_table="dim_product",
                to_column="product_id",
            )
        ],
    )
