"""OBO-authenticated SQL execution against Fabric SQL endpoints."""

from __future__ import annotations

import asyncio
import logging
import struct
from typing import Any

logger = logging.getLogger(__name__)

_SQL_SCOPE = "https://database.windows.net/.default"


def _get_token_bytes(token: str) -> bytes:
    """Pack an access token into the byte format pyodbc expects for SQL_COPT_SS_ACCESS_TOKEN."""
    encoded = token.encode("utf-16-le")
    return struct.pack(f"<I{len(encoded)}s", len(encoded), encoded)


async def acquire_sql_obo_token(user_token: str, tenant_id: str, client_id: str, client_secret: str) -> str:
    """Acquire an OBO token scoped to Azure SQL / Fabric SQL."""
    import msal

    obo_app = msal.ConfidentialClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
        client_credential=client_secret,
    )
    result = await asyncio.to_thread(
        obo_app.acquire_token_on_behalf_of,
        user_assertion=user_token,
        scopes=[_SQL_SCOPE],
    )
    if not result or "access_token" not in result:
        raise PermissionError("SQL OBO token acquisition failed")
    return result["access_token"]


def _execute_sql_sync(server: str, database: str, token: str, sql: str, row_cap: int) -> list[dict[str, Any]]:
    import pyodbc

    token_bytes = _get_token_bytes(token)
    conn_str = (
        f"DRIVER={{ODBC Driver 18 for SQL Server}};"
        f"SERVER={server};"
        f"DATABASE={database};"
        "Encrypt=yes;TrustServerCertificate=no;LoginTimeout=10;"
    )
    sql_copt_ss_access_token = 1256
    with pyodbc.connect(conn_str, attrs_before={sql_copt_ss_access_token: token_bytes}) as conn:
        conn.timeout = 30
        cursor = conn.cursor()
        cursor.execute(sql)
        columns = [col[0] for col in cursor.description]
        rows = []
        for row in cursor.fetchmany(row_cap):
            rows.append(dict(zip(columns, row)))
    return rows


async def execute_sql(server: str, database: str, token: str, sql: str, row_cap: int = 10000) -> list[dict[str, Any]]:
    """Execute a SQL query asynchronously."""
    return await asyncio.to_thread(_execute_sql_sync, server, database, token, sql, row_cap)
