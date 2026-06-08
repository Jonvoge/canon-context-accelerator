# Canon — The Context Accelerator

> Define your business context once. Keep it honest. Serve it everywhere.

Canon is a git-backed context layer where an organization authors business definitions once and serves them to any AI agent on any data platform. A scheduled, read-only scan keeps the authored context honest by flagging where reality has drifted from the definitions.

## Three Verbs, One Source of Truth

1. **Author** — Data Owners (with agent assistance) write business definitions, reviewed and approved through a lightweight change-request workflow.
2. **Scan** — A scheduled, read-only check compares platform state against authored definitions and surfaces every discrepancy as a tracked finding.
3. **Serve** — Agents fetch current context on demand via MCP, or Canon pushes it into platform-native AI surfaces.

## Repository Structure

```
domains/           Human-authored domain context (one subfolder per business domain)
  _template/       Copy source for new domains
  retail/          Example domain with full definitions
shared/            Repository-wide conventions
schemas/           JSON Schema definitions for validation
connectors/        Platform adapters (Power BI, Fabric SQL, Snowflake, Databricks)
serving/mcp/       MCP server for agent consumption
scripts/           Operational scripts (scan, init, interview, review)
evals/             Evaluation harness (questions + runner)
skills/            Agent skill definitions (bootstrap, health-check, review)
bootstrap-docs/    Uploaded source documentation (used during bootstrap only)
.canon-cache/      Machine-owned cache (gitignored from main)
.github/           Workflows, issue templates, PR template
```

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) for dependency management
- Azure credentials for Power BI / Fabric connectors (service principal)

## Quickstart

```bash
# Clone and install
git clone https://github.com/Jonvoge/canon-context-accelerator.git
cd canon-context-accelerator
uv sync

# Create a new domain
uv run canon init --domain my-domain

# Validate all domain files
uv run canon validate

# Run a structural scan
uv run canon scan --domain retail --config scan-config.yaml

# Start MCP server
uv run canon serve
```

## Adding a New Domain

1. Run `uv run canon init --domain <slug>` to scaffold from `_template/`
2. Fill in `metrics.yaml`, `ontology.yaml`, `glossary.yaml`
3. Write `domain-rules.md` and `data-quality.md`
4. Configure `scan-config.yaml` with connector bindings
5. Open a PR for review

## Schema Validation

All YAML files validate against JSON Schema definitions in `schemas/`. Editors with YAML language server support get autocomplete via the `# yaml-language-server: $schema=` directive.

## Contributing

- Branch naming: `canon/<type>/<domain>/<slug>`
- Commit format: `canon(<scope>): <imperative summary>`
- All domain changes require PR review from domain CODEOWNERS
- Machine-generated cache never merges to `main`

## License

MIT
