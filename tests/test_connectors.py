"""
Tests for Fabric connector implementations.

Uses unittest.mock to avoid live API calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

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

    # INFO.VIEW.MEASURES response
    measures_rows = [
        {
            "[TABLESILENAME]": "Sales",
            "[NAME]": "Total Revenue",
            "[EXPRESSION]": "SUM(Sales[Amount])",
            "[ISHIDDEN]": False,
            "[DISPLAYFOLDER]": "",
            "[FORMATSTRING]": "#,##0.00",
        }
    ]
    # INFO.VIEW.COLUMNS response
    columns_rows = [
        {
            "[TABLESILENAME]": "Sales",
            "[EXPLICITNAME]": "Amount",
            "[DATATYPE]": "Decimal",
            "[ISHIDDEN]": False,
        }
    ]
    # INFO.VIEW.RELATIONSHIPS response
    rels_rows: list = []

    mock_post.return_value.status_code = 200
    mock_post.return_value.json.side_effect = [
        _make_dax_response(measures_rows),
        _make_dax_response(columns_rows),
        _make_dax_response(rels_rows),
    ]

    connector = FabricSemanticConnector(semantic_config)
    snapshot = connector.fetch_metadata()

    assert snapshot.source_ref == "fabric_semantic:ds-456"
    assert len(snapshot.tables) == 1
    assert snapshot.tables[0].name == "Sales"
    assert len(snapshot.tables[0].measures) == 1
    assert snapshot.tables[0].measures[0].name == "Total Revenue"


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
    success_response = MagicMock()
    success_response.status_code = 200
    success_response.json.return_value = _make_dax_response([
        {
            "[TABLESILENAME]": "Sales",
            "[NAME]": "Revenue",
            "[EXPRESSION]": "SUM(Sales[Amt])",
            "[ISHIDDEN]": False,
            "[DISPLAYFOLDER]": "",
            "[FORMATSTRING]": "",
        }
    ])

    mock_post.side_effect = [error_response, success_response, success_response, success_response]

    connector = FabricSemanticConnector(semantic_config)
    snapshot = connector.fetch_metadata()

    assert any(t.measures for t in snapshot.tables)
