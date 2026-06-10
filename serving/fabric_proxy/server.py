from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path

import httpx
import jsonschema
import msal
import yaml
from mcp.server import Server
from mcp.types import TextContent, Tool

logger = logging.getLogger(__name__)

_EXECUTE_QUERIES_URL = "https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/datasets/{dataset_id}/executeQueries"
_DATASETS_URL = "https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/datasets"
_POWER_BI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
_SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "schemas" / "scan-config.schema.json"


def _load_scan_config(repo_root: Path | None = None) -> dict:
    if repo_root is not None:
        path = repo_root / "scan-config.yaml"
    else:
        path = Path(os.environ.get("CANON_SCAN_CONFIG", "scan-config.yaml"))
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _apply_env_overrides(scan_config: dict) -> dict:
    """Apply per-environment env var overrides to the first fabric_semantic connector.

    CANON_FABRIC_WORKSPACE_ID, CANON_FABRIC_DATASET_ID, CANON_FABRIC_DATASET_NAME
    take precedence over values in scan-config.yaml. This allows the Container App
    to be configured without rebuilding the image.
    """
    workspace_id = os.environ.get("CANON_FABRIC_WORKSPACE_ID", "")
    dataset_id = os.environ.get("CANON_FABRIC_DATASET_ID", "")
    dataset_name = os.environ.get("CANON_FABRIC_DATASET_NAME", "")

    if not any([workspace_id, dataset_id, dataset_name]):
        return scan_config

    import copy
    cfg = copy.deepcopy(scan_config)
    for connector in cfg.get("connectors", []):
        if connector.get("type") == "fabric_semantic":
            options = connector.setdefault("options", {})
            if workspace_id:
                options["workspace_id"] = workspace_id
            if dataset_id:
                options["dataset_id"] = dataset_id
            if dataset_name:
                options["dataset_name"] = dataset_name
    return cfg


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
        cfg = yaml.safe_load(raw) or {}
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


def _resolve_connector(scan_config: dict, domain: str, model: str) -> dict[str, str]:
    """Return workspace_id, dataset_id (may be empty), and dataset_name from scan-config."""
    domains = scan_config.get("domains", [])
    domain_cfg = next((d for d in domains if d.get("name") == domain), None)
    if domain_cfg is None:
        raise ValueError(f"Domain '{domain}' not found in scan-config.yaml")

    domain_connector = domain_cfg.get("semantic_connector")
    if model != domain_connector:
        raise ValueError(
            f"Connector '{model}' is not the semantic connector for domain '{domain}'. "
            f"Expected '{domain_connector}'. Use available_models from CanonMCP get_domain_context."
        )

    connectors = scan_config.get("connectors", [])
    connector = next((c for c in connectors if c.get("id") == model), None)
    if connector is None:
        raise ValueError(f"Connector '{model}' not found in scan-config.yaml")

    if connector.get("type") != "fabric_semantic":
        raise ValueError(f"Connector '{model}' is not of type fabric_semantic")

    options = connector.get("options", {})
    workspace_id = options.get("workspace_id", "")
    dataset_id = options.get("dataset_id", "")
    dataset_name = options.get("dataset_name", "")

    if not workspace_id:
        raise ValueError(
            f"Connector '{model}' missing workspace_id. "
            f"Fill options.workspace_id in scan-config.yaml and redeploy."
        )
    if not dataset_id and not dataset_name:
        raise ValueError(
            f"Connector '{model}' missing both dataset_id and dataset_name. "
            f"Fill options.dataset_id (or dataset_name) in scan-config.yaml and redeploy."
        )

    return {"workspace_id": workspace_id, "dataset_id": dataset_id, "dataset_name": dataset_name}


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


async def _call_fabric_execute_queries(
    workspace_id: str, dataset_id: str, dax: str, user_token: str
) -> dict:
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
    """Load sensitivity.yaml for a domain. Returns empty dict if not found."""
    if repo_root is None:
        repo_root = Path(os.environ.get("CANON_REPO_ROOT", "."))
    path = repo_root / "domains" / domain / "sensitivity.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


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


