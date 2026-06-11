from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from serving.fabric_proxy.server import (
    _apply_env_overrides,
    _call_fabric_execute_queries,
    _resolve_connector,
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
        "domains": [
            {
                "name": "retail",
                "semantic_connector": "retail-semantic",
                "models": [{"connector": "retail-semantic", "role": "semantic", "primary": True}],
            }
        ],
    }
    result = _resolve_connector(scan_config, domain="retail", model="retail-semantic")
    assert result == {
        "workspace_id": "ws-guid",
        "dataset_id": "ds-guid",
        "dataset_name": "",
        "connector_id": "retail-semantic",
        "role": "semantic",
    }


def test_resolve_connector_errors_on_unknown_model():
    scan_config = {
        "connectors": [
            {
                "id": "retail-semantic",
                "type": "fabric_semantic",
                "options": {"workspace_id": "ws-guid", "dataset_id": "ds-guid"},
            }
        ],
        "domains": [
            {
                "name": "retail",
                "semantic_connector": "retail-semantic",
                "models": [{"connector": "retail-semantic", "role": "semantic", "primary": True}],
            }
        ],
    }
    with pytest.raises(ValueError, match="not registered for domain"):
        _resolve_connector(scan_config, domain="retail", model="nonexistent")


def test_resolve_connector_rejects_wrong_domain_connector():
    scan_config = {
        "connectors": [
            {
                "id": "retail-semantic",
                "type": "fabric_semantic",
                "options": {"workspace_id": "ws1", "dataset_id": "ds1"},
            },
            {
                "id": "finance-semantic",
                "type": "fabric_semantic",
                "options": {"workspace_id": "ws2", "dataset_id": "ds2"},
            },
        ],
        "domains": [
            {
                "name": "retail",
                "semantic_connector": "retail-semantic",
                "models": [{"connector": "retail-semantic", "role": "semantic", "primary": True}],
            },
            {
                "name": "finance",
                "semantic_connector": "finance-semantic",
                "models": [{"connector": "finance-semantic", "role": "semantic", "primary": True}],
            },
        ],
    }
    with pytest.raises(ValueError, match="not registered for domain"):
        _resolve_connector(scan_config, domain="retail", model="finance-semantic")


async def test_execute_query_calls_fabric_api():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {}
    mock_response.json.return_value = {"results": [{"tables": [{"rows": [{"[Revenue]": 1000}]}]}]}

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
        "domains": [
            {
                "name": "retail",
                "semantic_connector": "retail-semantic",
                "models": [{"connector": "retail-semantic", "role": "semantic", "primary": True}],
            }
        ],
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
        "domains": [
            {
                "name": "retail",
                "semantic_connector": "retail-semantic",
                "models": [{"connector": "retail-semantic", "role": "semantic", "primary": True}],
            }
        ],
    }
    with pytest.raises(ValueError, match="missing both dataset_id and dataset_name"):
        _resolve_connector(scan_config, domain="retail", model="retail-semantic")


def test_env_var_override_logs_warning_and_leaves_config_unchanged(caplog):
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
    with patch.dict(
        os.environ,
        {
            "CANON_FABRIC_WORKSPACE_ID": "env-ws",
            "CANON_FABRIC_DATASET_ID": "env-ds",
        },
    ):
        result = _apply_env_overrides(scan_config)

    assert result == scan_config
    assert "Ignoring deprecated topology env vars" in caplog.text


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


def test_create_app_returns_server():
    from serving.fabric_proxy.server import create_app

    minimal_config = {
        "connectors": [
            {
                "id": "retail-semantic",
                "type": "fabric_semantic",
                "auth_secret_name": "S",
                "options": {"workspace_id": "ws", "dataset_id": "ds"},
            }
        ],
        "domains": [
            {
                "name": "retail",
                "semantic_connector": "retail-semantic",
                "models": [{"connector": "retail-semantic", "role": "semantic", "primary": True}],
            }
        ],
    }
    server = create_app(minimal_config)
    assert server is not None


def test_create_app_has_execute_metric_and_execute_query():
    import asyncio

    from serving.fabric_proxy.server import create_app

    minimal_config = {
        "connectors": [
            {
                "id": "retail-semantic",
                "type": "fabric_semantic",
                "auth_secret_name": "S",
                "options": {"workspace_id": "ws", "dataset_id": "ds"},
            }
        ],
        "domains": [
            {
                "name": "retail",
                "semantic_connector": "retail-semantic",
                "models": [{"connector": "retail-semantic", "role": "semantic", "primary": True}],
            }
        ],
    }
    server = create_app(minimal_config)
    from mcp.types import ListToolsRequest

    result = asyncio.run(server.request_handlers[ListToolsRequest](ListToolsRequest()))
    tool_names = {t.name for t in result.root.tools}
    assert "execute_metric" in tool_names
    assert "execute_query" in tool_names


def test_resolve_connector_defaults_to_primary_semantic():
    scan_config = {
        "connectors": [
            {
                "id": "retail-semantic",
                "type": "fabric_semantic",
                "options": {"workspace_id": "ws1", "dataset_id": "ds1"},
            },
            {"id": "retail-sql", "type": "fabric_sql", "options": {"server": "srv", "database": "db"}},
        ],
        "domains": [
            {
                "name": "retail",
                "models": [
                    {"connector": "retail-semantic", "role": "semantic", "primary": True},
                    {"connector": "retail-sql", "role": "warehouse"},
                ],
            }
        ],
    }
    result = _resolve_connector(scan_config, domain="retail", model=None)
    assert result["connector_id"] == "retail-semantic"
    assert result["role"] == "semantic"


