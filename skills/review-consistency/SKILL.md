# Review Consistency Skill

## Purpose
Step-by-step instructions for cross-file consistency review on PRs.

## When to Use
- PR review automation
- Manual consistency check before merging domain changes

## Steps

1. **Identify changed files** in the PR
2. **Load all domain files** for affected domain(s)
3. **Run cross-file checks:**
   - Folder name matches `domain` field in all YAML files
   - Metric names are unique within domain
   - Dimension names are unique within domain
   - No alias collides with another metric/dimension/term name
   - `depends_on` references resolve to existing metrics, dimensions, or terms
   - `sensitivity` references resolve to declared classifications
   - `related_metrics` and `related_dimensions` in glossary resolve
   - `profile_dimensions` in scan-config match `enumerate: true` dimensions
   - `last_reviewed` is not in the future
4. **Report findings** as PR review comments
5. **Block merge** if any critical consistency violation found

## Output
- PR review with inline comments on violations
- Pass/fail status check
