"""
Canon evaluation harness.

For each question in evals/{domain}/questions.yaml:
  1. Assemble domain context via the MCP serving layer
  2. Feed question + context to claude-haiku-4-5
  3. Score: correct_metric? correct_source? correct_filters?
  4. Write results to .canon-cache/{domain}/eval-results.json
  5. Compare to baseline in .canon-cache/{domain}/eval-baseline.json
  6. Exit non-zero if pass_rate < threshold or regression detected

Usage:
  uv run python evals/run_evals.py --domain retail
  uv run python evals/run_evals.py                   # all domains
  uv run python evals/run_evals.py --update-baseline  # save current results as baseline
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import anthropic
import yaml

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SYSTEM_PROMPT = """You are a data analyst assistant that routes business questions to the correct data source.

You will receive:
1. A Canon domain context (metrics, dimensions, rules)
2. A business question

Respond ONLY with a JSON object (no prose, no markdown) in this exact format:
{
  "metric": "<metric name from Canon, exactly as written>",
  "source": "primary" or "warehouse",
  "filters": {"<dimension name>": "<value or [values]>"},
  "time_scope": "<relative or absolute period, empty string if none>",
  "reasoning": "<one sentence>"
}"""


# ── Config ────────────────────────────────────────────────────────────────────


def _load_eval_config() -> dict:
    path = _REPO_ROOT / "eval-config.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


# ── Data models ───────────────────────────────────────────────────────────────


@dataclass
class EvalQuestion:
    id: str
    question: str
    expected_metric: str
    expected_source: str
    expected_filters: dict = field(default_factory=dict)
    expected_time_scope: str = ""
    difficulty: str = "medium"
    notes: str = ""
    refusal: bool = False


@dataclass
class EvalScore:
    question_id: str
    question: str
    expected_metric: str
    actual_metric: str
    expected_source: str
    actual_source: str
    metric_correct: bool
    source_correct: bool
    overall: bool
    model_response: str
    error: str = ""


@dataclass
class EvalResult:
    domain: str
    model: str
    run_at: str
    total: int
    passed: int
    failed: int
    pass_rate: float
    scores: list[EvalScore] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RegressionReport:
    domain: str
    baseline_pass_rate: float
    current_pass_rate: float
    delta: float
    regression: bool
    regression_threshold: float
    regressions: list[str] = field(default_factory=list)  # question IDs that regressed


# ── Loading ───────────────────────────────────────────────────────────────────


def _load_questions(domain: str) -> list[EvalQuestion]:
    path = _REPO_ROOT / "evals" / domain / "questions.yaml"
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [
        EvalQuestion(
            id=q["id"],
            question=q["question"],
            expected_metric=q["expected_metric"],
            expected_source=q["expected_source"],
            expected_filters=q.get("expected_filters", {}),
            expected_time_scope=q.get("expected_time_scope", ""),
            difficulty=q.get("difficulty", "medium"),
            notes=q.get("notes", ""),
            refusal=q.get("refusal", False),
        )
        for q in raw.get("questions", [])
    ]


def _assemble_context(domain: str) -> str:
    """Build a compact context string for the LLM from domain YAML files."""
    domain_path = _REPO_ROOT / "domains" / domain
    sections = []

    def _load_yaml(fname: str) -> dict:
        p = domain_path / fname
        return yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}

    def _load_md(fname: str) -> str:
        p = domain_path / fname
        return p.read_text(encoding="utf-8") if p.exists() else ""

    metrics = _load_yaml("metrics.yaml")
    ontology = _load_yaml("ontology.yaml")
    glossary = _load_yaml("glossary.yaml")
    rules_md = _load_md("domain-rules.md")

    # Compact metric summary
    metric_lines = []
    for m in metrics.get("metrics", []):
        if m.get("status") == "deprecated":
            continue
        aliases = ", ".join(m.get("aliases", []))
        primary = m.get("governed_sources", {}).get("primary", {})
        src = f"{primary.get('type', '')}/{primary.get('model', primary.get('object', ''))}"
        metric_lines.append(
            f"- {m['name']} | aliases: [{aliases}] | source: {src}\n"
            f"  definition: {m.get('definition', '').strip()[:200]}\n"
            f"  routing: {m.get('routing', '').strip()[:200]}"
        )

    if metric_lines:
        sections.append("## METRICS\n" + "\n".join(metric_lines))

    # Compact dimension summary
    dim_lines = []
    for d in ontology.get("dimensions", []):
        val_sample = list(d.get("value_descriptions", {}).keys())[:5]
        dim_lines.append(
            f"- {d['name']} (column: {d.get('column', '')}) " + (f"values: {val_sample}" if val_sample else "")
        )
    if dim_lines:
        sections.append("## DIMENSIONS\n" + "\n".join(dim_lines))

    # Glossary terms (compact)
    term_lines = [
        f"- {t['name']}: {t.get('definition', '').strip()[:100]}"
        for t in glossary.get("terms", [])
        if t.get("status") != "deprecated"
    ]
    if term_lines:
        sections.append("## GLOSSARY\n" + "\n".join(term_lines))

    if rules_md:
        sections.append("## DOMAIN RULES\n" + rules_md[:800])

    return "\n\n".join(sections)


# ── LLM call ─────────────────────────────────────────────────────────────────


def _ask_llm(client: anthropic.Anthropic, context: str, question: str, model: str, max_tokens: int) -> str:
    """Ask the LLM and return raw response text."""
    user_msg = f"## CANON DOMAIN CONTEXT\n{context}\n\n## QUESTION\n{question}"
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=0.0,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    return response.content[0].text.strip()


def _parse_response(raw: str) -> dict:
    """Parse LLM JSON response, tolerating markdown code fences."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = "\n".join(raw.split("\n")[1:])
    if raw.endswith("```"):
        raw = "\n".join(raw.split("\n")[:-1])
    return json.loads(raw.strip())


