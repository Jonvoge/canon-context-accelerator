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
    fetched_at: float


class RepoClient:
    def __init__(self, config: RepoConfig, cache_ttl_seconds: int = 60) -> None:
        self._config = config
        self._cache: dict[str, _CacheEntry] = {}
        self._ttl = cache_ttl_seconds
        self._http_client = httpx.AsyncClient()

    async def aclose(self) -> None:
        await self._http_client.aclose()

    async def fetch_file(self, path: str) -> str:
        cached = self._cache.get(path)
        if cached and (time.time() - cached.fetched_at) < self._ttl:
            return cached.content

        if self._config.provider == "github":
            content = await self._fetch_github(path)
        elif self._config.provider == "ado":
            content = await self._fetch_ado(path)
        else:
            raise ValueError(f"Unknown provider: {self._config.provider}")

        self._cache[path] = _CacheEntry(content=content, fetched_at=time.time())
        return content

    async def fetch_file_or_none(self, path: str) -> str | None:
        try:
            return await self.fetch_file(path)
        except FileNotFoundError:
            return None

    async def _fetch_github(self, path: str) -> str:
        url = f"https://api.github.com/repos/{self._config.owner}/{self._config.repo}/contents/{path}"
        params = {"ref": self._config.branch}
        headers = {
            "Authorization": f"Bearer {self._config.token}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        resp = await self._http_client.get(url, params=params, headers=headers, timeout=15)
        if resp.status_code == 404:
            raise FileNotFoundError(f"File not found: {path}")
        resp.raise_for_status()
        data = resp.json()
        return base64.b64decode(data["content"]).decode("utf-8")

    async def _fetch_ado(self, path: str) -> str:
        url = (
            f"https://dev.azure.com/{self._config.org}/{self._config.project}"
            f"/_apis/git/repositories/{self._config.repo}/items"
        )
        params = {"path": path, "versionDescriptor.version": self._config.branch, "api-version": "7.1"}
        headers = {"Authorization": f"Basic {base64.b64encode(f':{self._config.token}'.encode()).decode()}"}
        resp = await self._http_client.get(url, params=params, headers=headers, timeout=15)
        if resp.status_code == 404:
            raise FileNotFoundError(f"File not found: {path}")
        resp.raise_for_status()
        return resp.text