def _find_pattern(metrics: list[dict], metric: str, group_by: str | None) -> tuple[dict, dict]:
    """Return (metric_entry, pattern) best matching the request."""
    metric_entry = next((m for m in metrics if m["name"].lower() == metric.lower()), None)
    if metric_entry is None:
        names = [m["name"] for m in metrics]
        raise ValueError(f"Metric '{metric}' not found. Available: {names}")

    patterns = [
        p for p in metric_entry.get("usage_patterns", [])
        if p.get("dax")
    ]
    if not patterns:
        raise ValueError(
            f"Metric '{metric}' has no DAX usage_patterns. "
            "Add a dax pattern to metrics.yaml or use execute_query directly."
        )

    if group_by:
        # Prefer a pattern whose DAX references the group_by column
        col_lower = group_by.lower()
        for p in patterns:
            if col_lower in p["dax"].lower():
                return metric_entry, p
    # Fall back to first pattern (scalar)
    return metric_entry, patterns[0]


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
                f"DAX pattern requires parameter {{{key}}} but it was not provided. "
                "Required params: " + str(used_keys)
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




    app = Server(
        "canon-fabric-proxy",
        instructions=(
            "You are connected to the Canon Fabric Proxy. Protocol: "
            "1) Call CanonMCP list_domains to discover available domains. "
            "2) Call CanonMCP get_domain_context(domain) to get metrics definitions, "
            "model_schema (table/column/measure names), and available_models. "
            "3) Call execute_query(domain, model, dax) where domain and model come from step 2. "
            "DAX must start with EVALUATE. Use measure names from model_schema.measures and "
            "column names from ontology dimensions. "
            "Do NOT fall back to other Fabric tools for governed domains — "
            "if this proxy returns an error, report it to the user rather than switching tools."
        ),
    )

    sensitivity_cache: dict[str, dict] = {}

    def _get_sensitivity(domain: str) -> dict:
        if domain not in sensitivity_cache:
            sensitivity_cache[domain] = _load_sensitivity(domain, repo_root)
        return sensitivity_cache[domain]

    @app.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="execute_metric",
                description=(
                    "Execute a governed metric query against a Fabric semantic model. "
                    "Uses DAX patterns authored in Canon metrics.yaml — governance rules are baked in. "
                    "Call CanonMCP get_domain_context first to discover metric names and available_models. "
                    "Preferred over execute_query for defined metrics. "
                    "Response includes governed:true and provenance fields."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "domain": {
                            "type": "string",
                            "description": "Domain slug from CanonMCP, e.g. 'retail'.",
                        },
                        "model": {
                            "type": "string",
                            "description": "Connector id from CanonMCP available_models, e.g. 'retail-semantic'.",
                        },
                        "metric": {
                            "type": "string",
                            "description": "Metric name exactly as defined in Canon, e.g. 'Total Revenue'.",
                        },
                        "group_by": {
                            "type": "string",
                            "description": "Optional dimension column to group by, e.g. 'dim_product[category]'.",
                        },
                        "period_start": {
                            "type": "string",
                            "description": "Optional ISO date filter start, e.g. '2025-01-01'.",
                        },
                        "period_end": {
                            "type": "string",
                            "description": "Optional ISO date filter end, e.g. '2025-12-31'.",
                        },
                    },
                    "required": ["domain", "model", "metric"],
                },
            ),
            Tool(
                name="execute_query",
                description=(
                    "Execute a raw DAX query against a Fabric semantic model. "
                    "Use execute_metric instead for defined metrics — it has governance rules baked in. "
                    "Call CanonMCP get_domain_context first — the response includes available_models "
                    "(use one as 'model') and model_schema (table/column names for writing DAX). "
                    "Returns rows from the first result table. governed:false in response."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "domain": {
                            "type": "string",
                            "description": "Domain slug from CanonMCP, e.g. 'retail'.",
                        },
                        "model": {
                            "type": "string",
                            "description": "Connector id from CanonMCP available_models, e.g. 'retail-semantic'.",
                        },
                        "dax": {
                            "type": "string",
                            "description": "DAX query starting with EVALUATE.",
                        },
                    },
                    "required": ["domain", "model", "dax"],
                },
            ),
        ]

    @app.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        if name == "execute_metric":
            return await _handle_execute_metric(arguments)
        if name == "execute_query":
            return await _handle_execute_query(arguments)
        return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

    async def _get_obo_and_dataset(domain: str, model: str) -> tuple[str, str] | list[TextContent]:
        try:
            connector_info = _resolve_connector(scan_config, domain, model)
        except ValueError as e:
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

        from serving.auth import get_user_token
        user_token = get_user_token()
        if user_token is None:
            return [TextContent(type="text", text=json.dumps({"error": "No user token available"}))]

        try:
            obo_token = await _acquire_obo_token(user_token)
        except PermissionError as e:
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

        dataset_id = connector_info["dataset_id"]
        if not dataset_id:
            try:
                dataset_id = await _resolve_dataset_id_async(
                    connector_info["workspace_id"], connector_info["dataset_name"], obo_token
                )
            except ValueError as e:
                return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

        return connector_info["workspace_id"], dataset_id, obo_token

    async def _handle_execute_metric(arguments: dict) -> list[TextContent]:
        domain = arguments.get("domain", "")
        model = arguments.get("model", "")
        metric = arguments.get("metric", "")
        group_by = arguments.get("group_by", "")
        period_start = arguments.get("period_start", "")
        period_end = arguments.get("period_end", "")

        if not domain or not model or not metric:
            return [TextContent(type="text", text=json.dumps({"error": "domain, model, and metric are required"}))]

        try:
            metrics = _load_domain_metrics(domain, repo_root)
            metric_entry, pattern = _find_pattern(metrics, metric, group_by or None)
        except ValueError as e:
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

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
            dax = _substitute_params(pattern["dax"], params)
        except ValueError as e:
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

        result_or_error = await _get_obo_and_dataset(domain, model)
        if isinstance(result_or_error, list):
            return result_or_error
        workspace_id, dataset_id, obo_token = result_or_error

        try:
            result = await _call_fabric_execute_queries(workspace_id, dataset_id, dax, obo_token)
        except PermissionError as e:
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]
        except ValueError as e:
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]
        except Exception as e:
            logger.exception("Unexpected error in execute_metric")
            return [TextContent(type="text", text=json.dumps({"error": f"Unexpected error: {e}"}))]

        rows = result.get("results", [{}])[0].get("tables", [{}])[0].get("rows", [])
        sensitivity = _get_sensitivity(domain)
        rows, notices = _enforce_sensitivity(rows, sensitivity)

        response: dict = {
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
        }
        if notices:
            response["sensitivity_notices"] = notices
        return [TextContent(type="text", text=json.dumps(response, indent=2))]

    async def _handle_execute_query(arguments: dict) -> list[TextContent]:
        domain = arguments.get("domain", "")
        model = arguments.get("model", "")
        dax = arguments.get("dax", "")

        if not domain or not model or not dax:
            return [TextContent(type="text", text=json.dumps({"error": "domain, model, and dax are required"}))]

        if not dax.strip().upper().startswith("EVALUATE"):
            return [TextContent(type="text", text=json.dumps({"error": "DAX query must start with EVALUATE"}))]

        result_or_error = await _get_obo_and_dataset(domain, model)
        if isinstance(result_or_error, list):
            return result_or_error
        workspace_id, dataset_id, obo_token = result_or_error

        try:
            result = await _call_fabric_execute_queries(workspace_id, dataset_id, dax, obo_token)
        except PermissionError as e:
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]
        except ValueError as e:
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]
        except Exception as e:
            logger.exception("Unexpected error calling Fabric executeQueries")
            return [TextContent(type="text", text=json.dumps({"error": f"Unexpected error: {e}"}))]

        rows = result.get("results", [{}])[0].get("tables", [{}])[0].get("rows", [])
        sensitivity = _get_sensitivity(domain)
        rows, notices = _enforce_sensitivity(rows, sensitivity)

        response: dict = {"governed": False, "rows": rows, "row_count": len(rows)}
        if notices:
            response["sensitivity_notices"] = notices
        return [TextContent(type="text", text=json.dumps(response, indent=2))]

    return app


async def run_stdio_server(repo_root: Path | None = None) -> None:
    from mcp.server.stdio import stdio_server
    scan_config = _apply_env_overrides(_load_scan_config(repo_root))
    app = create_app(scan_config=scan_config)
    async with stdio_server() as (read_stream, write_write):
        await app.run(read_stream, write_write, app.create_initialization_options())


async def run_http_server(repo_root: Path | None = None, port: int = 8001) -> None:
    import uvicorn
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    from serving.auth import AuthConfig, create_asgi_auth_middleware, resource_metadata_route, authorization_server_metadata_route, authorization_proxy_route, token_proxy_route

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
        Route("/.well-known/oauth-authorization-server", authorization_server_metadata_route(auth_config), methods=["GET"]),
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
