from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import httpx
import msal
import yaml
from mcp.server import Server
from mcp.types import TextContent, Tool

logger = logging.getLogger(__name__)

_EXECUTE_QUERIES_URL = "https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/datasets/{dataset_id}/executeQueries"
_POWER_BI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"


def _load_scan_config(repo_root: Path | None = None) -> dict:
    if repo_root is not None:
        path = repo_root / "scan-config.yaml"
    else:
        path = Path(os.environ.get("CANON_SCAN_CONFIG", "scan-config.yaml"))
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _resolve_connector(scan_config: dict, domain: str, model: str) -> dict[str, str]:
    domains = scan_config.get("domains", [])
    domain_cfg = next((d for d in domains if d.get("name") == domain), None)
    if domain_cfg is None:
        raise ValueError(f"Domain '{domain}' not found")

    domain_connector = domain_cfg.get("semantic_connector")
    if model != domain_connector:
        raise ValueError(
            f"Connector '{model}' is not the semantic connector for domain '{domain}'. "
            f"Expected '{domain_connector}'."
        )

    connectors = scan_config.get("connectors", [])
    connector = next((c for c in connectors if c.get("id") == model), None)
    if connector is None:
        raise ValueError(f"Connector '{model}' not found")

    if connector.get("type") != "fabric_semantic":
        raise ValueError(f"Connector '{model}' is not of type fabric_semantic")

    options = connector.get("options", {})
    workspace_id = options.get("workspace_id")
    dataset_id = options.get("dataset_id")

    if not workspace_id or not dataset_id:
        raise ValueError(f"Connector '{model}' missing workspace_id or dataset_id")

    return {"workspace_id": workspace_id, "dataset_id": dataset_id}


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


def create_app(scan_config: dict | None = None, repo_root: Path | None = None) -> Server:
    if scan_config is None:
        scan_config = _load_scan_config(repo_root)

    app = Server("canon-fabric-proxy")

    @app.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="execute_query",
                description=(
                    "Execute a DAX query against a Fabric semantic model. "
                    "Returns rows from the first result table."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "domain": {
                            "type": "string",
                            "description": "Domain slug, e.g. 'retail'.",
                        },
                        "model": {
                            "type": "string",
                            "description": "Connector id of the semantic model, e.g. 'retail-semantic'.",
                        },
                        "dax": {
                            "type": "string",
                            "description": "DAX query starting with EVALUATE.",
                        },
                    },
                    "required": ["domain", "model", "dax"],
                },
            )
        ]

    @app.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        if name != "execute_query":
            return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

        domain = arguments.get("domain", "")
        model = arguments.get("model", "")
        dax = arguments.get("dax", "")

        if not domain or not model or not dax:
            return [TextContent(type="text", text=json.dumps({"error": "domain, model, and dax are required"}))]

        if not dax.strip().upper().startswith("EVALUATE"):
            return [TextContent(type="text", text=json.dumps({"error": "DAX query must start with EVALUATE"}))]

        try:
            ids = _resolve_connector(scan_config, domain, model)
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

        try:
            result = await _call_fabric_execute_queries(
                ids["workspace_id"], ids["dataset_id"], dax, obo_token
            )
        except PermissionError as e:
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]
        except ValueError as e:
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]
        except Exception as e:
            logger.exception("Unexpected error calling Fabric executeQueries")
            return [TextContent(type="text", text=json.dumps({"error": f"Unexpected error: {e}"}))]

        rows = result.get("results", [{}])[0].get("tables", [{}])[0].get("rows", [])
        return [TextContent(type="text", text=json.dumps({"rows": rows, "row_count": len(rows)}, indent=2))]

    return app


async def run_stdio_server(repo_root: Path | None = None) -> None:
    from mcp.server.stdio import stdio_server
    app = create_app(repo_root=repo_root)
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


async def run_http_server(repo_root: Path | None = None, port: int = 8001) -> None:
    import uvicorn
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    from serving.auth import AuthConfig, create_asgi_auth_middleware, resource_metadata_route, authorization_server_metadata_route, token_proxy_route

    auth_config = AuthConfig(
        tenant_id=os.environ.get("CANON_AUTH_TENANT_ID", ""),
        client_id=os.environ.get("CANON_AUTH_CLIENT_ID", ""),
        base_url=os.environ.get("CANON_FABRIC_PROXY_BASE_URL", ""),
        client_secret=os.environ.get("CANON_AUTH_CLIENT_SECRET", ""),
    )

    app = create_app(repo_root=repo_root)
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
        Route("/oauth/token", token_proxy_route(auth_config), methods=["POST"]),
        Mount("/mcp", app=mcp_asgi),
    ]

    starlette_app = Starlette(routes=routes)

    async with session_manager.run():
        config = uvicorn.Config(starlette_app, host="0.0.0.0", port=port, log_level="info")
        server = uvicorn.Server(config)
        logger.info("Canon Fabric Proxy MCP server running on http://0.0.0.0:%d", port)
        await server.serve()
