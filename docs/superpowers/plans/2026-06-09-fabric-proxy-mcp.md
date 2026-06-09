# Fabric Proxy MCP + Canon MCP Auth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Fabric Proxy MCP server (`execute_query` tool) with Entra ID OAuth + OBO, and add MCP auth spec compliance to the existing Canon MCP, both serving claude.ai users.

**Architecture:** Two separate MCP servers sharing an auth module. Canon MCP reads domain definitions from GitHub API at request time. Fabric Proxy exchanges user tokens via OBO and calls Fabric executeQueries. Both require Entra ID authentication when in HTTP mode.

**Tech Stack:** Python 3.12, Starlette, mcp SDK, msal, httpx, PyJWT/jwks, uvicorn

---

## File Structure

```
serving/
  auth.py                       ← NEW: MCP auth spec (resource metadata, 401 shape, JWT validation)
  repo_client.py                ← NEW: fetch domain files from GitHub/ADO API with cache
  mcp/
    server.py                   ← MODIFY: use repo_client, add auth middleware
  fabric_proxy/
    __init__.py                 ← NEW
    server.py                   ← NEW: execute_query tool, OBO, Fabric API call
schemas/
  scan-config.schema.json       ← MODIFY: add dataset_id to connector options docs
scan-config.yaml                ← MODIFY: add dataset_id to retail-semantic connector
tests/
  test_auth.py                  ← NEW
  test_repo_client.py           ← NEW
  test_fabric_proxy.py          ← NEW
pyproject.toml                  ← MODIFY: add msal, pyjwt, httpx deps
```

---

## Environment Variables (both servers)

```
# Auth (required in HTTP mode, ignored in stdio)
CANON_AUTH_TENANT_ID=<customer Entra tenant GUID>
CANON_AUTH_CLIENT_ID=<Entra app client_id>
CANON_AUTH_CLIENT_SECRET=<Entra app client secret — needed for OBO on Fabric Proxy>
CANON_MCP_BASE_URL=<public URL of this server, e.g. https://canon-mcp.xxx.azurecontainerapps.io>

# Repo access (Canon MCP only)
CANON_REPO_PROVIDER=github  # or "ado"
CANON_REPO_OWNER=<github org or user>
CANON_REPO_NAME=<repo name>
CANON_REPO_TOKEN=<fine-grained PAT with contents:read>
CANON_REPO_BRANCH=main

# Fabric Proxy only
CANON_FABRIC_PROXY_BASE_URL=<public URL of fabric proxy>
```

---

## Task 1: Add dependencies to pyproject.toml

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add msal, pyjwt, cryptography, httpx to dependencies**

```toml
dependencies = [
    "pyyaml>=6.0",
    "jsonschema>=4.20",
    "azure-identity>=1.15",
    "requests>=2.31",
    "mcp>=1.0",
    "click>=8.1",
    "pyodbc>=5.1",
    "aiohttp>=3.9",
    "starlette>=0.40",
    "uvicorn>=0.30",
    "msal>=1.28",
    "pyjwt[crypto]>=2.8",
    "httpx>=0.27",
]
```

- [ ] **Step 2: Run uv sync**

Run: `uv sync`
Expected: all deps install cleanly

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "feat: add msal, pyjwt, httpx deps for fabric proxy and auth"
```

---

## Task 2: Add `dataset_id` to scan-config schema and config

**Files:**
- Modify: `schemas/scan-config.schema.json`
- Modify: `scan-config.yaml`

- [ ] **Step 1: Update scan-config.yaml to include dataset_id**

In the `retail-semantic` connector, add `dataset_id`:

```yaml
connectors:
  - id: retail-semantic
    type: fabric_semantic
    auth_secret_name: CANON_FABRIC_CLIENT_SECRET
    options:
      workspace_id: "b53e615a-ab79-49f9-bcf0-67250a34f633"
      dataset_id: ""  # TODO: fill with actual dataset GUID from Power BI URL
      dataset_name: "RetailSemanticModel"
