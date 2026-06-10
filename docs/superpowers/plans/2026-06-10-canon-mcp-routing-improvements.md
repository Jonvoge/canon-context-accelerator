# Canon MCP Routing Improvements — Design Notes

**Date:** 2026-06-10
**Status:** Partially implemented — one pending item (cache persistence)

---

## Background

After a first live test of CanonMCP + FabricProxy in claude.ai, four root causes were identified for slow / incorrect routing:

1. `dataset_id` was empty in `scan-config.yaml` → FabricProxy hard-failed, fell back to FabricMCP
2. No Canon consumption skill → competing fabric-bi skill won routing by default
3. No schema in `get_domain_context` → agent had to probe table/column names via trial-and-error DAX
4. Weak tool descriptions → FabricProxy was not clearly tied to CanonMCP in the tool catalog

---

## Implemented (2026-06-09)

### 1. FabricProxy: dataset_id resolution by name
**File:** `serving/fabric_proxy/server.py`

`_resolve_connector` no longer hard-fails when `dataset_id` is empty. It returns
`dataset_name` alongside `workspace_id`. A new `_resolve_dataset_id_async` function
resolves the GUID via `GET /v1.0/myorg/groups/{ws}/datasets` using the already-acquired
OBO token, caching the result in `dataset_id` for the call.

All error messages now include remediation hints, e.g.:
> "Fill options.dataset_id in scan-config.yaml and redeploy."

### 2. Canon consumption skill
**File:** `skills/query-analytics/SKILL.md`

New skill defining the mandatory two-step sequence:
`CanonMCP get_domain_context` → `FabricProxy execute_query`

Covers domain routing signals, DAX patterns, parameter mapping, error table with
remediations, and Inspari brand palette for rendering. Modelled on the existing
fabric-bi skill. Install in the Claude environment; deactivate/scope-limit fabric-bi
so the two skills don't compete for the retail domain.

### 3. CanonMCP: model_schema in get_domain_context
**Files:** `serving/mcp/server.py`, `scripts/scan.py`

Scanner now writes `.canon-cache/{domain}/schema.json` after `fetch_metadata()`:
```json
{
  "tables": [{"name": "dim_product", "columns": ["category", "..."]}],
  "measures": ["Total Revenue", "Gross Margin %", ...]
}
```

`_assemble_domain` (local) and `_assemble_domain_remote` (GitHub API) both read this
file and include it as `model_schema` in the context response. Cache fingerprint updated
to invalidate on schema changes.

**Note on column visibility:** The RetailSemanticModel has all columns marked
`IsHidden=true`. The scanner filters hidden columns, so `model_schema.tables[].columns`
is currently empty for all tables. Agents should use `ontology.yaml` dimension column
references (e.g. `dim_product.category`) for DAX column names, and `model_schema.measures`
for measure names. This is sufficient for most semantic model DAX patterns.

### 4. Tool description improvements
**File:** `serving/fabric_proxy/server.py`

`execute_query` description updated to:
> "Call CanonMCP get_domain_context first — the response includes available_models
> (use one as 'model') and model_schema (table/column/measure names for writing DAX)."

`model` parameter description updated to reference `available_models` from CanonMCP.

---

## Retail domain context state (post-scan 2026-06-09)

| Layer | Status | Notes |
|---|---|---|
| `metrics.yaml` | ✓ Complete | 15 measures, all verified against live model |
| `ontology.yaml` | ✓ Adequate | 4 dimensions; `value_descriptions` empty |
| `glossary.yaml` | ~ Thin | 2 terms only |
| `domain-rules.md` | ✓ Good | Time semantics, inclusion/exclusion, exceptions |
| `data-quality.md` | ✓ Good | Known issues with tickets, refresh cadence |
| `sensitivity.yaml` | ✓ Complete | PII fields, aggregation rules |
| `schema.json` (cache) | ✓ Generated | 5 tables, 15 measures; columns empty (hidden) |
| `profiles.json` (cache) | ✓ Populated | Product Category: 5 values; Store Region: 8 values |

**Data quality finding from scan:** `Product Category` contains `"Spooooorts"` (7 o's).
This is a live data value, not a Canon issue — flag to the data owner.

---

## Pending: Cache persistence gap

### Problem

`.canon-cache/` is gitignored from `main` (`.canon-cache/.gitignore` excludes `*`).
The weekly scan workflow uploads the cache as a GitHub Actions artifact (30-day
retention) but never commits it to the repo.

The deployed Canon MCP (Azure Container App) reads files via `repo_client`
(GitHub API). Its `_assemble_domain_remote` tries to fetch
`.canon-cache/{domain}/schema.json` and `.canon-cache/{domain}/profiles.json`
from GitHub, but these files aren't in the repo — so `model_schema` is always
`null` for deployed users.

### Proposed fix

**Step 1 — Un-gitignore the MCP-serving cache files**

Edit `.canon-cache/.gitignore`:
```
# Before
*
!.gitignore

# After
*
!.gitignore
!*/schema.json
!*/profiles.json
```

This keeps noisy files (`eval-results.json`, `scan.json`, `bootstrap-report.json`)
out of git while allowing the two files the MCP server needs.

**Step 2 — Add commit step to scan workflow**

In `.github/workflows/scan.yml`, after the scan step:
```yaml
      - name: Commit scan cache to repo
        run: |
          git config user.name "canon-bot"
          git config user.email "canon-bot@users.noreply.github.com"
          git add .canon-cache/
          git diff --cached --quiet || git commit -m "chore(cache): update scan cache [skip ci]"
          git push
```

`[skip ci]` prevents a re-trigger loop. The `git diff --cached --quiet` guard
means nothing is committed if the scan produced identical output.

**Why not alternatives:**
- Uploading artifacts to blob storage: more infrastructure, more auth surface
- Baking cache into the container image: stale after every scan, forces redeploy
- Fetching at request time from Power BI API: adds latency + auth complexity to MCP
- The commit approach is the simplest path with the existing GitHub-based architecture

### Impact

After implementing: `get_domain_context` returns `model_schema` and populated
`dimension_profiles` for all users of the deployed MCP, updated every Monday scan.

---

## Open questions / future improvements

- **Column visibility:** Consider whether the hidden-column filter in
  `FabricSemanticConnector` should be relaxed or configurable. Hidden columns are
  semantically meaningful for DAX (they're still queryable). A `include_hidden_columns`
  option in `scan-config.yaml` could expose them.

- **Glossary depth:** `glossary.yaml` has only 2 terms. Worth expanding with key
  business terms from the domain (e.g. "Order Status", "Fiscal Year", "Comparable Store").

- **`value_descriptions` in ontology:** All dimensions have `value_descriptions: {}`
  empty. Adding plain-language descriptions (e.g. `"Spooooorts": "Legacy value — see DQ-003"`)
  would help agents produce better-labelled results.

- **Skill deactivation:** The existing fabric-bi skill in the Claude environment claims
  the retail domain. It should be deactivated or scoped to contoso/strava only after
  query-analytics is installed, to avoid routing competition.
