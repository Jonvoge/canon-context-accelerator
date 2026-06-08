# Health Check Skill

## Purpose
Step-by-step instructions for running the structural scan and reviewing drift findings.

## When to Use
- Scheduled weekly scan
- Manual health check before a domain review
- After platform changes (model refresh, schema migration)

## Steps

1. **Load scan-config.yaml** for domain connector bindings and policy
2. **Connect to platform** via configured connector
3. **Fetch current metadata** (measures, tables, columns, relationships)
4. **Load authored definitions** from `domains/{domain}/`
5. **Compute structural differences:**
   - Undocumented measures (in platform, not in Canon)
   - Orphaned definitions (in Canon, not in platform)
   - Name/alias mismatches
   - Missing sources (authored pointers that don't resolve)
6. **Profile enumerated dimensions** (if configured)
7. **Diff profiled values** against cached values
8. **Write findings** to `.canon-cache/{domain}/scan.json`
9. **Create GitHub issues** for high-priority findings
10. **Generate digest** for domain owners

## Output
- Updated `.canon-cache/{domain}/scan.json`
- GitHub issues for drift findings
- Digest message for Teams notification