```

Note: `dataset_id` is a new optional field. The schema already allows arbitrary keys in `options` via `"additionalProperties": {"type": ["string", "number", "boolean"]}`, so no schema change needed.

- [ ] **Step 2: Commit**

```bash
git add scan-config.yaml
git commit -m "feat: add dataset_id field to retail-semantic connector"
```

---

## Task 3: Build `serving/repo_client.py` — GitHub/ADO file fetcher

**Files:**
- Create: `serving/repo_client.py`
- Create: `tests/test_repo_client.py`

- [ ] **Step 1: Write failing test for repo_client**

```python
# tests/test_repo_client.py
import time
from unittest.mock import AsyncMock, patch

import pytest

from serving.repo_client import RepoClient, RepoConfig


@pytest.fixture
def github_config():
    return RepoConfig(
        provider="github",
        owner="test-org",
        repo="canon-repo",
        token="ghp_test123",
        branch="main",
    )


@pytest.mark.asyncio
async def test_fetch_file_github(github_config):
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json = lambda: {"content": "bWV0cmljczoKICAtIG5hbWU6IFJldmVudWU=", "sha": "abc123"}
    # base64 of "metrics:\n  - name: Revenue"

    with patch("httpx.AsyncClient.get", return_value=mock_response):
        client = RepoClient(github_config)
        content = await client.fetch_file("domains/retail/metrics.yaml")
        assert "Revenue" in content


@pytest.mark.asyncio
async def test_cache_returns_cached_on_same_sha(github_config):
    call_count = 0

    async def mock_get(url, **kwargs):
        nonlocal call_count
        call_count += 1
        resp = AsyncMock()
        resp.status_code = 200
        resp.json = lambda: {"content": "bWV0cmljczoKICAtIG5hbWU6IFJldmVudWU=", "sha": "abc123"}
        return resp

    with patch("httpx.AsyncClient.get", side_effect=mock_get):
        client = RepoClient(github_config, cache_ttl_seconds=60)
        await client.fetch_file("domains/retail/metrics.yaml")
        await client.fetch_file("domains/retail/metrics.yaml")
        # Second call should still hit API (TTL-based), but result comes from cache if sha matches
        assert call_count >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_repo_client.py -v`
Expected: ImportError — `serving.repo_client` does not exist

- [ ] **Step 3: Implement repo_client.py**

```python
# serving/repo_client.py
from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field

import httpx


@dataclass
class RepoConfig:
    provider: str  # "github" or "ado"
    owner: str
    repo: str
    token: str
    branch: str = "main"
    # ADO-specific
    org: str = ""
    project: str = ""


@dataclass
class _CacheEntry:
    content: str
    sha: str
    fetched_at: float


class RepoClient:
    def __init__(self, config: RepoConfig, cache_ttl_seconds: int = 60) -> None:
        self._config = config
        self._cache: dict[str, _CacheEntry] = {}
        self._ttl = cache_ttl_seconds

    async def fetch_file(self, path: str) -> str:
        cached = self._cache.get(path)
        if cached and (time.time() - cached.fetched_at) < self._ttl:
            return cached.content

        if self._config.provider == "github":
            content, sha = await self._fetch_github(path)
        elif self._config.provider == "ado":
            content, sha = await self._fetch_ado(path)
        else:
            raise ValueError(f"Unknown provider: {self._config.provider}")

        self._cache[path] = _CacheEntry(content=content, sha=sha, fetched_at=time.time())
        return content

    async def fetch_file_or_none(self, path: str) -> str | None:
        try:
            return await self.fetch_file(path)
        except FileNotFoundError:
            return None

    async def _fetch_github(self, path: str) -> tuple[str, str]:
        url = f"https://api.github.com/repos/{self._config.owner}/{self._config.repo}/contents/{path}"
        params = {"ref": self._config.branch}
        headers = {
            "Authorization": f"Bearer {self._config.token}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params, headers=headers, timeout=15)
        if resp.status_code == 404:
            raise FileNotFoundError(f"File not found: {path}")
        resp.raise_for_status()
        data = resp.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        return content, data["sha"]

    async def _fetch_ado(self, path: str) -> tuple[str, str]:
        url = (
            f"https://dev.azure.com/{self._config.org}/{self._config.project}"
            f"/_apis/git/repositories/{self._config.repo}/items"
        )
        params = {"path": path, "versionDescriptor.version": self._config.branch, "api-version": "7.1"}
        headers = {"Authorization": f"Basic {base64.b64encode(f':{self._config.token}'.encode()).decode()}"}
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params, headers=headers, timeout=15)
        if resp.status_code == 404:
            raise FileNotFoundError(f"File not found: {path}")
        resp.raise_for_status()
        # ADO returns raw content for items endpoint
        content = resp.text
        # Use ETag or objectId as sha equivalent
        sha = resp.headers.get("ETag", str(time.time()))
        return content, sha
