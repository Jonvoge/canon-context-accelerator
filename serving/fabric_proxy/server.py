from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import httpx
import jsonschema
import msal
import yaml
from mcp.server import Server
from mcp.types import TextContent, Tool

from canon.config import _normalize, load_scan_config

logger = logging.getLogger(__name__)

_EXECUTE_QUERIES_URL = "https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/datasets/{dataset_id}/executeQueries"
_DATASETS_URL = "https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/datasets"
_POWER_BI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
_SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "schemas" / "scan-config.schema.json"
_REMOTE_FILE_CACHE: dict[str, tuple[float, str | None]] = {}
_REMOTE_FILE_CACHE_TTL_SECONDS = 60
_MISSING_SENSITIVITY_WARNED: set[str] = set()


def _load_scan_config(repo_root: Path | None = None) -> dict:
    if repo_root is not None:
        path = repo_root / "scan-config.yaml"
    else:
        path = Path(os.environ.get("CANON_SCAN_CONFIG", "scan-config.yaml"))
    return load_scan_config(path)


def _apply_env_overrides(scan_config: dict) -> dict:
    """Topology env vars are deprecated; return the file config unchanged."""
    deprecated = [
        name
        for name in (
            "CANON_FABRIC_WORKSPACE_ID",
            "CANON_FABRIC_DATASET_ID",
            "CANON_FABRIC_DATASET_NAME",
            "CANON_SQL_SERVER",
            "CANON_SQL_DATABASE",
        )
        if os.environ.get(name)
    ]
    if deprecated:
        logger.warning(
            "Ignoring deprecated topology env vars; move topology to scan-config.yaml: %s",
            ", ".join(deprecated),
        )
    return scan_config


async def _load_scan_config_from_repo() -> dict:
    """Load scan-config.yaml from the GitHub repo via repo_client, then apply env overrides.

    Used by the HTTP server (Container App) so config changes only require a git push,
    not an image rebuild. Falls back to local file if repo env vars are not set.
    """
    from serving.repo_client import RepoClient, RepoConfig

    provider = os.environ.get("CANON_REPO_PROVIDER")
    if not provider:
        cfg = _load_scan_config()
        return _apply_env_overrides(cfg)

    config = RepoConfig(
        provider=provider,
        owner=os.environ.get("CANON_REPO_OWNER", ""),
        repo=os.environ.get("CANON_REPO_NAME", ""),
        token=os.environ.get("CANON_REPO_TOKEN", ""),
        branch=os.environ.get("CANON_REPO_BRANCH", "main"),
        org=os.environ.get("CANON_REPO_ADO_ORG", ""),
        project=os.environ.get("CANON_REPO_ADO_PROJECT", ""),
    )
    client = RepoClient(config)
    try:
        raw = await client.fetch_file("scan-config.yaml")
        cfg = _normalize(yaml.safe_load(raw) or {})
    finally:
        await client.aclose()

    return _apply_env_overrides(cfg)


def _validate_scan_config(scan_config: dict) -> None:
    """Validate scan_config against the JSON schema. Logs all violations and exits on failure."""
    if not _SCHEMA_PATH.exists():
        logger.warning("scan-config schema not found at %s — skipping validation", _SCHEMA_PATH)
        return
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    errors = list(validator.iter_errors(scan_config))
    if not errors:
        return
    logger.error("scan-config.yaml failed schema validation — %d error(s):", len(errors))
    for err in errors:
        pointer = " → ".join(str(p) for p in err.absolute_path) if err.absolute_path else "root"
        logger.error("  %s: %s  (fix: scan-config.yaml → %s)", pointer, err.message, pointer)
    sys.exit(1)


