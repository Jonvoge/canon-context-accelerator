"""Build domains/_index.json — compiled domain routing index."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from canon.config import load_scan_config


def build_index(repo_root: Path) -> dict:
    scan_cfg = load_scan_config(repo_root / "scan-config.yaml")
    domains = []
    for domain_entry in scan_cfg.get("domains", []):
        name = domain_entry["name"]
        domain_path = repo_root / "domains" / name
        metrics = {}
        ontology = {}
        rules_desc = ""

        m_path = domain_path / "metrics.yaml"
        if m_path.exists():
            metrics = yaml.safe_load(m_path.read_text(encoding="utf-8")) or {}
        o_path = domain_path / "ontology.yaml"
        if o_path.exists():
            ontology = yaml.safe_load(o_path.read_text(encoding="utf-8")) or {}
        r_path = domain_path / "domain-rules.md"
        if r_path.exists():
            for line in r_path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    rules_desc = stripped
                    break

        aliases = []
        for metric in metrics.get("metrics", []):
            aliases.extend(metric.get("aliases", []))

        models_summary = []
        for model in domain_entry.get("models", []):
            models_summary.append(
                {
                    "id": model["connector"],
                    "role": model.get("role", "semantic"),
                    "primary": model.get("primary", False),
                    "description": model.get("description", ""),
                }
            )
        if not models_summary and domain_entry.get("semantic_connector"):
            models_summary.append(
                {"id": domain_entry["semantic_connector"], "role": "semantic", "primary": True, "description": ""}
            )

        domains.append(
            {
                "name": name,
                "description": rules_desc,
                "metric_count": len(metrics.get("metrics", [])),
                "dimension_count": len(ontology.get("dimensions", [])),
                "trigger_aliases": aliases[:20],
                "models": models_summary,
            }
        )

    return {"domains": domains}


if __name__ == "__main__":
    import sys

    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    index = build_index(root)
    out = root / "domains" / "_index.json"
    out.write_text(json.dumps(index, indent=2), encoding="utf-8")
    print(f"Written {out}")
