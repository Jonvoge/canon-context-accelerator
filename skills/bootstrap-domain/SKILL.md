W# Bootstrap Domain Skill

## Purpose

Guide an agent through onboarding a new domain into Canon — from scaffolding to bootstrap to PR.

## When to Use

- User says "add a new domain", "onboard {name}", or "bootstrap {domain}"
- First-time domain creation
- Re-bootstrap after major platform changes

## Prerequisites

- `scan-config.yaml` must have a connector entry for the domain's data platform
- Business documentation (PDFs, markdown) should be placed in `bootstrap-docs/{domain}/`
- `ANTHROPIC_API_KEY` env var enables LLM enrichment (optional — stubs work without it)

## Steps

### 1. Scaffold the domain folder

```bash
canon init --domain {slug}
```

Creates `domains/{slug}/` from `_template` with all 6 files, plus `bootstrap-docs/{slug}/`.

### 2. Register the domain in scan-config.yaml

Add an entry under `domains:`:

```yaml
  - slug: {slug}
    semantic_connector: {connector-id}    # must match a connectors[].id
    warehouse_connector: {connector-id}   # optional
    owners: ["@github-handle"]
    profile_dimensions: []                # fill after ontology is authored
```

### 3. Add bootstrap documentation

Place PDF/markdown files in `bootstrap-docs/{slug}/`. The orchestrator parses these for measure definitions, business terms, and dimension descriptions.

### 4. Run bootstrap

```bash
canon bootstrap --domain {slug}
```

This will:
- Parse docs from `bootstrap-docs/{slug}/`
- Connect to the semantic model via the configured connector
- Cross-reference doc mentions with live platform measures
- Generate `metrics.yaml` entries (deterministic stubs or LLM-enriched if key is set)
- Open a **draft PR** on branch `canon/bootstrap/{slug}-{date}` with a review checklist

Add `--dry-run` to preview without creating a branch/PR.
Add `--no-pr` to write files locally without pushing.

### 5. Review the PR

The PR body contains a per-measure checklist. The domain owner should:
- Confirm or correct each definition
- Fill any `# TODO` fields
- Add aliases and routing instructions
- Run `canon validate --domain {slug}` before merging

### 6. Post-merge: wire the scan

After the PR is merged, the weekly `scan.yml` workflow will automatically include the new domain. Verify with:

```bash
canon scan --domain {slug}
```

## Output

- `domains/{slug}/` fully populated
- Draft PR with review checklist
- Domain registered in `scan-config.yaml`