def _resolve_connector(scan_config: dict, domain: str, model: str | None) -> dict[str, str]:
    """Resolve a domain model to connector metadata."""
    scan_config = _normalize(scan_config)
    domains = scan_config.get("domains", [])
    domain_cfg = next((d for d in domains if d.get("name") == domain), None)
    if domain_cfg is None:
        raise ValueError(f"Domain '{domain}' not found in scan-config.yaml")

    models = domain_cfg.get("models", [])
    if not models:
        raise ValueError(f"Domain '{domain}' has no configured models")

    if not model:
        primary = next((m for m in models if m.get("role") == "semantic" and m.get("primary")), None)
        if primary is None:
            primary = next((m for m in models if m.get("role") == "semantic"), None)
        if primary is None:
            raise ValueError(f"Domain '{domain}' has no semantic model configured")
        selected = primary
    else:
        selected = next((m for m in models if m.get("connector") == model), None)
        if selected is None:
            allowed = [m.get("connector", "") for m in models]
            raise ValueError(
                f"Connector '{model}' is not registered for domain '{domain}'. Allowed models: {allowed}. "
                "Use available_models from CanonMCP get_domain_context."
            )

    connector_id = selected.get("connector", "")
    connectors = scan_config.get("connectors", [])
    connector = next((c for c in connectors if c.get("id") == connector_id), None)
    if connector is None:
        raise ValueError(f"Connector '{connector_id}' not found in scan-config.yaml")

    options = connector.get("options", {})
    workspace_id = options.get("workspace_id", "")
    dataset_id = options.get("dataset_id", "")
    dataset_name = options.get("dataset_name", "")

    if selected.get("role") == "semantic":
        if not workspace_id:
            raise ValueError(
                f"Connector '{connector_id}' missing workspace_id. Fill options.workspace_id in scan-config.yaml and redeploy."
            )
        if not dataset_id and not dataset_name:
            raise ValueError(
                f"Connector '{connector_id}' missing both dataset_id and dataset_name. Fill options.dataset_id (or dataset_name) in scan-config.yaml and redeploy."
            )

    return {
        "workspace_id": workspace_id,
        "dataset_id": dataset_id,
        "dataset_name": dataset_name,
        "connector_id": connector_id,
        "role": selected.get("role", "semantic"),
    }


async def _fetch_repo_text(path: str) -> str | None:
    provider = os.environ.get("CANON_REPO_PROVIDER")
    if not provider:
        return None
    now = time.time()
    cached = _REMOTE_FILE_CACHE.get(path)
    if cached and (now - cached[0]) < _REMOTE_FILE_CACHE_TTL_SECONDS:
        return cached[1]
    from serving.repo_client import RepoClient, RepoConfig

    client = RepoClient(
        RepoConfig(
            provider=provider,
            owner=os.environ.get("CANON_REPO_OWNER", ""),
            repo=os.environ.get("CANON_REPO_NAME", ""),
            token=os.environ.get("CANON_REPO_TOKEN", ""),
            branch=os.environ.get("CANON_REPO_BRANCH", "main"),
            org=os.environ.get("CANON_REPO_ADO_ORG", ""),
            project=os.environ.get("CANON_REPO_ADO_PROJECT", ""),
        ),
        cache_ttl_seconds=_REMOTE_FILE_CACHE_TTL_SECONDS,
    )
    try:
        content = await client.fetch_file_or_none(path)
    finally:
        await client.aclose()
    _REMOTE_FILE_CACHE[path] = (now, content)
    return content


async def _load_domain_metrics_async(domain: str, repo_root: Path | None = None) -> list[dict]:
    remote = await _fetch_repo_text(f"domains/{domain}/metrics.yaml")
    if remote is not None:
        return (yaml.safe_load(remote) or {}).get("metrics", [])
    return _load_domain_metrics(domain, repo_root)


async def _load_sensitivity_async(domain: str, repo_root: Path | None = None) -> dict:
    remote = await _fetch_repo_text(f"domains/{domain}/sensitivity.yaml")
    if remote is not None:
        try:
            return yaml.safe_load(remote) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid sensitivity.yaml for domain '{domain}': {exc}") from exc
    return _load_sensitivity(domain, repo_root)


async def _resolve_dataset_id_async(workspace_id: str, dataset_name: str, pbi_token: str) -> str:
    """Look up a dataset GUID by name via the Power BI REST API."""
    url = _DATASETS_URL.format(workspace_id=workspace_id)
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers={"Authorization": f"Bearer {pbi_token}"}, timeout=30)
    if resp.status_code != 200:
        raise ValueError(
            f"Could not list datasets in workspace '{workspace_id}': HTTP {resp.status_code}. "
            f"Alternatively, set options.dataset_id in scan-config.yaml to avoid this lookup."
        )
    for ds in resp.json().get("value", []):
        if ds.get("name") == dataset_name:
            return ds["id"]
    raise ValueError(
        f"Dataset '{dataset_name}' not found in workspace '{workspace_id}'. "
        f"Verify dataset_name in scan-config.yaml or set options.dataset_id directly."
    )


