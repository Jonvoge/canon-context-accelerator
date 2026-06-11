"""Tests for the SQL statement gate and row-cap injection."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from serving.fabric_proxy.server import (
    _apply_sql_row_cap,
    _validate_sql_statement,
)

# ── _validate_sql_statement ───────────────────────────────────────────────────


class TestValidateSqlStatement:
    def test_accepts_select(self):
        _validate_sql_statement("SELECT id, name FROM orders")

    def test_accepts_select_with_leading_whitespace(self):
        _validate_sql_statement("  SELECT * FROM sales")

    def test_accepts_with_cte(self):
        _validate_sql_statement("WITH cte AS (SELECT 1) SELECT * FROM cte")

    def test_accepts_select_case_insensitive(self):
        _validate_sql_statement("select * from t")

    def test_rejects_insert(self):
        with pytest.raises(ValueError, match="SQL must start with SELECT or WITH"):
            _validate_sql_statement("INSERT INTO t VALUES (1)")

    def test_rejects_update(self):
        with pytest.raises(ValueError, match="SQL must start with SELECT or WITH"):
            _validate_sql_statement("UPDATE t SET x = 1")

    def test_rejects_delete(self):
        with pytest.raises(ValueError, match="SQL must start with SELECT or WITH"):
            _validate_sql_statement("DELETE FROM t")

    def test_rejects_drop(self):
        with pytest.raises(ValueError, match="SQL must start with SELECT or WITH"):
            _validate_sql_statement("DROP TABLE t")

    def test_rejects_semicolon(self):
        with pytest.raises(ValueError, match="single statement"):
            _validate_sql_statement("SELECT 1; DROP TABLE t")

    def test_rejects_into_keyword(self):
        with pytest.raises(ValueError, match="forbidden"):
            _validate_sql_statement("SELECT * INTO #tmp FROM orders")

    def test_rejects_exec(self):
        with pytest.raises(ValueError, match="forbidden"):
            _validate_sql_statement("SELECT 1 EXEC xp_cmdshell('ls')")

    def test_rejects_openrowset(self):
        with pytest.raises(ValueError, match="forbidden"):
            _validate_sql_statement("SELECT * FROM OPENROWSET('provider', 'src', 'query')")

    def test_rejects_openquery(self):
        with pytest.raises(ValueError, match="forbidden"):
            _validate_sql_statement("SELECT * FROM OPENQUERY(linkedsrv, 'SELECT 1')")

    def test_into_inside_literal_is_allowed(self):
        # The word INTO inside a string literal must not trigger the gate.
        _validate_sql_statement("SELECT 'INSERT INTO' AS description FROM t")

    def test_semicolon_inside_literal_is_allowed(self):
        _validate_sql_statement("SELECT 'a;b' AS label FROM t")


# ── _apply_sql_row_cap ────────────────────────────────────────────────────────


class TestApplySqlRowCap:
    def test_injects_top_on_plain_select(self):
        result = _apply_sql_row_cap("SELECT id FROM orders", 100)
        assert result == "SELECT TOP (100) id FROM orders"

    def test_case_insensitive_select(self):
        result = _apply_sql_row_cap("select id from orders", 50)
        assert result.lower().startswith("select top (50)")

    def test_skips_injection_when_top_present(self):
        sql = "SELECT TOP (5) * FROM t"
        assert _apply_sql_row_cap(sql, 100) == sql

    def test_skips_injection_when_limit_present(self):
        sql = "SELECT * FROM t LIMIT 5"
        assert _apply_sql_row_cap(sql, 100) == sql

    def test_with_cte_injects_on_final_select(self):
        sql = "WITH cte AS (SELECT 1 AS n) SELECT n FROM cte"
        result = _apply_sql_row_cap(sql, 200)
        # TOP must appear before the outer SELECT's column list
        assert "SELECT TOP (200) n FROM cte" in result
        # The inner SELECT inside the CTE must not be modified
        assert "SELECT 1 AS n" in result

    def test_top_inside_literal_does_not_block_injection(self):
        sql = "SELECT 'TOP 10' AS label FROM t"
        result = _apply_sql_row_cap(sql, 99)
        assert "TOP (99)" in result


# ── execute_sql handler (mocked sql_exec) ────────────────────────────────────


_WAREHOUSE_SCAN_CONFIG = {
    "connectors": [
        {
            "id": "retail-sql",
            "type": "fabric_sql",
            "options": {"server": "srv.sql.azuresynapse.net", "database": "retail_dw"},
        }
    ],
    "domains": [
        {
            "name": "retail",
            "models": [
                {"connector": "retail-sql", "role": "warehouse", "primary": True},
            ],
        }
    ],
}


@pytest.mark.asyncio
async def test_execute_sql_happy_path():
    """Valid SELECT returns rows and injects row cap."""
    from serving.fabric_proxy.server import create_app

    app = create_app(_WAREHOUSE_SCAN_CONFIG)

    fake_rows = [{"OrderID": 1, "Amount": 99.0}]

    with (
        patch("serving.auth.get_user_token", return_value="bearer-tok"),
        patch("serving.fabric_proxy.sql_exec.acquire_sql_obo_token", new=AsyncMock(return_value="sql-tok")),
        patch("serving.fabric_proxy.sql_exec.execute_sql", new=AsyncMock(return_value=fake_rows)),
        patch("serving.fabric_proxy.server._load_sensitivity_async", new=AsyncMock(return_value={})),
    ):
        from mcp.types import CallToolRequest, CallToolRequestParams

        req = CallToolRequest(
            method="tools/call",
            params=CallToolRequestParams(
                name="execute_sql",
                arguments={"domain": "retail", "model": "retail-sql", "sql": "SELECT Amount FROM orders"},
            ),
        )
        handler = app.request_handlers[CallToolRequest]
        result = await handler(req)

    import json

    payload = json.loads(result.root.content[0].text)
    assert payload["role"] == "warehouse"
    assert payload["rows"] == fake_rows
    assert payload["row_count"] == 1


@pytest.mark.asyncio
async def test_execute_sql_rejects_invalid_statement():
    """The gate must reject non-SELECT statements before reaching sql_exec."""
    from serving.fabric_proxy.server import create_app

    app = create_app(_WAREHOUSE_SCAN_CONFIG)

    with (
        patch("serving.auth.get_user_token", return_value="bearer-tok"),
        patch("serving.fabric_proxy.sql_exec.acquire_sql_obo_token", new=AsyncMock()) as mock_obo,
        patch("serving.fabric_proxy.sql_exec.execute_sql", new=AsyncMock()) as mock_exec,
    ):
        from mcp.types import CallToolRequest, CallToolRequestParams

        req = CallToolRequest(
            method="tools/call",
            params=CallToolRequestParams(
                name="execute_sql",
                arguments={"domain": "retail", "model": "retail-sql", "sql": "DROP TABLE orders"},
            ),
        )
        handler = app.request_handlers[CallToolRequest]
        result = await handler(req)

        mock_obo.assert_not_called()
        mock_exec.assert_not_called()

    import json

    payload = json.loads(result.root.content[0].text)
    assert "error" in payload
