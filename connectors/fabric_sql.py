"""
Fabric SQL endpoint connector via pyodbc with Azure AD token auth.

Auth: ClientSecretCredential → token for https://database.windows.net/.default
Metadata: INFORMATION_SCHEMA queries
Dimension profiling: SELECT DISTINCT
"""

from __future__ import annotations

import logging
import struct
from typing import Any

import pyodbc
from azure.identity import ClientSecretCredential

from connectors.base import (
    BaseConnector,
    ColumnMetadata,
    MetadataSnapshot,
    TableMetadata,
)

logger = logging.getLogger(__name__)

_SQL_SCOPE = "https://database.windows.net/.default"
_DRIVER = "{ODBC Driver 18 for SQL Server}"


def _get_token_bytes(token: str) -> bytes:
    """Convert Azure AD token to bytes for pyodbc SQL_COPT_SS_ACCESS_TOKEN."""
    token_bytes = token.encode("utf-16-le")
    token_struct = struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)
    return token_struct


class FabricSqlConnector(BaseConnector):
    """Fabric lakehouse/warehouse SQL endpoint metadata and fallback SQL access."""

    def __init__(self, config: dict) -> None:
        """
        config keys:
          server        (required) — SQL endpoint server name (host,port)
          database      (required) — database name
          tenant_id     (required)
          client_id     (required)
          client_secret (required)
        """
        self.config = config
        self._credential: ClientSecretCredential | None = None

    def _get_credential(self) -> ClientSecretCredential:
        if self._credential is None:
            self._credential = ClientSecretCredential(
                tenant_id=self.config["tenant_id"],
                client_id=self.config["client_id"],
                client_secret=self.config["client_secret"],
            )
        return self._credential

    def _get_token(self) -> str:
        return self._get_credential().get_token(_SQL_SCOPE).token

    def _connect(self) -> pyodbc.Connection:
        token = self._get_token()
        token_bytes = _get_token_bytes(token)
        server = self.config["server"]
        database = self.config["database"]
        conn_str = f"Driver={_DRIVER};Server={server};Database={database};Encrypt=yes;TrustServerCertificate=no;"
        conn = pyodbc.connect(conn_str, attrs_before={1256: token_bytes})
        return conn

    def validate_config(self) -> list[str]:
        errors = []
        for key in ("server", "database", "tenant_id", "client_id", "client_secret"):
            if not self.config.get(key):
                errors.append(f"Missing required config: {key}")
        return errors

    def test_connection(self) -> bool:
        try:
            conn = self._connect()
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            conn.close()
            return True
        except Exception as e:
            logger.error("SQL connection test failed: %s", e)
            return False

    def fetch_metadata(self) -> MetadataSnapshot:
        conn = self._connect()
        try:
            cursor = conn.cursor()

            # Tables and views
            cursor.execute("""
                SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE
                FROM INFORMATION_SCHEMA.TABLES
                ORDER BY TABLE_SCHEMA, TABLE_NAME
            """)
            table_rows = cursor.fetchall()

            # Columns
            cursor.execute("""
                SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE
                FROM INFORMATION_SCHEMA.COLUMNS
                ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION
            """)
            col_rows = cursor.fetchall()

            # Build tables with columns
            table_col_map: dict[str, list[ColumnMetadata]] = {}
            for schema, tname, cname, dtype in col_rows:
                key = f"{schema}.{tname}"
                if key not in table_col_map:
                    table_col_map[key] = []
                table_col_map[key].append(ColumnMetadata(name=cname, table=f"{schema}.{tname}", data_type=dtype))

            tables = []
            for schema, tname, ttype in table_rows:
                key = f"{schema}.{tname}"
                tables.append(
                    TableMetadata(
                        name=f"{schema}.{tname}",
                        columns=table_col_map.get(key, []),
                    )
                )

            return MetadataSnapshot(tables=tables)
        finally:
            conn.close()

    def profile_dimension(self, source_ref: str, max_values: int = 500) -> list[Any]:
        """Profile distinct values for a column. source_ref: 'schema.table.column'."""
        parts = source_ref.rsplit(".", 1)
        if len(parts) != 2:
            raise ValueError(f"source_ref must be 'schema.table.column', got: '{source_ref}'")
        table_ref, column = parts

        conn = self._connect()
        try:
            cursor = conn.cursor()
            query = f"SELECT DISTINCT TOP {max_values} [{column}] FROM {table_ref} ORDER BY [{column}]"
            cursor.execute(query)
            return [row[0] for row in cursor.fetchall() if row[0] is not None]
        finally:
            conn.close()

    def execute_sql(self, sql: str) -> list[dict[str, Any]]:
        """Execute arbitrary SQL and return rows as dicts."""
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(sql)
            columns = [col[0] for col in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        finally:
            conn.close()

    def supports_dimension_profiling(self) -> bool:
        return True