```

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/test_repo_client.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add serving/repo_client.py tests/test_repo_client.py
git commit -m "feat: add repo_client for fetching domain files from GitHub/ADO"
```

---

## Task 4: Build `serving/auth.py` — MCP auth spec middleware

**Files:**
- Create: `serving/auth.py`
- Create: `tests/test_auth.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_auth.py
from unittest.mock import patch, AsyncMock

import pytest
from starlette.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from serving.auth import AuthConfig, create_auth_middleware, resource_metadata_route


@pytest.fixture
def auth_config():
    return AuthConfig(
        tenant_id="a7ed0222-1883-488c-8bbb-6ee4f043da6d",
        client_id="test-client-id",
        base_url="https://canon-mcp.example.com",
    )


def test_resource_metadata_endpoint(auth_config):
    app = Starlette(routes=[
        Route("/.well-known/oauth-protected-resource", resource_metadata_route(auth_config), methods=["GET"]),
    ])
    client = TestClient(app)
    resp = client.get("/.well-known/oauth-protected-resource")
    assert resp.status_code == 200
    body = resp.json()
    assert "authorization_servers" in body
    assert "https://login.microsoftonline.com/a7ed0222-1883-488c-8bbb-6ee4f043da6d/v2.0" in body["authorization_servers"]
    assert body["resource"] == "https://canon-mcp.example.com"


def test_unauthenticated_request_returns_401(auth_config):
    async def dummy(request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[
        Route("/.well-known/oauth-protected-resource", resource_metadata_route(auth_config), methods=["GET"]),
        Route("/mcp", create_auth_middleware(auth_config)(dummy), methods=["POST"]),
    ])
    client = TestClient(app)
    resp = client.post("/mcp", json={})
    assert resp.status_code == 401
    assert "resource_metadata" in resp.headers["www-authenticate"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_auth.py -v`
Expected: ImportError — `serving.auth` does not exist

- [ ] **Step 3: Implement auth.py**

