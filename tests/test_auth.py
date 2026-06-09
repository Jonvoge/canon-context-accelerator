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
