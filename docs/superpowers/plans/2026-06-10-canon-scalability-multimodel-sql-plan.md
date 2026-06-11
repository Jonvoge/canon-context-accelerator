# Canon Scalability, Multi-Model & SQL Serving — Design and Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Work the phases in order — Phase 0 unblocks deployment, Phase 1 is a prerequisite for Phase 2, Phase 2 for Phase 3.

**Goal:** Make Canon correct at HEAD, scalable to N domains × M models per domain, honest about its governance boundaries, and able to serve detail-level questions via a governed SQL path — without breaking the existing retail deployment.

**Repo:** `Jonvoge/canon-context-accelerator` (main)

**Tech stack:** Python 3.12, uv, mcp SDK, msal, httpx, Starlette, pyodbc + msodbcsql18 (new), pytest

-----

## Background — findings this plan addresses

From code review of HEAD (commit `1c0a723`) and architecture discussion, 2026-06-10:

1. **Deploy blocker:** `def create_app(...)` is missing from `serving/fabric_proxy/server.py`. Lines ~365–597 (Server construction, tool registration, handlers) are unreachable dead code nested inside `_parse_iso_to_parts` after its `return`. `run_http_server`/`run_stdio_server` raise `NameError`. Tests pass because they import only helpers.
1. **Single-model assumption in three places:** global `CANON_FABRIC_WORKSPACE_ID`/`DATASET_NAME` env vars clobber per-connector `options` in `scripts/scan.py::_build_connector` and `_apply_env_overrides` (proxy); `scan.yml` “all domains” branch hardcodes `--domain retail`. Two domains on different models cannot scan or serve correctly.
1. **Single-connector-per-domain assumption:** `semantic_connector` is a scalar in scan-config; `available_models` is a one-element list; `_resolve_connector` rejects any other model; `.canon-cache/{domain}/schema.json` collides for 2+ models; `_find_pattern` ignores which model executes. Meanwhile the authored layer (`governed_sources`: `primary`, `also_exists_in`, `warehouse`, `routing`, per-pattern `source:`) already models multi-source richly — the plumbing collapses it.
1. **No SQL serving path:** FabricProxy is DAX-only. `connectors/fabric_sql.py` exists (scan-side, SP auth), `metrics.yaml` already carries `sql:` usage patterns, but nothing executes them. DAX-only excludes detail/row-level extracts, lakehouse tables not in the model, and wide exports; `executeQueries` caps ~100K rows. The live model has all columns `IsHidden=true`, hobbling freeform DAX further.
1. **Routing weakness in deployed mode:** `_list_domains_remote` returns names only (counts null, aliases empty) — the agent routes on a slug. No per-domain description exists anywhere. `resolve` works within one domain only.
1. **Remote/local asymmetry:** remote mode drops `dimension_profiles`; proxy loads scan-config live from git but `metrics.yaml`/`sensitivity.yaml` from the baked image — metric/sensitivity edits silently require a rebuild while connector edits don’t.
1. **Sensitivity enforcement is cosmetic:** post-hoc name-based column stripping; `SELECTCOLUMNS` with an alias bypasses it; missing `sensitivity.yaml` fails open to no filtering.
1. **Authoring-flow defects:** `skills/bootstrap-domain/SKILL.md` says register domains with `slug:` but all code keys on `name:`; `interview.py` hardcodes `RetailSemanticModel`; interview is terminal-only, unusable by the Data Owner persona.
1. **Hygiene:** empty `response.json` committed at root; `.canon-cache/retail/*` committed while README says machine cache never merges; `_get_obo_and_dataset` annotated `tuple[str, str]` but returns a 3-tuple; `except (jwt.PyJWTError, httpx.HTTPError, Exception)`; `write_write` typo in `run_stdio_server`.

-----

## Phase 0 — Restore main to deployable (CRITICAL, do first)

### Task 0.1 — Restore `create_app` in FabricProxy

