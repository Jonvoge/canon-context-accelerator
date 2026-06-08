"""Canon MCP server — serves domain context to AI agents."""

from pathlib import Path

import yaml
from mcp.server import Server
from mcp.types import Tool, TextContent

CANON_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def load_domain_yaml(domain_path: Path, filename: str) -> dict | None:
    filepath = domain_path / filename
    if filepath.exists():
        return yaml.safe_load(filepath.read_text(encoding="utf-8"))
    return None


def load_domain_markdown(domain_path: Path, filename: str) -> str | None:
    filepath = domain_path / filename
    if filepath.exists():
        return filepath.read_text(encoding="utf-8")
    return None


def get_available_domains(repo_root: Path) -> list[dict]:
    domains_dir = repo_root / "domains"
    domains = []
    for d in sorted(domains_dir.iterdir()):
        if d.is_dir() and d.name != "_template":
            metrics = load_domain_yaml(d, "metrics.yaml")
            domains.append({
                "name": d.name,
                "path": str(d.relative_to(repo_root)),
                "metric_count": len(metrics.get("metrics", [])) if metrics else 0,
            })
    return domains


def assemble_domain_context(domain: str, repo_root: Path) -> dict:
    domain_path = repo_root / "domains" / domain
    if not domain_path.exists():
        return {"error": f"Domain '{domain}' not found"}

    context = {
        "domain": domain,
        "metrics": load_domain_yaml(domain_path, "metrics.yaml"),
        "ontology": load_domain_yaml(domain_path, "ontology.yaml"),
        "glossary": load_domain_yaml(domain_path, "glossary.yaml"),
        "sensitivity": load_domain_yaml(domain_path, "sensitivity.yaml"),
        "domain_rules": load_domain_markdown(domain_path, "domain-rules.md"),
        "data_quality": load_domain_markdown(domain_path, "data-quality.md"),
    }

    # Include dimension profiles from cache if available
    profiles_path = repo_root / ".canon-cache" / domain / "profiles.json"
    if profiles_path.exists():
        import json
        context["dimension_profiles"] = json.loads(profiles_path.read_text(encoding="utf-8"))
    else:
        context["dimension_profiles"] = {}

    return context


app = Server("canon-mcp")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="list_domains",
            description="List all available Canon domains with their descriptions and metric counts.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_domain_context",
            description="Get the full assembled context for a Canon domain including metrics, ontology, glossary, rules, and sensitivity guidance.",
            inputSchema={
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "The domain slug (e.g., 'retail')",
                    }
                },
                "required": ["domain"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    import json

    if name == "list_domains":
        domains = get_available_domains(CANON_REPO_ROOT)
        return [TextContent(type="text", text=json.dumps(domains, indent=2))]

    elif name == "get_domain_context":
        domain = arguments.get("domain", "")
        context = assemble_domain_context(domain, CANON_REPO_ROOT)
        return [TextContent(type="text", text=json.dumps(context, indent=2, default=str))]

    return [TextContent(type="text", text=f"Unknown tool: {name}")]


if __name__ == "__main__":
    import asyncio
    from mcp.server.stdio import stdio_server

    async def main():
        async with stdio_server() as (read_stream, write_stream):
            await app.run(read_stream, write_stream, app.create_initialization_options())

    asyncio.run(main())
