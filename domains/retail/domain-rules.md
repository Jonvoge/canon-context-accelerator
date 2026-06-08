# Domain Rules

## Scope
- Covers all retail sales, product, and store metrics for the Danish retail operation
- Does not cover wholesale, B2B, or marketplace channels
- Does not cover financial consolidation or group-level reporting

## Query Routing Rules
- Use the semantic model (Retail Planning) for all direct metric questions
- Use the Fabric SQL endpoint when the question requires joins or dimensions not in the semantic model
- Never query Finance Reporting (known discrepancy, overstates 2-4%)

## Time Semantics
- Default date field: `Sales.OrderDate`
- Fiscal year: calendar year (Jan-Dec)
- Partial periods: always label as "partial" in responses; do not compare partial to full periods without explicit user request
- Historical data reliable from 2023-01-01; older data has known gaps

## Inclusion and Exclusion Rules
- Revenue always excludes promotional bundles (Finance decision 2026-04-22)
- Revenue always filters to `Status = 'Completed'`
- Returns and cancelled orders are excluded from all revenue metrics

## Comparison Rules
- Year-over-year: use comparable stores only unless user asks for "total" or "all stores"
- Month-over-month: include all stores
- Like-for-like: stores open 12+ months, no major renovation

## Known Business Exceptions
- Accessories category valid from 2025-06-01 (previously part of Electronics)
- Cancelled status unreliable before 2024-03-01

## Approval Log
| Date | Approver | Summary |
|---|---|---|
| 2026-05-15 | Retail Analytics Team | Initial domain definition |
| 2026-04-22 | Finance Review | Exclude promotional bundles from revenue |