**Files:** `serving/fabric_proxy/server.py`

- [ ] Insert `def create_app(scan_config: dict, repo_root: Path | None = None) -> Server:` above the orphaned `app = Server("canon-fabric-proxy", ...)` block (~line 363) and dedent verification: the block from `app = Server(` through `return app` must sit at function body level, no longer inside `_parse_iso_to_parts`.
- [ ] Run `uv run canon serve-fabric-proxy --transport stdio` locally to confirm boot.
- [ ] Diff against the last image actually deployed to the Container App to confirm no other lines were lost in commit `1c0a723`.

### Task 0.2 — Smoke tests that would have caught this

**Files:** `tests/test_fabric_proxy.py`, `tests/test_mcp_server.py`, `.github/workflows/` (new `test.yml`)

- [ ] Test: `create_app({minimal valid scan_config})` returns a `Server`; `list_tools()` returns `execute_metric` and `execute_query`.
- [ ] Same for CanonMCP `create_app`: four tools (`list_domains`, `get_domain_context`, `get_metric_context`, `resolve`).
- [ ] Add `test.yml` workflow: `uv run pytest` + `uv run ruff check` on every PR. There is currently no CI test gate — that is how an undeployable main merged.

### Task 0.3 — Hygiene sweep

**Files:** various

- [ ] Delete `response.json` (empty, accidental).
- [ ] Decide `.canon-cache` policy: it IS read by the MCP server, so committing `profiles.json`/`schema.json` is intentional — update README “Contributing” to say scan outputs under `.canon-cache/` are committed by the scan workflow; `scan.json` findings are not. Align `.canon-cache/.gitignore` with the decision.
- [ ] Fix `_get_obo_and_dataset` return annotation → `tuple[str, str, str] | list[TextContent]`.
- [ ] `except (jwt.PyJWTError, httpx.HTTPError, Exception)` → `except Exception`.
- [ ] `run_stdio_server`: `write_write` → `write_stream`.
- [ ] `skills/bootstrap-domain/SKILL.md`: `slug:` → `name:` in the scan-config registration example (code keys on `name` everywhere).
- [ ] `scripts/interview.py`: replace hardcoded `RetailSemanticModel` in question 3 with connector names read from scan-config for the domain.

**Acceptance:** `main` boots both servers; CI fails any PR that breaks boot; no stray files.

-----

## Phase 1 — One source of config truth (prerequisite for Phase 2)

**Principle:** `scan-config.yaml` is authoritative for all workspace/dataset/connector topology. Environment variables carry **secrets only**. The proxy already fetches scan-config live from git (`_load_scan_config_from_repo`), so topology changes are a git push — env overrides for topology are now a liability, not a convenience.

### Task 1.1 — Remove topology env overrides

**Files:** `serving/fabric_proxy/server.py`, `scripts/scan.py`, `.env.example`, `scan-config.yaml` comments

- [ ] Delete `_apply_env_overrides` (proxy) or reduce it to a deprecation warning that logs and ignores `CANON_FABRIC_WORKSPACE_ID`/`DATASET_ID`/`DATASET_NAME` when set. (It currently applies one global value to **every** `fabric_semantic` connector — wrong the moment a second model exists.)
- [ ] `scripts/scan.py::_build_connector`: read `workspace_id`/`dataset_name`/`server`/`database` from `connector_config["options"]` only. Env supplies `tenant_id`, `client_id`, and the secret resolved via `auth_secret_name` (the per-connector secret indirection already exists — use it).
- [ ] Update `.env.example` and scan-config comment block: topology lives in scan-config; secrets in env; one secret can be shared via `auth_secret_name`.
- [ ] Migration note in PR description: Container App env vars for workspace/dataset can be removed after the scan-config values are verified non-empty (Task 1.2 from the 2026-06-10 improvements plan already schema-enforces this).

### Task 1.2 — Matrix the scan workflow over domains

