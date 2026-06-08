"""
Tests for canon/schema/validator.py.

Covers:
  - Retail domain passes validation
  - Layer 1 JSON Schema violation (bad status value)
  - Layer 2 cross-file rule: domain field mismatch
  - Layer 2 cross-file rule: duplicate metric names
  - Layer 2 cross-file rule: unresolved depends_on
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from canon.schema.validator import ValidationResult, validate_domain


def test_retail_domain_passes(repo_root: Path) -> None:
    result = validate_domain("retail", repo_root)
    errors = [f for f in result.findings if f.severity == "error"]
    assert not errors, f"Expected no errors, got: {[f.message for f in errors]}"


def test_invalid_status_fails_layer1(tmp_path: Path, repo_root: Path) -> None:
    """A metrics.yaml with invalid status value should produce an error."""
    # Copy retail domain files to tmp_path
    src = repo_root / "domains" / "retail"
    dst = tmp_path / "domains" / "retail"
    dst.mkdir(parents=True)
    for f in src.iterdir():
        (dst / f.name).write_bytes(f.read_bytes())

    metrics = yaml.safe_load((dst / "metrics.yaml").read_text(encoding="utf-8"))
    metrics["metrics"][0]["status"] = "invalid-status-value"
    (dst / "metrics.yaml").write_text(
        yaml.dump(metrics, allow_unicode=True, default_flow_style=False), encoding="utf-8"
    )

    result = validate_domain("retail", tmp_path)
    assert any(f.severity == "error" for f in result.findings)


def test_domain_field_mismatch(tmp_path: Path, repo_root: Path) -> None:
    """If metrics.yaml domain field doesn't match folder name, should raise error."""
    src = repo_root / "domains" / "retail"
    dst = tmp_path / "domains" / "retail"
    dst.mkdir(parents=True)
    for f in src.iterdir():
        (dst / f.name).write_bytes(f.read_bytes())

    metrics = yaml.safe_load((dst / "metrics.yaml").read_text(encoding="utf-8"))
    metrics["domain"] = "finance"  # wrong domain name
    (dst / "metrics.yaml").write_text(
        yaml.dump(metrics, allow_unicode=True, default_flow_style=False), encoding="utf-8"
    )

    result = validate_domain("retail", tmp_path)
    rule_ids = [f.rule for f in result.findings if f.severity == "error"]
    assert "domain-field-match" in rule_ids


def test_duplicate_metric_names(tmp_path: Path, repo_root: Path) -> None:
    """Duplicate metric names in the same file should produce an error."""
    src = repo_root / "domains" / "retail"
    dst = tmp_path / "domains" / "retail"
    dst.mkdir(parents=True)
    for f in src.iterdir():
        (dst / f.name).write_bytes(f.read_bytes())

    metrics = yaml.safe_load((dst / "metrics.yaml").read_text(encoding="utf-8"))
    dup = dict(metrics["metrics"][0])
    metrics["metrics"].append(dup)  # add duplicate
    (dst / "metrics.yaml").write_text(
        yaml.dump(metrics, allow_unicode=True, default_flow_style=False), encoding="utf-8"
    )

    result = validate_domain("retail", tmp_path)
    rule_ids = [f.rule for f in result.findings if f.severity == "error"]
    assert "unique-metric-names" in rule_ids


def test_unresolved_depends_on(tmp_path: Path, repo_root: Path) -> None:
    """depends_on referencing a non-existent metric should raise a warning."""
    src = repo_root / "domains" / "retail"
    dst = tmp_path / "domains" / "retail"
    dst.mkdir(parents=True)
    for f in src.iterdir():
        (dst / f.name).write_bytes(f.read_bytes())

    metrics = yaml.safe_load((dst / "metrics.yaml").read_text(encoding="utf-8"))
    metrics["metrics"][0]["depends_on"] = ["NonExistentMetric"]
    (dst / "metrics.yaml").write_text(
        yaml.dump(metrics, allow_unicode=True, default_flow_style=False), encoding="utf-8"
    )

    result = validate_domain("retail", tmp_path)
    rule_ids = [f.rule for f in result.findings]
    assert "depends-on-resolution" in rule_ids
