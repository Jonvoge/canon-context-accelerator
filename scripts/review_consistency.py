"""Canon cross-file consistency validation for PRs."""

from __future__ import annotations

from pathlib import Path

import yaml


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def review_consistency(domain: str, repo_root: Path | None = None) -> list[str]:
    """
    Run cross-file consistency checks for a domain.

    Checks:
    - All metrics.yaml depends_on entries resolve to dimensions in ontology.yaml
    - All metric aliases are unique within the domain
    - All governed_sources of type semantic_model have a non-empty measure field
    - All glossary related_metrics resolve to metric names in metrics.yaml
    - All glossary related_dimensions resolve to dimension names in ontology.yaml

    Returns a list of human-readable findings (empty = no issues).
    """
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent

    domain_path = repo_root / "domains" / domain
    metrics_data = _load_yaml(domain_path / "metrics.yaml")
    ontology_data = _load_yaml(domain_path / "ontology.yaml")
    glossary_data = _load_yaml(domain_path / "glossary.yaml")

    findings: list[str] = []

    metrics = metrics_data.get("metrics", [])
    dimensions = ontology_data.get("dimensions", [])
    terms = glossary_data.get("terms", [])

    metric_names = {m["name"] for m in metrics}
    dimension_names = {d["name"] for d in dimensions}

    # Check 1: depends_on references resolve to known dimensions or metrics
    for m in metrics:
        for dep in m.get("depends_on", []):
            if dep not in dimension_names and dep not in metric_names:
                findings.append(
                    f"metrics.yaml: '{m['name']}' depends_on '{dep}' "
                    f"which is not defined in ontology.yaml or metrics.yaml"
                )

    # Check 2: aliases are unique within the domain (across all metrics)
    alias_to_metric: dict[str, str] = {}
    for m in metrics:
        for alias in m.get("aliases", []):
            a = alias.lower()
            if a in alias_to_metric:
                findings.append(
                    f"metrics.yaml: alias '{alias}' is shared by '{alias_to_metric[a]}' "
                    f"and '{m['name']}' — aliases must be unique"
                )
            else:
                alias_to_metric[a] = m["name"]

    # Check 3: semantic_model sources must have a measure field
    for m in metrics:
        sources = [m.get("governed_sources", {}).get("primary")]
        sources += m.get("governed_sources", {}).get("also_exists_in", [])
        for src in sources:
            if not src:
                continue
            if src.get("type") == "semantic_model" and not src.get("measure"):
                findings.append(
                    f"metrics.yaml: '{m['name']}' has a semantic_model source "
                    f"with no 'measure' field"
                )

    # Check 4: glossary related_metrics resolve to known metrics
    for t in terms:
        for ref in t.get("related_metrics", []):
            if ref not in metric_names:
                findings.append(
                    f"glossary.yaml: term '{t['name']}' references metric '{ref}' "
                    f"which is not defined in metrics.yaml"
                )

    # Check 5: glossary related_dimensions resolve to known dimensions
    for t in terms:
        for ref in t.get("related_dimensions", []):
            if ref not in dimension_names:
                findings.append(
                    f"glossary.yaml: term '{t['name']}' references dimension '{ref}' "
                    f"which is not defined in ontology.yaml"
                )

    return findings

