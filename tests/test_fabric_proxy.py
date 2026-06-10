from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from serving.fabric_proxy.server import (
    _call_fabric_execute_queries,
    _resolve_connector,
    _apply_env_overrides,
    _resolve_dataset_id_async,
    _validate_scan_config,
)


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
    assert result == {"workspace_id": "ws-guid", "dataset_id": "ds-guid", "dataset_name": ""}


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
    with pytest.raises(ValueError, match="not the semantic connector"):
        _resolve_connector(scan_config, domain="retail", model="nonexistent")


def test_resolve_connector_rejects_wrong_domain_connector():
    scan_config = {
        "connectors": [
            {"id": "retail-semantic", "type": "fabric_semantic", "options": {"workspace_id": "ws1", "dataset_id": "ds1"}},
            {"id": "finance-semantic", "type": "fabric_semantic", "options": {"workspace_id": "ws2", "dataset_id": "ds2"}},
        ],
        "domains": [
            {"name": "retail", "semantic_connector": "retail-semantic"},
            {"name": "finance", "semantic_connector": "finance-semantic"},
        ],
    }
    with pytest.raises(ValueError, match="not the semantic connector"):
        _resolve_connector(scan_config, domain="retail", model="finance-semantic")


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


# ── New tests for Phase 1.2 / 1.3 ────────────────────────────────────────────

def test_resolve_connector_uses_dataset_name_when_id_empty():
    scan_config = {
        "connectors": [
            {
                "id": "retail-semantic",
                "type": "fabric_semantic",
                "options": {"workspace_id": "ws-guid", "dataset_id": "", "dataset_name": "RetailModel"},
            }
        ],
        "domains": [{"name": "retail", "semantic_connector": "retail-semantic"}],
    }
    result = _resolve_connector(scan_config, domain="retail", model="retail-semantic")
    assert result["dataset_id"] == ""
    assert result["dataset_name"] == "RetailModel"
    assert result["workspace_id"] == "ws-guid"


def test_resolve_connector_raises_when_both_id_and_name_empty():
    scan_config = {
        "connectors": [
            {
                "id": "retail-semantic",
                "type": "fabric_semantic",
                "options": {"workspace_id": "ws-guid", "dataset_id": "", "dataset_name": ""},
            }
        ],
        "domains": [{"name": "retail", "semantic_connector": "retail-semantic"}],
    }
    with pytest.raises(ValueError, match="missing both dataset_id and dataset_name"):
        _resolve_connector(scan_config, domain="retail", model="retail-semantic")


def test_env_var_override_beats_file_value():
    scan_config = {
        "connectors": [
            {
                "id": "retail-semantic",
                "type": "fabric_semantic",
                "options": {"workspace_id": "file-ws", "dataset_id": "file-ds", "dataset_name": ""},
            }
        ],
        "domains": [],
    }
    with patch.dict(os.environ, {
        "CANON_FABRIC_WORKSPACE_ID": "env-ws",
        "CANON_FABRIC_DATASET_ID": "env-ds",
    }):
        result = _apply_env_overrides(scan_config)

    connector = result["connectors"][0]
    assert connector["options"]["workspace_id"] == "env-ws"
    assert connector["options"]["dataset_id"] == "env-ds"


def test_validate_scan_config_exits_on_empty_workspace_id():
    bad_config = {
        "schema_version": "1.0",
        "scanner": {"distinct_value_cap": 500, "max_workers": 4, "stale_after_hours": 168},
        "schedules": {"scan_cron": "0 6 * * 1", "timezone": "UTC"},
        "connectors": [
            {
                "id": "retail-semantic",
                "type": "fabric_semantic",
                "auth_secret_name": "SECRET",
                "options": {"workspace_id": "", "dataset_name": "RetailModel"},
            }
        ],
        "domains": [
            {
                "name": "retail",
                "path": "domains/retail",
                "owners": ["@owner"],
                "profile_dimensions": [],
                "issue_labels": ["domain:retail"],
            }
        ],
    }
    with pytest.raises(SystemExit):
        _validate_scan_config(bad_config)


async def test_resolve_dataset_id_async_matches_by_name():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "value": [
            {"id": "guid-1", "name": "OtherModel"},
            {"id": "guid-2", "name": "RetailModel"},
        ]
    }
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("serving.fabric_proxy.server.httpx.AsyncClient") as mock_async_client:
        mock_async_client.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_async_client.return_value.__aexit__ = AsyncMock(return_value=None)
        result = await _resolve_dataset_id_async("ws-guid", "RetailModel", "fake-token")

    assert result == "guid-2"


async def test_resolve_dataset_id_async_raises_on_not_found():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"value": [{"id": "guid-1", "name": "OtherModel"}]}
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("serving.fabric_proxy.server.httpx.AsyncClient") as mock_async_client:
        mock_async_client.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_async_client.return_value.__aexit__ = AsyncMock(return_value=None)
        with pytest.raises(ValueError, match="not found in workspace"):
            await _resolve_dataset_id_async("ws-guid", "RetailModel", "fake-token")
