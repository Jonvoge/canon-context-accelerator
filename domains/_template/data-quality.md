# Data Quality — __DOMAIN_SLUG__

<!--
HOW TO USE THIS FILE:
  Document known data issues here so agents can surface caveats instead of returning
  wrong answers confidently. Every entry here reduces hallucination risk.

  Commit message convention: canon(__DOMAIN_SLUG__): <what changed>
-->

## Refresh Cadence

<!--
  When does each source refresh? Agents use this to warn users about data latency.
  Add a row for each source this domain queries.
-->

| Source | Expected Refresh | SLA | Notes |
|---|---|---|---|
| TODO | TODO | TODO | |

---

## Known Issues

<!--
  Document ongoing data quality issues. Include a workaround where possible.
  Remove entries when the issue is resolved (log the resolution date in the ticket).
-->

| ID | Severity | Affected Metric/Dimension | Description | Workaround | Ticket |
|---|---|---|---|---|---|
| DQ-001 | TODO | TODO | TODO | TODO | TODO |

---

## Trust Boundaries

<!--
  Which sources are trustworthy for which question types?
  Help agents avoid confidently wrong answers from unreliable sources.
-->

**Trusted for financial reporting:**
- TODO

**Trusted for operational queries:**
- TODO

**Known to lag or be incomplete:**
- TODO (e.g. "fact_sales has a 24-hour ingestion lag. Do not use for same-day queries.")

---

## Source System Caveats

<!--
  Platform-specific issues that affect query results.
-->

- TODO: list any platform-specific caveats (e.g. schema migrations, calculation changes)

**Historical breakpoints:**
- TODO: list dates where data methodology changed and results are not comparable across the break

---

## Open Remediation Items

| Item | Owner | Priority | Next Review | Linked Ticket |
|---|---|---|---|---|
| TODO | TODO | TODO | TODO | TODO |
