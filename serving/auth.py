from __future__ import annotations

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
