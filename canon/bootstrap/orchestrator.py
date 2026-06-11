"""
Canon bootstrap orchestrator.

Stages:
  1. Parse uploaded documentation from bootstrap-docs/{domain}/
  2. Scan the platform connector for current metadata
  3. Cross-reference doc mentions against platform measures (exact + fuzzy)
  4. Draft metrics.yaml / ontology.yaml entries:
       - Deterministic stubs always (no API key required)
       - LLM enrichment when ANTHROPIC_API_KEY is set (optional)
  5. Create branch + commit drafts + open draft PR
  6. Write bootstrap-report.json with confidence scores and evidence

Re-running is safe (idempotent): already-documented measures are preserved untouched.
Only undocumented measures are added to the draft PR.

Usage:
  uv run canon bootstrap --domain retail
"""

from __future__ import annotations

import json
import logging
import os
import re
import textwrap
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from canon.bootstrap.parsing.parsers import ParsedChunk, parse_directory

logger = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5"


# ── Data models ───────────────────────────────────────────────────────────────


@dataclass
class BootstrapEvidence:
    measure_name: str
    platform_found: bool
    doc_mentions: list[str] = field(default_factory=list)  # verbatim text snippets
    doc_sources: list[str] = field(default_factory=list)  # file names


@dataclass
class BootstrapDraft:
    measure_name: str
    confidence: str  # high | medium | low
    metric_entry: dict = field(default_factory=dict)
    evidence: BootstrapEvidence | None = None
    needs_interview: bool = False


@dataclass
class BootstrapReport:
    domain: str
    run_at: str
    doc_files: list[str]
    platform_measures: list[str]
    drafts: list[BootstrapDraft]
    branch: str
    pr_url: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ── Cross-reference ───────────────────────────────────────────────────────────


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def _find_doc_mentions(measure_name: str, chunks: list[ParsedChunk], min_length: int = 5) -> list[tuple[str, str]]:
    """
    Find chunks that mention the measure name (or normalised form).
    Returns list of (source_file, snippet).
    """
    name_norm = _normalize(measure_name)
    results = []
    for chunk in chunks:
        if _normalize(chunk.section).__contains__(name_norm) or _normalize(chunk.text[:500]).__contains__(name_norm):
            snippet = chunk.text[:300].replace("\n", " ")
            results.append((chunk.source_file, snippet))
    return results[:3]  # cap at 3 snippets per measure


# ── LLM drafting (optional — only when ANTHROPIC_API_KEY is set) ─────────────


