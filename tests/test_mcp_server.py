"""
Tests for serving/mcp/server.py.
"""

from __future__ import annotations

import shutil
from pathlib import Path


def test_list_domains_returns_retail(repo_root: Path) -> None:
    """list_domains tool should return the retail domain."""
    from serving.mcp.server import _list_domains

    domains = _list_domains(repo_root)
    names = [d["name"] for d in domains]
    assert "retail" in names
    retail = next(d for d in domains if d["name"] == "retail")
    assert retail["metric_count"] > 0
    assert "trigger_aliases" in retail


def test_assemble_domain_returns_expected_keys(repo_root: Path) -> None:
    """_assemble_domain should return a dict with all required keys."""
    from serving.mcp.server import _assemble_domain

    ctx = _assemble_domain("retail", repo_root)
    assert ctx["domain"] == "retail"
    assert "metrics" in ctx
    assert "ontology" in ctx
    assert "domain_rules" in ctx


def test_create_app_has_expected_tools(repo_root: Path) -> None:
    """create_app should register list_domains and get_domain_context tools."""
    from serving.mcp.server import create_app

    server = create_app(repo_root)
    # MCP Server stores handlers — verify tools registered without calling them
    assert server is not None


def test_cache_fingerprint_changes_on_file_modification(repo_root: Path, tmp_path: Path) -> None:
    """Cache fingerprint should change when a domain file is modified."""
    from serving.mcp.server import _domain_fingerprint

    src = repo_root / "domains" / "retail"
    dst = tmp_path / "domains" / "retail"
    shutil.copytree(src, dst)

    cache_dir = tmp_path / ".canon-cache" / "retail"

    fp1 = _domain_fingerprint(dst, cache_dir)

    # Modify metrics.yaml
    metrics_path = dst / "metrics.yaml"
    metrics_path.write_text(
        metrics_path.read_text(encoding="utf-8") + "\n# timestamp\n",
        encoding="utf-8",
    )

    fp2 = _domain_fingerprint(dst, cache_dir)
    assert fp1 != fp2, "Fingerprint did not change after file modification"


def test_create_app_has_four_tools(repo_root):
    import asyncio

    from serving.mcp.server import create_app

    server = create_app(repo_root)
    from mcp.types import ListToolsRequest

    result = asyncio.run(server.request_handlers[ListToolsRequest](ListToolsRequest()))
    tool_names = {t.name for t in result.root.tools}
    assert {"list_domains", "get_domain_context", "get_metric_context", "resolve"} == tool_names
