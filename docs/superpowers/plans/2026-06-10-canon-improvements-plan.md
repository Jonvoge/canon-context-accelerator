# Canon Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Work the phases in order — Phase 1 unblocks everything else.

**Goal:** Make the CanonMCP → FabricProxy consumption path reliable, self-describing, and governed end-to-end, based on field findings from live use in claude.ai on 2026-06-10.

**Repo:** `Jonvoge/canon-context-accelerator` (main)

**Tech stack:** Python 3.12, uv, mcp SDK, msal, httpx, Starlette, pytest

-----

## Background — what happened in the field

A claude.ai session asked for retail revenue via CanonMCP + FabricProxy. Observed failures, in causal order:

1. `FabricProxy:execute_query` failed every call with `Connector 'retail-semantic' missing workspace_id or dataset_id`. Root cause: `scan-config.yaml` ships `dataset_id: ""` and `_resolve_connector()` in `serving/fabric_proxy/server.py` reads **only** `dataset_id`, never `dataset_name` — while the config file’s own comments tell the user to configure `dataset_name`. The bad config was baked into the container image (`Dockerfile.fabric-proxy` does `COPY scan-config.yaml .`), so it failed at first query in production, not at boot.
1. The agent fell back to a competing FabricMCP server. Nothing in Canon’s served context or tool descriptions tells an agent the intended routing (CanonMCP context → FabricProxy execution, `model` = connector id from config).
1. Even on the fallback path, the agent burned 3 queries discovering `dim_date` physical column names (`year`, `month_number`, `month_name` — lowercase snake_case). Canon’s `get_domain_context` includes dimension mappings but **no physical schema inventory** for the semantic model tables.
1. Nothing validates that agent-written DAX honors governed definitions (e.g. Total Revenue’s bundle exclusion). Governance is honor-system at the execution layer.

-----

## Phase 1 — Fix FabricProxy connector resolution (CRITICAL, do first)

### Task 1.1 — Resolve `dataset_name` → `dataset_id` at runtime

**Files:** `serving/fabric_proxy/server.py`

- [ ] In `_resolve_connector()` (~line 29): if `options.dataset_id` is empty but `options.dataset_name` is set, return a sentinel indicating name-based resolution is needed.
- [ ] Add `async def _resolve_dataset_id(workspace_id, dataset_name, token) -> str` that calls `GET https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/datasets`, matches on `name` (case-sensitive, per scan-config comment), and returns the dataset `id`.
- [ ] Cache the resolution in-process (`dict[(workspace_id, dataset_name)] -> dataset_id`); invalidate on a 404 from `executeQueries`.
- [ ] Error if neither `dataset_id` nor `dataset_name` is set, or if name lookup finds 0 or 2+ matches. Error text must name the file and field: `"Connector '<id>': set options.dataset_id or options.dataset_name in scan-config.yaml and redeploy"`.

### Task 1.2 — Validate config at startup, fail loudly

**Files:** `serving/fabric_proxy/server.py`, `serving/mcp/server.py`, `schemas/scan-config.schema.json`

- [ ] Update `scan-config.schema.json`: for `type: fabric_semantic`, require `workspace_id` AND (`dataset_id` OR `dataset_name`), with `minLength: 1` so empty strings fail. For `fabric_sql`, require non-empty `server` and `database`.
- [ ] On server startup (`create_app` / `run_http_server`), validate the loaded scan-config against the schema. On failure: log every violation with file path + JSON pointer, then `sys.exit(1)`. A proxy that cannot serve any query must not pass health checks.
- [ ] Align the scan-config comment block with reality: document both `dataset_id` (preferred, no extra API call) and `dataset_name` (resolved at runtime).

### Task 1.3 — Stop baking config into the image

**Files:** `Dockerfile.fabric-proxy`, `deploy.ps1`, `serving/fabric_proxy/server.py`

- [ ] Support env overrides `CANON_FABRIC_WORKSPACE_ID` / `CANON_FABRIC_DATASET_ID` / `CANON_FABRIC_DATASET_NAME` taking precedence over scan-config values (the scan-config comments already promise this for Actions; make the proxy honor the same convention).
- [ ] Alternatively/additionally: load scan-config via `serving/repo_client.py` from the repo at startup (same pattern CanonMCP already uses), so config fixes are a git push, not an image rebuild. Keep file-based load as fallback for stdio/local mode.
- [ ] Update `deploy.ps1` to pass the env vars to the Container App.

**Tests (`tests/test_fabric_proxy.py`):**

- [ ] empty `dataset_id` + valid `dataset_name` → name resolution path called, query succeeds (mock httpx)
- [ ] both empty → startup validation exits non-zero
- [ ] duplicate dataset names in workspace → clear error
- [ ] env var override beats file value

