"""Canon CLI entry point."""

import click


@click.group()
def main():
    """Canon — The Context Accelerator."""
    pass


@main.command()
@click.option("--domain", required=True, help="Domain slug to validate")
def validate(domain: str):
    """Validate domain YAML files against schemas."""
    click.echo(f"Validating domain: {domain}")
    # Phase 1 implementation
    raise NotImplementedError("Phase 1 implementation")


@main.command()
@click.option("--domain", required=True, help="Domain slug to scan")
@click.option("--config", default="scan-config.yaml", help="Path to scan config")
def scan(domain: str, config: str):
    """Run structural scan for a domain."""
    click.echo(f"Scanning domain: {domain} with config: {config}")
    # Phase 3 implementation
    raise NotImplementedError("Phase 3 implementation")


@main.command()
def serve():
    """Start the Canon MCP server."""
    import asyncio
    from serving.mcp.server import app
    from mcp.server.stdio import stdio_server

    async def run():
        async with stdio_server() as (read_stream, write_stream):
            await app.run(read_stream, write_stream, app.create_initialization_options())

    asyncio.run(run())


@main.command("init")
@click.option("--domain", required=True, help="New domain slug")
def init_domain(domain: str):
    """Scaffold a new domain from template."""
    click.echo(f"Initializing domain: {domain}")
    # Phase 1 implementation
    raise NotImplementedError("Phase 1 implementation")


if __name__ == "__main__":
    main()