**Files:** `.github/workflows/scan.yml`, `scripts/cli.py`

- [ ] Add `canon list-domains --json` CLI command emitting `["retail", ...]` from scan-config.
- [ ] `scan.yml`: a setup job runs it and emits a matrix; the scan job runs per-domain with `--domain ${{ matrix.domain }}`. Manual `domain` input bypasses the matrix.
- [ ] Same fix in `bootstrap.yml` env block: drop `CANON_FABRIC_WORKSPACE_ID`/`DATASET_NAME`.

**Tests:** unit test that `_build_connector` ignores `CANON_FABRIC_WORKSPACE_ID` when options carry a value; matrix script output shape.

**Acceptance:** two domains with two different `workspace_id`/`dataset_name` values in scan-config scan against the correct models in one scheduled run, with no per-domain workflow edits.

-----

## Phase 2 — Multiple models per domain

**Design:** a domain declares a **list** of connectors with roles and routing descriptions. The authored `governed_sources` layer already supports this; Phase 2 makes the config schema, serving, and scan match it. Every execution response carries provenance of which model answered.

### Task 2.1 — Config schema: `domains[].models`

**Files:** `schemas/scan-config.schema.json`, `scan-config.yaml`, shared loader (new `canon/config.py`)

New shape:

```yaml
domains:
  - name: retail
    path: domains/retail
    owners: ["@finance-lead"]
    models:
      - connector: retail-semantic        # connectors[].id
        role: semantic                    # semantic | warehouse
        primary: true
        description: >
          Retail Planning model — daily grain, completed orders only,
          governed measures. Use for KPI/aggregate questions.
      - connector: retail-sql
        role: warehouse
        description: >
          retail_lakehouse SQL endpoint — row-level detail, joins,
          exports. Use when the semantic model lacks the columns.
```

- [ ] Create `canon/config.py` with a single `load_scan_config(...)` used by scan, CanonMCP, and FabricProxy (today there are three loaders). It normalizes legacy keys — `semantic_connector` → `models: [{connector, role: semantic, primary: true}]`, `warehouse_connector` → warehouse entry — and logs a deprecation warning. Schema accepts both shapes for one release.
- [ ] Schema: `models` requires ≥1 entry, exactly one `primary: true` among `role: semantic` entries (or zero semantic entries for SQL-only domains), `description` required (it is the routing signal — see Phase 4).
- [ ] `canon validate` validates scan-config too, not just domain folders.

### Task 2.2 — Serving: `available_models` becomes structured

**Files:** `serving/mcp/server.py` (local + remote assemble paths)

- [ ] `available_models` → list of `{id, role, primary, description, platform_type}` built from the normalized config. **Both** `_assemble_domain` and `_assemble_domain_remote` — remote must never be the degraded path.
- [ ] Update CanonMCP server `instructions` and `get_domain_context` tool description: “pick `model` from `available_models` using its `description`; prefer `primary` for governed aggregates.”

### Task 2.3 — Schema cache keyed by connector

**Files:** `scripts/scan.py`, `serving/mcp/server.py`

- [ ] Scan writes `.canon-cache/{domain}/{connector_id}/schema.json` and `profiles.json` (profiling declares its connector — see Task 2.5).
- [ ] `model_schema` in `get_domain_context` becomes `{connector_id: schema}`. `_domain_fingerprint` includes every connector’s cache files.
- [ ] One-time migration: move existing `retail` cache files under `retail/retail-semantic/`; keep a fallback read of the old path for one release so a stale clone still serves.

### Task 2.4 — FabricProxy: membership, defaulting, pattern binding

**Files:** `serving/fabric_proxy/server.py`, `schemas/metrics.schema.json`, `domains/_template/metrics.yaml`

