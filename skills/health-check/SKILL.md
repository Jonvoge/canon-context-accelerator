# Health Check Skill

## Purpose

Guide an agent through running a structural scan and triaging drift findings for a domain.

## When to Use

- User says "scan {domain}", "health check", or "check for drift"
- After a platform change (model refresh, schema migration, new measures deployed)
- Investigating why definitions seem stale

## Prerequisites

- `scan-config.yaml` has the domain registered with valid connector(s)
- Connector credentials available as env vars (see README)
- `GITHUB_TOKEN` or `CANON_GITHUB_TOKEN` set if `--create-issues` is used

## Steps

### 1. Run the scan

```bash
canon scan --domain {slug}
```

Findings are printed to stdout grouped by severity (HIGH / MEDIUM / LOW).

### 2. Understand finding types

| Type | Meaning |
|---|---|
| `undocumented_measure` | Exists in the platform model but has no entry in `metrics.yaml` |
| `orphaned_definition` | Exists in Canon but not found in the platform model |
| `missing_source` | `governed_sources[].measure` doesn't resolve in the model |
| `dimension_value_drift` | Profiled dimension values changed since last scan |

### 3. Create GitHub issues (optional)

```bash
canon scan --domain {slug} --create-issues --github-repo owner/repo
```

Each finding becomes a GitHub issue. Domain owners listed in `scan-config.yaml → owners` are `cc @mentioned` in the issue body for email notification.

### 4. Triage findings

For each finding:
- **undocumented_measure** → either run `canon bootstrap` or manually add to `metrics.yaml`
- **orphaned_definition** → confirm if the measure was removed from the platform; if so, archive or delete from `metrics.yaml`
- **missing_source** → fix the `governed_sources` pointer in `metrics.yaml`
- **dimension_value_drift** → update `ontology.yaml` enumerated values or mark dimension as non-enumerated

### 5. Validate after fixes

```bash
canon validate --domain {slug}
```

## Output

- Scan findings printed to terminal
- Optional: GitHub issues with owner mentions
- Digest message for Teams notification
