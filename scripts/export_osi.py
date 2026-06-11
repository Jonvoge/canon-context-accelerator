"""Export Canon domain definitions to OSI (Open Semantic Interchange) format.

OSI spec: https://github.com/open-semantic-interchange/OSI/blob/main/core-spec/spec.md
Version targeted: 0.1.1 (stable)

Mapping:
  Canon ontology dimensions + schema inventory → OSI datasets / fields
  Canon metrics (usage_patterns SQL) → OSI metrics (ANSI_SQL expressions)
  Canon metric aliases → OSI ai_context.synonyms
  Canon domain rules summary → model-level ai_context.instructions
  Canon governance data (discrepancies, sensitivity, routing) → custom_extensions COMMON/canon

Limitations (by design):
  - DAX expressions are not an OSI dialect — ANSI_SQL only from warehouse usage_patterns
  - OSI import is out of scope
  - Custom_extensions payload is lossy — round-trip not guaranteed
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _load_text(path: Path) -> str | None:
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8").strip() or None


def _build_datasets(ontology: dict, schema_inventory: dict | None) -> list[dict]:
    """Map ontology dimensions to OSI datasets (one per underlying table)."""
    # Collect unique table names from dimension columns (format: table.column)
    tables: dict[str, list[dict]] = {}
    for dim in ontology.get("dimensions", []):
        col_ref = dim.get("column", "")
        if "." not in col_ref:
            continue
        table, col = col_ref.split(".", 1)
        if table not in tables:
            tables[table] = []
        field: dict = {
            "name": col,
            "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": col}]},
            "description": dim.get("name"),
            "ai_context": {
                "synonyms": dim.get("aliases", []),
            },
        }
        if dim.get("enumerate"):
            field["dimension"] = {"is_time": False}
        tables[table].append(field)

    # Add any additional columns from the schema inventory
    if schema_inventory:
        for tbl in schema_inventory.get("tables", []):
            tbl_name = tbl.get("name", "")
            if tbl_name not in tables:
                tables[tbl_name] = []
            existing_cols = {f["name"] for f in tables[tbl_name]}
            for col in tbl.get("columns", []):
                if col not in existing_cols:
                    tables[tbl_name].append(
                        {
                            "name": col,
                            "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": col}]},
                        }
                    )

    datasets = []
    for table_name, fields in sorted(tables.items()):
        datasets.append(
            {
                "name": table_name,
                "source": table_name,
                "fields": fields,
            }
        )
    return datasets


def _build_metrics(metrics_data: dict) -> list[dict]:
    """Map Canon metrics to OSI metrics using ANSI_SQL expressions from warehouse usage_patterns."""
    result = []
    for m in metrics_data.get("metrics", []):
        name = m.get("name", "")

        # Find ANSI SQL expression: prefer warehouse usage_pattern SQL, fall back to governed_sources
        sql_expression = None
        for pattern in m.get("usage_patterns", []):
            if pattern.get("sql") and pattern.get("source") in ("warehouse", "primary"):
                raw = pattern["sql"].strip()
                # Extract the aggregate expression from a SELECT ... FROM ... WHERE ... pattern
                # Take the full SQL as the expression — OSI allows this
                sql_expression = raw
                break

        if not sql_expression:
            # Derive a stub from governed_sources measure name
            measure = m.get("governed_sources", {}).get("primary", {}).get("measure", name)
            sql_expression = f"-- Fabric DAX measure: {measure} (no ANSI SQL pattern available)"

        osi_metric: dict = {
            "name": name,
            "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": sql_expression}]},
            "description": m.get("definition", "").strip(),
            "ai_context": {
                "synonyms": m.get("aliases", []),
                "instructions": m.get("routing", "").strip() or None,
            },
        }
        # Remove None values from ai_context
        osi_metric["ai_context"] = {k: v for k, v in osi_metric["ai_context"].items() if v}
        result.append(osi_metric)
    return result


def _build_canon_extension(
    metrics_data: dict,
    sensitivity: dict,
    domain_rules: str | None,
) -> dict:
    """Package Canon governance content that has no OSI home into custom_extensions."""
    canon_payload: dict = {}

    # Known discrepancies per metric
    discrepancies = {}
    for m in metrics_data.get("metrics", []):
        also_in = m.get("governed_sources", {}).get("also_exists_in", [])
        for alt in also_in:
            if alt.get("known_discrepancy"):
                discrepancies[m["name"]] = alt["known_discrepancy"].strip()
    if discrepancies:
        canon_payload["known_discrepancies"] = discrepancies

    # Sensitivity rules (classification labels only — no PII column values)
    if sensitivity:
        canon_payload["sensitivity"] = {
            "default_classification": sensitivity.get("default_classification"),
            "usage_rules": [
                {
                    "id": r.get("id"),
                    "instruction": r.get("instruction"),
                    "forbid_raw_values": r.get("forbid_raw_values", False),
                }
                for r in sensitivity.get("usage_rules", [])
            ],
        }

    if domain_rules:
        canon_payload["domain_rules"] = domain_rules

    return {
        "vendor_name": "COMMON",
        "data": json.dumps({"canon": canon_payload}, ensure_ascii=False),
    }


def export_domain(domain: str, repo_root: Path) -> dict:
    """Build and return the OSI YAML structure for a Canon domain."""
    domain_path = repo_root / "domains" / domain
    if not domain_path.exists():
        raise ValueError(f"Domain '{domain}' not found at {domain_path}")

    metrics_data = _load_yaml(domain_path / "metrics.yaml")
    ontology = _load_yaml(domain_path / "ontology.yaml")
    sensitivity = _load_yaml(domain_path / "sensitivity.yaml")
    domain_rules = _load_text(domain_path / "domain-rules.md")

    # Load schema inventory from cache if available
    cache_path = repo_root / ".canon-cache" / domain / "schema.json"
    schema_inventory: dict | None = None
    if cache_path.exists():
        schema_inventory = json.loads(cache_path.read_text(encoding="utf-8"))

    datasets = _build_datasets(ontology, schema_inventory)
    osi_metrics = _build_metrics(metrics_data)
    canon_extension = _build_canon_extension(metrics_data, sensitivity, domain_rules)

    # Build model-level ai_context instructions from domain rules summary
    instructions = (
        f"Canon governed domain: {domain}. "
        "Metrics defined here include governed filters and exclusions — "
        "use the measure expressions as defined. "
        "See custom_extensions.COMMON.canon for discrepancies, sensitivity rules, and domain routing."
    )
    if domain_rules:
        first_lines = "\n".join(domain_rules.split("\n")[:5])
        instructions = f"{instructions}\n\nDomain rules summary:\n{first_lines}"

    model: dict = {
        "name": domain,
        "description": f"Canon governed semantic model — {domain} domain",
        "ai_context": {
            "instructions": instructions,
            "synonyms": [],
        },
        "datasets": datasets,
        "metrics": osi_metrics,
        "custom_extensions": [canon_extension],
    }

    return {"semantic_model": [model]}
