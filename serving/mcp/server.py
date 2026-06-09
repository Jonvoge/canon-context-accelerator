"""
Canon MCP server — serves domain context to AI agents.

Tools:
  list_domains()          → available domains with descriptions
  get_domain_context(domain) → full assembled context including dimension profiles

Transports: stdio | streamable-http (default)

Caching: per-domain file-fingerprint cache (sha256 of mtime+size per file).
Cache is invalidated automatically when any domain file changes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

import yaml
from mcp.server import Server
from mcp.types import Tool, TextContent

logger = logging.getLogger(__name__)

_REPO_ROOT_ENV = "CANON_REPO_ROOT"

# ── File fingerprinting ───────────────────────────────────────────────────────

def _file_fingerprint(path: Path) -> str:
    if not path.exists():
        return ""
    stat = path.stat()
    raw = f"{stat.st_mtime_ns}:{stat.st_size}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _domain_fingerprint(domain_path: Path, cache_dir: Path) -> str:
    parts = []
    for fname in ("metrics.yaml", "ontology.yaml", "glossary.yaml", "sensitivity.yaml",
                  "domain-rules.md", "data-quality.md"):
        parts.append(_file_fingerprint(domain_path / fname))
    # Include profiles from cache
    parts.append(_file_fingerprint(cache_dir / "profiles.json"))
    return hashlib.sha256(":".join(parts).encode()).hexdigest()[:16]


# ── Domain loading ────────────────────────────────────────────────────────────

def _load_yaml(path: Path) -> dict | None:
    if not path.exists():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _load_md(path: Path) -> str | None:
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def _assemble_domain(domain: str, repo_root: Path) -> dict[str, Any]:
    domain_path = repo_root / "domains" / domain
    if not domain_path.exists():
        return {"error": f"Domain '{domain}' not found"}

    ctx: dict[str, Any] = {
        "domain": domain,
        "metrics": _load_yaml(domain_path / "metrics.yaml"),
        "ontology": _load_yaml(domain_path / "ontology.yaml"),
        "glossary": _load_yaml(domain_path / "glossary.yaml"),
        "sensitivity": _load_yaml(domain_path / "sensitivity.yaml"),
        "domain_rules": _load_md(domain_path / "domain-rules.md"),
        "data_quality": _load_md(domain_path / "data-quality.md"),
    }

    # Include dimension profiles if available
    profiles_path = repo_root / ".canon-cache" / domain / "profiles.json"
    if profiles_path.exists():
        ctx["dimension_profiles"] = json.loads(profiles_path.read_text(encoding="utf-8"))
    else:
        ctx["dimension_profiles"] = {}

    return ctx


def _list_domains(repo_root: Path) -> list[dict]:
    domains_dir = repo_root / "domains"
    result = []
    for d in sorted(domains_dir.iterdir()):
        if d.is_dir() and d.name != "_template":
            metrics = _load_yaml(d / "metrics.yaml")
            ontology = _load_yaml(d / "ontology.yaml")
            metric_count = len(metrics.get("metrics", [])) if metrics else 0
            dimension_count = len(ontology.get("dimensions", [])) if ontology else 0

            # Build trigger aliases from metric aliases
            aliases: list[str] = []
            if metrics:
                for m in metrics.get("metrics", []):
                    aliases.extend(m.get("aliases", []))

            result.append({
                "name": d.name,
                "metric_count": metric_count,
                "dimension_count": dimension_count,
                "trigger_aliases": aliases[:20],  # cap for token economy
            })
    return result


# ── Per-session cache ─────────────────────────────────────────────────────────

class _DomainCache:
    def __init__(self) -> None:
        self._cache: dict[str, tuple[str, dict]] = {}  # domain → (fingerprint, context)

    def get(self, domain: str, repo_root: Path) -> dict:
        domain_path = repo_root / "domains" / domain
        cache_dir = repo_root / ".canon-cache" / domain
        fp = _domain_fingerprint(domain_path, cache_dir)
        cached = self._cache.get(domain)
        if cached and cached[0] == fp:
            return cached[1]
        ctx = _assemble_domain(domain, repo_root)
        self._cache[domain] = (fp, ctx)
        return ctx


# ── Factory helpers ──────────────────────────────────────────────────────────

def _create_repo_client() -> "RepoClient | None":
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


def _create_auth_config() -> "AuthConfig | None":
    from serving.auth import AuthConfig
    tenant_id = os.environ.get("CANON_AUTH_TENANT_ID")
    if not tenant_id:
        return None
    return AuthConfig(
        tenant_id=tenant_id,
        client_id=os.environ.get("CANON_AUTH_CLIENT_ID", ""),
        base_url=os.environ.get("CANON_MCP_BASE_URL", ""),
        required=True,
    )


# ── Remote async loaders ──────────────────────────────────────────────────────

async def _assemble_domain_remote(domain: str, repo_client) -> dict:
    prefix = f"domains/{domain}"

    async def fetch_yaml(name: str) -> dict | None:
        content = await repo_client.fetch_file_or_none(f"{prefix}/{name}")
        if content is None:
            return None
        return yaml.safe_load(content) or {}

    async def fetch_md(name: str) -> str | None:
        return await repo_client.fetch_file_or_none(f"{prefix}/{name}")

    return {
        "domain": domain,
        "metrics": await fetch_yaml("metrics.yaml"),
        "ontology": await fetch_yaml("ontology.yaml"),
        "glossary": await fetch_yaml("glossary.yaml"),
        "sensitivity": await fetch_yaml("sensitivity.yaml"),
        "domain_rules": await fetch_md("domain-rules.md"),
        "data_quality": await fetch_md("data-quality.md"),
        "dimension_profiles": {},
    }


async def _list_domains_remote(repo_client) -> list[dict]:
    content = await repo_client.fetch_file("scan-config.yaml")
    scan_config = yaml.safe_load(content)
    result = []
    for d in scan_config.get("domains", []):
        result.append({
            "name": d["name"],
            "metric_count": None,
            "dimension_count": None,
            "trigger_aliases": [],
        })
    return result


# ── MCP server factory ────────────────────────────────────────────────────────

def create_app(repo_root: Path, repo_client=None) -> Server:
    app = Server("canon-mcp")
    cache = _DomainCache()

    @app.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="list_domains",
                description=(
                    "List all available Canon domains with their metric and dimension counts. "
                    "Call this first to discover what context is available."
                ),
                inputSchema={"type": "object", "properties": {}, "required": []},
            ),
            Tool(
                name="get_domain_context",
                description=(
                    "Get the full assembled context for a Canon domain. Returns metrics definitions, "
                    "ontology (dimensions + enumerated values), glossary terms, domain routing rules, "
                    "data quality notes, sensitivity guidance, and latest dimension value profiles."
                ),
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
        ]

    @app.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        if name == "list_domains":
            if repo_client is not None:
                domains = await _list_domains_remote(repo_client)
            else:
                domains = _list_domains(repo_root)
            return [TextContent(type="text", text=json.dumps(domains, indent=2))]

        elif name == "get_domain_context":
            domain = arguments.get("domain", "")
            if not domain:
                return [TextContent(type="text", text='{"error": "domain is required"}')]
            if repo_client is not None:
                ctx = await _assemble_domain_remote(domain, repo_client)
            else:
                ctx = cache.get(domain, repo_root)
            return [TextContent(type="text", text=json.dumps(ctx, indent=2, default=str))]

        return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

    return app


# ── Transport runners ─────────────────────────────────────────────────────────

async def run_stdio_server(repo_root: Path) -> None:
    from mcp.server.stdio import stdio_server
    app = create_app(repo_root)
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


async def run_http_server(repo_root: Path, port: int = 8000) -> None:
    import uvicorn
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    repo_client = _create_repo_client()
    auth_config = _create_auth_config()

    app = create_app(repo_root, repo_client=repo_client)
    session_manager = StreamableHTTPSessionManager(app, json_response=True, stateless=True)

    async def handle_mcp(scope, receive, send):
        await session_manager.handle_request(scope, receive, send)

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    routes = [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/readyz", healthz, methods=["GET"]),
    ]

    if auth_config:
        from serving.auth import resource_metadata_route, create_asgi_auth_middleware
        routes.append(Route("/.well-known/oauth-protected-resource", resource_metadata_route(auth_config), methods=["GET"]))
        mcp_handler = create_asgi_auth_middleware(auth_config)(handle_mcp)
    else:
        mcp_handler = handle_mcp

    routes.append(Mount("/mcp", app=mcp_handler))

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

