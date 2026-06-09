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
        content = resp.text
        sha = resp.headers.get("ETag", str(time.time()))
        return content, sha
