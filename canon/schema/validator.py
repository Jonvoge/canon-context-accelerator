"""
Canon schema validator.

Layer 1: JSON Schema validation for each YAML file type.
Layer 2: Cross-file consistency checks (17 rules).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import jsonschema
import yaml

_SCHEMA_DIR = Path(__file__).resolve().parent.parent.parent / "schemas"


@dataclass
class Finding:
    rule: str
    file: str
    message: str
    severity: str = "error"  # error | warning


@dataclass
class ValidationResult:
    domain: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(f.severity == "error" for f in self.findings)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]


def _load_schema(name: str) -> dict:
    path = _SCHEMA_DIR / f"{name}.schema.json"
    if not path.exists():
        raise FileNotFoundError(f"Schema not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _load_yaml(path: Path) -> dict | None:
    if not path.exists():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _validate_against_schema(instance: dict, schema_name: str, filepath: str) -> list[Finding]:
    findings = []
    schema = _load_schema(schema_name)
    validator = jsonschema.Draft202012Validator(schema)
    for error in validator.iter_errors(instance):
        path = " → ".join(str(p) for p in error.absolute_path) if error.absolute_path else "root"
        findings.append(Finding(
            rule=f"schema:{schema_name}",
            file=filepath,
            message=f"{path}: {error.message}",
        ))
    return findings


def validate_domain(domain_path: Path | str, repo_root: Path | None = None) -> ValidationResult:
    """
    Run layer 1 (JSON Schema) and layer 2 (cross-file consistency) checks
    on a single domain folder.

    Accepts either:
    - validate_domain(Path("domains/retail"))
    - validate_domain("retail", repo_root)  — slug + root
    """
    if isinstance(domain_path, str):
        if repo_root is None:
            raise ValueError("repo_root is required when domain_path is a string slug")
        domain_path = repo_root / "domains" / domain_path
    domain_slug = domain_path.name
    result = ValidationResult(domain=domain_slug)

    def err(rule: str, file_name: str, msg: str) -> None:
        result.findings.append(Finding(rule=rule, file=str(domain_path / file_name), message=msg))

    def warn(rule: str, file_name: str, msg: str) -> None:
        result.findings.append(Finding(rule=rule, file=str(domain_path / file_name), message=msg, severity="warning"))

    # ── Load all domain YAML files ────────────────────────────────────────────
    metrics_raw = _load_yaml(domain_path / "metrics.yaml")
    ontology_raw = _load_yaml(domain_path / "ontology.yaml")
    glossary_raw = _load_yaml(domain_path / "glossary.yaml")
    sensitivity_raw = _load_yaml(domain_path / "sensitivity.yaml")

    # ── Layer 1: JSON Schema validation ──────────────────────────────────────
    if metrics_raw:
        result.findings.extend(_validate_against_schema(metrics_raw, "metrics", "metrics.yaml"))
    if ontology_raw:
        result.findings.extend(_validate_against_schema(ontology_raw, "ontology", "ontology.yaml"))
    if glossary_raw:
        result.findings.extend(_validate_against_schema(glossary_raw, "glossary", "glossary.yaml"))
    if sensitivity_raw:
        result.findings.extend(_validate_against_schema(sensitivity_raw, "sensitivity", "sensitivity.yaml"))

    # Stop here if schema is broken — cross-file checks will be noisy
    if result.errors:
        return result

    # ── Layer 2: Cross-file consistency ──────────────────────────────────────
    metrics_data = metrics_raw or {}
    ontology_data = ontology_raw or {}
    glossary_data = glossary_raw or {}
    sensitivity_data = sensitivity_raw or {}

    metrics_list: list[dict] = metrics_data.get("metrics", [])
    dimensions_list: list[dict] = ontology_data.get("dimensions", [])
    terms_list: list[dict] = glossary_data.get("terms", [])

    # Collect sets for cross-reference resolution
    metric_names = {m["name"] for m in metrics_list}
    dimension_names = {d["name"] for d in dimensions_list}
    term_names = {t["name"] for t in terms_list}
    all_names = metric_names | dimension_names | term_names

    # Collect declared classification IDs
    classification_ids = {c["id"] for c in sensitivity_data.get("classifications", [])}

    # Rule 1: folder name matches domain field in each file
    for filename, data in [
        ("metrics.yaml", metrics_data),
        ("ontology.yaml", ontology_data),
        ("glossary.yaml", glossary_data),
        ("sensitivity.yaml", sensitivity_data),
    ]:
        if data and data.get("domain") and data["domain"] != domain_slug:
            err("cross:domain-mismatch", filename,
                f"domain field '{data['domain']}' does not match folder name '{domain_slug}'")

    # Rule 2: metrics[].domain matches file-level domain
    for m in metrics_list:
        if m.get("domain") and m["domain"] != metrics_data.get("domain"):
            err("cross:metric-domain-mismatch", "metrics.yaml",
                f"metric '{m['name']}' has domain '{m['domain']}' but file domain is '{metrics_data.get('domain')}'")

    # Rule 3: metric names unique within domain
    seen_metric_names: set[str] = set()
    for m in metrics_list:
        if m["name"] in seen_metric_names:
            err("cross:duplicate-metric-name", "metrics.yaml",
                f"duplicate metric name: '{m['name']}'")
        seen_metric_names.add(m["name"])

    # Rule 4: dimension names unique within domain
    seen_dim_names: set[str] = set()
    for d in dimensions_list:
        if d["name"] in seen_dim_names:
            err("cross:duplicate-dimension-name", "ontology.yaml",
                f"duplicate dimension name: '{d['name']}'")
        seen_dim_names.add(d["name"])

    # Rule 5: glossary term names unique within domain
    seen_term_names: set[str] = set()
    for t in terms_list:
        if t["name"] in seen_term_names:
            err("cross:duplicate-term-name", "glossary.yaml",
                f"duplicate glossary term: '{t['name']}'")
        seen_term_names.add(t["name"])

    # Rule 6: no alias duplicates another name in the same domain
    all_aliases: dict[str, str] = {}  # alias → origin name
    for m in metrics_list:
        for alias in m.get("aliases", []):
            if alias in all_names:
                err("cross:alias-collides-name", "metrics.yaml",
                    f"metric '{m['name']}' alias '{alias}' collides with an existing name")
            if alias in all_aliases:
                err("cross:duplicate-alias", "metrics.yaml",
                    f"alias '{alias}' on metric '{m['name']}' already used by '{all_aliases[alias]}'")
            else:
                all_aliases[alias] = m["name"]

    for d in dimensions_list:
        for alias in d.get("aliases", []):
            if alias in all_names:
                err("cross:alias-collides-name", "ontology.yaml",
                    f"dimension '{d['name']}' alias '{alias}' collides with an existing name")

    for t in terms_list:
        for alias in t.get("aliases", []):
            if alias in all_names:
                err("cross:alias-collides-name", "glossary.yaml",
                    f"term '{t['name']}' alias '{alias}' collides with an existing name")

    # Rule 7: depends_on resolves
    for m in metrics_list:
        for dep in m.get("depends_on", []):
            if dep not in all_names:
                err("cross:unresolved-depends-on", "metrics.yaml",
                    f"metric '{m['name']}' depends_on '{dep}' which does not exist in this domain")

    # Rule 8: metrics[].sensitivity references declared classification
    for m in metrics_list:
        if m.get("sensitivity") and m["sensitivity"] not in classification_ids:
            err("cross:unknown-sensitivity", "metrics.yaml",
                f"metric '{m['name']}' sensitivity '{m['sensitivity']}' not in sensitivity.yaml classifications")

    # Rule 9: glossary sensitivity + override classifications reference declared IDs
    for t in terms_list:
        if t.get("sensitivity") and t["sensitivity"] not in classification_ids:
            err("cross:unknown-sensitivity", "glossary.yaml",
                f"term '{t['name']}' sensitivity '{t['sensitivity']}' not declared in sensitivity.yaml")

    for override_key in ("metric_overrides", "dimension_overrides", "column_overrides"):
        for override in sensitivity_data.get(override_key, []):
            if override.get("classification") and override["classification"] not in classification_ids:
                err("cross:unknown-classification", "sensitivity.yaml",
                    f"{override_key} entry classification '{override['classification']}' not declared")

    # Rule 10: glossary.related_metrics references existing metric name
    for t in terms_list:
        for ref in t.get("related_metrics", []):
            if ref not in metric_names:
                warn("cross:unresolved-related-metric", "glossary.yaml",
                     f"term '{t['name']}' related_metric '{ref}' does not exist in metrics.yaml")

    # Rule 11: glossary.related_dimensions references existing dimension name
    for t in terms_list:
        for ref in t.get("related_dimensions", []):
            if ref not in dimension_names:
                warn("cross:unresolved-related-dimension", "glossary.yaml",
                     f"term '{t['name']}' related_dimension '{ref}' does not exist in ontology.yaml")

    # Rule 13: ontology.value_descriptions keys vs profiles.json (if cache present)
    if repo_root is not None:
        profiles_path = repo_root / ".canon-cache" / domain_slug / "profiles.json"
        if profiles_path.exists():
            import json as _json
            profiles = _json.loads(profiles_path.read_text(encoding="utf-8"))
            for d in dimensions_list:
                if d.get("value_descriptions"):
                    cached_values = set(profiles.get(d["name"], {}).get("values", []))
                    if cached_values:
                        for key in d["value_descriptions"]:
                            if key not in cached_values:
                                warn("cross:value-description-not-in-profile", "ontology.yaml",
                                     f"dimension '{d['name']}' value_descriptions key '{key}' "
                                     f"not in latest profile")

    # Rule 14: warehouse block required when usage_patterns source=warehouse
    for m in metrics_list:
        for up in m.get("usage_patterns", []):
            if up.get("source") == "warehouse" and not m.get("warehouse"):
                err("cross:missing-warehouse-block", "metrics.yaml",
                    f"metric '{m['name']}' has usage_pattern source=warehouse but no warehouse block")

    # Rule 15: routing required when primary source is semantic_model
    for m in metrics_list:
        primary = m.get("governed_sources", {}).get("primary", {})
        if primary.get("type") == "semantic_model" and not m.get("routing"):
            err("cross:missing-routing", "metrics.yaml",
                f"metric '{m['name']}' primary source is semantic_model but routing is not set")

    # Rule 16: last_reviewed not in the future
    today = date.today()
    for m in metrics_list:
        lr = m.get("last_reviewed")
        if lr:
            reviewed = lr if isinstance(lr, date) else date.fromisoformat(str(lr))
            if reviewed > today:
                err("cross:future-last-reviewed", "metrics.yaml",
                    f"metric '{m['name']}' last_reviewed {reviewed} is in the future")

    # Rule 17: deprecated metrics retain required fields
    for m in metrics_list:
        if m.get("status") == "deprecated":
            for req_field in ("definition", "owner", "routing"):
                if not m.get(req_field):
                    err("cross:deprecated-missing-field", "metrics.yaml",
                        f"deprecated metric '{m['name']}' is missing required field '{req_field}'")

    return result


def validate_all_domains(repo_root: Path) -> dict[str, ValidationResult]:
    """Validate every non-template domain in the repo."""
    domains_dir = repo_root / "domains"
    results: dict[str, ValidationResult] = {}
    for d in sorted(domains_dir.iterdir()):
        if d.is_dir() and d.name != "_template":
            results[d.name] = validate_domain(d, repo_root=repo_root)
    return results
