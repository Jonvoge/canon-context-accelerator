# Data Quality

## Refresh Cadence
| Source | Expected Refresh | SLA | Notes |
|---|---|---|---|
| Retail Planning (semantic model) | Daily 06:00 CET | Before 07:00 CET | Includes previous day's transactions |
| retail_lakehouse (SQL endpoint) | Daily 05:00 CET | Before 06:00 CET | Raw data load from POS system |

## Known Issues
| ID | Severity | Affected Metric/Dimension | Description | Workaround | Ticket |
|---|---|---|---|---|---|
| DQ-001 | Low | Order Status (Cancelled) | Cancelled status unreliable before 2024-03-01 | Filter to dates >= 2024-03-01 for cancellation analysis | JIRA-3891 |
| DQ-002 | Medium | Finance Reporting model | Overstates revenue by 2-4% (missing bundle exclusion) | Use Retail Planning as primary source | JIRA-4521 |

## Trust Boundaries
- Retail Planning semantic model is the governed source for all revenue questions
- Finance Reporting should never be queried by agents (known discrepancy)
- SQL endpoint is trusted for granular joins but may lag semantic model by 1 hour

## Source System Caveats
- POS system migration in 2023-Q1 caused a 3-day data gap (Jan 15-17)
- Product hierarchy restructured June 2025 (Accessories split from Electronics)

## Open Remediation Items
- Owner: Retail Analytics Team
- Next review: 2026-06-15
- Linked ticket: JIRA-4521 (Finance model alignment)