def _try_import_anthropic() -> Any:
    """Return anthropic.Anthropic client if available and API key set, else None."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    try:
        import anthropic as _anthropic

        return _anthropic.Anthropic(api_key=api_key)
    except ImportError:
        logger.warning("anthropic package not installed — LLM drafting disabled")
        return None


def _draft_metric_from_evidence(
    client: Any,
    measure_name: str,
    evidence: BootstrapEvidence,
    domain: str,
    today: str,
) -> dict:
    """Ask Claude to draft a metrics.yaml entry from platform + doc evidence."""
    doc_text = ""
    if evidence.doc_mentions:
        doc_text = "\n\nRelevant documentation snippets:\n" + "\n---\n".join(evidence.doc_mentions)

    prompt = textwrap.dedent(f"""
        Draft a Canon metric definition for the measure named "{measure_name}" in the {domain} domain.

        This measure exists in the platform (Fabric semantic model).{doc_text}

        Produce ONE metrics.yaml entry as JSON in this exact structure:
        {{
            "name": "{measure_name}",
            "domain": "{domain}",
            "owner": "{domain.title()} Analytics Team",
            "last_reviewed": "{today}",
            "status": "active",
            "aliases": [],
            "definition": "<clear 1-3 sentence business definition derived from the evidence above>",
            "governed_sources": {{
                "primary": {{
                    "platform": "fabric",
                    "type": "semantic_model",
                    "measure": "{measure_name}"
                }}
            }},
            "routing": "Query primary semantic model for direct metric questions.",
            "depends_on": [],
            "sensitivity": "internal"
        }}

        Return ONLY valid JSON (no prose, no markdown fences).
        If the documentation contains a clear definition, use it verbatim.
        If not, write a conservative inferred definition.
    """).strip()

    response = client.messages.create(
        model=_MODEL,
        max_tokens=700,
        temperature=0.0,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = "\n".join(raw.split("\n")[1:])
    if raw.endswith("```"):
        raw = "\n".join(raw.split("\n")[:-1])
    return json.loads(raw.strip())


def _draft_ontology_dimension(
    client: Any,
    table: str,
    column: str,
    sample_values: list[str],
    domain: str,
) -> dict:
    """Draft an ontology.yaml dimension entry."""
    prompt = textwrap.dedent(f"""
        Draft a Canon ontology dimension entry for the column {table}.{column} in the {domain} domain.
        Sample values: {sample_values[:10]}

        Return ONE dimension entry as JSON:
        {{
            "name": "<human-readable dimension name>",
            "column": "{table}.{column}",
            "aliases": [],
            "enumerate": {str(len(sample_values) <= 20).lower()},
            "value_descriptions": {{}}
        }}

        Return ONLY valid JSON.
    """).strip()

    response = client.messages.create(
        model=_MODEL,
        max_tokens=300,
        temperature=0.0,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = "\n".join(raw.split("\n")[1:])
    if raw.endswith("```"):
        raw = "\n".join(raw.split("\n")[:-1])
    return json.loads(raw.strip())


# ── Deterministic stub (no LLM) ───────────────────────────────────────────────


def _stub_metric(measure_name: str, domain: str, today: str) -> dict:
    """Build a minimal metrics.yaml stub without LLM — all TODO fields for human review."""
    return {
        "name": measure_name,
        "domain": domain,
        "owner": f"{domain.title()} Analytics Team",
        "last_reviewed": today,
        "status": "active",
        "aliases": [],
        "definition": "# TODO: add business definition",
        "governed_sources": {
            "primary": {
                "platform": "fabric",
                "type": "semantic_model",
                "measure": measure_name,
            }
        },
        "routing": "# TODO: add routing instructions",
        "depends_on": [],
        "sensitivity": "internal",
    }


# ── Main orchestrator ─────────────────────────────────────────────────────────


def run_bootstrap(
    domain: str,
    config_path: Path,
    repo_root: Path,
    create_pr: bool = True,
    dry_run: bool = False,
) -> BootstrapReport:
    today = datetime.now(UTC).date().isoformat()
    now_iso = datetime.now(UTC).isoformat()
    branch = f"canon/bootstrap/{domain}-{datetime.now(UTC).strftime('%Y%m%d')}"

    # LLM client is optional — deterministic stubs are always produced first
    llm_client = _try_import_anthropic()
    if llm_client:
        logger.info("ANTHROPIC_API_KEY found — LLM enrichment enabled")
    else:
        logger.info("No ANTHROPIC_API_KEY — deterministic stubs only (add key to enable LLM enrichment)")

    report = BootstrapReport(
        domain=domain,
        run_at=now_iso,
        doc_files=[],
        platform_measures=[],
        drafts=[],
        branch=branch,
    )

    # ── Stage 1: Parse documentation ──────────────────────────────────────────
    docs_dir = repo_root / "bootstrap-docs" / domain
    chunks: list[ParsedChunk] = []
    if docs_dir.exists():
        chunks = parse_directory(docs_dir)
        report.doc_files = list({c.source_file for c in chunks})
        logger.info("Stage 1: Parsed %d chunks from %d files", len(chunks), len(report.doc_files))
    else:
        logger.info("Stage 1: No bootstrap-docs/%s/ directory — proceeding platform-only", domain)

    # ── Stage 2: Scan platform ─────────────────────────────────────────────────
    import yaml as _yaml

    config = _yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    from scripts.scan import _build_connector, _get_domain_config

    domain_cfg = _get_domain_config(config, domain)
    semantic_connector_id = domain_cfg.get("semantic_connector", "")
    connector_cfgs = {c["id"]: c for c in config.get("connectors", [])}

    if not semantic_connector_id or semantic_connector_id not in connector_cfgs:
        report.error = f"No semantic connector '{semantic_connector_id}' in scan-config"
        return report

    connector_cfg = connector_cfgs[semantic_connector_id]
    connector = _build_connector(connector_cfg, config)
    errors = connector.validate_config()
    if errors:
        report.error = "Connector config errors: " + "; ".join(errors)
        return report

    snapshot = connector.fetch_metadata()
    platform_measure_names = [m.name for m in snapshot.measures]
    report.platform_measures = platform_measure_names
    logger.info("Stage 2: Found %d platform measures", len(platform_measure_names))

    # ── Stage 3: Load existing Canon definitions (skip already documented) ─────
    domain_path = repo_root / "domains" / domain
    existing_metrics_path = domain_path / "metrics.yaml"
    existing_data = {}
    if existing_metrics_path.exists():
        existing_data = _yaml.safe_load(existing_metrics_path.read_text(encoding="utf-8")) or {}

    existing_names = {m["name"] for m in existing_data.get("metrics", [])}
    undocumented = [n for n in platform_measure_names if n not in existing_names]
    logger.info("Stage 3: %d undocumented measures to bootstrap", len(undocumented))

    if not undocumented:
        logger.info("All measures already documented — nothing to bootstrap")
        return report

    # ── Stage 4: Cross-reference + draft ──────────────────────────────────────
    drafts: list[BootstrapDraft] = []

    for measure_name in undocumented:
        mentions = _find_doc_mentions(measure_name, chunks)
        evidence = BootstrapEvidence(
            measure_name=measure_name,
            platform_found=True,
            doc_mentions=[snip for _, snip in mentions],
            doc_sources=[src for src, _ in mentions],
        )

        # Confidence scoring
        if mentions:
            confidence = "high"
        else:
            confidence = "medium"  # platform-only

        if llm_client:
            try:
                metric_entry = _draft_metric_from_evidence(llm_client, measure_name, evidence, domain, today)
                needs_interview = confidence == "low"
            except Exception as e:
                logger.warning("LLM draft failed for '%s': %s", measure_name, e)
                metric_entry = _stub_metric(measure_name, domain, today)
                confidence = "low"
                needs_interview = True
        else:
            metric_entry = _stub_metric(measure_name, domain, today)
            needs_interview = True

        drafts.append(
            BootstrapDraft(
                measure_name=measure_name,
                confidence=confidence,
                metric_entry=metric_entry,
                evidence=evidence,
                needs_interview=needs_interview,
            )
        )
        logger.info("  Drafted '%s' [confidence=%s]", measure_name, confidence)

    report.drafts = drafts

    # ── Stage 5: Dimension drafting (from platform columns) ───────────────────
    existing_ontology_path = domain_path / "ontology.yaml"
    existing_ontology = {}
    if existing_ontology_path.exists():
        existing_ontology = _yaml.safe_load(existing_ontology_path.read_text(encoding="utf-8")) or {}

    existing_dim_columns = {d.get("column", "") for d in existing_ontology.get("dimensions", [])}

    new_dimensions = []
    if llm_client:
        for table in snapshot.tables:
            for col in table.columns:
                col_ref = f"{table.name}.{col.name}"
                if col_ref in existing_dim_columns:
                    continue
                try:
                    values = connector.profile_dimension(col_ref, max_values=30)
                except Exception:
                    values = []
                if values and len(values) <= 20:
                    try:
                        dim = _draft_ontology_dimension(
                            llm_client, table.name, col.name, [str(v) for v in values], domain
                        )
                        new_dimensions.append(dim)
                        logger.info("  Drafted dimension %s.%s [%d values]", table.name, col.name, len(values))
                    except Exception as e:
                        logger.warning("Could not draft dimension %s.%s: %s", table.name, col.name, e)
    else:
        logger.info("Stage 5: LLM not available — skipping dimension drafting")

    if dry_run:
        logger.info("Dry run — skipping branch/PR creation")
        return report

    # ── Stage 6: Commit to branch and open draft PR ───────────────────────────
    if not create_pr:
        return report

    try:
        from scripts import git_ops

        git_ops.create_branch(branch)

        # metrics.yaml — append new entries (existing entries are never modified)
        new_metric_entries = [d.metric_entry for d in drafts]
        updated_metrics = dict(existing_data)
        updated_metrics.setdefault("metrics", []).extend(new_metric_entries)
        metrics_yaml = "# yaml-language-server: $schema=../../schemas/metrics.schema.json\n" + _yaml.dump(
            updated_metrics, allow_unicode=True, sort_keys=False, default_flow_style=False
        )

        metrics_sha = None
        try:
            mf = git_ops.get_file(f"domains/{domain}/metrics.yaml")
            metrics_sha = mf.sha
        except Exception:
            pass

        git_ops.commit_file(
            path=f"domains/{domain}/metrics.yaml",
            content=metrics_yaml,
            message=f"canon({domain}): bootstrap draft metrics for {len(new_metric_entries)} measures",
            branch=branch,
            sha=metrics_sha,
        )

        # ontology.yaml — append new dimensions if any
        if new_dimensions:
            updated_ontology = dict(existing_ontology)
            updated_ontology.setdefault("dimensions", []).extend(new_dimensions)
            ontology_yaml = "# yaml-language-server: $schema=../../schemas/ontology.schema.json\n" + _yaml.dump(
                updated_ontology, allow_unicode=True, sort_keys=False, default_flow_style=False
            )
            ontology_sha = None
            try:
                of = git_ops.get_file(f"domains/{domain}/ontology.yaml")
                ontology_sha = of.sha
            except Exception:
                pass

            git_ops.commit_file(
                path=f"domains/{domain}/ontology.yaml",
                content=ontology_yaml,
                message=f"canon({domain}): bootstrap draft dimensions",
                branch=branch,
                sha=ontology_sha,
            )

        # Write bootstrap report to local cache
        cache_dir = repo_root / ".canon-cache" / domain
        cache_dir.mkdir(parents=True, exist_ok=True)
        report_path = cache_dir / "bootstrap-report.json"
        report_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")

        # Build PR body — a checklist of inferred values for human review
        high = sum(1 for d in drafts if d.confidence == "high")
        medium = sum(1 for d in drafts if d.confidence == "medium")
        low = sum(1 for d in drafts if d.confidence == "low")
        llm_mode = "LLM-enriched" if llm_client else "deterministic stubs (no LLM key)"
        confirmed_items = [
            f"- [ ] **{d.measure_name}** [{d.confidence}] — {d.metric_entry.get('definition', '')[:100]}"
            for d in drafts
        ]

        pr_body = textwrap.dedent(f"""
            ## Canon Bootstrap — {domain}

            > This is a **draft PR**. Review each inferred value below, correct what's wrong, then
            > mark this ready for review. The `review.yml` workflow will validate schema and
            > cross-file consistency when you push changes.

            **Run mode:** {llm_mode}
            **Source documents:** {", ".join(report.doc_files) if report.doc_files else "none (platform-only)"}
            **Platform measures found:** {len(platform_measure_names)}
            **Already documented (preserved):** {len(existing_names)}
            **New definitions drafted:** {len(drafts)}

            ### Confidence breakdown
            - High (doc evidence + platform match): {high}
            - Medium (platform-only): {medium}
            - Low (stub — needs human definition): {low}

            ### Confirm or correct each inferred definition

            {chr(10).join(confirmed_items)}

            ### Before merging
            - [ ] Fill in all `# TODO` fields
            - [ ] Verify `governed_sources` workspace/model/measure paths are correct
            - [ ] Set correct `owner` and `sensitivity` per your data classification policy
            - [ ] Run `uv run canon validate --domain {domain}` locally

            _Generated by `canon bootstrap` on {today} · Re-running is safe (idempotent)_
        """).strip()

        pr = git_ops.open_pr(
            title=f"canon({domain}): bootstrap draft definitions for {len(drafts)} measures",
            body=pr_body,
            head_branch=branch,
            base_branch="main",
            draft=True,
            labels=["canon"],
        )
        report.pr_url = pr.get("html_url", "")
        logger.info("Draft PR opened: %s", report.pr_url)

    except Exception as e:
        logger.error("Failed to create PR: %s", e)
        report.error = str(e)

    return report