def test_resolve_connector_allows_registered_warehouse_model():
    scan_config = {
        "connectors": [
            {
                "id": "retail-semantic",
                "type": "fabric_semantic",
                "options": {"workspace_id": "ws1", "dataset_id": "ds1"},
            },
            {"id": "retail-sql", "type": "fabric_sql", "options": {"server": "srv", "database": "db"}},
        ],
        "domains": [
            {
                "name": "retail",
                "models": [
                    {"connector": "retail-semantic", "role": "semantic", "primary": True},
                    {"connector": "retail-sql", "role": "warehouse"},
                ],
            }
        ],
    }
    result = _resolve_connector(scan_config, domain="retail", model="retail-sql")
    assert result["connector_id"] == "retail-sql"
    assert result["role"] == "warehouse"


# ──────────────────────────────────────────────────────────────────────────────
# _find_pattern — cross-role fallback removed
# ──────────────────────────────────────────────────────────────────────────────


def _make_metric(name: str, patterns: list[dict]) -> dict:
    return {"name": name, "usage_patterns": patterns}


def test_find_pattern_warehouse_errors_when_no_sql_pattern():
    """warehouse connector must not silently fall back to a DAX pattern."""
    from serving.fabric_proxy.server import _find_pattern

    metrics = [_make_metric("Revenue", [{"pattern_id": "p1", "dax": "EVALUATE ROW(...)"}])]
    with pytest.raises(ValueError, match="no compatible usage_patterns"):
        _find_pattern(metrics, "Revenue", None, connector_role="warehouse")


def test_find_pattern_semantic_errors_when_no_dax_pattern():
    """semantic connector must not silently fall back to a SQL pattern."""
    from serving.fabric_proxy.server import _find_pattern

    metrics = [_make_metric("Revenue", [{"pattern_id": "p1", "sql": "SELECT 1"}])]
    with pytest.raises(ValueError, match="no compatible usage_patterns"):
        _find_pattern(metrics, "Revenue", None, connector_role="semantic")


def test_find_pattern_warehouse_selects_sql_pattern():
    from serving.fabric_proxy.server import _find_pattern

    metrics = [
        _make_metric(
            "Revenue",
            [
                {"pattern_id": "sql-p", "sql": "SELECT SUM(revenue) FROM sales"},
                {"pattern_id": "dax-p", "dax": "EVALUATE ROW(...)"},
            ],
        )
    ]
    _, pattern = _find_pattern(metrics, "Revenue", None, connector_role="warehouse")
    assert pattern["pattern_id"] == "sql-p"


def test_find_pattern_semantic_selects_dax_pattern():
    from serving.fabric_proxy.server import _find_pattern

    metrics = [
        _make_metric(
            "Revenue",
            [
                {"pattern_id": "sql-p", "sql": "SELECT SUM(revenue) FROM sales"},
                {"pattern_id": "dax-p", "dax": "EVALUATE ROW(...)"},
            ],
        )
    ]
    _, pattern = _find_pattern(metrics, "Revenue", None, connector_role="semantic")
    assert pattern["pattern_id"] == "dax-p"


# ──────────────────────────────────────────────────────────────────────────────
# _get_discrepancy_notice
# ──────────────────────────────────────────────────────────────────────────────


def test_get_discrepancy_notice_matches_by_model_name():
    from serving.fabric_proxy.server import _get_discrepancy_notice

    metric_entry = {
        "name": "Revenue",
        "governed_sources": {
            "also_exists_in": [
                {"model": "Finance Reporting", "known_discrepancy": "Overstates 2-4%. JIRA-4521."},
            ]
        },
    }
    notice = _get_discrepancy_notice(metric_entry, "Finance Reporting")
    assert notice == "Overstates 2-4%. JIRA-4521."


def test_get_discrepancy_notice_case_insensitive():
    from serving.fabric_proxy.server import _get_discrepancy_notice

    metric_entry = {
        "name": "Revenue",
        "governed_sources": {
            "also_exists_in": [
                {"model": "Finance Reporting", "known_discrepancy": "Some discrepancy."},
            ]
        },
    }
    assert _get_discrepancy_notice(metric_entry, "finance reporting") == "Some discrepancy."


def test_get_discrepancy_notice_returns_none_when_no_match():
    from serving.fabric_proxy.server import _get_discrepancy_notice

    metric_entry = {
        "name": "Revenue",
        "governed_sources": {
            "also_exists_in": [{"model": "Other Model", "known_discrepancy": "Some note."}]
        },
    }
    assert _get_discrepancy_notice(metric_entry, "RetailSemanticModel") is None


def test_get_discrepancy_notice_returns_none_when_no_discrepancy_field():
    from serving.fabric_proxy.server import _get_discrepancy_notice

    metric_entry = {
        "name": "Revenue",
        "governed_sources": {"also_exists_in": [{"model": "Finance Reporting"}]},
    }
    assert _get_discrepancy_notice(metric_entry, "Finance Reporting") is None


def test_get_discrepancy_notice_returns_none_when_no_also_exists_in():
    from serving.fabric_proxy.server import _get_discrepancy_notice

    metric_entry = {"name": "Revenue", "governed_sources": {}}
    assert _get_discrepancy_notice(metric_entry, "Finance Reporting") is None
