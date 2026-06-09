from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from serving.fabric_proxy.server import _call_fabric_execute_queries, _resolve_connector


def test_resolve_connector_finds_by_domain_and_model():
    scan_config = {
        "connectors": [
            {
                "id": "retail-semantic",
                "type": "fabric_semantic",
                "options": {"workspace_id": "ws-guid", "dataset_id": "ds-guid"},
            }
        ],
        "domains": [{"name": "retail", "semantic_connector": "retail-semantic"}],
    }
    result = _resolve_connector(scan_config, domain="retail", model="retail-semantic")
    assert result == {"workspace_id": "ws-guid", "dataset_id": "ds-guid"}


def test_resolve_connector_errors_on_unknown_model():
    scan_config = {
        "connectors": [
            {
                "id": "retail-semantic",
                "type": "fabric_semantic",
                "options": {"workspace_id": "ws-guid", "dataset_id": "ds-guid"},
            }
        ],
        "domains": [{"name": "retail", "semantic_connector": "retail-semantic"}],
    }
    with pytest.raises(ValueError, match="not found"):
        _resolve_connector(scan_config, domain="retail", model="nonexistent")


async def test_execute_query_calls_fabric_api():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {}
    mock_response.json.return_value = {
        "results": [{"tables": [{"rows": [{"[Revenue]": 1000}]}]}]
    }

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("serving.fabric_proxy.server.httpx.AsyncClient") as mock_async_client:
        mock_async_client.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_async_client.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await _call_fabric_execute_queries(
            workspace_id="ws-guid",
            dataset_id="ds-guid",
            dax="EVALUATE ROW('x', 1)",
            user_token="fake",
        )

    rows = result["results"][0]["tables"][0]["rows"]
    assert rows[0]["[Revenue]"] == 1000