```python
# serving/auth.py
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

import jwt
import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)


@dataclass
class AuthConfig:
    tenant_id: str
    client_id: str
    base_url: str  # Public URL of this MCP server
    client_secret: str = ""  # Only needed for OBO (Fabric Proxy)
    required: bool = True  # Set False to disable auth (local/stdio mode)


_JWKS_CACHE: dict[str, dict] = {}


async def _get_jwks(tenant_id: str) -> dict:
    if tenant_id in _JWKS_CACHE:
        return _JWKS_CACHE[tenant_id]
    url = f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, timeout=10)
        resp.raise_for_status()
    _JWKS_CACHE[tenant_id] = resp.json()
    return _JWKS_CACHE[tenant_id]


async def validate_token(token: str, config: AuthConfig) -> dict | None:
    """Validate a Bearer token from Entra ID. Returns claims dict or None."""
    try:
        jwks = await _get_jwks(config.tenant_id)
        jwks_client = jwt.PyJWKClient.__new__(jwt.PyJWKClient)
        # Build signing keys from fetched JWKS
        from jwt import PyJWKClient
        # Use the fetched keys directly
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        key = None
        for k in jwks.get("keys", []):
            if k.get("kid") == kid:
                key = jwt.algorithms.RSAAlgorithm.from_jwk(k)
                break
        if key is None:
            logger.warning("No matching key for kid=%s", kid)
            return None

        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=f"api://{config.client_id}",
            issuer=f"https://login.microsoftonline.com/{config.tenant_id}/v2.0",
        )
        return claims
    except jwt.PyJWTError as e:
        logger.warning("Token validation failed: %s", e)
        return None


def resource_metadata_route(config: AuthConfig) -> Callable:
    """Returns a Starlette endpoint that serves RFC 9728 Protected Resource Metadata."""
    async def endpoint(request: Request) -> JSONResponse:
        return JSONResponse({
            "resource": config.base_url,
            "authorization_servers": [
                f"https://login.microsoftonline.com/{config.tenant_id}/v2.0"
            ],
            "scopes_supported": [f"api://{config.client_id}/access"],
            "bearer_methods_supported": ["header"],
        })
    return endpoint


def create_auth_middleware(config: AuthConfig) -> Callable:
    """Returns a decorator that wraps a Starlette endpoint with JWT validation."""
    def decorator(handler: Callable) -> Callable:
        async def wrapped(request: Request) -> Response:
            if not config.required:
                return await handler(request)

            auth_header = request.headers.get("authorization", "")
            if not auth_header.startswith("Bearer "):
                return Response(
                    status_code=401,
                    headers={
                        "WWW-Authenticate": (
                            f'Bearer resource_metadata="{config.base_url}/.well-known/oauth-protected-resource"'
                        )
                    },
                )

            token = auth_header[7:]
            claims = await validate_token(token, config)
            if claims is None:
                return Response(
                    status_code=401,
                    headers={
                        "WWW-Authenticate": (
                            f'Bearer resource_metadata="{config.base_url}/.well-known/oauth-protected-resource",'
                            ' error="invalid_token"'
                        )
                    },
                )

            # Store claims and raw token on request state for downstream use
            request.state.user_claims = claims
            request.state.user_token = token
            return await handler(request)
        return wrapped
    return decorator
```

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/test_auth.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add serving/auth.py tests/test_auth.py
git commit -m "feat: add MCP auth spec middleware (resource metadata + JWT validation)"
```

---

## Task 5: Modify Canon MCP server to use repo_client + auth

**Files:**
- Modify: `serving/mcp/server.py`
- Modify: `tests/test_mcp_server.py`

- [ ] **Step 1: Refactor server.py to support both filesystem (stdio) and GitHub API (HTTP) modes**

The key change: replace direct file reads with either filesystem OR repo_client, depending on transport mode.

```python
# serving/mcp/server.py — replace the domain loading section

# Add at top of file:
import os
from serving.repo_client import RepoClient, RepoConfig

# New: create repo_client from env vars
def _create_repo_client() -> RepoClient | None:
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
```

Replace `_assemble_domain` with an async version that uses repo_client when available, filesystem when not. Replace `_list_domains` similarly — when using repo_client, fetch `scan-config.yaml` from the repo to discover domains.

The `run_http_server` function adds the auth routes and middleware. The `run_stdio_server` function remains unchanged (no auth).

Full implementation details in the refactoring: the `create_app` factory gains an optional `repo_client` and `auth_config` parameter. HTTP mode passes them in, stdio mode does not.

- [ ] **Step 2: Update run_http_server to include auth routes**

Add the `/.well-known/oauth-protected-resource` route and wrap the `/mcp` handler with auth middleware when `CANON_AUTH_TENANT_ID` is set.

- [ ] **Step 3: Run existing tests + verify they still pass in filesystem mode**

Run: `uv run python -m pytest tests/test_mcp_server.py -v`
Expected: PASS (filesystem mode unaffected)

- [ ] **Step 4: Commit**

```bash
git add serving/mcp/server.py
git commit -m "feat: Canon MCP supports repo_client (GitHub/ADO) and auth middleware"
```

---

## Task 6: Build `serving/fabric_proxy/server.py` — the Fabric Proxy MCP

**Files:**
- Create: `serving/fabric_proxy/__init__.py`
- Create: `serving/fabric_proxy/server.py`
- Create: `tests/test_fabric_proxy.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_fabric_proxy.py
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from serving.fabric_proxy.server import create_app, _resolve_connector


