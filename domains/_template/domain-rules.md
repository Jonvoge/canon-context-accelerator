# Domain Rules — __DOMAIN_SLUG__

<!--
HOW TO USE THIS FILE:
  Complete every section before merging. Agents use these rules to decide how to
  answer questions, which sources to query, and how to interpret results.

  This file is NOT validated by JSON Schema — but the review.yml workflow checks
  that required section headers are present. Do not remove headers.

  Commit message convention: canon(__DOMAIN_SLUG__): <what changed>
-->

## Scope

<!--
  What business questions does this domain cover?
  What does it explicitly NOT cover (to avoid ambiguity with other domains)?
  Be precise. "Sales data" is too vague. "Retail transaction data from POS systems,
  excluding e-commerce and B2B contracts" is correct.
-->

**Covers:**
- TODO: list what this domain covers

**Out of scope:**
- TODO: list what is explicitly excluded and where to find it instead

---

## Query Routing Rules

<!--
  Agents will follow these rules to decide which source to query.
  The semantic model is usually the primary source; the warehouse is a fallback.
  Be specific about when each applies.
-->

**Primary source (semantic model):**
- TODO: describe when to use the semantic model and what questions it handles best

**Warehouse fallback:**
- TODO: describe when the warehouse fallback should be used (e.g. row-level detail,
  historical data before migration, non-aggregated queries)

**Explicitly forbidden sources:**
- TODO: list any sources agents must never use for this domain, and why

---

## Time Semantics

<!--
  Ambiguous time handling causes wrong answers. Define it explicitly.
-->

**Default date field:** TODO (e.g. `dim_date.transaction_date`)

**Fiscal calendar:** TODO (e.g. calendar year Jan-Dec, or fiscal year Feb-Jan)

**Partial period handling:**
- TODO: describe how to handle month-to-date, year-to-date, and incomplete periods.
  For example: "Current month is always partial. Do not compare partial months to
  full prior months without explicit user instruction."

---

## Inclusion and Exclusion Rules

<!--
  These rules apply to ALL queries in this domain unless overridden by a metric definition.
-->

**Always include:**
- TODO

**Always exclude:**
- TODO (e.g. "Exclude cancelled orders from all revenue calculations")

---

## Comparison Rules

<!--
  How should agents handle period-over-period and cohort comparisons?
  Incorrect comparisons (e.g. comparing partial weeks to full weeks) are a common failure mode.
-->

**Allowed comparisons:**
- TODO (e.g. "Year-over-year: same calendar period. Month-over-month: same week count.")

**Comparable-store / like-for-like logic:**
- TODO (if applicable)

---

## Known Business Exceptions

<!--
  Temporary or permanent exceptions to the rules above. Include effective dates.
-->

| Exception | Effective Date | Expiry | Owner | Description |
|---|---|---|---|---|
| *(none)* | | | | |

---

## Approval Log

<!--
  Record every significant change to domain rules here.
  This is the audit trail. Do not edit or delete past entries.
-->

| Date | Approver | Summary |
|---|---|---|
| TODO | TODO | Initial domain rules |


## Query routing

- **Aggregates / KPIs → execute_metric (DAX, primary semantic model)**
- **Detail rows, joins, exports, columns absent from the model → execute_metric SQL pattern, else execute_sql**
- Agent must state in its answer which path produced the number.
