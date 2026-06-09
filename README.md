# Canon — The Context Accelerator

> Define your business context once. Keep it honest. Serve it everywhere.

Canon is a git-backed context layer where data owners author business definitions once and serve
them to any AI agent on any data platform. A scheduled, read-only scan keeps the authored context
honest by flagging where reality has drifted from the definitions.

**One runtime. One auth path. No bot. No Azure.**

---

## Three Verbs, One Source of Truth

1. **Author** — Run `canon bootstrap` to generate a draft PR from your platform. Review and correct
   the checklist in the PR. Merge when accurate.
2. **Scan** — A weekly scheduled Action compares platform state against authored definitions and
   opens a GitHub issue for every discrepancy.
3. **Serve** — Agents fetch current context on demand via MCP, or Canon pushes it into
   platform-native AI surfaces.

---

## Repository Structure

```
domains/           Human-authored domain context (one subfolder per business domain)
  _template/       Copy source for new domains — heavily commented, safe to edit in a PR
  retail/          Example domain with full definitions
shared/            Repository-wide conventions
schemas/           JSON Schema definitions for validation
connectors/        Platform adapters (Power BI / Fabric, Fabric SQL)
serving/mcp/       MCP server for agent consumption
scripts/           CLI scripts (scan, bootstrap, init, validate)
bootstrap-docs/    Upload source documentation here before running bootstrap
.canon-cache/      Machine-owned cache (gitignored from main)
.github/workflows/ Automated workflows (scan, bootstrap, review, setup, eval)
optional/          Advanced integrations not required for v1
  teams-bot/       Teams bot + Azure Container Apps deploy (parked, revivable)
evals/             Optional eval harness for benchmarking answer quality
```

---

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) for dependency management
- Azure service principal with Power BI / Fabric `Dataset.ReadWrite.All` and `Workspace.Read.All`
- GitHub repository with Actions enabled

---

## Quickstart

```bash
# 1. Clone and install
git clone https://github.com/Jonvoge/canon-context-accelerator.git
cd canon-context-accelerator
uv sync

# 2. Run setup workflow (creates labels + setup checklist issue)
#    Trigger: Actions → Canon Setup → Run workflow
```

**3. Create a Fabric service principal and set Actions secrets**

In Azure Portal / Entra ID → App registrations → New registration:
- Note the **Application (client) ID** → `CANON_FABRIC_CLIENT_ID`
- Note the **Directory (tenant) ID** → `CANON_FABRIC_TENANT_ID`
- Certificates & secrets → New client secret → copy the value → `CANON_FABRIC_CLIENT_SECRET`

In Power BI Admin portal → Tenant settings → enable "Service principals can use Fabric APIs".
Add the SP to the workspace as a **Member** (read access is sufficient for scan).

Set in GitHub repo → Settings → Secrets and variables → Actions:

| Secret | Value |
|---|---|
| `CANON_FABRIC_CLIENT_SECRET` | SP client secret |
| `CANON_FABRIC_TENANT_ID` | Azure AD tenant GUID |
| `CANON_FABRIC_CLIENT_ID` | SP application GUID |

| Variable | Value |
|---|---|
| `CANON_FABRIC_WORKSPACE_ID` | GUID from the workspace URL: `app.powerbi.com/groups/<this-guid>/...` |
| `CANON_FABRIC_DATASET_NAME` | Exact semantic model name as shown in the workspace |

For SQL endpoint (optional — needed for warehouse queries):

| Secret | Value |
|---|---|
| `CANON_SQL_SERVER` | From workspace → Lakehouse/Warehouse → "Copy SQL connection string" |
| `CANON_SQL_DATABASE` | Database name from the same dialog |

**4. Update `scan-config.yaml`**

Open `scan-config.yaml` and update:
- `connectors[].options.workspace_id` — workspace GUID (same as `CANON_FABRIC_WORKSPACE_ID`)
- `connectors[].options.dataset_name` — semantic model name
- `connectors[].options.server` / `database` — SQL endpoint if used
- `domains[].owners` — GitHub `@handle` of the domain owner(s)

Then update `CODEOWNERS` with the same handles for the domain folder.

```bash
# 5. Bootstrap your first domain (generates a draft PR for review)
uv run canon bootstrap --domain retail --config scan-config.yaml

# 6. Review and merge the draft PR
#    The review.yml workflow validates schema and consistency on every push.

# 7. Start the MCP server
uv run canon serve
```

---

## Onboarding a New Domain

The required workflow is five steps:

1. **Upload docs** — Drop any relevant documentation into `bootstrap-docs/<domain>/`
   (PDFs, Word docs, Markdown, Excel — all parsed automatically).
2. **Run bootstrap** — `uv run canon bootstrap --domain <slug>` (or trigger `bootstrap.yml`
   from Actions). A draft PR opens on `canon/bootstrap/<slug>-<date>`.
3. **Review the PR** — The PR body is a checklist of every inferred value. Correct what's wrong.
   Fill in `# TODO` fields. The `review.yml` Action validates on every push.
4. **Merge** — Merge when all checklist items are confirmed.
5. **Schedule scan** — Enable the `scan.yml` cron. Findings appear as GitHub issues.

**Re-running bootstrap is safe (idempotent).** Already-documented measures are never modified.
Only new platform measures are added to the draft.

---

## Schema Validation

All YAML files validate against JSON Schema definitions in `schemas/`. Editors with YAML language
server support (VS Code + YAML extension) get autocomplete and inline errors via the
`# yaml-language-server: $schema=` directive at the top of each file.

---

## What's Deliberately Not Here

| Feature | Why not in v1 | Where to find it |
|---|---|---|
| Teams bot | Requires Azure + Bot Framework + 11 extra env vars | `optional/teams-bot/` |
| Eval as PR gate | Adds LLM cost and flakiness to every merge | Run manually: `eval.yml` → `workflow_dispatch` |
| SMTP/SendGrid digest | Not needed — GitHub emails @mentioned issue assignees natively | Add one Action step when a client needs it |
| Snowflake / Databricks | Connectors exist but untested with live data | `connectors/snowflake.py`, `connectors/databricks.py` |

---

## Contributing

- Branch naming: `canon/<type>/<domain>/<slug>`
- Commit format: `canon(<scope>): <imperative summary>`
- All domain changes require PR review from domain CODEOWNERS
- Machine-generated cache (`.canon-cache/`) never merges to `main`

## License

MIT
