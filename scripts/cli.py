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
    from scripts.scan import run_scan, create_github_issues, _get_domain_config, _load_scan_config

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
        domain_cfg = _get_domain_config(_load_scan_config(config_path), domain)
        notify = [o for o in domain_cfg.get("owners", []) if o.startswith("@")]
        digest = domain_cfg.get("digest_target", "")
        if digest.startswith("@") and digest not in notify:
            notify.append(digest)
        create_github_issues(result, github_repo, github_token, notify=notify)

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


@main.command("interview")
@click.option("--domain", required=True, help="Domain slug to interview")
@click.option("--repo-root", default=None, type=click.Path())
def interview(domain: str, repo_root: str | None):
    """Run a terminal interview to draft definitions for undocumented measures."""
    from scripts.interview import start_interview

    root = Path(repo_root) if repo_root else _REPO_ROOT
    start_interview(domain=domain, repo_root=root)


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


@main.command("serve-fabric-proxy")
@click.option("--transport", default="streamable-http", show_default=True,
              type=click.Choice(["stdio", "streamable-http"]))
@click.option("--port", default=8001, show_default=True, envvar="CANON_FABRIC_PROXY_PORT")
@click.option("--repo-root", default=None, type=click.Path(), envvar="CANON_REPO_ROOT")
def serve_fabric_proxy(transport: str, port: int, repo_root: str | None):
    """Start the Fabric Proxy MCP server."""
    import asyncio
    from serving.fabric_proxy.server import run_http_server, run_stdio_server

    root = Path(repo_root) if repo_root else _REPO_ROOT

    if transport == "stdio":
        asyncio.run(run_stdio_server(root))
    else:
        asyncio.run(run_http_server(root, port=port))


@main.command("bootstrap")
@click.option("--domain", required=True, help="Domain slug to bootstrap")
@click.option("--config", default="scan-config.yaml", help="Path to scan config")
@click.option("--repo-root", default=None, type=click.Path())
@click.option("--no-pr", is_flag=True, default=False, help="Skip branch and PR creation")
@click.option("--dry-run", is_flag=True, default=False, help="Parse and draft but do not commit")
def bootstrap(domain: str, config: str, repo_root: str | None, no_pr: bool, dry_run: bool):
    """Bootstrap domain definitions from platform scan + uploaded documentation."""
    from canon.bootstrap.orchestrator import run_bootstrap

    root = Path(repo_root) if repo_root else _REPO_ROOT
    config_path = root / config

    click.echo(f"Bootstrapping domain: {domain}")
    if dry_run:
        click.echo("  (dry run — no branch or PR will be created)")

    report = run_bootstrap(
        domain=domain,
        config_path=config_path,
        repo_root=root,
        create_pr=not no_pr,
        dry_run=dry_run,
    )

    if report.error:
        click.echo(click.style(f"BOOTSTRAP ERROR: {report.error}", fg="red"))
        sys.exit(1)

    high = sum(1 for d in report.drafts if d.confidence == "high")
    medium = sum(1 for d in report.drafts if d.confidence == "medium")
    low = sum(1 for d in report.drafts if d.confidence == "low")

    click.echo(click.style(f"✓ Bootstrapped {len(report.drafts)} measures:", fg="green"))
    click.echo(f"  High confidence (doc+platform): {high}")
    click.echo(f"  Medium confidence (platform only): {medium}")
    click.echo(f"  Low confidence (needs review): {low}")

    needs_review = [d.measure_name for d in report.drafts if d.needs_interview]
    if needs_review:
        click.echo(click.style(f"\n⚠ {len(needs_review)} measures need interview before merge:", fg="yellow"))
        for name in needs_review:
            click.echo(f"  - {name}")

    if report.pr_url:
        click.echo(f"\nPR: {report.pr_url}")


if __name__ == "__main__":
    main()