**Acceptance:** with the current production `scan-config.yaml` content, the proxy either works (via name resolution) or refuses to boot with an actionable message. No more first-query surprises.

-----

## Phase 2 — Make Canon self-describing for agents (routing)

### Task 2.1 — Serve routing instructions from CanonMCP

**Files:** `serving/mcp/server.py`, `serving/fabric_proxy/server.py`

- [ ] Add MCP server-level `instructions` (supported in `create_initialization_options`) to **both** servers describing the protocol: “1) `list_domains` → 2) `get_domain_context(domain)` → 3) execute via FabricProxy `execute_query(domain, model, dax)` where `model` is the domain’s semantic connector id from the served context. Do not fall back to other Fabric tools for governed domains.”
- [ ] Include the domain’s `semantic_connector` id and `warehouse_connector` id explicitly in `get_domain_context` output (an `execution:` section), so the agent never guesses the `model` argument.
- [ ] Expand FabricProxy’s `execute_query` tool description: state that valid `model` values come from CanonMCP’s `get_domain_context` → `execution.semantic_connector`, that DAX must start with EVALUATE, and that results reflect governed measures only when governed measure names are used.

### Task 2.2 — Ship a consumption skill

**Files:** new `skills/consume-domain/SKILL.md`

- [ ] Write a SKILL.md (mirror the structure/tone of `skills/health-check/SKILL.md`) defining: mandatory call sequence, model-argument rule, fallback policy (retry proxy once; if proxy is down, tell the user — do not silently switch to ungoverned tools), and answer-formatting rules (cite metric name + source model + filters applied, flag partial periods per domain rules).
- [ ] Add a README section “Using Canon from an agent” linking the skill and showing one full worked example (question → context → DAX → answer).

**Acceptance:** a fresh agent session given only the two MCP servers (no human routing hints) follows the intended path. Verify manually in claude.ai.

-----

## Phase 3 — Physical schema in served context

### Task 3.1 — Scan emits a model schema inventory

**Files:** `canon/` scan modules, `connectors/fabric_semantic.py`, `connectors/fabric_sql.py`

- [ ] During scan, capture table → column inventory (name, data type) for each connector the domain references. For `fabric_semantic`, pull tables/columns/measures via the existing metadata path the scan already uses for drift detection; for `fabric_sql`, query `INFORMATION_SCHEMA.COLUMNS`.
- [ ] Write it to the machine cache (`.canon-cache/<domain>/schema-inventory.json`) alongside existing scan outputs, with a `captured_at` timestamp.

### Task 3.2 — `get_domain_context` includes the inventory

**Files:** `serving/mcp/server.py`

- [ ] Append a compact `physical_schema` section to the served context: per table, a single line of columns (`dim_date: date_key, date, year, month_number, month_name, ...`). Compact format — this must not blow up context size.
- [ ] If no inventory exists (scan never ran), state that explicitly: `physical_schema: not yet captured — run canon scan`. Agents handle a stated gap better than a silent one.

**Tests:** scan against a mocked connector produces the inventory; MCP context includes it; missing cache produces the explicit notice.

**Acceptance:** an agent can write correct DAX/SQL column references on the first attempt without probing queries.

-----

## Phase 4 — Governed execution path (declarative queries)

This closes the biggest structural gap: execution derived from authored definitions instead of free-hand DAX.

### Task 4.1 — Extend metrics schema with DAX patterns

**Files:** `schemas/metrics.schema.json`, `domains/retail/metrics.yaml`, `domains/_template/metrics.yaml`

- [ ] `usage_patterns` entries currently carry `source` + `sql`. Add optional `dax` (parameterized, e.g. `{GROUP_BY_COLUMN}`, `{START}`, `{END}`) and a `pattern_id` slug.
- [ ] Author DAX patterns for the retail domain’s core metrics (at minimum: Total Revenue scalar, Total Revenue by period, Total Revenue by dimension), each encoding the governed filters (Completed status, bundle exclusion) inside the pattern.

### Task 4.2 — Add `execute_metric` tool to FabricProxy

**Files:** `serving/fabric_proxy/server.py`, `serving/repo_client.py`

- [ ] New tool `execute_metric(domain, metric, group_by?, filters?, period_start?, period_end?)`. The proxy loads the metric’s pattern from the domain definitions (via repo_client), substitutes parameters with strict allow-listing (group_by/filter columns must exist in the ontology or schema inventory — reject anything else; never string-interpolate raw user input into DAX beyond validated identifiers and ISO dates).
- [ ] Response includes provenance: `{rows, row_count, governed: true, metric, pattern_id, filters_applied}`.
- [ ] Keep `execute_query` (raw DAX) as the escape hatch, but add `"governed": false` to its responses and mention in its tool description that `execute_metric` is preferred for defined metrics.

