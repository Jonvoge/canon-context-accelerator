---
name: query-analytics
description: Guides Claude through analytics queries using CanonMCP + FabricProxy — mandatory get_domain_context before any DAX, correct model routing, schema-aware query writing, and branded result rendering.
---

# SKILL.md — Canon Analytics Query Guidance

This file is Claude environment-level skill guidance for the **CanonMCP + FabricProxy** two-server setup.
Install it in the Claude environment that calls these MCP servers.

It is not runtime data — do not load it in either MCP server.

---

## Purpose and scope

Use this skill to keep routing and query behavior consistent when using:

- **CanonMCP** (`canon-mcp`) — domain context server. Exposes `list_domains` and `get_domain_context`.
- **FabricProxy** (`canon-fabric-proxy`) — DAX execution server. Exposes `execute_query`.

The two servers are designed to work together. CanonMCP gives you everything you need to write a correct
DAX query; FabricProxy executes it against the live Fabric semantic model.

Treat the rules below as hard requirements, not suggestions.

---

## Non-negotiable runtime sequence

```
1. CanonMCP: get_domain_context(domain)
2. FabricProxy: execute_query(domain, model, dax)
3. Render result
```

**Step 1 is mandatory before any DAX.** CanonMCP's response contains:

| Field | Use |
|---|---|
| `available_models` | List of valid connector ids — pick one for the `model` param in `execute_query` |
| `model_schema.tables` | Table names and column lists — use these when writing DAX |
| `model_schema.measures` | Measure names available in the semantic model |
| `metrics` | Business definitions, formatting rules, and canonical measure names |
| `ontology` | Dimension columns, enumerated values, aliases |
| `glossary` | Business term definitions |
| `domain_rules` | Routing rules and known discrepancies |
| `data_quality` | Known data gaps and caveats to surface to users |

You cannot skip `get_domain_context`. The schema varies per domain and changes over time.

---

## Domain routing

If the domain is obvious from the request, call `get_domain_context(domain=<slug>)` directly.
If ambiguous, call `list_domains` first to see what is available.

Route by signal words:

| Domain | Route when request mentions |
|---|---|
| `retail` | product, category, brand, budget, margin, e-commerce, online sales |
| `contoso` | store, in-store, brick-and-mortar, customer demographics, occupation, gender |
| `strava` | run, ride, hike, swim, activity, pace, distance, heart rate, fitness |

If a request mixes domains or is underspecified, call `list_domains` and then `get_domain_context` for
the most likely match, then confirm with the user if needed.

---

## Writing DAX queries

After calling `get_domain_context`:

1. Use `model_schema.tables` for exact table names — do not guess or invent table names.
2. Use `model_schema.measures` for measure names. If a measure from `metrics` appears in `model_schema.measures`, reference it by name directly in DAX (e.g., `[Total Revenue]`).
3. Use `ontology.dimensions[].column` for dimension column references (format: `'TableName'[ColumnName]`).
4. Use `domain_rules` (from `domain_rules` field) to avoid known discrepancies.

**DAX must start with `EVALUATE`.**

Typical patterns:

```dax
-- Measure by dimension
EVALUATE
SUMMARIZECOLUMNS(
    'dim_date'[year],
    "Total Revenue", [Total Revenue]
)
ORDER BY 'dim_date'[year]

-- Filter slice
EVALUATE
CALCULATETABLE(
    SUMMARIZECOLUMNS(
        'dim_product'[category],
        "Revenue", [Total Revenue]
    ),
    'dim_date'[year] = 2025
)
```

---

## execute_query parameters

| Parameter | Value |
|---|---|
| `domain` | The domain slug from `get_domain_context`, e.g. `"retail"` |
| `model` | One of the ids from `available_models`, e.g. `"retail-semantic"` |
| `dax` | A DAX query starting with `EVALUATE` |

The `model` value must come from `available_models` — never hardcode or guess a connector id.

---

## Error handling

| Error | Likely cause | Fix |
|---|---|---|
| `Connector '...' missing workspace_id or dataset_id` | `scan-config.yaml` options incomplete | Fill `options.dataset_id` and redeploy the FabricProxy container |
| `Dataset '...' not found in workspace` | `dataset_name` mismatch | Verify the exact name in Power BI service and update `scan-config.yaml` |
| `Connector '...' is not the semantic connector` | Wrong `model` value | Use `available_models` from `get_domain_context` |
| `DAX error` | Invalid DAX syntax or unknown column/measure | Cross-check table/column names against `model_schema` |
| `OBO token acquisition failed` | User not authenticated | User needs to re-authenticate via the MCP OAuth flow |

When the proxy returns an error, relay the full message to the user — it includes remediation hints.

---

## Rendering results

`execute_query` returns `{ "rows": [...], "row_count": N }`.

- For single numbers: present as a formatted KPI using the `formatting` spec from the metric definition in `metrics`.
- For time series / breakdowns: render as a Chart.js HTML artifact using the brand palette below.
- For tables: render as a markdown table or HTML table.

**Never silently swallow an empty result.** If `row_count` is 0, tell the user and suggest why (filter too narrow, wrong dimension value, data not yet loaded).

### Brand color palette

| Priority | Name | Hex | Use |
|---|---|---|---|
| 1 | Inspari Teal | `#00A4BD` | Primary series, KPI highlights |
| 2 | Dark Navy | `#003C43` | Secondary series, headings |
| 3 | Warm Tan | `#C4A68C` | Third series, accents |
| 4 | Inspari Cyan | `#00AFC3` | Fourth series, gradients |
| 5 | Slate Blue | `#334B5B` | Fifth series, backgrounds |
| 6 | Deep Brown | `#786455` | Sixth series |

- Single series → always Inspari Teal.
- Multi-series → assign colors in priority order above.
- Background: `#FFFFFF`. Alternating sections: `#EEEEEE`.
- Font stack: `Inter, "Segoe UI", -apple-system, sans-serif`.
- Card border-radius: 8–12px.

---

## Final checklist before answering

1. Identify the domain from the request.
2. Call `CanonMCP get_domain_context(domain)` — mandatory.
3. Check `domain_rules` and `data_quality` for caveats to surface.
4. Use `available_models[0]` as the `model` parameter for `execute_query`.
5. Use `model_schema` for exact table/column/measure names.
6. Build DAX query starting with `EVALUATE`.
7. Call `FabricProxy execute_query(domain, model, dax)`.
8. Render result using brand palette and metric formatting rules.
