# Canon Context Accelerator — Copilot Instructions

## What This Repo Is

Canon is a git-native context layer for governed BI definitions. It stores human-authored metric, dimension, and glossary definitions alongside structural scan logic and an MCP server that exposes them to AI surfaces.

## Architecture (v5)

- **Runtime**: GitHub Actions only (no Azure, no Teams bot in default path)
- **Auth**: `GITHUB_TOKEN` + Fabric service principal credentials
- **CLI entry point**: `canon` (defined in `pyproject.toml` → `scripts.cli:main`)
- **Package manager**: `uv` (use `uv sync --frozen` to install)

## Key Commands

| Command | Purpose |
|---|---|
| `canon init --domain {slug}` | Scaffold new domain from template |
| `canon bootstrap --domain {slug}` | Generate definitions from docs + platform metadata |
| `canon scan --domain {slug}` | Detect drift between platform and authored definitions |
| `canon validate --domain {slug}` | Schema + cross-file consistency validation |
| `canon review-consistency --domain {slug}` | Detailed cross-file reference checks |
| `canon interview --domain {slug}` | Terminal interview for undocumented measures (requires `ANTHROPIC_API_KEY`) |
| `canon serve` | Start MCP server (streamable-http default, port 8000) |

## Repo Structure

```
domains/{slug}/          — authored definitions (metrics, ontology, glossary, rules, quality, sensitivity)
domains/_template/       — scaffolding template (heavily commented)
bootstrap-docs/{slug}/   — uploaded business documentation for bootstrap ingestion
connectors/              — platform adapters (fabric_semantic, fabric_sql, snowflake, databricks)
canon/bootstrap/         — bootstrap orchestrator
canon/schema/            — YAML schema validator
scripts/                 — CLI commands, scan engine, git operations
serving/mcp/             — MCP server (list_domains, get_domain_context)
schemas/                 — JSON schemas for all YAML files
evals/                   — evaluation question sets (workflow_dispatch only)
.github/workflows/       — scan.yml, bootstrap.yml, review.yml, setup.yml, eval.yml
shared/conventions.md    — naming and authoring rules
scan-config.yaml         — connector registry and domain configuration
```

## Skills

Load these for step-by-step guidance on specific workflows:

| Skill | File | When |
|---|---|---|
| Bootstrap Domain | `skills/bootstrap-domain/SKILL.md` | Onboarding a new domain |
| Health Check | `skills/health-check/SKILL.md` | Running scans and triaging drift |
| Review Consistency | `skills/review-consistency/SKILL.md` | Validating cross-file references |

## Coding Conventions

- Python 3.12, type hints on function signatures
- No comments or docstrings unless explaining non-obvious logic
- `click` for CLI, `httpx` for HTTP, `pyyaml` for YAML
- Tests in `tests/` — run with `uv run python -m pytest tests/ -q`
- Connectors inherit from `connectors.base.BaseConnector`

## Domain File Schemas

All YAML files in `domains/` are validated against JSON schemas in `schemas/`. The `_template` folder contains fully commented examples of every field.

## LLM Usage

LLM (Anthropic Claude) is optional. It enhances bootstrap definitions when `ANTHROPIC_API_KEY` is set. Without it, the bootstrap produces deterministic stubs with `# TODO` markers. Never make LLM a hard dependency for core workflows.
