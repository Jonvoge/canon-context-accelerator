"""
Canon structural scan engine.

Structural reconciliation (v1):
  - Undocumented measures (in platform, not in Canon)
  - Orphaned definitions (in Canon, not in platform)
  - Missing sources (authored pointers that don't resolve)
  - Discrepancy verification for known alternate sources
  - Dimension value drift (for enumerate: true dimensions)
  - Staleness check

Outputs:
  - .canon-cache/{domain}/scan.json  — findings + timestamp
  - .canon-cache/{domain}/{connector}/profiles.json — updated dimension value snapshots
  - .canon-cache/{domain}/{connector}/schema.json — scanned schema snapshot
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from canon.config import load_scan_config

logger = logging.getLogger(__name__)


@dataclass
class ScanFinding:
    type: str  # undocumented_measure | orphaned_definition | missing_source | discrepancy_unverified | dimension_values | staleness
    severity: str
    domain: str
    subject: str
    description: str
    suggested_action: str
    source_ref: str = ""
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
    return load_scan_config(config_path)


def _get_domain_config(config: dict, domain: str) -> dict:
    for domain_cfg in config.get("domains", []):
        if domain_cfg["name"] == domain:
            return domain_cfg
    return {}


def _build_connector(connector_config: dict, global_config: dict) -> Any:
    ctype = connector_config["type"]
    auth_secret = connector_config["auth_secret_name"]
    tenant_id = os.environ.get("CANON_FABRIC_TENANT_ID", "")
    client_id = os.environ.get("CANON_FABRIC_CLIENT_ID", "")
    client_secret = os.environ.get(f"CANON_{auth_secret}", os.environ.get("CANON_FABRIC_CLIENT_SECRET", ""))
    options = connector_config.get("options", {})

    if ctype == "fabric_semantic":
        from connectors.fabric_semantic import FabricSemanticConnector

        return FabricSemanticConnector(
            {
                "workspace_id": options.get("workspace_id", ""),
                "dataset_id": options.get("dataset_id", ""),
                "dataset_name": options.get("dataset_name", ""),
                "tenant_id": tenant_id,
                "client_id": client_id,
                "client_secret": client_secret,
            }
        )
    if ctype == "fabric_sql":
        from connectors.fabric_sql import FabricSqlConnector

        return FabricSqlConnector(
            {
                "server": options.get("server", ""),
                "database": options.get("database", ""),
                "tenant_id": tenant_id,
                "client_id": client_id,
                "client_secret": client_secret,
            }
        )
    raise ValueError(f"Unknown connector type: {ctype}")


def _connector_cache_dir(repo_root: Path, domain: str, connector_id: str) -> Path:
    return repo_root / ".canon-cache" / domain / connector_id


def _load_json_if_exists(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _load_cached_json(new_path: Path, old_path: Path) -> dict:
    if new_path.exists():
        return _load_json_if_exists(new_path)
    if old_path.exists():
        return _load_json_if_exists(old_path)
    return {}


def _connector_model_identifiers(connector_cfg: dict) -> set[str]:
    options = connector_cfg.get("options", {})
    identifiers = {connector_cfg.get("id", "")}
    for key in ("dataset_name", "dataset_id", "database"):
        value = options.get(key, "")
        if value:
            identifiers.add(value)
    return {value for value in identifiers if value}


def _find_connector_id_for_source(
    domain_cfg: dict, connector_cfgs: dict[str, dict], source_model: str, role: str
) -> str | None:
    for model in domain_cfg.get("models", []):
        if model.get("role") != role:
            continue
        connector_id = model.get("connector", "")
        connector_cfg = connector_cfgs.get(connector_id)
        if not connector_cfg:
            continue
        if source_model in _connector_model_identifiers(connector_cfg):
            return connector_id
    return None


def _primary_semantic_connector_id(domain_cfg: dict) -> str:
    for model in domain_cfg.get("models", []):
        if model.get("role") == "semantic" and model.get("primary"):
            return model["connector"]
    for model in domain_cfg.get("models", []):
        if model.get("role") == "semantic":
            return model["connector"]
    return ""


def run_scan(domain: str, config_path: Path, repo_root: Path, run_id: str = "") -> ScanResult:
    if not run_id:
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")

    now_iso = datetime.now(UTC).isoformat()
    result = ScanResult(domain=domain, scanned_at=now_iso, connector_id="", findings=[])

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
    models = domain_cfg.get("models", [])
    connector_cfgs = {connector["id"]: connector for connector in config.get("connectors", [])}

    primary_semantic_connector_id = _primary_semantic_connector_id(domain_cfg)
    result.connector_id = primary_semantic_connector_id
    if not models or not primary_semantic_connector_id:
        result.error = f"No semantic connector configured for domain '{domain}'"
        return result

    authored_metrics = {metric["name"]: metric for metric in metrics_data.get("metrics", [])}
    authored_measure_names = set(authored_metrics)
    authored_all_names: set[str] = set(authored_measure_names)
    for metric in authored_metrics.values():
        authored_all_names.update(metric.get("aliases", []))

    cache_root = repo_root / ".canon-cache" / domain
    cache_root.mkdir(parents=True, exist_ok=True)
    old_profiles_path = cache_root / "profiles.json"

    connector_snapshots: dict[str, Any] = {}
    connector_roles: dict[str, str] = {}

    for model in models:
        connector_id = model["connector"]
        connector_cfg = connector_cfgs.get(connector_id)
        if connector_cfg is None:
            result.error = f"Connector '{connector_id}' referenced by domain '{domain}' was not found"
            return result

        connector_roles[connector_id] = model.get("role", "semantic")
        try:
            connector = _build_connector(connector_cfg, config)
            errors = connector.validate_config()
            if errors:
                result.error = f"Connector config errors for '{connector_id}': " + "; ".join(errors)
                return result
            snapshot = connector.fetch_metadata()
        except Exception as exc:
            result.error = f"Connector '{connector_id}' failed: {exc}"
            logger.exception("Connector error during scan")
            return result

        connector_snapshots[connector_id] = snapshot

        connector_cache_dir = _connector_cache_dir(repo_root, domain, connector_id)
        connector_cache_dir.mkdir(parents=True, exist_ok=True)
        schema = {
            "tables": [
                {"name": table.name, "columns": [column.name for column in table.columns]} for table in snapshot.tables
            ],
            "measures": [measure.name for measure in snapshot.measures],
        }
        (connector_cache_dir / "schema.json").write_text(json.dumps(schema, indent=2), encoding="utf-8")

        if model.get("role") != "semantic":
            continue

        dimensions = ontology_data.get("dimensions", [])
        profile_dims = set(domain_cfg.get("profile_dimensions", []))
        cap = scanner_cfg.get("distinct_value_cap", 500)
        profiles_path = connector_cache_dir / "profiles.json"
        old_profiles = _load_cached_json(profiles_path, old_profiles_path)
        new_profiles: dict[str, dict[str, Any]] = {}

        for dim in dimensions:
            dim_name = dim["name"]
            if not dim.get("enumerate") or dim_name not in profile_dims:
                continue
            column = dim.get("column", "")
            if not column:
                continue
            try:
                values = connector.profile_dimension(column, max_values=cap)
            except Exception as exc:
                logger.warning("Could not profile dimension '%s' on '%s': %s", dim_name, connector_id, exc)
                continue
            values_set = {str(value) for value in values}
            old_values_set = set(old_profiles.get(dim_name, {}).get("values", []))
            added = sorted(values_set - old_values_set)
            removed = sorted(old_values_set - values_set)
            new_profiles[dim_name] = {"values": sorted(values_set), "profiled_at": now_iso}
            if (added or removed) and old_profiles:
                result.findings.append(
                    ScanFinding(
                        type="dimension_values",
                        severity="medium",
                        domain=domain,
                        subject=dim_name,
                        description=(
                            f"Dimension '{dim_name}' value drift on '{connector_id}': "
                            + (f"added {added}" if added else "")
                            + (" " if added and removed else "")
                            + (f"removed {removed}" if removed else "")
                        ),
                        suggested_action="Review new values and update value_descriptions in ontology.yaml if needed.",
                        source_ref=connector_id,
                        scan_run_id=run_id,
                    )
                )
        if new_profiles:
            profiles_path.write_text(json.dumps(new_profiles, indent=2), encoding="utf-8")

    primary_snapshot = connector_snapshots[primary_semantic_connector_id]
    platform_measures = {measure.name: measure for measure in primary_snapshot.measures}
    platform_measure_names = set(platform_measures)

    undocumented = platform_measure_names - authored_all_names
    for name in sorted(undocumented):
        measure = platform_measures[name]
        result.findings.append(
            ScanFinding(
                type="undocumented_measure",
                severity="medium",
                domain=domain,
                subject=name,
                description=f"Measure '{name}' exists in the platform but has no Canon definition.",
                suggested_action=f"Add a definition to domains/{domain}/metrics.yaml or mark as intentionally undocumented.",
                source_ref=f"{primary_semantic_connector_id}:{measure.table}/{name}"
                if measure.table
                else f"{primary_semantic_connector_id}:{name}",
                scan_run_id=run_id,
            )
        )

    for metric_name, metric in authored_metrics.items():
        primary = metric.get("governed_sources", {}).get("primary", {})
        primary_model_name = primary.get("model", "")
        primary_connector_id = _find_connector_id_for_source(domain_cfg, connector_cfgs, primary_model_name, "semantic")
        if primary_connector_id:
            snapshot = connector_snapshots.get(primary_connector_id)
            if snapshot:
                measures = {measure.name for measure in snapshot.measures}
                measure_ref = primary.get("measure", metric_name)
                if measure_ref not in measures:
                    result.findings.append(
                        ScanFinding(
                            type="orphaned_definition",
                            severity="high",
                            domain=domain,
                            subject=metric_name,
                            description=(
                                f"Canon defines '{metric_name}' but '{measure_ref}' was not found in its primary model '{primary_model_name}'."
                            ),
                            suggested_action="Verify the measure still exists in the primary model, or deprecate/remove the definition.",
                            source_ref=f"{primary_connector_id}:{primary_model_name}/{measure_ref}",
                            scan_run_id=run_id,
                        )
                    )

        sources = [metric.get("governed_sources", {}).get("primary")]
        sources += metric.get("governed_sources", {}).get("also_exists_in", [])
        for source in sources:
            if not source or source.get("type") != "semantic_model":
                continue
            source_model_name = source.get("model", "")
            connector_id = _find_connector_id_for_source(domain_cfg, connector_cfgs, source_model_name, "semantic")
            if not connector_id:
                continue
            snapshot = connector_snapshots.get(connector_id)
            if snapshot is None:
                continue
            platform_measure_names = {measure.name for measure in snapshot.measures}
            measure_ref = source.get("measure", metric_name)
            if measure_ref not in platform_measure_names:
                result.findings.append(
                    ScanFinding(
                        type="missing_source",
                        severity="high",
                        domain=domain,
                        subject=metric_name,
                        description=f"Authored source '{source_model_name}/{measure_ref}' not found in platform.",
                        suggested_action="Update the source reference or remove the stale pointer.",
                        source_ref=f"{connector_id}:{source.get('workspace', '')}/{source_model_name}/{measure_ref}",
                        scan_run_id=run_id,
                    )
                )
            elif source is not primary:
                result.findings.append(
                    ScanFinding(
                        type="discrepancy_unverified",
                        severity="low",
                        domain=domain,
                        subject=metric_name,
                        description=(
                            f"Known alternate source '{source_model_name}/{measure_ref}' still exists in model '{connector_id}'."
                        ),
                        suggested_action="Reconfirm the documented discrepancy and keep routing guidance current.",
                        source_ref=f"{connector_id}:{source.get('workspace', '')}/{source_model_name}/{measure_ref}",
                        scan_run_id=run_id,
                    )
                )

    scan_path = cache_root / "scan.json"
    if scan_path.exists():
        previous = json.loads(scan_path.read_text(encoding="utf-8"))
        previous_ts = previous.get("scanned_at")
        if previous_ts:
            previous_dt = datetime.fromisoformat(previous_ts)
            age_hours = (datetime.now(UTC) - previous_dt).total_seconds() / 3600
            if age_hours > stale_after:
                result.stale = True
                result.findings.append(
                    ScanFinding(
                        type="staleness",
                        severity="low",
                        domain=domain,
                        subject="scan",
                        description=f"Scan is {age_hours:.1f}h old (SLA: {stale_after}h).",
                        suggested_action="Check that the scan workflow is running on schedule.",
                        scan_run_id=run_id,
                    )
                )

    scan_path.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
    logger.info("Scan complete for domain '%s': %d findings", domain, len(result.findings))
    return result


def create_github_issues(result: ScanResult, repo_slug: str, token: str, notify: list[str] | None = None) -> None:
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
        response = _req.post(
            f"https://api.github.com/repos/{repo_slug}/issues",
            json=payload,
            headers=headers,
            timeout=15,
        )
        if response.status_code == 201:
            logger.info("Created issue #%s for '%s'", response.json()["number"], finding.subject)
        else:
            logger.warning("Failed to create issue for '%s': %s", finding.subject, response.text[:200])