async def _acquire_obo_token(user_token: str) -> str:
    tenant_id = os.environ.get("CANON_AUTH_TENANT_ID", "")
    client_id = os.environ.get("CANON_AUTH_CLIENT_ID", "")
    client_secret = os.environ.get("CANON_AUTH_CLIENT_SECRET", "")

    obo_app = msal.ConfidentialClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
        client_credential=client_secret,
    )
    result = await asyncio.to_thread(
        obo_app.acquire_token_on_behalf_of,
        user_assertion=user_token,
        scopes=[_POWER_BI_SCOPE],
    )
    if not result or "access_token" not in result:
        raise PermissionError("OBO token acquisition failed")
    return result["access_token"]


async def _call_fabric_execute_queries(workspace_id: str, dataset_id: str, dax: str, user_token: str) -> dict:
    url = _EXECUTE_QUERIES_URL.format(workspace_id=workspace_id, dataset_id=dataset_id)
    payload = {
        "queries": [{"query": dax}],
        "serializerSettings": {"includeNulls": True},
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {user_token}",
                "Content-Type": "application/json",
            },
        )
    if resp.status_code == 401:
        raise PermissionError("Fabric API returned 401 Unauthorized")
    if resp.status_code == 400:
        try:
            error_detail = resp.json()
        except Exception:
            error_detail = resp.text
        raise ValueError(f"DAX error: {error_detail}")
    return resp.json()


# ── Sensitivity enforcement ───────────────────────────────────────────────────


def _load_sensitivity(domain: str, repo_root: Path | None = None) -> dict:
    """Load sensitivity.yaml for a domain."""
    if repo_root is None:
        repo_root = Path(os.environ.get("CANON_REPO_ROOT", "."))
    path = repo_root / "domains" / domain / "sensitivity.yaml"
    if not path.exists():
        if domain not in _MISSING_SENSITIVITY_WARNED:
            logger.warning("No sensitivity.yaml found for domain '%s'; proceeding without advisory redaction", domain)
            _MISSING_SENSITIVITY_WARNED.add(domain)
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid sensitivity.yaml for domain '{domain}': {exc}") from exc


def _get_blocked_columns(sensitivity: dict) -> set[str]:
    """Return lowercased column names that must never appear in results."""
    blocked = set()
    for override in sensitivity.get("column_overrides", []):
        if override.get("pii") or override.get("classification") == "restricted":
            col = override.get("column", "")
            if "." in col:
                col = col.split(".")[-1]
            blocked.add(col.lower())
    for rule in sensitivity.get("usage_rules", []):
        if rule.get("forbid_raw_values"):
            for target in rule.get("applies_to", []):
                blocked.add(target.lower())
    return blocked


def _enforce_sensitivity(rows: list[dict], sensitivity: dict) -> tuple[list[dict], list[str]]:
    """Remove blocked columns from rows and return (cleaned_rows, notices)."""
    blocked = _get_blocked_columns(sensitivity)
    if not blocked:
        return rows, []

    notices = []
    cleaned = []
    for row in rows:
        clean_row = {}
        for k, v in row.items():
            # DAX result keys are like "[ColumnName]" or "[Table][ColumnName]"
            col_bare = k.strip("[]").split("][")[-1].lower()
            if col_bare in blocked:
                if not notices:
                    notices.append(f"Column(s) blocked by sensitivity rules: {k}")
            else:
                clean_row[k] = v
        cleaned.append(clean_row)

    return cleaned, notices


# ── execute_metric helpers ────────────────────────────────────────────────────