- [ ] `_resolve_connector(scan_config, domain, model)`: model must be **a member** of the domain’s `models` list (not equal to the single connector). If `model` is omitted, default to the primary semantic connector.
- [ ] Pattern selection respects the execution target: `_find_pattern(metrics, metric, group_by, connector_role)` filters `usage_patterns` by compatibility — `source: semantic_model` patterns run on `role: semantic` connectors, `source: warehouse` on `role: warehouse`. Optional explicit `connector:` field on a pattern pins it to one model (schema addition, optional).
- [ ] Error messages list the valid models for the domain with their descriptions.

### Task 2.5 — Scan: loop all models, model-scoped reconciliation

**Files:** `scripts/scan.py`, `scan-config.yaml` (`profile_dimensions` shape), `.github/ISSUE_TEMPLATE/`

- [ ] `run_scan` iterates every `models[]` entry for the domain; `ScanResult` gains per-connector sections (or one `ScanResult` per connector aggregated in the report — pick one, keep `scan.json` shape versioned).
- [ ] **Model-scoped findings:** a measure documented with `governed_sources.primary` pointing at model A is not “orphaned” because model B lacks it. Reconcile each definition against the model its `governed_sources` names; flag `missing_source` only when the *named* source doesn’t resolve.
- [ ] **New finding type `discrepancy_unverified`:** for each `also_exists_in` entry, scan the named model and report whether the measure still exists there. This turns the known-discrepancy annotation from prose into a checked invariant — a scan capability that is impossible with one model per domain and a strong demo story.
- [ ] `profile_dimensions` entries declare which connector profiles them (`{dimension: ..., connector: ...}`); default to primary semantic.

### Task 2.6 — Provenance everywhere

**Files:** `serving/fabric_proxy/server.py`

- [ ] Every `execute_metric`/`execute_query` (and Phase 3 `execute_sql`) response includes `"model": <connector_id>` and `"role": semantic|warehouse`.
- [ ] When the answering model matches an `also_exists_in` entry with `known_discrepancy`, append the discrepancy text to `notices`. Multi-model must not silently reintroduce the two-numbers problem inside Canon’s own tools — the user always learns which model answered and what its known biases are.

**Tests:** legacy→new config normalization; membership resolution incl. omitted model defaulting; pattern filtering by role; per-connector cache paths; orphan check not firing cross-model; discrepancy notice emission.

**Acceptance:** a domain with one semantic model + one SQL endpoint (retail, after Phase 3) and a second test domain with a *different* semantic model both: scan cleanly in one run, serve correct `available_models` with descriptions in remote mode, and stamp every query response with the model that answered.

-----

## Phase 3 — SQL serving path (governed fallback)

**Design:** SQL widens use cases (detail, joins, exports, un-modeled tables) but raw SQL bypasses semantic-model measures — the exact two-numbers problem Canon exists to kill. So SQL ships in three tiers of decreasing governance, each labeled: (1) authored `sql:` patterns via `execute_metric` (`governed: true`), (2) raw `execute_sql` with guardrails (`governed: false`), (3) never silent — routing rules tell agents when each tier applies and responses say which ran.

### Task 3.1 — Transport: OBO → SQL endpoint

**Files:** `Dockerfile.fabric-proxy`, `serving/fabric_proxy/sql_exec.py` (new), `serving/fabric_proxy/server.py`

- [ ] Add `msodbcsql18` + `unixodbc` to `Dockerfile.fabric-proxy` (apt, `ACCEPT_EULA=Y`). Note: image grows ~100MB; acceptable.
- [ ] New `sql_exec.py`: acquire OBO token for scope `https://database.windows.net/.default` (parallel to the Power BI OBO in `_acquire_obo_token` — refactor to `_acquire_obo_token(user_token, scope)`), connect via pyodbc using the `SQL_COPT_SS_ACCESS_TOKEN` plumbing already written in `connectors/fabric_sql.py::_get_token_bytes` (move it to a shared module).
- [ ] Connection settings: `LoginTimeout=10`, query timeout 30s, read-only intent (`ApplicationIntent=ReadOnly` where supported), connections not pooled across users (token-per-user — pool keyed by user OID with short TTL if perf demands later, not in v1).
- [ ] Run pyodbc calls via `asyncio.to_thread` — it is blocking.

