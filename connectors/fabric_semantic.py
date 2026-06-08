"""
Fabric semantic model connector via INFO.VIEW.* DAX functions (executeQueries REST API).

Auth: ClientSecretCredential from azure-identity.
Tiered backend:
  1. INFO.VIEW.* DAX table functions (primary, requires executeQueries permission)
  2. INFO.*() without VIEW prefix (fallback for older engines)
  3. Error with clear message if both fail (Scanner API requires admin, not supported)
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import ClientSecretCredential

from connectors.base import (
    BaseConnector,
    ColumnMetadata,
    MeasureMetadata,
    MetadataSnapshot,
    RelationshipMetadata,
    TableMetadata,
)

logger = logging.getLogger(__name__)

_POWER_BI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
_EXECUTE_QUERIES_URL = (
    "https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/datasets/{dataset_id}/executeQueries"
)
_DATASETS_URL = "https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/datasets"

_INFO_VIEW_MEASURES = "EVALUATE INFO.VIEW.MEASURES()"
_INFO_VIEW_TABLES = "EVALUATE INFO.VIEW.TABLES()"
_INFO_VIEW_COLUMNS = "EVALUATE INFO.VIEW.COLUMNS()"
_INFO_VIEW_RELATIONSHIPS = "EVALUATE INFO.VIEW.RELATIONSHIPS()"


def _col_name(col: str) -> str:
    """Normalize DAX column header: '[Table].[Column]' or '[Column]' → 'Column'."""
    col = col.strip("[]")
    if "." in col:
        col = col.split(".")[-1].strip("[]")
    return col


class _TokenCache:
    def __init__(self, credential: ClientSecretCredential, scope: str, refresh_buffer: int = 300) -> None:
        self._credential = credential
        self._scope = scope
        self._refresh_buffer = refresh_buffer
        self._token: str | None = None
        self._expires_at: float = 0.0

    def get(self) -> str:
        if self._token is None or time.time() >= self._expires_at - self._refresh_buffer:
            t = self._credential.get_token(self._scope)
            self._token = t.token
            self._expires_at = float(t.expires_on)
        return self._token  # type: ignore[return-value]


class FabricSemanticConnector(BaseConnector):
    """Power BI / Fabric semantic model metadata via INFO.VIEW.* DAX functions (executeQueries REST API)."""

    def __init__(self, config: dict) -> None:
        """
        config keys:
          workspace_id  (required) — GUID of the Power BI workspace
          dataset_id    (optional) — GUID of the dataset; resolved from dataset_name if omitted
          dataset_name  (optional) — name of the dataset (used if dataset_id not given)
          tenant_id     (required)
          client_id     (required)
          client_secret (required)
        """
        self.config = config
        self._token_cache: _TokenCache | None = None
        self._dataset_id: str | None = config.get("dataset_id")
        self._use_view: bool = True  # flipped False on first 400 from INFO.VIEW.*

    def _credential(self) -> ClientSecretCredential:
        return ClientSecretCredential(
            tenant_id=self.config["tenant_id"],
            client_id=self.config["client_id"],
            client_secret=self.config["client_secret"],
        )

    def _token(self) -> str:
        if self._token_cache is None:
            self._token_cache = _TokenCache(self._credential(), _POWER_BI_SCOPE)
        return self._token_cache.get()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"}

    def validate_config(self) -> list[str]:
        errors = []
        for key in ("workspace_id", "tenant_id", "client_id", "client_secret"):
            if not self.config.get(key):
                errors.append(f"Missing required config: {key}")
        if not self.config.get("dataset_id") and not self.config.get("dataset_name"):
            errors.append("Provide either dataset_id or dataset_name")
        return errors

    def test_connection(self) -> bool:
        try:
            self._token()
            return True
        except (ClientAuthenticationError, Exception):
            return False

    def _resolve_dataset_id(self) -> str:
        if self._dataset_id:
            return self._dataset_id
        workspace_id = self.config["workspace_id"]
        url = _DATASETS_URL.format(workspace_id=workspace_id)
        resp = requests.get(url, headers=self._headers(), timeout=30)
        resp.raise_for_status()
        name = self.config.get("dataset_name", "")
        for ds in resp.json().get("value", []):
            if ds.get("name") == name:
                self._dataset_id = ds["id"]
                return self._dataset_id  # type: ignore[return-value]
        raise ValueError(f"Dataset '{name}' not found in workspace {workspace_id}")

    def _execute_dax(self, query: str) -> list[dict[str, Any]]:
        workspace_id = self.config["workspace_id"]
        dataset_id = self._resolve_dataset_id()
        url = _EXECUTE_QUERIES_URL.format(workspace_id=workspace_id, dataset_id=dataset_id)
        payload = {
            "queries": [{"query": query}],
            "serializerSettings": {"includeNulls": True},
        }
        resp = requests.post(url, json=payload, headers=self._headers(), timeout=60)

        if resp.status_code == 400 and self._use_view and "INFO.VIEW." in query:
            logger.warning("INFO.VIEW.* returned 400, falling back to INFO.*() — %s", resp.text[:200])
            self._use_view = False
            return self._execute_dax(query.replace("INFO.VIEW.", "INFO."))

        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            return []

        table = results[0].get("tables", [{}])[0]
        columns = [_col_name(c["name"]) for c in table.get("columns", [])]
        rows = table.get("rows", [])
        if not rows:
            return []
        if isinstance(rows[0], dict):
            return [{_col_name(k): v for k, v in row.items()} for row in rows]
        return [dict(zip(columns, row)) for row in rows]

    def _q(self, view_query: str) -> list[dict[str, Any]]:
        query = view_query if self._use_view else view_query.replace("INFO.VIEW.", "INFO.")
        return self._execute_dax(query)

    def fetch_metadata(self) -> MetadataSnapshot:
        tables_raw = self._q(_INFO_VIEW_TABLES)
        columns_raw = self._q(_INFO_VIEW_COLUMNS)
        measures_raw = self._q(_INFO_VIEW_MEASURES)
        rels_raw = self._q(_INFO_VIEW_RELATIONSHIPS)

        table_id_to_name: dict[Any, str] = {
            t.get("ID"): (t.get("Name") or t.get("ExplicitName") or "")
            for t in tables_raw
        }

        tables = [
            TableMetadata(
                name=t.get("Name") or t.get("ExplicitName") or "",
                description=t.get("Description"),
            )
            for t in tables_raw
            if not t.get("IsHidden")
        ]

        table_col_map: dict[str, list[ColumnMetadata]] = {t.name: [] for t in tables}
        col_id_map: dict[Any, tuple[str, str]] = {}

        for col in columns_raw:
            if col.get("IsHidden"):
                continue
            tname = table_id_to_name.get(col.get("TableID"), "")
            cname = col.get("ExplicitName") or col.get("Name") or ""
            col_obj = ColumnMetadata(name=cname, table=tname, description=col.get("Description"))
            if tname in table_col_map:
                table_col_map[tname].append(col_obj)
            col_id_map[col.get("ID")] = (tname, cname)

        for t in tables:
            t.columns = table_col_map.get(t.name, [])

        measures = [
            MeasureMetadata(
                name=m.get("ExplicitName") or m.get("Name") or "",
                expression=m.get("Expression"),
                description=m.get("Description"),
                table=table_id_to_name.get(m.get("TableID")),
            )
            for m in measures_raw
            if not m.get("IsHidden")
        ]

        relationships = [
            RelationshipMetadata(
                from_table=col_id_map.get(r.get("FromColumnID"), ("", ""))[0],
                from_column=col_id_map.get(r.get("FromColumnID"), ("", ""))[1],
                to_table=col_id_map.get(r.get("ToColumnID"), ("", ""))[0],
                to_column=col_id_map.get(r.get("ToColumnID"), ("", ""))[1],
                is_active=bool(r.get("IsActive", True)),
            )
            for r in rels_raw
        ]

        return MetadataSnapshot(tables=tables, measures=measures, relationships=relationships)

    def profile_dimension(self, source_ref: str, max_values: int = 500) -> list[Any]:
        """Profile distinct values via DAX DISTINCT. source_ref: 'Table[Column]' or 'Table.Column'."""
        if "[" in source_ref:
            table, col = source_ref.split("[", 1)
            col = col.rstrip("]")
        elif "." in source_ref:
            table, col = source_ref.rsplit(".", 1)
        else:
            raise ValueError(f"Cannot parse source_ref: '{source_ref}'. Use 'Table[Column]' format.")
        dax = (
            f"EVALUATE "
            f"TOPN({max_values}, "
            f"DISTINCT(SELECTCOLUMNS('{table}', \"v\", '{table}'[{col}])), "
            f"[v], ASC)"
        )
        rows = self._execute_dax(dax)
        return [list(r.values())[0] for r in rows if list(r.values())[0] is not None]

    def supports_dimension_profiling(self) -> bool:
        return True
