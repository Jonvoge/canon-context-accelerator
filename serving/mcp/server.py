"""
Canon MCP server — serves domain context to AI agents.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from serving.auth import AuthConfig
    from serving.repo_client import RepoClient

import yaml
from mcp.server import Server
from mcp.types import TextContent, Tool

from canon.config import _normalize, load_scan_config

logger = logging.getLogger(__name__)

_REPO_ROOT_ENV = "CANON_REPO_ROOT"


def _file_fingerprint(path: Path) -> str:
    if not path.exists():
        return ""
    stat = path.stat()
    raw = f"{stat.st_mtime_ns}:{stat.st_size}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _domain_fingerprint(domain_path: Path, cache_dir: Path) -> str:
    parts = []
    for fname in (
        "metrics.yaml",
        "ontology.yaml",
        "glossary.yaml",
        "sensitivity.yaml",
        "domain-rules.md",
        "data-quality.md",
    ):
        parts.append(_file_fingerprint(domain_path / fname))
    if cache_dir.exists():
        for path in sorted(cache_dir.rglob("*")):
            if path.is_file() and path.name in {"profiles.json", "schema.json"}:
                parts.append(_file_fingerprint(path))
    return hashlib.sha256(":".join(parts).encode()).hexdigest()[:16]


def _load_yaml(path: Path) -> dict | None:
    if not path.exists():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _load_md(path: Path) -> str | None:
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def _available_models(domain_entry: dict | None) -> list[dict[str, Any]]:
    if not domain_entry:
        return []
    return [
        {
            "id": model["connector"],
            "role": model["role"],
            "primary": model.get("primary", False),
            "description": model.get("description", ""),
        }
        for model in domain_entry.get("models", [])
    ]


def _load_connector_cache(repo_root: Path, domain: str, connector_id: str, filename: str) -> Any:
    new_path = repo_root / ".canon-cache" / domain / connector_id / filename
    old_path = repo_root / ".canon-cache" / domain / filename
    target = new_path if new_path.exists() else old_path
    if not target.exists():
        return {} if filename == "profiles.json" else None
    return json.loads(target.read_text(encoding="utf-8"))


def _assemble_domain(domain: str, repo_root: Path) -> dict[str, Any]:
    domain_path = repo_root / "domains" / domain
    if not domain_path.exists():
        return {"error": f"Domain '{domain}' not found"}

    scan_cfg = load_scan_config(repo_root / "scan-config.yaml") if (repo_root / "scan-config.yaml").exists() else {}
    domain_entry = next((entry for entry in scan_cfg.get("domains", []) if entry.get("name") == domain), None)
    available_models = _available_models(domain_entry)

    model_schema = {
        model["id"]: _load_connector_cache(repo_root, domain, model["id"], "schema.json") for model in available_models
    }
    dimension_profiles = {
        model["id"]: _load_connector_cache(repo_root, domain, model["id"], "profiles.json")
        for model in available_models
    }

    return {
        "domain": domain,
        "metrics": _load_yaml(domain_path / "metrics.yaml"),
        "ontology": _load_yaml(domain_path / "ontology.yaml"),
        "glossary": _load_yaml(domain_path / "glossary.yaml"),
        "sensitivity": _load_yaml(domain_path / "sensitivity.yaml"),
        "domain_rules": _load_md(domain_path / "domain-rules.md"),
        "data_quality": _load_md(domain_path / "data-quality.md"),
        "dimension_profiles": dimension_profiles,
        "model_schema": model_schema,
        "available_models": available_models,
    }


def _list_domains(repo_root: Path) -> list[dict]:
    domains_dir = repo_root / "domains"
    result = []
    for domain_dir in sorted(domains_dir.iterdir()):
        if domain_dir.is_dir() and domain_dir.name != "_template":
            metrics = _load_yaml(domain_dir / "metrics.yaml")
            ontology = _load_yaml(domain_dir / "ontology.yaml")
            metric_count = len(metrics.get("metrics", [])) if metrics else 0
            dimension_count = len(ontology.get("dimensions", [])) if ontology else 0
            aliases: list[str] = []
            if metrics:
                for metric in metrics.get("metrics", []):
                    aliases.extend(metric.get("aliases", []))
            result.append(
                {
                    "name": domain_dir.name,
                    "metric_count": metric_count,
                    "dimension_count": dimension_count,
                    "trigger_aliases": aliases[:20],
                }
            )
    return result


class _DomainCache:
    def __init__(self) -> None:
        self._cache: dict[str, tuple[str, dict]] = {}

    def get(self, domain: str, repo_root: Path) -> dict:
        domain_path = repo_root / "domains" / domain
        cache_dir = repo_root / ".canon-cache" / domain
        fingerprint = _domain_fingerprint(domain_path, cache_dir)
        cached = self._cache.get(domain)
        if cached and cached[0] == fingerprint:
            return cached[1]
        ctx = _assemble_domain(domain, repo_root)
        self._cache[domain] = (fingerprint, ctx)
        return ctx


def _create_repo_client() -> RepoClient | None:
    from serving.repo_client import RepoClient, RepoConfig

    provider = os.environ.get("CANON_REPO_PROVIDER")
    if not provider:
        return None
    config = RepoConfig(
        provider=provider,
        owner=os.environ.get("CANON_REPO_OWNER", ""),
        repo=os.environ.get("CANON_REPO_NAME", ""),
        token=os.environ.get("CANON_REPO_TOKEN", ""),
        branch=os.environ.get("CANON_REPO_BRANCH", "main"),
        org=os.environ.get("CANON_REPO_ADO_ORG", ""),
        project=os.environ.get("CANON_REPO_ADO_PROJECT", ""),
    )
    return RepoClient(config)


def _create_auth_config() -> AuthConfig | None:
    from serving.auth import AuthConfig

    tenant_id = os.environ.get("CANON_AUTH_TENANT_ID")
    if not tenant_id:
        return None
    return AuthConfig(
        tenant_id=tenant_id,
        client_id=os.environ.get("CANON_AUTH_CLIENT_ID", ""),
        base_url=os.environ.get("CANON_MCP_BASE_URL", ""),
        client_secret=os.environ.get("CANON_AUTH_CLIENT_SECRET", ""),
        required=True,
    )


async def _assemble_domain_remote(domain: str, repo_client) -> dict:
    prefix = f"domains/{domain}"

    async def fetch_yaml(name: str) -> dict | None:
        content = await repo_client.fetch_file_or_none(f"{prefix}/{name}")
        if content is None:
            return None
        return yaml.safe_load(content) or {}

    async def fetch_md(name: str) -> str | None:
        return await repo_client.fetch_file_or_none(f"{prefix}/{name}")

    scan_cfg_raw = await repo_client.fetch_file_or_none("scan-config.yaml")
    if scan_cfg_raw:
        scan_cfg = _normalize(yaml.safe_load(scan_cfg_raw) or {})
        domain_entry = next((entry for entry in scan_cfg.get("domains", []) if entry.get("name") == domain), None)
        available_models = _available_models(domain_entry)
    else:
        available_models = []

    async def fetch_connector_cache(connector_id: str, filename: str) -> Any:
        content = await repo_client.fetch_file_or_none(f".canon-cache/{domain}/{connector_id}/{filename}")
        if content is None:
            content = await repo_client.fetch_file_or_none(f".canon-cache/{domain}/{filename}")
        if content is None:
            return {} if filename == "profiles.json" else None
        return json.loads(content)

    model_schema = {model["id"]: await fetch_connector_cache(model["id"], "schema.json") for model in available_models}
    dimension_profiles = {
        model["id"]: await fetch_connector_cache(model["id"], "profiles.json") for model in available_models
    }

    return {
        "domain": domain,
        "metrics": await fetch_yaml("metrics.yaml"),
        "ontology": await fetch_yaml("ontology.yaml"),
        "glossary": await fetch_yaml("glossary.yaml"),
        "sensitivity": await fetch_yaml("sensitivity.yaml"),
        "domain_rules": await fetch_md("domain-rules.md"),
        "data_quality": await fetch_md("data-quality.md"),
        "dimension_profiles": dimension_profiles,
        "model_schema": model_schema,
        "available_models": available_models,
    }


async def _list_domains_remote(repo_client) -> list[dict]:
    content = await repo_client.fetch_file("scan-config.yaml")
    scan_config = _normalize(yaml.safe_load(content) or {})
    result = []
    for domain in scan_config.get("domains", []):
        result.append({"name": domain["name"], "metric_count": None, "dimension_count": None, "trigger_aliases": []})
    return result


def _scope_metric_context(full_ctx: dict, metric_name: str) -> dict:
    metrics_data = full_ctx.get("metrics") or {}
    all_metrics = metrics_data.get("metrics", []) if metrics_data else []
    metric_entry = next(
        (metric for metric in all_metrics if metric.get("name", "").lower() == metric_name.lower()), None
    )
    if metric_entry is None:
        return {"error": f"Metric '{metric_name}' not found in domain context."}

    glossary_data = full_ctx.get("glossary") or {}
    all_terms = glossary_data.get("terms", []) if glossary_data else []
    related_names = {metric_entry["name"].lower()} | {dep.lower() for dep in metric_entry.get("depends_on", [])}
    glossary_terms = [term for term in all_terms if term.get("term", "").lower() in related_names]

    return {
        "domain": full_ctx.get("domain"),
        "metric": metric_entry,
        "glossary_terms": glossary_terms,
        "domain_rules": full_ctx.get("domain_rules"),
        "model_schema": full_ctx.get("model_schema"),
        "execution": {"available_models": full_ctx.get("available_models", [])},
    }


def _resolve_metric(full_ctx: dict, question: str) -> list[dict]:
    metrics_data = full_ctx.get("metrics") or {}
    all_metrics = metrics_data.get("metrics", []) if metrics_data else []
    q_tokens = set(question.lower().split())
    scored = []
    for metric in all_metrics:
        name = metric.get("name", "")
        candidates = [name.lower()] + [alias.lower() for alias in metric.get("aliases", [])]
        score = 0
        for candidate in candidates:
            candidate_tokens = set(candidate.split())
            overlap = len(q_tokens & candidate_tokens)
            if overlap:
                if candidate in question.lower():
                    score = max(score, 100 + overlap)
                else:
                    score = max(score, overlap * 10)
        if score > 0:
            scored.append({"metric": name, "confidence": score})
    scored.sort(key=lambda item: item["confidence"], reverse=True)
    top = scored[:3]
    if top:
        max_score = top[0]["confidence"]
        for item in top:
            item["confidence"] = round(item["confidence"] / max_score, 2)
    return top


def _load_domain_index(repo_root: Path) -> dict:
    index_path = repo_root / "domains" / "_index.json"
    if index_path.exists():
        return json.loads(index_path.read_text(encoding="utf-8"))
    return {"domains": _list_domains(repo_root)}


def _score_text(question: str, *texts: str) -> int:
    q_lower = question.lower()
    q_tokens = set(q_lower.split())
    score = 0
    for text in texts:
        lowered = (text or "").lower()
        if not lowered:
            continue
        tokens = set(lowered.split())
        overlap = len(q_tokens & tokens)
        if overlap:
            score = max(score, overlap * 10)
        if lowered and lowered in q_lower:
            score = max(score, 100 + len(tokens))
    return score


def _resolve_metric_across_domains(repo_root: Path, question: str) -> list[dict]:
    index = _load_domain_index(repo_root)
    scored = []
    for domain_entry in index.get("domains", []):
        domain = domain_entry["name"]
        ctx = _assemble_domain(domain, repo_root)
        metrics = (ctx.get("metrics") or {}).get("metrics", [])
        for metric in metrics:
            score = _score_text(
                question, metric.get("name", ""), *(metric.get("aliases", [])), domain_entry.get("description", "")
            )
            if score > 0:
                scored.append({"domain": domain, "metric": metric.get("name", ""), "confidence": score})
    scored.sort(key=lambda item: item["confidence"], reverse=True)
    top = scored[:3]
    if top:
        max_score = top[0]["confidence"]
        for item in top:
            item["confidence"] = round(item["confidence"] / max_score, 2)
    return top


async def _resolve_metric_across_domains_remote(repo_client, question: str) -> list[dict]:
    index_raw = await repo_client.fetch_file_or_none("domains/_index.json")
    if index_raw:
        index = json.loads(index_raw)
        domains = [entry["name"] for entry in index.get("domains", [])]
        descriptions = {entry["name"]: entry.get("description", "") for entry in index.get("domains", [])}
    else:
        domains = [entry["name"] for entry in await _list_domains_remote(repo_client)]
        descriptions = {}
    scored = []
    for domain in domains:
        ctx = await _assemble_domain_remote(domain, repo_client)
        metrics = (ctx.get("metrics") or {}).get("metrics", [])
        for metric in metrics:
            score = _score_text(
                question, metric.get("name", ""), *(metric.get("aliases", [])), descriptions.get(domain, "")
            )
            if score > 0:
                scored.append({"domain": domain, "metric": metric.get("name", ""), "confidence": score})
    scored.sort(key=lambda item: item["confidence"], reverse=True)
    top = scored[:3]
    if top:
        max_score = top[0]["confidence"]
        for item in top:
            item["confidence"] = round(item["confidence"] / max_score, 2)
    return top


def create_app(repo_root: Path, repo_client=None) -> Server:
    app = Server(
        "canon-mcp",
        instructions=(
            "You are connected to CanonMCP — the governed context layer for business metrics. "
            "Protocol: "
            "1) Call list_domains to see available domains. "
            "2) Call get_domain_context(domain) before any data query — this returns metrics definitions, "
            "ontology, domain rules, available_models, and model_schema. "
            "3) Pick model from available_models using its description; prefer primary: true for governed aggregates. "
            "4) Use FabricProxy execute_query or execute_metric with that model. "
            "Do not fall back to other Fabric tools for governed domains — Canon definitions take precedence."
        ),
    )
    cache = _DomainCache()

    @app.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="list_domains",
                description="List all available Canon domains with their metric and dimension counts. Call this first to discover what context is available.",
                inputSchema={"type": "object", "properties": {}, "required": []},
            ),
            Tool(
                name="get_domain_context",
                description="Get the full assembled context for a Canon domain. Returns metrics, ontology, glossary, routing rules, dimension profiles, structured available_models, and model_schema from the last scan.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "domain": {
                            "type": "string",
                            "description": "Domain slug, e.g. 'retail'. Use list_domains to discover available slugs.",
                        }
                    },
                    "required": ["domain"],
                },
            ),
            Tool(
                name="get_metric_context",
                description="Get scoped context for a single metric — metric definition, governed sources, applicable domain rules, related glossary terms, available_models, and relevant schema tables.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "domain": {"type": "string", "description": "Domain slug, e.g. 'retail'."},
                        "metric": {
                            "type": "string",
                            "description": "Metric name exactly as defined in Canon, e.g. 'Total Revenue'.",
                        },
                    },
                    "required": ["domain", "metric"],
                },
            ),
            Tool(
                name="resolve",
                description="Match a natural language question to the most relevant Canon metric(s). Returns up to 3 candidates with confidence scores.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "domain": {"type": "string", "description": "Optional domain slug, e.g. 'retail'."},
                        "question": {"type": "string", "description": "The user's natural language question."},
                    },
                    "required": ["question"],
                },
            ),
        ]

    @app.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        if name == "list_domains":
            domains = await _list_domains_remote(repo_client) if repo_client is not None else _list_domains(repo_root)
            return [TextContent(type="text", text=json.dumps(domains, indent=2))]
        if name == "get_domain_context":
            domain = arguments.get("domain", "")
            if not domain:
                return [TextContent(type="text", text='{"error": "domain is required"}')]
            ctx = (
                await _assemble_domain_remote(domain, repo_client)
                if repo_client is not None
                else cache.get(domain, repo_root)
            )
            return [TextContent(type="text", text=json.dumps(ctx, indent=2, default=str))]
        if name == "get_metric_context":
            domain = arguments.get("domain", "")
            metric = arguments.get("metric", "")
            if not domain or not metric:
                return [TextContent(type="text", text='{"error": "domain and metric are required"}')]
            ctx = (
                await _assemble_domain_remote(domain, repo_client)
                if repo_client is not None
                else cache.get(domain, repo_root)
            )
            return [
                TextContent(type="text", text=json.dumps(_scope_metric_context(ctx, metric), indent=2, default=str))
            ]
        if name == "resolve":
            domain = arguments.get("domain", "")
            question = arguments.get("question", "")
            if not question:
                return [TextContent(type="text", text='{"error": "question is required"}')]
            if domain:
                ctx = (
                    await _assemble_domain_remote(domain, repo_client)
                    if repo_client is not None
                    else cache.get(domain, repo_root)
                )
                matches = _resolve_metric(ctx, question)
                return [
                    TextContent(
                        type="text",
                        text=json.dumps({"domain": domain, "question": question, "matches": matches}, indent=2),
                    )
                ]
            matches = (
                await _resolve_metric_across_domains_remote(repo_client, question)
                if repo_client is not None
                else _resolve_metric_across_domains(repo_root, question)
            )
            return [TextContent(type="text", text=json.dumps({"question": question, "matches": matches}, indent=2))]
        return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

    return app


async def run_stdio_server(repo_root: Path) -> None:
    from mcp.server.stdio import stdio_server

    app = create_app(repo_root)
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


async def run_http_server(repo_root: Path, port: int = 8000) -> None:
    import uvicorn
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    repo_client = _create_repo_client()
    auth_config = _create_auth_config()
    app = create_app(repo_root, repo_client=repo_client)
    session_manager = StreamableHTTPSessionManager(app, json_response=True, stateless=True)

    async def handle_mcp(scope, receive, send):
        await session_manager.handle_request(scope, receive, send)

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    routes = [Route("/healthz", healthz, methods=["GET"]), Route("/readyz", healthz, methods=["GET"])]

    if auth_config:
        from serving.auth import (
            authorization_proxy_route,
            authorization_server_metadata_route,
            create_asgi_auth_middleware,
            resource_metadata_route,
            token_proxy_route,
        )

        routes.append(
            Route("/.well-known/oauth-protected-resource", resource_metadata_route(auth_config), methods=["GET"])
        )
        routes.append(
            Route(
                "/.well-known/oauth-authorization-server",
                authorization_server_metadata_route(auth_config),
                methods=["GET"],
            )
        )
        routes.append(Route("/authorize", authorization_proxy_route(auth_config), methods=["GET"]))
        routes.append(Route("/token", token_proxy_route(auth_config), methods=["POST"]))
        routes.append(Route("/oauth/token", token_proxy_route(auth_config), methods=["POST"]))
        mcp_handler = create_asgi_auth_middleware(auth_config)(handle_mcp)
    else:
        mcp_handler = handle_mcp

    routes.append(Mount("/", app=mcp_handler))
    starlette_app = Starlette(routes=routes)

    async with session_manager.run():
        config = uvicorn.Config(starlette_app, host="0.0.0.0", port=port, log_level="info")
        server = uvicorn.Server(config)
        logger.info("Canon MCP server running on http://0.0.0.0:%d", port)
        await server.serve()


if __name__ == "__main__":
    import asyncio

    root = Path(os.environ.get(_REPO_ROOT_ENV, Path(__file__).resolve().parent.parent.parent))
    port = int(os.environ.get("CANON_MCP_PORT", 8000))
    transport = os.environ.get("CANON_MCP_TRANSPORT", "streamable-http")

    if transport == "stdio":
        asyncio.run(run_stdio_server(root))
    else:
        asyncio.run(run_http_server(root, port=port))
