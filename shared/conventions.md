# Shared Conventions

## Naming Rules
- Metric names: Title Case, business-friendly (e.g., "Total Revenue", "Gross Margin %")
- Dimension names: Title Case (e.g., "Order Status", "Product Category")
- Domain slugs: lowercase kebab-case (e.g., "retail", "supply-chain")
- Folder naming: match domain slug exactly

## Alias Rules
- Lowercase natural-language aliases
- Include common synonyms and conversational phrasings
- Include common misspellings if they occur frequently
- Forbidden: aliases that collide with another metric or dimension name in the same domain

## Date and Time Rules
- Timezone: CET/CEST (Europe/Copenhagen)
- Fiscal calendar: calendar year (Jan-Dec) unless domain specifies otherwise
- Relative period wording: "last month", "previous quarter", "year to date" are relative to current date
- Date format in YAML: ISO 8601 (YYYY-MM-DD)

## Currency and Unit Rules
- Currency codes: ISO 4217 three-letter codes (DKK, EUR, USD)
- Percent formatting: use % suffix, precision as specified per metric
- Scaling: use K (thousands), M (millions), B (billions) in conversational responses

## Query Example Rules
- Use `{START}` and `{END}` as date range placeholders
- SQL style: ANSI SQL, uppercase keywords
- DAX examples should use EVALUATE + SUMMARIZECOLUMNS where applicable
- Always include the relevant filter (e.g., Status = 'Completed')

## Review Rules
- Every metric must have `last_reviewed` within the past 6 months
- PR reviewers must be domain CODEOWNERS
- Deprecated metrics require a deprecation PR with routing update
