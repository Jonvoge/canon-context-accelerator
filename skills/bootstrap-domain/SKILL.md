# Bootstrap Domain Skill

## Purpose
Step-by-step instructions for drafting an initial domain from platform scans and uploaded documentation.

## When to Use
- First-time domain creation
- Re-bootstrap after major platform changes

## Steps

1. **Load uploaded documentation** from `bootstrap-docs/{domain}/`
2. **Scan platform metadata** via the configured connector
3. **Cross-reference** documentation mentions with platform measures
4. **Draft metrics.yaml** with pre-populated definitions, sources, and routing
5. **Draft ontology.yaml** with dimensions found in the model
6. **Draft glossary.yaml** with business terms extracted from documentation
7. **Create stubs** for domain-rules.md, data-quality.md, sensitivity.yaml
8. **Open PR** on branch `canon/bootstrap/{domain}` for human review

## Output
- Draft PR with all domain files pre-populated
- Bootstrap report in `.canon-cache/{domain}/bootstrap-report.json`