### Task 3.2 — `execute_metric` learns SQL patterns

**Files:** `serving/fabric_proxy/server.py`

- [ ] When the resolved connector has `role: warehouse`, `_find_pattern` selects from `source: warehouse` patterns and `_substitute_params` substitutes into the `sql:` template (`{START}`/`{END}` raw ISO usage is already anticipated in the code comments; identifier params keep the existing allow-list regex).
- [ ] Response: `governed: true`, `model`, `pattern_id` — identical provenance shape to the DAX path.

### Task 3.3 — `execute_sql` raw tool with guardrails

**Files:** `serving/fabric_proxy/server.py`

- [ ] New tool `execute_sql(domain, model, sql)` where `model` must be a `role: warehouse` member of the domain.
- [ ] Statement gate (defense-in-depth on top of OBO permissions, not a substitute): single statement only (reject `;` outside string literals), must match `^\s*(WITH|SELECT)\b` case-insensitive, reject `INTO`, `EXEC`, `OPENROWSET`, `OPENQUERY` tokens. Keep the gate dumb and strict; do not attempt full SQL parsing.
- [ ] Server-side row cap (wrap in `SELECT TOP (N)` if absent, N=10,000 default, configurable per domain) and the 30s timeout.
- [ ] Apply sensitivity redaction (Phase 5) — easier here than in DAX: blocked columns matched against cursor description names *and* a check that no blocked base-column name appears as an identifier in the SQL text (catches trivial aliasing; documented as advisory).
- [ ] Response: `governed: false`, `model`, `row_count`, notices.
- [ ] Tool description: “Use only after execute_metric cannot answer (no pattern fits, detail/join required). Call CanonMCP get_domain_context first; honor domain rules (e.g. status=‘Completed’ filters).”

### Task 3.4 — Routing guidance for the two paths

**Files:** `domains/_template/domain-rules.md`, `domains/retail/domain-rules.md`, `skills/query-analytics/SKILL.md`, server `instructions`

- [ ] Codify the decision rule in domain-rules and the consumption skill: **aggregates/KPIs → execute_metric (DAX, primary model); detail rows, joins, exports, columns absent from the model → execute_metric SQL pattern, else execute_sql.** Agent must state in its answer which path produced the number.
- [ ] Update the per-metric `routing:` prose in retail metrics.yaml to reference the new tool names.

### Task 3.5 — Verify the actual governance boundary

**Files:** docs

- [ ] Before claiming user-level governance on the SQL path: verify what the OBO user can actually read on the lakehouse SQL endpoint (workspace role vs item permissions vs RLS). Document the result in `docs/`. If users hold broad workspace roles, `execute_sql` exposes everything those roles allow — that is a deployment decision per client, and the plan’s guardrails (SELECT-only, row cap, redaction) are *not* the security boundary. Say so explicitly in README and the pitch material.

**Tests:** statement gate accept/reject matrix (CTE accepted, `;` chain rejected, `select * into` rejected, comments handled); TOP-injection; SQL pattern substitution; mocked pyodbc execution; redaction on SQL rows.

**Acceptance:** “list the 20 largest completed orders last month with customer name” — impossible via the semantic model — answers via execute_sql under the user’s identity, capped, labeled `governed: false`, with model provenance; “total revenue 2025” still routes to the governed DAX path.

-----

## Phase 4 — Routing at N domains + remote-mode parity

### Task 4.1 — Compiled domain index (minimal compile step)

**Files:** `scripts/build_index.py` (new), `.github/workflows/` (on merge to main), `serving/mcp/server.py`