def test_resolve_connector_finds_by_domain_and_model():
    scan_config = {
        "connectors": [
            {"id": "retail-semantic", "type": "fabric_semantic", "options": {
                "workspace_id": "ws-guid", "dataset_id": "ds-guid"
            }},
        ],
        "domains": [
            {"name": "retail", "semantic_connector": "retail-semantic"}
        ],
    }
    result = _resolve_connector(scan_config, domain="retail", model="retail-semantic")
    assert result == {"workspace_id": "ws-guid", "dataset_id": "ds-guid"}


def test_resolve_connector_errors_on_unknown_model():
    scan_config = {
        "connectors": [{"id": "retail-semantic", "type": "fabric_semantic", "options": {}}],
        "domains": [{"name": "retail", "semantic_connector": "retail-semantic"}],
    }
    with pytest.raises(ValueError, match="not found"):
        _resolve_connector(scan_config, domain="retail", model="nonexistent")


@pytest.mark.asyncio
async def test_execute_query_calls_fabric_api():
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json = lambda: {
        "results": [{"tables": [{"rows": [{"[Region]": "North", "[Revenue]": 1000}]}]}]
    }

    with patch("httpx.AsyncClient.post", return_value=mock_response):
        from serving.fabric_proxy.server import _call_fabric_execute_queries
        result = await _call_fabric_execute_queries(
            workspace_id="ws-guid",
            dataset_id="ds-guid",
            dax="EVALUATE ROW('x', 1)",
            user_token="fake-obo-token",
        )
        assert result["results"][0]["tables"][0]["rows"][0]["[Revenue]"] == 1000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_fabric_proxy.py -v`
Expected: ImportError — `serving.fabric_proxy.server` does not exist

- [ ] **Step 3: Implement fabric_proxy/server.py**

```python
# serving/fabric_proxy/__init__.py
```

```python
# serving/fabric_proxy/server.py
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx
import msal
import yaml
from mcp.server import Server
from mcp.types import Tool, TextContent

logger = logging.getLogger(__name__)

_EXECUTE_QUERIES_URL = (
    "https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/datasets/{dataset_id}/executeQueries"
)
_POWER_BI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"


def _load_scan_config(repo_root: Path | None = None) -> dict:
    """Load scan-config.yaml from filesystem (for local/baked-in mode)."""
    if repo_root:
        path = repo_root / "scan-config.yaml"
    else:
        path = Path(os.environ.get("CANON_SCAN_CONFIG", "scan-config.yaml"))
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _resolve_connector(scan_config: dict, domain: str, model: str) -> dict[str, str]:
    """Resolve domain + model (connector id) → workspace_id + dataset_id."""
    # Validate that the model belongs to the domain
    domain_cfg = None
    for d in scan_config.get("domains", []):
        if d["name"] == domain:
            domain_cfg = d
            break
    if domain_cfg is None:
        raise ValueError(f"Domain '{domain}' not found in scan-config")

    # Find the connector
    connector = None
    for c in scan_config.get("connectors", []):
        if c["id"] == model:
            connector = c
            break
    if connector is None:
        raise ValueError(f"Model (connector) '{model}' not found in scan-config")

    if connector["type"] != "fabric_semantic":
        raise ValueError(f"Connector '{model}' is type '{connector['type']}', not fabric_semantic")

    options = connector.get("options", {})
    workspace_id = options.get("workspace_id")
    dataset_id = options.get("dataset_id")
    if not workspace_id or not dataset_id:
        raise ValueError(f"Connector '{model}' missing workspace_id or dataset_id in options")

    return {"workspace_id": workspace_id, "dataset_id": dataset_id}


