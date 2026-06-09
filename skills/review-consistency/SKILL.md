# Review Consistency Skill

## Purpose

Guide an agent through cross-file consistency validation for a domain, typically as part of a PR review.

## When to Use

- User says "review this PR", "check consistency", or "validate {domain}"
- Before merging any change to `domains/**`
- After bootstrap or manual edits to domain files

## Steps

### 1. Run schema + consistency validation

```bash
canon validate --domain {slug}
```

This runs both JSON schema validation and cross-file consistency checks.

### 2. Run detailed consistency review

```bash
canon review-consistency --domain {slug}
```

This checks:
- All `depends_on` entries resolve to dimensions in `ontology.yaml`
- All metric aliases are unique within the domain
- All `governed_sources` of type `semantic_model` have a non-empty `measure` field
- All glossary `related_metrics` resolve to metric names in `metrics.yaml`
- All glossary `related_dimensions` resolve to dimension names in `ontology.yaml`
- `profile_dimensions` in `scan-config.yaml` match `enumerate: true` dimensions

### 3. Interpret findings

Each finding is a human-readable string explaining the broken reference.

Common fixes:
- **Unresolved depends_on** → typo in dimension name, or dimension missing from `ontology.yaml`
- **Alias collision** → two metrics share the same alias; rename one
- **Empty governed_sources.measure** → fill in the platform measure name
- **Broken glossary reference** → metric or dimension was renamed; update the glossary entry

### 4. Fix and re-validate

After fixing, re-run:

```bash
canon validate --domain {slug}
```

Zero findings = safe to merge.

## Automation

The `review.yml` workflow runs this automatically on PRs touching `domains/**`, `shared/**`, or `scan-config.yaml`. It blocks merge on any critical violation.

## Output

- Pass/fail with human-readable findings
- In CI: status check on the PR