- [ ] CI job on merge generates `domains/_index.json`: per domain — `name`, `description` (new required field, source: first paragraph of `domain-rules.md` or a `description:` key in a small `domain.yaml`; pick the `domain.yaml` option so it’s schema-validated), `metric_count`, `dimension_count`, `trigger_aliases` (capped), `models` summaries.
- [ ] `list_domains` serves from the index in **both** modes: local reads the file, remote fetches it — one repo_client call instead of N. This kills the names-only remote listing.
- [ ] This deliberately resurrects the “compile step” from the original architecture in its smallest useful form. Keep it to this one artifact; no bundles.

### Task 4.2 — Cross-domain `resolve`

**Files:** `serving/mcp/server.py`

- [ ] `resolve(question, domain=None)`: when `domain` is omitted, score across all domains using the index (names + aliases + description tokens), return top 3 `(domain, metric, confidence)`. Existing token-overlap scorer is sufficient to ~10 domains; an `# upgrade path: embeddings` comment marks the ceiling. Do not build embeddings now.
- [ ] Skill update: for ambiguous questions, call `resolve` first, then `get_metric_context(domain, metric)` — steering agents to scoped retrieval and keeping token cost flat as domains grow.

### Task 4.3 — Remote is a superset of local

**Files:** `serving/mcp/server.py`, `serving/fabric_proxy/server.py`

- [ ] `_assemble_domain_remote` fetches `.canon-cache/{domain}/{connector}/profiles.json` (currently silently `{}`).
- [ ] FabricProxy loads `metrics.yaml` and `sensitivity.yaml` via `repo_client` when `CANON_REPO_PROVIDER` is set (same pattern as its scan-config load), with the 60s TTL cache; local file fallback for stdio. This closes the “metric edits need an image rebuild but connector edits don’t” inconsistency.
- [ ] Add a one-line architecture rule to README: *anything a serving path reads must be readable from git at runtime; the image carries code, not content.*

**Acceptance:** with `CANON_REPO_PROVIDER=github`, editing a metric’s DAX pattern or sensitivity rule takes effect within the cache TTL, no rebuild; `list_domains` from the deployed server shows descriptions and aliases.

-----

## Phase 5 — Sensitivity honesty

**Files:** `serving/fabric_proxy/server.py`, README, `schemas/sensitivity.schema.json` docs

- [ ] Rename the concept in docs and response fields: `sensitivity_notices` → keep, but document `_enforce_sensitivity` as **advisory redaction**, not enforcement. State plainly: real enforcement is OBO identity + platform RLS/OLS; name-based stripping is bypassed by `SELECTCOLUMNS` aliasing.
- [ ] Fail-closed on parse errors: `sensitivity.yaml` exists but is invalid → return an error from the tool, do not silently serve unredacted. File genuinely absent → log a warning once per domain, proceed (absence is a valid authored state).
- [ ] Apply redaction on the SQL path (Task 3.3) using cursor column names.

-----

## Phase 6 — Authoring flow for the Data Owner persona

### Task 6.1 — Issue-driven interview

**Files:** `canon/bootstrap/orchestrator.py`, new `.github/workflows/interview-intake.yml`, `scripts/interview.py`

- [ ] Bootstrap opens one GitHub issue per low-confidence measure containing the five interview questions (label `canon-interview`, assigned to domain owners from scan-config). The questions are already good; the terminal is the wrong channel for a Data Owner.
- [ ] An issue-comment-triggered workflow parses the owner’s answers, runs the existing `_draft_metric` LLM call, and pushes the drafted entry as a commit to the open bootstrap PR (or a new PR), linking back to the issue.
- [ ] Keep `canon interview` (terminal) as the consultant fast path; both feed the same drafting function.

### Task 6.2 — Multi-domain dry run as release gate

