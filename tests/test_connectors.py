"""
Tests for Fabric connector implementations.

Uses unittest.mock to avoid live API calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from connectors.base import MetadataSnapshot
from connectors.fabric_semantic import FabricSemanticConnector


@pytest.fixture
def semantic_config() -> dict:
    return {
        "workspace_id": "ws-123",
        "tenant_id": "tenant-abc",
        "client_id": "client-xyz",
        "client_secret": "secret-999",
        "dataset_id": "ds-456",
    }


def _make_dax_response(rows: list[list]) -> dict:
    """Build a Power BI executeQueries response JSON."""
    return {
        "results": [
            {
                "tables": [
                    {"rows": rows}
                ]
            }
        ]
    }


@patch("connectors.fabric_semantic.ClientSecretCredential")
@patch("connectors.fabric_semantic.requests.post")
def test_fetch_metadata_happy_path(mock_post: MagicMock, mock_cred: MagicMock, semantic_config: dict) -> None:
    """fetch_metadata() should return a MetadataSnapshot when DAX queries succeed."""
    mock_token = MagicMock()
    mock_token.token = "fake-token"
    mock_cred.return_value.get_token.return_value = mock_token

    # INFO.VIEW.TABLES response — keys match Fabric API: "[FieldName]" → _col_name → "FieldName"
    tables_rows = [
        {
            "[Name]": "Sales",
            "[ExplicitName]": "Sales",
            "[IsHidden]": False,
            "[ID]": 1,
        }
    ]
    # INFO.VIEW.COLUMNS response
    columns_rows = [
        {
            "[TableID]": 1,
            "[ExplicitName]": "Amount",
            "[DataType]": "Decimal",
            "[IsHidden]": False,
            "[ID]": 100,
        }
    ]
    # INFO.VIEW.MEASURES response
    measures_rows = [
        {
            "[TableID]": 1,
            "[Name]": "Total Revenue",
            "[ExplicitName]": "Total Revenue",
            "[Expression]": "SUM(Sales[Amount])",
            "[IsHidden]": False,
            "[DisplayFolder]": "",
            "[FormatString]": "#,##0.00",
        }
    ]
    # INFO.VIEW.RELATIONSHIPS response
    rels_rows: list = []

    mock_post.return_value.status_code = 200
    mock_post.return_value.json.side_effect = [
        _make_dax_response(tables_rows),
        _make_dax_response(columns_rows),
        _make_dax_response(measures_rows),
        _make_dax_response(rels_rows),
    ]

    connector = FabricSemanticConnector(semantic_config)
    snapshot = connector.fetch_metadata()

    assert len(snapshot.tables) >= 1
    sales_table = next((t for t in snapshot.tables if t.name == "Sales"), None)
    assert sales_table is not None
    assert any(m.name == "Total Revenue" for m in snapshot.measures)


@patch("connectors.fabric_semantic.ClientSecretCredential")
@patch("connectors.fabric_semantic.requests.post")
def test_fallback_to_info_functions(mock_post: MagicMock, mock_cred: MagicMock, semantic_config: dict) -> None:
    """When INFO.VIEW.* returns 400, connector should retry with INFO.*() and succeed."""
    mock_token = MagicMock()
    mock_token.token = "fake-token"
    mock_cred.return_value.get_token.return_value = mock_token

    # First call (INFO.VIEW.*) returns 400
    error_response = MagicMock()
    error_response.status_code = 400
    error_response.text = "Unknown function 'INFO.VIEW.MEASURES'"

    # Second call (INFO.*) succeeds
    tables_success = MagicMock()
    tables_success.status_code = 200
    tables_success.json.return_value = _make_dax_response([
        {"[Name]": "Sales", "[ExplicitName]": "Sales", "[IsHidden]": False, "[ID]": 1}
    ])

    # Subsequent calls succeed with empty results
    empty_success = MagicMock()
    empty_success.status_code = 200
    empty_success.json.return_value = _make_dax_response([])

    mock_post.side_effect = [error_response, tables_success, empty_success, empty_success, empty_success]

    connector = FabricSemanticConnector(semantic_config)
    snapshot = connector.fetch_metadata()

    # After fallback, should have returned a valid snapshot (tables + measures may be empty)
    assert isinstance(snapshot, MetadataSnapshot)