async def _acquire_obo_token(user_token: str) -> str:
    """Exchange user's token for a Fabric-scoped token via On-Behalf-Of."""
    tenant_id = os.environ["CANON_AUTH_TENANT_ID"]
    client_id = os.environ["CANON_AUTH_CLIENT_ID"]
    client_secret = os.environ["CANON_AUTH_CLIENT_SECRET"]

    authority = f"https://login.microsoftonline.com/{tenant_id}"
    app = msal.ConfidentialClientApplication(
        client_id,
        authority=authority,
        client_credential=client_secret,
    )
    result = app.acquire_token_on_behalf_of(
        user_assertion=user_token,
        scopes=[_POWER_BI_SCOPE],
    )
    if "access_token" not in result:
        error = result.get("error_description", result.get("error", "Unknown OBO error"))
        raise PermissionError(f"OBO token exchange failed: {error}")
    return result["access_token"]


async def _call_fabric_execute_queries(
    workspace_id: str, dataset_id: str, dax: str, user_token: str
) -> dict[str, Any]:
    """Call Fabric executeQueries API with the user's OBO token."""
    url = _EXECUTE_QUERIES_URL.format(workspace_id=workspace_id, dataset_id=dataset_id)
    payload = {
        "queries": [{"query": dax}],
        "serializerSettings": {"includeNulls": True},
    }
    headers = {
        "Authorization": f"Bearer {user_token}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload, headers=headers, timeout=60)
    if resp.status_code == 401:
        raise PermissionError("Fabric rejected the token — user may lack access to this semantic model")
    if resp.status_code == 400:
        error_body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        detail = error_body.get("error", {}).get("message", resp.text[:500])
        raise ValueError(f"DAX query error: {detail}")
    resp.raise_for_status()
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
                    "Use the domain slug and model (connector ID) from Canon's domain context. "
                    "Returns the query result as a table."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "domain": {
                            "type": "string",
                            "description": "Domain slug (e.g. 'retail'). Must match a domain in Canon.",
                        },
                        "model": {
                            "type": "string",
                            "description": "Connector ID for the semantic model (e.g. 'retail-semantic'). From Canon's domain context.",
                        },
                        "dax": {
                            "type": "string",
                            "description": "DAX query to execute. Must start with EVALUATE.",
                        },
                    },
                    "required": ["domain", "model", "dax"],
                },
            ),
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
            target = _resolve_connector(scan_config, domain, model)
        except ValueError as e:
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

        # Get user token from request context (injected by auth middleware)
        # In MCP SDK, tool context carries request state
        from mcp.server import request_ctx
        ctx = request_ctx.get()
        user_token = getattr(ctx, "user_token", None) if ctx else None

        if not user_token:
            return [TextContent(type="text", text=json.dumps({"error": "No authenticated user token — authentication required"}))]

        try:
            obo_token = await _acquire_obo_token(user_token)
            result = await _call_fabric_execute_queries(
                workspace_id=target["workspace_id"],
                dataset_id=target["dataset_id"],
                dax=dax,
                user_token=obo_token,
            )
            # Flatten to readable table format
            tables = result.get("results", [{}])[0].get("tables", [])
            rows = tables[0].get("rows", []) if tables else []
            return [TextContent(type="text", text=json.dumps({"rows": rows, "row_count": len(rows)}, indent=2))]
        except PermissionError as e:
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]
        except ValueError as e:
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]
        except Exception as e:
            logger.exception("Unexpected error in execute_query")
            return [TextContent(type="text", text=json.dumps({"error": f"Internal error: {type(e).__name__}: {e}"}))]

    return app


# ── Transport runners ─────────────────────────────────────────────────────────

async def run_stdio_server(repo_root: Path | None = None) -> None:
    """Stdio mode — for local Claude Desktop. Uses device-code auth (user runs login tool first)."""
    from mcp.server.stdio import stdio_server
    app = create_app(repo_root=repo_root)
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