# ── Scoring ───────────────────────────────────────────────────────────────────


def _score(q: EvalQuestion, actual: dict) -> EvalScore:
    actual_metric = actual.get("metric", "")
    actual_source = actual.get("source", "")
    reasoning = actual.get("reasoning", "")

    if q.refusal:
        refused = (not actual_metric and not actual_source) and bool(reasoning)
        return EvalScore(
            question_id=q.id,
            question=q.question,
            expected_metric=q.expected_metric,
            actual_metric=actual_metric,
            expected_source=q.expected_source,
            actual_source=actual_source,
            metric_correct=refused,
            source_correct=refused,
            overall=refused,
            model_response=json.dumps(actual),
        )

    metric_correct = actual_metric.lower() == q.expected_metric.lower()
    source_correct = actual_source == q.expected_source

    return EvalScore(
        question_id=q.id,
        question=q.question,
        expected_metric=q.expected_metric,
        actual_metric=actual_metric,
        expected_source=q.expected_source,
        actual_source=actual_source,
        metric_correct=metric_correct,
        source_correct=source_correct,
        overall=metric_correct and source_correct,
        model_response=json.dumps(actual),
    )


# ── Baseline management ───────────────────────────────────────────────────────


def _load_baseline(domain: str) -> dict | None:
    path = _REPO_ROOT / ".canon-cache" / domain / "eval-baseline.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _save_baseline(domain: str, result: EvalResult) -> None:
    cache_dir = _REPO_ROOT / ".canon-cache" / domain
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / "eval-baseline.json"
    path.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
    logger.info("Baseline saved to %s", path)


def _check_regression(result: EvalResult, baseline: dict, regression_threshold: float) -> RegressionReport:
    baseline_rate = baseline.get("pass_rate", 0.0)
    delta = result.pass_rate - baseline_rate
    regression = delta < -regression_threshold

    # Find questions that passed in baseline but fail now
    baseline_passed = {s["question_id"] for s in baseline.get("scores", []) if s.get("overall")}
    current_passed = {s.question_id for s in result.scores if s.overall}
    regressions = sorted(baseline_passed - current_passed)

    return RegressionReport(
        domain=result.domain,
        baseline_pass_rate=baseline_rate,
        current_pass_rate=result.pass_rate,
        delta=delta,
        regression=regression,
        regression_threshold=regression_threshold,
        regressions=regressions,
    )


# ── Runner ────────────────────────────────────────────────────────────────────