- [ ] Stand up a second dummy domain on a different (tiny) semantic model and run the full loop: init → register (`models` list) → bootstrap → interview issue → merge → scheduled matrix scan → query both domains via claude.ai in one conversation.
- [ ] Treat this as the acceptance test for Phases 1–4 combined. The slug/`name` mismatch and env clobbering were exactly the class of bug only a second domain surfaces; do not pitch multi-domain capability before this run passes.

-----

## Cross-cutting requirements

- **One config loader** (`canon/config.py`) shared by scan, CanonMCP, FabricProxy — normalization, schema validation, legacy-key handling live in exactly one place.
- **Back-compat for one release:** legacy `semantic_connector`/`warehouse_connector` keys and old cache paths keep working with deprecation warnings; remove in the release after the dummy-domain dry run passes.
- **CI gate** (Phase 0): pytest + ruff on every PR; both servers must construct.
- **Provenance invariant:** no tool response ever omits which connector answered.
- **Error messages name the file and field to fix** (existing convention — keep it).

## Out of scope (explicitly)

- Embeddings-based resolve (token-overlap ceiling documented instead)
- Next.js portal; full compile step beyond `_index.json`
- Snowflake/Databricks connector implementations (stubs stay stubs; the `models` schema is platform-neutral so they slot in later)
- Genie/Cortex push sync
- Per-user connection pooling for SQL (revisit on perf evidence)
- Write operations of any kind (Canon stays read-only against platforms)

## Priority order and rationale

|Order|Phase                  |Days (est.)|Why this position                                                                                                         |
|-----|-----------------------|-----------|--------------------------------------------------------------------------------------------------------------------------|
|1    |0 — Deployable main    |0.5        |main is currently undeployable; everything else builds on it                                                              |
|2    |1 — Config truth       |1          |the env-clobbering refactor and the multi-model refactor touch the same lines; doing 1 before 2 avoids touching them twice|
|3    |2 — Multi-model        |3          |stated product requirement; unlocks the SQL phase (warehouse models) and the discrepancy-verification scan feature        |
|4    |3 — SQL serving        |2          |depends on `role: warehouse` models from Phase 2; biggest use-case expansion                                              |
|5    |4 — Routing/parity     |2          |independent of 2–3 but benefits from model descriptions; required before any multi-domain demo                            |
|6    |5 — Sensitivity honesty|0.5        |mostly docs + fail-closed; SQL redaction lands with Phase 3                                                               |
|7    |6 — Authoring flow     |1.5        |needed for the self-serve story, not for the demo; dry run gates the release                                              |

**Total: ~10.5 days.**

## Design decisions and known risks

1. **Config-in-git over env overrides.** Trades a small operational habit change (edit scan-config, push) for eliminating the entire class of “global env silently wrong for connector N” bugs. The proxy already reads scan-config from git; this just finishes the thought.
1. **`models` list with required `description`.** The description is doing double duty: human documentation and the agent’s routing signal. Making it schema-required is deliberate — an undescribed model is unroutable.
1. **SQL guardrails are not a security boundary.** OBO + platform permissions are. The statement gate, row cap, and redaction reduce blast radius and accidents; Task 3.5 forces the honest verification per deployment. Overselling this is the biggest reputational risk in the plan.
1. **Two-numbers risk is managed, not eliminated.** Multi-model + SQL means the same business question can yield different values. Mitigations: primary-model defaulting, governed/ungoverned labeling, mandatory provenance, `known_discrepancy` notices, and the new `discrepancy_unverified` scan finding. Residual risk: agents ignoring routing rules — monitored via the eval harness (add eval questions that must route to SQL and must route to DAX).
1. **`_index.json` is a compile step.** Intentionally minimal; if it grows past one artifact, that is the signal the original compile-step architecture is actually needed.
1. **pyodbc in a container.** Adds image weight and a native dependency; the alternative (no SQL) was the thing this plan exists to fix. If driver maintenance bites, the fallback is the Fabric REST `livySessions`/GraphQL surfaces — not pursued now.