async def run_http_server(repo_root: Path | None = None, port: int = 8001) -> None:
    """HTTP mode — for claude.ai. Uses Entra OAuth + OBO."""
    import uvicorn
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from serving.auth import AuthConfig, resource_metadata_route, create_auth_middleware

    auth_config = AuthConfig(
        tenant_id=os.environ["CANON_AUTH_TENANT_ID"],
        client_id=os.environ["CANON_AUTH_CLIENT_ID"],
        base_url=os.environ["CANON_FABRIC_PROXY_BASE_URL"],
        client_secret=os.environ.get("CANON_AUTH_CLIENT_SECRET", ""),
        required=True,
    )

    app = create_app(repo_root=repo_root)
    session_manager = StreamableHTTPSessionManager(app, json_response=True, stateless=True)

    # Wrap MCP handler with auth
    auth_wrap = create_auth_middleware(auth_config)

    async def handle_mcp(scope, receive, send):
        # Auth validation happens at the Starlette route level via middleware
        await session_manager.handle_request(scope, receive, send)

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    starlette_app = Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Route("/.well-known/oauth-protected-resource", resource_metadata_route(auth_config), methods=["GET"]),
            # MCP endpoint — auth middleware wraps this
            Route("/mcp", auth_wrap(handle_mcp), methods=["GET", "POST"]),
        ]
    )

    async with session_manager.run():
        config = uvicorn.Config(starlette_app, host="0.0.0.0", port=port, log_level="info")
        server = uvicorn.Server(config)
        logger.info("Fabric Proxy MCP running on http://0.0.0.0:%d", port)
        await server.serve()


if __name__ == "__main__":
    import asyncio
    root = Path(os.environ.get("CANON_REPO_ROOT", Path(__file__).resolve().parent.parent.parent))
    port = int(os.environ.get("CANON_FABRIC_PROXY_PORT", 8001))
    transport = os.environ.get("CANON_MCP_TRANSPORT", "streamable-http")

    if transport == "stdio":
        asyncio.run(run_stdio_server(root))
    else:
        asyncio.run(run_http_server(root, port=port))
```

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/test_fabric_proxy.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add serving/fabric_proxy/ tests/test_fabric_proxy.py
git commit -m "feat: add Fabric Proxy MCP server (execute_query + OBO)"
```

---

## Task 7: Update Canon MCP `get_domain_context` to include available models

**Files:**
- Modify: `serving/mcp/server.py`

The `get_domain_context` response must include the list of connector IDs so the LLM knows what to pass to `execute_query`. Add a `models` field to the response:

- [ ] **Step 1: Add connector IDs to domain context assembly**

When assembling domain context (both filesystem and repo_client paths), also load `scan-config.yaml` and include the semantic connectors for this domain:

```python
# In the context assembly, add:
"available_models": ["retail-semantic"]  # connector IDs associated with this domain
```

This comes from matching the domain name against `scan-config.yaml`'s `domains[].semantic_connector` (and future `semantic_connectors[]`).

- [ ] **Step 2: Run tests**

Run: `uv run python -m pytest tests/test_mcp_server.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add serving/mcp/server.py
git commit -m "feat: include available_models in domain context for Fabric Proxy routing"
```

---

## Task 8: Add CLI entrypoint for Fabric Proxy

**Files:**
- Modify: `scripts/cli.py`

- [ ] **Step 1: Add `canon serve-fabric-proxy` command**

```python
@cli.command("serve-fabric-proxy")
@click.option("--port", default=8001, type=int, help="Port for HTTP transport")
@click.option("--transport", default="streamable-http", type=click.Choice(["streamable-http", "stdio"]))
def serve_fabric_proxy(port: int, transport: str):
    """Start the Fabric Proxy MCP server."""
    import asyncio
    from pathlib import Path
    from serving.fabric_proxy.server import run_http_server, run_stdio_server

    root = Path.cwd()
    if transport == "stdio":
        asyncio.run(run_stdio_server(root))
    else:
        asyncio.run(run_http_server(root, port=port))
```

