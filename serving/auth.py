from __future__ import annotations

import contextvars
import logging
from dataclasses import dataclass, field
from typing import Callable

import httpx
import jwt
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

_user_token_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("user_token", default=None)
_user_claims_var: contextvars.ContextVar[dict | None] = contextvars.ContextVar("user_claims", default=None)


def get_user_token() -> str | None:
    return _user_token_var.get()


async def _get_jwks(tenant_id: str) -> dict:
    if tenant_id in _JWKS_CACHE:
        return _JWKS_CACHE[tenant_id]
    url = f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, timeout=10)
        resp.raise_for_status()
    _JWKS_CACHE[tenant_id] = resp.json()
    return _JWKS_CACHE[tenant_id]


def _find_key(jwks: dict, kid: str | None) -> object | None:
    for k in jwks.get("keys", []):
        if k.get("kid") == kid:
            return jwt.algorithms.RSAAlgorithm.from_jwk(k)
    return None


async def validate_token(token: str, config: AuthConfig) -> dict | None:
    """Validate a Bearer token from Entra ID. Returns claims dict or None."""
    try:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")

        jwks = await _get_jwks(config.tenant_id)
        key = _find_key(jwks, kid)

        if key is None:
            # kid not in cache — may be a new key after rotation; fetch fresh once
            _JWKS_CACHE.pop(config.tenant_id, None)
            jwks = await _get_jwks(config.tenant_id)
            key = _find_key(jwks, kid)

        if key is None:
            logger.warning("No matching key for kid=%s after cache refresh", kid)
            return None

        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=f"api://{config.client_id}",
            issuer=f"https://login.microsoftonline.com/{config.tenant_id}/v2.0",
        )
        return claims
    except (jwt.PyJWTError, httpx.HTTPError, Exception) as e:
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
                request.state.user_claims = None
                request.state.user_token = None
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

            request.state.user_claims = claims
            request.state.user_token = token
            return await handler(request)
        return wrapped
    return decorator


def create_asgi_auth_middleware(config: AuthConfig):
    """Wraps an ASGI app with JWT auth. For wrapping raw ASGI callables like the MCP session manager."""
    def middleware(asgi_app):
        async def wrapped_asgi(scope, receive, send):
            if scope["type"] not in ("http", "websocket"):
                await asgi_app(scope, receive, send)
                return

            if not config.required:
                scope["state"] = scope.get("state", {})
                scope["state"]["user_claims"] = None
                scope["state"]["user_token"] = None
                t1 = _user_token_var.set(None)
                t2 = _user_claims_var.set(None)
                try:
                    await asgi_app(scope, receive, send)
                finally:
                    _user_token_var.reset(t1)
                    _user_claims_var.reset(t2)
                return

            # Extract Authorization header from scope
            headers = dict(scope.get("headers", []))
            auth_header = headers.get(b"authorization", b"").decode("utf-8")

            if not auth_header.startswith("Bearer "):
                await _send_401(scope, send, config, error=None)
                return

            token = auth_header[7:]
            claims = await validate_token(token, config)
            if claims is None:
                await _send_401(scope, send, config, error="invalid_token")
                return

            # Store in scope state for downstream handlers
            scope.setdefault("state", {})
            scope["state"]["user_claims"] = claims
            scope["state"]["user_token"] = token
            token_token = _user_token_var.set(token)
            claims_token = _user_claims_var.set(claims)
            try:
                await asgi_app(scope, receive, send)
            finally:
                _user_token_var.reset(token_token)
                _user_claims_var.reset(claims_token)

        return wrapped_asgi
    return middleware


async def _send_401(scope, send, config: AuthConfig, error: str | None):
    """Send a 401 Unauthorized ASGI response."""
    www_auth = f'Bearer resource_metadata="{config.base_url}/.well-known/oauth-protected-resource"'
    if error:
        www_auth += f', error="{error}"'
    await send({
        "type": "http.response.start",
        "status": 401,
        "headers": [
            (b"www-authenticate", www_auth.encode()),
            (b"content-type", b"application/json"),
        ],
    })
    await send({
        "type": "http.response.body",
        "body": b'{"error": "unauthorized"}',
        "more_body": False,
    })