def run_evals(domain: str, api_key: str | None = None, config: dict | None = None) -> EvalResult:
    if config is None:
        config = _load_eval_config()

    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY is required")

    model = config.get("model", "claude-haiku-4-5")
    max_tokens = config.get("max_tokens", 512)

    client = anthropic.Anthropic(api_key=api_key)
    questions = _load_questions(domain)
    if not questions:
        logger.warning("No questions found for domain '%s'", domain)

    context = _assemble_context(domain)
    scores = []

    for q in questions:
        logger.info("  [%s] %s", q.id, q.question[:60])
        try:
            raw = _ask_llm(client, context, q.question, model, max_tokens)
            parsed = _parse_response(raw)
            score = _score(q, parsed)
        except Exception as e:
            score = EvalScore(
                question_id=q.id,
                question=q.question,
                expected_metric=q.expected_metric,
                actual_metric="",
                expected_source=q.expected_source,
                actual_source="",
                metric_correct=False,
                source_correct=False,
                overall=False,
                model_response="",
                error=str(e),
            )
        scores.append(score)
        status = "✓" if score.overall else "✗"
        logger.info("    %s metric=%s source=%s", status, score.metric_correct, score.source_correct)

    passed = sum(1 for s in scores if s.overall)
    result = EvalResult(
        domain=domain,
        model=model,
        run_at=datetime.now(UTC).isoformat(),
        total=len(scores),
        passed=passed,
        failed=len(scores) - passed,
        pass_rate=passed / len(scores) if scores else 0.0,
        scores=scores,
    )

    cache_dir = _REPO_ROOT / ".canon-cache" / domain
    cache_dir.mkdir(parents=True, exist_ok=True)
    out_path = cache_dir / "eval-results.json"
    out_path.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
    logger.info("Results written to %s", out_path)

    return result


# ── CLI ───────────────────────────────────────────────────────────────────────


def _main() -> None:
    parser = argparse.ArgumentParser(description="Canon eval harness")
    parser.add_argument("--domain", default=None, help="Domain to evaluate (default: all)")
    parser.add_argument("--api-key", default=None, help="Anthropic API key")
    parser.add_argument("--update-baseline", action="store_true", help="Save current results as the new baseline")
    parser.add_argument(
        "--check-regression", action="store_true", help="Exit non-zero if regression vs baseline is detected"
    )
    args = parser.parse_args()

    config = _load_eval_config()
    pass_threshold = config.get("pass_threshold", 0.80)
    regression_threshold = config.get("regression_threshold", 0.05)
    auto_update = config.get("auto_update_baseline", False)

    if args.domain:
        domains = [args.domain]
    else:
        configured = config.get("domains", [])
        if configured:
            domains = configured
        else:
            domains_dir = _REPO_ROOT / "domains"
            domains = [d.name for d in sorted(domains_dir.iterdir()) if d.is_dir() and d.name != "_template"]

    any_failure = False

    for domain in domains:
        logger.info("Running evals for domain: %s", domain)
        result = run_evals(domain, api_key=args.api_key, config=config)
        logger.info("  %s/%s passed (%.0f%%)", result.passed, result.total, result.pass_rate * 100)

        # Baseline handling
        baseline = _load_baseline(domain)
        if baseline:
            report = _check_regression(result, baseline, regression_threshold)
            if report.regression:
                logger.warning(
                    "  REGRESSION: pass rate dropped %.1f%% vs baseline (%.0f%% → %.0f%%). Regressed questions: %s",
                    abs(report.delta) * 100,
                    report.baseline_pass_rate * 100,
                    report.current_pass_rate * 100,
                    ", ".join(report.regressions) or "none",
                )
                if args.check_regression:
                    any_failure = True
            else:
                delta_str = f"+{report.delta * 100:.1f}%" if report.delta >= 0 else f"{report.delta * 100:.1f}%"
                logger.info(
                    "  vs baseline: %s (%.0f%% → %.0f%%)",
                    delta_str,
                    report.baseline_pass_rate * 100,
                    result.pass_rate * 100,
                )
        else:
            logger.info("  No baseline found — run with --update-baseline to establish one")

        # Save baseline if requested or auto-update when passing
        if args.update_baseline or (auto_update and result.pass_rate >= pass_threshold):
            _save_baseline(domain, result)

        if result.pass_rate < pass_threshold:
            logger.warning("  BELOW THRESHOLD (%.0f%%)", pass_threshold * 100)
            any_failure = True

    import sys

    sys.exit(1 if any_failure else 0)


if __name__ == "__main__":
    _main()
