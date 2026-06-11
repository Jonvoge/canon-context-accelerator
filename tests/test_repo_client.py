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
async def test_cache_hit_within_ttl(github_config):
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
        assert call_count == 1
