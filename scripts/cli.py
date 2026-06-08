"""Canon CLI entry point."""

from __future__ import annotations

import sys
from pathlib import Path

import click

_REPO_ROOT = Path(__file__).resolve().parent.parent


@click.group()
def main():
    """Canon — The Context Accelerator."""
    pass


@main.command()
@click.option("--domain", default=None, help="Specific domain to validate (default: all)")
@click.option("--repo-root", default=None, type=click.Path(), help="Path to repo root")
def validate(domain: str | None, repo_root: str | None):
    """Validate domain YAML files against schemas and cross-file rules."""
    import json
    from canon.schema.validator import validate_all_domains, validate_domain

    root = Path(repo_root) if repo_root else _REPO_ROOT

    if domain:
        domain_path = root / "domains" / domain
        results = {domain: validate_domain(domain_path, repo_root=root)}
    else:
        results = validate_all_domains(root)

    any_error = False
    for d, result in results.items():
        if result.ok:
            click.echo(click.style(f"✓ {d}: valid", fg="green"))
        else:
            any_error = True
            click.echo(click.style(f"✗ {d}: {len(result.errors)} error(s), {len(result.warnings)} warning(s)", fg="red"))
            for f in result.errors:
                click.echo(f"  ERROR [{f.rule}] {f.message}")
            for f in result.warnings:
                click.echo(click.style(f"  WARN  [{f.rule}] {f.message}", fg="yellow"))

    sys.exit(1 if any_error else 0)


@main.command()
@click.option("--domain", required=True, help="Domain slug to scan")
@click.option("--config", default="scan-config.yaml", help="Path to scan config")
@click.option("--repo-root", default=None, type=click.Path(), help="Path to repo root")
@click.option("--create-issues/--no-create-issues", default=False, help="Create GitHub issues for findings")
@click.option("--github-repo", default=None, envvar="GITHUB_REPOSITORY", help="GitHub repo slug (owner/repo)")
@click.option("--github-token", default=None, envvar="GITHUB_TOKEN", help="GitHub token for issue creation")
def scan(domain: str, config: str, repo_root: str | None, create_issues: bool, github_repo: str | None, github_token: str | None):
    """Run structural scan for a domain."""
    from scripts.scan import run_scan, create_github_issues

    root = Path(repo_root) if repo_root else _REPO_ROOT
    config_path = root / config

    click.echo(f"Scanning domain: {domain}")
    result = run_scan(domain, config_path, root)

    if result.error:
        click.echo(click.style(f"SCAN ERROR: {result.error}", fg="red"))
        sys.exit(1)

    _print_findings(result)

    if create_issues and github_repo and github_token:
        click.echo("Creating GitHub issues...")
        create_github_issues(result, github_repo, github_token)

    sys.exit(1 if any(f.severity == "high" for f in result.findings) else 0)


def _print_findings(result) -> None:
    if not result.findings:
        click.echo(click.style("No findings.", fg="green"))
        return
    for f in result.findings:
        color = "red" if f.severity == "high" else "yellow" if f.severity == "medium" else "white"
        click.echo(click.style(f"  [{f.severity.upper()}] {f.type}: {f.subject}", fg=color))
        click.echo(f"    {f.description}")


@main.command()
@click.option("--transport", default="streamable-http", show_default=True,
              type=click.Choice(["stdio", "sse", "streamable-http"]))
@click.option("--port", default=8000, show_default=True, envvar="CANON_MCP_PORT")
@click.option("--repo-root", default=None, type=click.Path(), envvar="CANON_REPO_ROOT")
def serve(transport: str, port: int, repo_root: str | None):
    """Start the Canon MCP server."""
    import asyncio
    from serving.mcp.server import create_app, run_http_server, run_stdio_server

    root = Path(repo_root) if repo_root else _REPO_ROOT

    if transport == "stdio":
        asyncio.run(run_stdio_server(root))
    else:
        asyncio.run(run_http_server(root, port=port))


@main.command("init")
@click.option("--domain", required=True, help="New domain slug")
@click.option("--repo-root", default=None, type=click.Path())
def init_domain(domain: str, repo_root: str | None):
    """Scaffold a new domain from template."""
    from scripts.canon_init import init_domain as _init

    root = Path(repo_root) if repo_root else _REPO_ROOT
    path = _init(domain, root)
    click.echo(click.style(f"✓ Domain '{domain}' created at {path}", fg="green"))
    click.echo("Next steps:")
    click.echo(f"  1. Edit domains/{domain}/metrics.yaml")
    click.echo(f"  2. Edit domains/{domain}/ontology.yaml")
    click.echo(f"  3. Upload docs to bootstrap-docs/{domain}/")
    click.echo(f"  4. Run: canon validate --domain {domain}")


if __name__ == "__main__":
    main()