_PARAM_RE = re.compile(r"\{([A-Z0-9_]+)\}")
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9 _\-\.%]+$")
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _load_domain_metrics(domain: str, repo_root: Path | None = None) -> list[dict]:
    if repo_root is None:
        repo_root = Path(os.environ.get("CANON_REPO_ROOT", "."))
    path = repo_root / "domains" / domain / "metrics.yaml"
    if not path.exists():
        raise ValueError(f"Domain '{domain}' not found at {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("metrics", [])


def _find_pattern(
    metrics: list[dict], metric: str, group_by: str | None, connector_role: str | None = None
) -> tuple[dict, dict]:
    """Return (metric_entry, pattern) best matching the request."""
    metric_entry = next((m for m in metrics if m["name"].lower() == metric.lower()), None)
    if metric_entry is None:
        names = [m["name"] for m in metrics]
        raise ValueError(f"Metric '{metric}' not found. Available: {names}")

    patterns = metric_entry.get("usage_patterns", [])
    if connector_role == "warehouse":
        preferred = [pattern for pattern in patterns if pattern.get("sql")]
        fallback_key = "sql"
    else:
        preferred = [pattern for pattern in patterns if pattern.get("dax")]
        fallback_key = "dax"

    if not preferred:
        preferred = [pattern for pattern in patterns if pattern.get(fallback_key)]
    if not preferred and connector_role == "warehouse":
        preferred = [pattern for pattern in patterns if pattern.get("dax")]
    if not preferred and connector_role == "semantic":
        preferred = [pattern for pattern in patterns if pattern.get("sql")]
    if not preferred:
        raise ValueError(
            f"Metric '{metric}' has no compatible usage_patterns for role '{connector_role or 'semantic'}'."
        )

    if group_by:
        col_lower = group_by.lower()
        key = "sql" if connector_role == "warehouse" else "dax"
        for pattern in preferred:
            query = str(pattern.get(key, pattern.get("dax", pattern.get("sql", ""))))
            if col_lower in query.lower():
                return metric_entry, pattern
    return metric_entry, preferred[0]


def _substitute_params(dax_template: str, params: dict[str, str]) -> str:
    """Substitute {PARAM} tokens in a DAX template with validated values.

    Only the keys present in the template are substituted. All values are
    validated: dates as ISO 8601, identifiers against a strict allow-list.
    """
    used_keys = {m.group(1) for m in _PARAM_RE.finditer(dax_template)}

    def replace(match: re.Match) -> str:
        key = match.group(1)
        if key not in params:
            raise ValueError(
                f"DAX pattern requires parameter {{{key}}} but it was not provided. Required params: " + str(used_keys)
            )
        value = str(params[key])
        # Date component params (YEAR/MONTH/DAY) — must be numeric
        if key.endswith(("_YEAR", "_MONTH", "_DAY")):
            if not value.isdigit():
                raise ValueError(f"Parameter {{{key}}} must be a number, got: {value!r}")
            return value
        # ISO date params — parse and convert to year/month/day inline
        if key in ("START", "END"):
            m = _ISO_DATE_RE.match(value)
            if not m:
                raise ValueError(f"Parameter {{{key}}} must be ISO date (YYYY-MM-DD), got: {value!r}")
            return value  # used raw in SQL patterns only
        # Identifier params (column names, dimension values)
        if not _SAFE_IDENTIFIER_RE.match(value):
            raise ValueError(
                f"Parameter {{{key}}} contains unsafe characters: {value!r}. "
                "Only alphanumeric, space, underscore, hyphen, dot, % allowed."
            )
        return value

    return _PARAM_RE.sub(replace, dax_template)


def _parse_iso_to_parts(iso: str, prefix: str) -> dict[str, str]:
    """Convert '2025-01-31' to {'PREFIX_YEAR': '2025', 'PREFIX_MONTH': '01', 'PREFIX_DAY': '31'}."""
    m = _ISO_DATE_RE.match(iso)
    if not m:
        raise ValueError(f"Expected ISO date YYYY-MM-DD, got: {iso!r}")
    return {
        f"{prefix}_YEAR": m.group(1),
        f"{prefix}_MONTH": m.group(2),
        f"{prefix}_DAY": m.group(3),
    }


def _get_domain_entry(scan_config: dict, domain: str) -> dict:
    scan_config = _normalize(scan_config)
    domain_cfg = next((d for d in scan_config.get("domains", []) if d.get("name") == domain), None)
    if domain_cfg is None:
        raise ValueError(f"Domain '{domain}' not found in scan-config.yaml")
    return domain_cfg


def _get_connector_config(scan_config: dict, connector_id: str) -> dict:
    connector = next((c for c in scan_config.get("connectors", []) if c.get("id") == connector_id), None)
    if connector is None:
        raise ValueError(f"Connector '{connector_id}' not found in scan-config.yaml")
    return connector


def _get_domain_sql_row_cap(scan_config: dict, domain: str) -> int:
    domain_cfg = _get_domain_entry(scan_config, domain)
    value = domain_cfg.get("sql_row_cap", 10000)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 10000


def _validate_sql_statement(sql: str) -> None:
    stripped_literals = re.sub(r"'[^']*'", "''", sql)
    if not re.match(r"^\s*(WITH|SELECT)\b", stripped_literals, flags=re.IGNORECASE):
        raise ValueError("SQL must start with SELECT or WITH")
    if ";" in stripped_literals:
        raise ValueError("SQL must be a single statement")
    if re.search(r"\b(INTO|EXEC|OPENROWSET|OPENQUERY)\b", stripped_literals, flags=re.IGNORECASE):
        raise ValueError("SQL contains forbidden statements or functions")


def _apply_sql_row_cap(sql: str, row_cap: int) -> str:
    stripped_literals = re.sub(r"'[^']*'", "''", sql)
    if re.search(r"\b(TOP|LIMIT)\b", stripped_literals, flags=re.IGNORECASE):
        return sql
    matches = list(re.finditer(r"\bSELECT\b", sql, flags=re.IGNORECASE))
    if not matches:
        return sql
    target = matches[-1] if stripped_literals.lstrip().upper().startswith("WITH") else matches[0]
    idx = target.end()
    return sql[:idx] + f" TOP ({row_cap})" + sql[idx:]


def create_app(scan_config: dict, repo_root: Path | None = None) -> Server:
    app = Server(
        "canon-fabric-proxy",
        instructions=(
            "You are connected to the Canon Fabric Proxy. Protocol: "
            "1) Call CanonMCP list_domains to discover available domains. "
            "2) Call CanonMCP get_domain_context(domain) to get metrics definitions, model_schema, and available_models. "
            "3) Pick model from available_models using its description; prefer primary: true for governed aggregates. "
            "4) Use execute_metric for governed answers, execute_query for raw DAX on semantic models, and execute_sql for warehouse detail only after Canon context says SQL is appropriate. "
            "Do NOT fall back to other Fabric tools for governed domains."
        ),
    )

    sensitivity_cache: dict[str, dict] = {}

    async def _get_sensitivity(domain: str) -> dict:
        if domain not in sensitivity_cache:
            sensitivity_cache[domain] = await _load_sensitivity_async(domain, repo_root)
        return sensitivity_cache[domain]

    def _get_user_token_or_error() -> str | list[TextContent]:
        from serving.auth import get_user_token

        user_token = get_user_token()
        if user_token is None:
            return [TextContent(type="text", text=json.dumps({"error": "No user token available"}))]
        return user_token

    @app.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="execute_metric",
                description=(
                    "Execute a governed Canon metric query. Uses DAX for semantic models or authored SQL patterns for warehouse models. "
                    "Call CanonMCP get_domain_context first to discover metric names and available_models."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "domain": {"type": "string", "description": "Domain slug from CanonMCP, e.g. 'retail'."},
                        "model": {
                            "type": "string",
                            "description": "Optional connector id from CanonMCP available_models. Defaults to the primary semantic model.",
                        },
                        "metric": {"type": "string", "description": "Metric name exactly as defined in Canon."},
                        "group_by": {"type": "string", "description": "Optional dimension column to group by."},
                        "period_start": {
                            "type": "string",
                            "description": "Optional ISO date filter start, e.g. '2025-01-01'.",
                        },
                        "period_end": {
                            "type": "string",
                            "description": "Optional ISO date filter end, e.g. '2025-12-31'.",
                        },
                    },
                    "required": ["domain", "metric"],
                },
            ),
            Tool(
                name="execute_query",
                description=(
                    "Execute a raw DAX query against a Fabric semantic model. Use execute_metric instead for defined metrics. "
                    "Call CanonMCP get_domain_context first. governed:false in response."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "domain": {"type": "string", "description": "Domain slug from CanonMCP, e.g. 'retail'."},
                        "model": {
                            "type": "string",
                            "description": "Optional semantic connector id from CanonMCP available_models.",
                        },
                        "dax": {"type": "string", "description": "DAX query starting with EVALUATE."},
                    },
                    "required": ["domain", "dax"],
                },
            ),
            Tool(
                name="execute_sql",
                description=(
                    "Execute a raw SQL query against a governed Fabric SQL endpoint. Use only after execute_metric cannot answer. "
                    "Call CanonMCP get_domain_context first; honor domain rules. governed:false in response."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "domain": {"type": "string", "description": "Domain slug from CanonMCP, e.g. 'retail'."},
                        "model": {
                            "type": "string",
                            "description": "Warehouse connector id from CanonMCP available_models.",
                        },
                        "sql": {"type": "string", "description": "Single SELECT or WITH query."},
                    },
                    "required": ["domain", "model", "sql"],
                },
            ),
        ]

    @app.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        if name == "execute_metric":
            return await _handle_execute_metric(arguments)
        if name == "execute_query":
            return await _handle_execute_query(arguments)
        if name == "execute_sql":
            return await _handle_execute_sql(arguments)
        return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

    async def _get_obo_and_dataset(domain: str, model: str) -> tuple[str, str, str] | list[TextContent]:
        try:
            connector_info = _resolve_connector(scan_config, domain, model)
        except ValueError as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

        user_token = _get_user_token_or_error()
        if isinstance(user_token, list):
            return user_token

        try:
            obo_token = await _acquire_obo_token(user_token)
        except PermissionError as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

        dataset_id = connector_info["dataset_id"]
        if not dataset_id:
            try:
                dataset_id = await _resolve_dataset_id_async(
                    connector_info["workspace_id"], connector_info["dataset_name"], obo_token
                )
            except ValueError as exc:
                return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

        return connector_info["workspace_id"], dataset_id, obo_token

    async def _handle_execute_metric(arguments: dict) -> list[TextContent]:
        domain = arguments.get("domain", "")
        model = arguments.get("model")
        metric = arguments.get("metric", "")
        group_by = arguments.get("group_by", "")
        period_start = arguments.get("period_start", "")
        period_end = arguments.get("period_end", "")

        if not domain or not metric:
            return [TextContent(type="text", text=json.dumps({"error": "domain and metric are required"}))]

        try:
            metrics = await _load_domain_metrics_async(domain, repo_root)
            connector_info = _resolve_connector(scan_config, domain, model)
            connector_cfg = _get_connector_config(scan_config, connector_info["connector_id"])
            metric_entry, pattern = _find_pattern(metrics, metric, group_by or None, connector_info["role"])
        except ValueError as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

        params: dict[str, str] = {}
        if period_start:
            params.update(_parse_iso_to_parts(period_start, "START"))
            params["START"] = period_start
        if period_end:
            params.update(_parse_iso_to_parts(period_end, "END"))
            params["END"] = period_end
        if group_by:
            params["GROUP_BY_COLUMN"] = group_by

        try:
            sensitivity = await _get_sensitivity(domain)
        except ValueError as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

        if connector_info["role"] == "warehouse":
            sql_template = pattern.get("sql")
            if not sql_template:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps({"error": f"Metric '{metric}' has no SQL pattern for warehouse execution"}),
                    )
                ]
            try:
                sql = _substitute_params(sql_template, params)
                user_token = _get_user_token_or_error()
                if isinstance(user_token, list):
                    return user_token
                from serving.fabric_proxy import sql_exec

                options = connector_cfg.get("options", {})
                sql_token = await sql_exec.acquire_sql_obo_token(
                    user_token,
                    os.environ.get("CANON_AUTH_TENANT_ID", ""),
                    os.environ.get("CANON_AUTH_CLIENT_ID", ""),
                    os.environ.get("CANON_AUTH_CLIENT_SECRET", ""),
                )
                rows = await sql_exec.execute_sql(
                    options.get("server", ""),
                    options.get("database", ""),
                    sql_token,
                    sql,
                    row_cap=_get_domain_sql_row_cap(scan_config, domain),
                )
            except (PermissionError, ValueError) as exc:
                return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]
            except Exception as exc:
                logger.exception("Unexpected error in execute_metric SQL path")
                return [TextContent(type="text", text=json.dumps({"error": f"Unexpected error: {exc}"}))]

            rows, notices = _enforce_sensitivity(rows, sensitivity)
            response = {
                "governed": True,
                "metric": metric_entry["name"],
                "pattern_id": pattern.get("pattern_id", ""),
                "filters_applied": {
                    "period_start": period_start or None,
                    "period_end": period_end or None,
                    "group_by": group_by or None,
                },
                "rows": rows,
                "row_count": len(rows),
                "model": connector_info["connector_id"],
                "role": connector_info["role"],
            }
            if notices:
                response["sensitivity_notices"] = notices
            return [TextContent(type="text", text=json.dumps(response, indent=2, default=str))]

        try:
            dax = _substitute_params(pattern["dax"], params)
        except ValueError as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

        result_or_error = await _get_obo_and_dataset(domain, connector_info["connector_id"])
        if isinstance(result_or_error, list):
            return result_or_error
        workspace_id, dataset_id, obo_token = result_or_error

        try:
            result = await _call_fabric_execute_queries(workspace_id, dataset_id, dax, obo_token)
        except (PermissionError, ValueError) as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]
        except Exception as exc:
            logger.exception("Unexpected error in execute_metric")
            return [TextContent(type="text", text=json.dumps({"error": f"Unexpected error: {exc}"}))]

        rows = result.get("results", [{}])[0].get("tables", [{}])[0].get("rows", [])
        rows, notices = _enforce_sensitivity(rows, sensitivity)
        response = {
            "governed": True,
            "metric": metric_entry["name"],
            "pattern_id": pattern.get("pattern_id", ""),
            "filters_applied": {
                "period_start": period_start or None,
                "period_end": period_end or None,
                "group_by": group_by or None,
            },
            "rows": rows,
            "row_count": len(rows),
            "model": connector_info["connector_id"],
            "role": connector_info["role"],
        }
        if notices:
            response["sensitivity_notices"] = notices
        return [TextContent(type="text", text=json.dumps(response, indent=2, default=str))]

    async def _handle_execute_query(arguments: dict) -> list[TextContent]:
        domain = arguments.get("domain", "")
        model = arguments.get("model")
        dax = arguments.get("dax", "")

        if not domain or not dax:
            return [TextContent(type="text", text=json.dumps({"error": "domain and dax are required"}))]
        if not dax.strip().upper().startswith("EVALUATE"):
            return [TextContent(type="text", text=json.dumps({"error": "DAX query must start with EVALUATE"}))]

        try:
            connector_info = _resolve_connector(scan_config, domain, model)
        except ValueError as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]
        if connector_info["role"] != "semantic":
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {"error": "execute_query only supports semantic models; use execute_sql for warehouse models"}
                    ),
                )
            ]

        result_or_error = await _get_obo_and_dataset(domain, connector_info["connector_id"])
        if isinstance(result_or_error, list):
            return result_or_error
        workspace_id, dataset_id, obo_token = result_or_error

        try:
            result = await _call_fabric_execute_queries(workspace_id, dataset_id, dax, obo_token)
        except (PermissionError, ValueError) as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]
        except Exception as exc:
            logger.exception("Unexpected error calling Fabric executeQueries")
            return [TextContent(type="text", text=json.dumps({"error": f"Unexpected error: {exc}"}))]

        rows = result.get("results", [{}])[0].get("tables", [{}])[0].get("rows", [])
        try:
            sensitivity = await _get_sensitivity(domain)
        except ValueError as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]
        rows, notices = _enforce_sensitivity(rows, sensitivity)
        response = {
            "governed": False,
            "rows": rows,
            "row_count": len(rows),
            "model": connector_info["connector_id"],
            "role": connector_info["role"],
        }
        if notices:
            response["sensitivity_notices"] = notices
        return [TextContent(type="text", text=json.dumps(response, indent=2, default=str))]

    async def _handle_execute_sql(arguments: dict) -> list[TextContent]:
        domain = arguments.get("domain", "")
        model = arguments.get("model", "")
        sql = arguments.get("sql", "")
        if not domain or not model or not sql:
            return [TextContent(type="text", text=json.dumps({"error": "domain, model, and sql are required"}))]

        try:
            connector_info = _resolve_connector(scan_config, domain, model)
            if connector_info["role"] != "warehouse":
                raise ValueError("execute_sql requires a warehouse model")
            connector_cfg = _get_connector_config(scan_config, connector_info["connector_id"])
            _validate_sql_statement(sql)
            row_cap = _get_domain_sql_row_cap(scan_config, domain)
            bounded_sql = _apply_sql_row_cap(sql, row_cap)
            user_token = _get_user_token_or_error()
            if isinstance(user_token, list):
                return user_token
            from serving.fabric_proxy import sql_exec

            sql_token = await sql_exec.acquire_sql_obo_token(
                user_token,
                os.environ.get("CANON_AUTH_TENANT_ID", ""),
                os.environ.get("CANON_AUTH_CLIENT_ID", ""),
                os.environ.get("CANON_AUTH_CLIENT_SECRET", ""),
            )
            options = connector_cfg.get("options", {})
            rows = await sql_exec.execute_sql(
                options.get("server", ""),
                options.get("database", ""),
                sql_token,
                bounded_sql,
                row_cap=row_cap,
            )
        except (PermissionError, ValueError) as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]
        except Exception as exc:
            logger.exception("Unexpected error in execute_sql")
            return [TextContent(type="text", text=json.dumps({"error": f"Unexpected error: {exc}"}))]

        try:
            sensitivity = await _get_sensitivity(domain)
        except ValueError as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]
        rows, notices = _enforce_sensitivity(rows, sensitivity)
        response = {
            "governed": False,
            "model": connector_info["connector_id"],
            "role": connector_info["role"],
            "row_count": len(rows),
            "rows": rows,
            "notices": notices,
        }
        if bounded_sql != sql:
            response["notices"].append(f"Applied row cap TOP ({row_cap})")
        return [TextContent(type="text", text=json.dumps(response, indent=2, default=str))]

    return app


