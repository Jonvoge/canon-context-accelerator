from unittest.mock import patch

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from serving.auth import AuthConfig, create_auth_middleware, resource_metadata_route, validate_token


@pytest.fixture
def auth_config():
    return AuthConfig(
        tenant_id="a7ed0222-1883-488c-8bbb-6ee4f043da6d",
        client_id="test-client-id",
        base_url="https://canon-mcp.example.com",
    )


def test_resource_metadata_endpoint(auth_config):
    app = Starlette(
        routes=[
            Route("/.well-known/oauth-protected-resource", resource_metadata_route(auth_config), methods=["GET"]),
        ]
    )
    client = TestClient(app)
    resp = client.get("/.well-known/oauth-protected-resource")
    assert resp.status_code == 200
    body = resp.json()
    assert "authorization_servers" in body
    assert "https://canon-mcp.example.com" in body["authorization_servers"]
    assert body["resource"] == "https://canon-mcp.example.com"


def test_unauthenticated_request_returns_401(auth_config):
    async def dummy(request):
        return JSONResponse({"ok": True})

    app = Starlette(
        routes=[
            Route("/.well-known/oauth-protected-resource", resource_metadata_route(auth_config), methods=["GET"]),
            Route("/mcp", create_auth_middleware(auth_config)(dummy), methods=["POST"]),
        ]
    )
    client = TestClient(app)
    resp = client.post("/mcp", json={})
    assert resp.status_code == 401
    assert "resource_metadata" in resp.headers["www-authenticate"]


def test_required_false_bypasses_auth():
    """When required=False, request passes through without auth."""
    config = AuthConfig(
        tenant_id="test-tenant",
        client_id="test-client",
        base_url="https://example.com",
        required=False,
    )

    async def dummy(request):
        assert request.state.user_token is None
        assert request.state.user_claims is None
        return JSONResponse({"ok": True})

    app = Starlette(
        routes=[
            Route("/mcp", create_auth_middleware(config)(dummy), methods=["POST"]),
        ]
    )
    client = TestClient(app)
    resp = client.post("/mcp", json={})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_validate_token_returns_none_on_network_error():
    """JWKS fetch failure returns None, not exception."""
    config = AuthConfig(
        tenant_id="test-tenant",
        client_id="test-client",
        base_url="https://example.com",
    )
    from serving.auth import _JWKS_CACHE

    _JWKS_CACHE.clear()

    with patch("serving.auth._get_jwks", side_effect=httpx.ConnectError("network down")):
        result = await validate_token("fake.token.here", config)
        assert result is None
