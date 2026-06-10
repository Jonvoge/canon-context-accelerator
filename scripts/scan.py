"""
Canon structural scan engine.

Structural reconciliation (v1):
  - Undocumented measures (in platform, not in Canon)
  - Orphaned definitions (in Canon, not in platform)
  - Missing sources (authored pointers that don't resolve)
  - Dimension value drift (for enumerate: true dimensions)
  - Staleness check

Outputs:
  - .canon-cache/{domain}/scan.json  — findings + timestamp
  - .canon-cache/{domain}/profiles.json — updated dimension value snapshots
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass
class ScanFinding:
    type: str          # undocumented_measure | orphaned_definition | missing_source | dimension_values | staleness
    severity: str      # high | medium | low
    domain: str
    subject: str       # measure/dimension/rule name
    description: str
    suggested_action: str
    source_ref: str = ""  # platform reference (workspace/model/measure etc.)
    scan_run_id: str = ""


@dataclass
class ScanResult:
    domain: str
    scanned_at: str
    connector_id: str
    findings: list[ScanFinding] = field(default_factory=list)
    stale: bool = False
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _load_scan_config(config_path: Path | str) -> dict:
    return yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}


def _get_domain_config(config: dict, domain: str) -> dict:
    for d in config.get("domains", []):
        if d["name"] == domain:
            return d
    return {}


def _build_connector(connector_config: dict, global_config: dict) -> Any:
    """Instantiate a connector from scan-config connector definition."""
    ctype = connector_config["type"]
    auth_secret = connector_config["auth_secret_name"]

    # Resolve credentials: env vars named CANON_<UPPERCASED_SECRET_NAME>_*
    # or fall back to generic CANON_ vars
    tenant_id = os.environ.get("CANON_FABRIC_TENANT_ID", "")
    client_id = os.environ.get("CANON_FABRIC_CLIENT_ID", "")
    client_secret = os.environ.get(f"CANON_{auth_secret}", os.environ.get("CANON_FABRIC_CLIENT_SECRET", ""))

    options = connector_config.get("options", {})

    if ctype == "fabric_semantic":
        from connectors.fabric_semantic import FabricSemanticConnector
        return FabricSemanticConnector({
            "workspace_id": os.environ.get("CANON_FABRIC_WORKSPACE_ID", options.get("workspace_id", "")),
            "dataset_name": os.environ.get("CANON_FABRIC_DATASET_NAME", options.get("dataset_name", "")),
            "tenant_id": tenant_id,
            "client_id": client_id,
            "client_secret": client_secret,
        })
    elif ctype == "fabric_sql":
        from connectors.fabric_sql import FabricSqlConnector
        return FabricSqlConnector({
            "server": os.environ.get("CANON_SQL_SERVER", ""),
            "database": os.environ.get("CANON_SQL_DATABASE", ""),
            "tenant_id": tenant_id,
            "client_id": client_id,
            "client_secret": client_secret,
        })
    else:
        raise ValueError(f"Unknown connector type: {ctype}")


def run_scan(
    domain: str,
    config_path: Path,
    repo_root: Path,
    run_id: str = "",
) -> ScanResult:
    """
    Run a full structural scan for a domain.
    Writes results to .canon-cache/{domain}/scan.json and profiles.json.
    """
    if not run_id:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")

    now_iso = datetime.now(timezone.utc).isoformat()
    result = ScanResult(domain=domain, scanned_at=now_iso, connector_id="", findings=[])

    # ── Load authored definitions ─────────────────────────────────────────────
    domain_path = repo_root / "domains" / domain
    if not domain_path.exists():
        result.error = f"Domain '{domain}' not found at {domain_path}"
        return result

    metrics_data = _load_yaml(domain_path / "metrics.yaml")
    ontology_data = _load_yaml(domain_path / "ontology.yaml")
    config = _load_scan_config(config_path)
    domain_cfg = _get_domain_config(config, domain)
    scanner_cfg = config.get("scanner", {})

    stale_after = scanner_cfg.get("stale_after_hours", 168)

    # ── Find connector for this domain ───────────────────────────────────────
    semantic_connector_id = domain_cfg.get("semantic_connector", "")
    connector_cfgs = {c["id"]: c for c in config.get("connectors", [])}

    if not semantic_connector_id or semantic_connector_id not in connector_cfgs:
        result.error = f"No semantic connector '{semantic_connector_id}' found in scan-config"
        return result

    connector_cfg = connector_cfgs[semantic_connector_id]
    result.connector_id = semantic_connector_id

    # ── Connect and fetch metadata ────────────────────────────────────────────
    try:
        connector = _build_connector(connector_cfg, config)
        errors = connector.validate_config()
        if errors:
            result.error = "Connector config errors: " + "; ".join(errors)
            return result
        snapshot = connector.fetch_metadata()
    except Exception as e:
        result.error = f"Connector failed: {e}"
        logger.exception("Connector error during scan")
        return result

    # ── Authored sets ─────────────────────────────────────────────────────────
    authored_metrics = {m["name"]: m for m in metrics_data.get("metrics", [])}
    authored_measure_names = set(authored_metrics.keys())

    # Collect all authored aliases too (for name matching)
    authored_all_names: set[str] = set(authored_measure_names)
    for m in authored_metrics.values():
        authored_all_names.update(m.get("aliases", []))

    # ── Platform sets ─────────────────────────────────────────────────────────
    platform_measures = {m.name: m for m in snapshot.measures}
    platform_measure_names = set(platform_measures.keys())

    # ── Finding: undocumented measures (in platform, not in Canon) ───────────
    undocumented = platform_measure_names - authored_all_names
    for name in sorted(undocumented):
        m = platform_measures[name]
        result.findings.append(ScanFinding(
            type="undocumented_measure",
            severity="medium",
            domain=domain,
            subject=name,
            description=f"Measure '{name}' exists in the platform but has no Canon definition.",
            suggested_action=f"Add a definition to domains/{domain}/metrics.yaml or mark as intentionally undocumented.",
            source_ref=f"{m.table}/{name}" if m.table else name,
            scan_run_id=run_id,
        ))

    # ── Finding: orphaned definitions (in Canon, not in platform) ────────────
    orphaned = authored_measure_names - platform_measure_names
    for name in sorted(orphaned):
        m = authored_metrics[name]
        primary = m.get("governed_sources", {}).get("primary", {})
        result.findings.append(ScanFinding(
            type="orphaned_definition",
            severity="high",
            domain=domain,
            subject=name,
            description=f"Canon defines '{name}' but the measure was not found in the platform.",
            suggested_action="Verify the measure still exists, or deprecate/remove the definition.",
            source_ref=f"{primary.get('model', '')}/{name}",
            scan_run_id=run_id,
        ))

    # ── Finding: missing sources (authored source pointer doesn't resolve) ────
    platform_table_names = {t.name for t in snapshot.tables}
    for metric_name, m in authored_metrics.items():
        sources = [m.get("governed_sources", {}).get("primary")]
        sources += m.get("governed_sources", {}).get("also_exists_in", [])
        for src in sources:
            if not src:
                continue
            if src.get("type") == "semantic_model":
                measure_ref = src.get("measure", metric_name)
                if measure_ref not in platform_measure_names:
                    result.findings.append(ScanFinding(
                        type="missing_source",
                        severity="high",
                        domain=domain,
                        subject=metric_name,
                        description=(
                            f"Authored source '{src.get('model', '')}/{measure_ref}' "
                            f"not found in platform."
                        ),
                        suggested_action="Update the source reference or remove the stale pointer.",
                        source_ref=f"{src.get('workspace', '')}/{src.get('model', '')}/{measure_ref}",
                        scan_run_id=run_id,
                    ))

    # ── Dimension value profiling ─────────────────────────────────────────────
    dimensions = ontology_data.get("dimensions", [])
    profile_dims = set(domain_cfg.get("profile_dimensions", []))
    cap = scanner_cfg.get("distinct_value_cap", 500)

    cache_dir = repo_root / ".canon-cache" / domain
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Write semantic model schema for CanonMCP context enrichment
    schema = {
        "tables": [
            {"name": t.name, "columns": [c.name for c in t.columns]}
            for t in snapshot.tables
        ],
        "measures": [m.name for m in snapshot.measures],
    }
    (cache_dir / "schema.json").write_text(json.dumps(schema, indent=2), encoding="utf-8")

    profiles_path = cache_dir / "profiles.json"

    old_profiles: dict[str, list] = {}
    if profiles_path.exists():
        old_profiles = json.loads(profiles_path.read_text(encoding="utf-8"))

    new_profiles: dict[str, dict] = {}

    for dim in dimensions:
        dim_name = dim["name"]
        if not dim.get("enumerate") or dim_name not in profile_dims:
            continue

        column = dim.get("column", "")
        if not column:
            continue

        try:
            values = connector.profile_dimension(column, max_values=cap)
            values_set = set(str(v) for v in values)
            old_values_set = set(old_profiles.get(dim_name, {}).get("values", []))

            added = sorted(values_set - old_values_set)
            removed = sorted(old_values_set - values_set)

            new_profiles[dim_name] = {
                "values": sorted(values_set),
                "profiled_at": now_iso,
            }

            if (added or removed) and old_profiles:  # only flag if we had a previous snapshot
                result.findings.append(ScanFinding(
                    type="dimension_values",
                    severity="medium",
                    domain=domain,
                    subject=dim_name,
                    description=(
                        f"Dimension '{dim_name}' value drift: "
                        + (f"added {added}" if added else "")
                        + (" " if added and removed else "")
                        + (f"removed {removed}" if removed else "")
                    ),
                    suggested_action=(
                        "Review new values and update value_descriptions in ontology.yaml if needed."
                    ),
                    scan_run_id=run_id,
                ))
        except Exception as e:
            logger.warning("Could not profile dimension '%s': %s", dim_name, e)

    if new_profiles:
        profiles_path.write_text(json.dumps(new_profiles, indent=2), encoding="utf-8")

    # ── Staleness check ───────────────────────────────────────────────────────
    scan_path = cache_dir / "scan.json"
    if scan_path.exists():
        prev = json.loads(scan_path.read_text(encoding="utf-8"))
        prev_ts = prev.get("scanned_at")
        if prev_ts:
            prev_dt = datetime.fromisoformat(prev_ts)
            age_hours = (datetime.now(timezone.utc) - prev_dt).total_seconds() / 3600
            if age_hours > stale_after:
                result.stale = True
                result.findings.append(ScanFinding(
                    type="staleness",
                    severity="low",
                    domain=domain,
                    subject="scan",
                    description=f"Scan is {age_hours:.1f}h old (SLA: {stale_after}h).",
                    suggested_action="Check that the scan workflow is running on schedule.",
                    scan_run_id=run_id,
                ))

    # ── Write findings to cache ───────────────────────────────────────────────
    scan_path.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
    logger.info(
        "Scan complete for domain '%s': %d findings",
        domain, len(result.findings)
    )

    return result


def create_github_issues(result: ScanResult, repo_slug: str, token: str, notify: list[str] | None = None) -> None:
    """
    Create GitHub issues for high/medium findings in the scan result.

    notify: list of @handles to mention on each issue so GitHub emails them
            natively (the v5 digest mechanism — no Teams, no SMTP required).
    """
    import requests as _req

    if not token or not repo_slug:
        logger.warning("GitHub issue creation skipped: no token or repo slug")
        return

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    type_to_label = {
        "undocumented_measure": "undocumented-measure",
        "orphaned_definition": "orphaned-definition",
        "missing_source": "missing-source",
        "dimension_values": "dimension-values",
    }

    mention = ""
    if notify:
        mention = "\n\n---\ncc " + " ".join(notify)

    for finding in result.findings:
        if finding.severity == "low":
            continue
        label = type_to_label.get(finding.type)
        if not label:
            continue

        issue_body = (
            f"**Domain:** {finding.domain}\n"
            f"**Subject:** {finding.subject}\n"
            f"**Source:** {finding.source_ref}\n\n"
            f"**Description:**\n{finding.description}\n\n"
            f"**Suggested Action:**\n{finding.suggested_action}\n\n"
            f"**Scan Run:** {finding.scan_run_id}"
            f"{mention}"
        )
        payload = {
            "title": f"[Drift] {finding.subject}",
            "body": issue_body,
            "labels": ["canon", "drift", label, f"domain:{finding.domain}"],
        }
        resp = _req.post(
            f"https://api.github.com/repos/{repo_slug}/issues",
            json=payload,
            headers=headers,
            timeout=15,
        )
        if resp.status_code == 201:
            logger.info("Created issue #%s for '%s'", resp.json()["number"], finding.subject)
        else:
            logger.warning("Failed to create issue for '%s': %s", finding.subject, resp.text[:200])