async def run_stdio_server(repo_root: Path | None = None) -> None:
    from mcp.server.stdio import stdio_server

    scan_config = _apply_env_overrides(_load_scan_config(repo_root))
    app = create_app(scan_config=scan_config)
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


async def run_http_server(repo_root: Path | None = None, port: int = 8001) -> None:
    import uvicorn
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    from serving.auth import (
        AuthConfig,
        authorization_proxy_route,
        authorization_server_metadata_route,
        create_asgi_auth_middleware,
        resource_metadata_route,
        token_proxy_route,
    )

    auth_config = AuthConfig(
        tenant_id=os.environ.get("CANON_AUTH_TENANT_ID", ""),
        client_id=os.environ.get("CANON_AUTH_CLIENT_ID", ""),
        base_url=os.environ.get("CANON_FABRIC_PROXY_BASE_URL", ""),
        client_secret=os.environ.get("CANON_AUTH_CLIENT_SECRET", ""),
    )

    scan_config = await _load_scan_config_from_repo()
    app = create_app(scan_config=scan_config)
    session_manager = StreamableHTTPSessionManager(app, json_response=True, stateless=True)

    async def handle_mcp(scope, receive, send):
        await session_manager.handle_request(scope, receive, send)

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    mcp_asgi = create_asgi_auth_middleware(auth_config)(handle_mcp)

    routes = [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource", resource_metadata_route(auth_config), methods=["GET"]),
        Route(
            "/.well-known/oauth-authorization-server", authorization_server_metadata_route(auth_config), methods=["GET"]
        ),
        Route("/authorize", authorization_proxy_route(auth_config), methods=["GET"]),
        Route("/token", token_proxy_route(auth_config), methods=["POST"]),
        Route("/oauth/token", token_proxy_route(auth_config), methods=["POST"]),
        Mount("/", app=mcp_asgi),
    ]

    starlette_app = Starlette(routes=routes)

    async with session_manager.run():
        config = uvicorn.Config(starlette_app, host="0.0.0.0", port=port, log_level="info")
        server = uvicorn.Server(config)
        logger.info("Canon Fabric Proxy MCP server running on http://0.0.0.0:%d", port)
        await server.serve()