- [ ] **Step 2: Run to verify it starts**

Run: `uv run canon serve-fabric-proxy --transport stdio` (will exit immediately without input, but validates import chain)

- [ ] **Step 3: Commit**

```bash
git add scripts/cli.py
git commit -m "feat: add 'canon serve-fabric-proxy' CLI command"
```

---

## Task 9: Integration test — full flow mock

**Files:**
- Create: `tests/test_integration_flow.py`

- [ ] **Step 1: Write integration test that simulates the full Canon → Fabric flow**

```python
# tests/test_integration_flow.py
"""Integration test: Canon returns context with models, Fabric Proxy executes query."""
import json
from unittest.mock import patch, AsyncMock

import pytest

from serving.fabric_proxy.server import create_app as create_fabric_app, _resolve_connector


def test_full_flow_resolve_and_execute():
    """Verify that domain+model resolves to correct workspace/dataset."""
    scan_config = {
        "connectors": [
            {
                "id": "retail-semantic",
                "type": "fabric_semantic",
                "auth_secret_name": "X",
                "options": {
                    "workspace_id": "b53e615a-ab79-49f9-bcf0-67250a34f633",
                    "dataset_id": "d4e5f6a7-1234-5678-9abc-def012345678",
                    "dataset_name": "RetailSemanticModel",
                },
            },
        ],
        "domains": [
            {"name": "retail", "semantic_connector": "retail-semantic"},
        ],
    }

    # Step 1: Resolve (simulates what happens inside execute_query)
    target = _resolve_connector(scan_config, domain="retail", model="retail-semantic")
    assert target["workspace_id"] == "b53e615a-ab79-49f9-bcf0-67250a34f633"
    assert target["dataset_id"] == "d4e5f6a7-1234-5678-9abc-def012345678"


def test_unknown_domain_raises():
    scan_config = {"connectors": [], "domains": []}
    with pytest.raises(ValueError, match="not found"):
        _resolve_connector(scan_config, domain="finance", model="retail-semantic")
```

- [ ] **Step 2: Run test**

Run: `uv run python -m pytest tests/test_integration_flow.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_integration_flow.py
git commit -m "test: integration test for Canon → Fabric Proxy flow"
```

---

## Task 10: Dockerfile for Fabric Proxy

**Files:**
- Create: `Dockerfile.fabric-proxy`

- [ ] **Step 1: Create Dockerfile**

```dockerfile
FROM python:3.12-slim-bookworm

WORKDIR /app

# Install uv for fast dependency resolution
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install dependencies
RUN uv sync --frozen --no-dev

# Copy application code
COPY serving/ serving/
COPY connectors/ connectors/
COPY scripts/ scripts/
COPY schemas/ schemas/
COPY scan-config.yaml .

EXPOSE 8001

CMD ["uv", "run", "python", "-m", "serving.fabric_proxy.server"]
```

- [ ] **Step 2: Verify build**

Run: `docker build -f Dockerfile.fabric-proxy -t canon-fabric-proxy:test .`
Expected: builds successfully

- [ ] **Step 3: Commit**

```bash
git add Dockerfile.fabric-proxy
git commit -m "feat: add Dockerfile for Fabric Proxy MCP"
```

---

## Summary — What the customer admin does after deployment:

1. **Entra ID**: Register app, set redirect URI, assign users to security group
2. **GitHub**: Create fine-grained PAT with `contents:read`
3. **Container Apps**: Deploy both images with env vars
4. **scan-config.yaml**: Ensure `dataset_id` is filled for all semantic connectors
5. **Claude.ai**: Add two custom connectors (Canon MCP URL + Fabric Proxy URL) with the app's client_id

---

## Open items for implementation (not code — manual steps):

- [ ] Register Entra app (manual, per customer)
- [ ] Create GitHub PAT (manual, per customer)
- [ ] Deploy Container Apps (CI/CD pipeline — separate PR)
- [ ] Fill `dataset_id` in scan-config for real retail model