### Task 4.3 — Minimal sensitivity enforcement

**Files:** `serving/fabric_proxy/server.py`, `schemas/sensitivity.schema.json`

- [ ] Parse the domain’s `sensitivity.yaml` at startup. Support two machine-enforceable rule types: `blocked_columns` (result columns matching these names are rejected with an explanatory error) and `min_group_size` (rows in grouped results with a row-count below N are suppressed, with a notice in the response).
- [ ] Apply to **both** `execute_metric` and raw `execute_query` results. This converts sensitivity from agent guidance into a control — crude is fine; absent is not.

**Tests:** pattern substitution rejects unknown columns; injection attempt in `filters` is rejected; blocked column in result → error; small groups suppressed.

**Acceptance:** the retail eval questions answerable via `execute_metric` return numbers identical to manually-written governed DAX, and a raw query selecting a blocked column fails.

-----

## Phase 5 — Context delivery scaling (scoped retrieval)

- [ ] Add CanonMCP tool `get_metric_context(domain, metric)` returning only: the metric definition, its governed sources + discrepancies, applicable domain rules, related glossary terms, the execution section, and relevant physical schema tables. Target ≤ 20% of full-context token size.
- [ ] Add `resolve(domain, question)` that matches against metric names + `trigger_aliases` (the alias data already exists in `list_domains`) and returns the top metric(s) with confidence — agents call this, then `get_metric_context`.
- [ ] Keep `get_domain_context` unchanged for exploratory use; mention in its description that scoped tools are preferred for single-metric questions.
- [ ] Extend `evals/run_evals.py` with a mode that uses scoped context instead of the full blob and compares pass rates — this is the evidence the scoping doesn’t hurt accuracy.

**Acceptance:** single-metric questions need ≤ 2 CanonMCP calls and materially fewer context tokens, with eval pass rate ≥ full-context baseline.

-----

## Phase 6 — OSI export (strategic, low effort)

Open Semantic Interchange (open-semantic-interchange.org, spec v0.1.x) standardizes datasets/relationships/fields/metrics with `ai_context` and `custom_extensions`. Canon’s differentiation (discrepancies, routing rules, drift) sits above OSI — so export, don’t restructure.

- [ ] New `scripts/export_osi.py` + CLI verb `canon export --domain <slug> --format osi --out <file>`.
- [ ] Mapping: ontology dimensions + schema inventory → OSI `datasets`/`fields` (use `ANSI_SQL` dialect expressions from existing SQL mappings); metrics → OSI `metrics` (ANSI_SQL expression from `usage_patterns` SQL; synonyms from `trigger_aliases` → `ai_context.synonyms`); domain rules summary → model-level `ai_context.instructions`.
- [ ] Canon-specific content with no OSI home (known_discrepancies, sensitivity, governed sources) → one `custom_extensions` entry, `vendor_name: COMMON`, JSON payload under a `canon` key.
- [ ] Validate output against the OSI repo’s schema if one is published; otherwise structural self-check. Add a round-trip-safety note in README: export is lossy by design (OSI can’t carry governance), import is out of scope for now.
- [ ] DAX is not an OSI dialect yet — do **not** invent one; emit ANSI_SQL only and note the limitation.

**Acceptance:** `canon export --domain retail --format osi` produces a YAML file that parses, follows the spec’s class structure, and round-trips Canon’s metric names/synonyms/descriptions.

-----

## Cross-cutting requirements

- [ ] Every new/changed error message must state the fix, not just the failure (file, field, action).
- [ ] All new YAML surface area gets JSON Schema coverage in `schemas/` with the `yaml-language-server` directive.
- [ ] Follow repo conventions: branch `canon/<type>/<domain>/<slug>`, commits `canon(<scope>): <imperative summary>`, PR per phase, machine cache never merges to main.
- [ ] Run `uv run pytest` and `uv run canon validate` green before each PR.
- [ ] Update `docs/superpowers/plans/` with this plan file and check off tasks as completed.

## Out of scope (explicitly)

- Snowflake/Databricks connector validation (consider removing from main until proven — separate decision).
- OSI import.
- Full row-level security — Phase 4.3 is deliberately minimal enforcement, not RLS.

## Priority order and rationale

1. **Phase 1** — production is hard-down for the proxy path; nothing else is testable until fixed.
1. **Phase 2** — cheapest fix for the routing failures observed in the field.
1. **Phase 3** — eliminates the probing latency; also feeds Phases 4 and 6.
1. **Phase 4** — the structural differentiator: execution derived from governed definitions.
1. **Phase 5** — matters before domains grow past ~30 metrics.
1. **Phase 6** — strategic positioning, independent of the rest, can run in parallel any time after Phase 3.