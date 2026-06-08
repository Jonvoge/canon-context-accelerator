"""
Canon evaluation harness.

For each question in evals/{domain}/questions.yaml:
  1. Assemble domain context via the MCP serving layer
  2. Feed question + context to claude-3-haiku-20240307
  3. Score: correct_metric? correct_source? correct_filters?
  4. Write results to .canon-cache/{domain}/eval-results.json

Usage:
  uv run python evals/run_evals.py --domain retail
  uv run python evals/run_evals.py  (all domains)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import yaml

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MODEL = "claude-haiku-4-5"
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
            f"- {d['name']} (column: {d.get('column', '')}) "
            + (f"values: {val_sample}" if val_sample else "")
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
        # Include only the most relevant sections
        sections.append("## DOMAIN RULES\n" + rules_md[:800])

    return "\n\n".join(sections)


def _ask_llm(client: anthropic.Anthropic, context: str, question: str) -> tuple[str, str]:
    """Ask the LLM and return (raw_response, parsed_json_str)."""
    user_msg = f"## CANON DOMAIN CONTEXT\n{context}\n\n## QUESTION\n{question}"
    response = client.messages.create(
        model=_MODEL,
        max_tokens=512,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    raw = response.content[0].text.strip()
    return raw, raw


def _parse_response(raw: str) -> dict:
    """Parse LLM JSON response, tolerating minor formatting."""
    # Strip markdown code fences if present
    raw = raw.strip()
    if raw.startswith("```"):
        raw = "\n".join(raw.split("\n")[1:])
    if raw.endswith("```"):
        raw = "\n".join(raw.split("\n")[:-1])
    return json.loads(raw.strip())


def _score(q: EvalQuestion, actual: dict) -> EvalScore:
    actual_metric = actual.get("metric", "")
    actual_source = actual.get("source", "")
    reasoning = actual.get("reasoning", "")

    # Refusal questions: pass if agent explicitly refuses (empty metric/source + reasoning mentions refusal)
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


def run_evals(domain: str, api_key: str | None = None) -> EvalResult:
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY is required")

    client = anthropic.Anthropic(api_key=api_key)
    questions = _load_questions(domain)
    if not questions:
        logger.warning("No questions found for domain '%s'", domain)

    context = _assemble_context(domain)
    scores = []

    for q in questions:
        logger.info("  [%s] %s", q.id, q.question[:60])
        try:
            raw, _ = _ask_llm(client, context, q.question)
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
        model=_MODEL,
        run_at=datetime.now(timezone.utc).isoformat(),
        total=len(scores),
        passed=passed,
        failed=len(scores) - passed,
        pass_rate=passed / len(scores) if scores else 0.0,
        scores=scores,
    )

    # Write to cache
    cache_dir = _REPO_ROOT / ".canon-cache" / domain
    cache_dir.mkdir(parents=True, exist_ok=True)
    out_path = cache_dir / "eval-results.json"
    out_path.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
    logger.info("Results written to %s", out_path)

    return result


def _main() -> None:
    parser = argparse.ArgumentParser(description="Canon eval harness")
    parser.add_argument("--domain", default=None, help="Domain to evaluate (default: all)")
    parser.add_argument("--api-key", default=None, help="Anthropic API key")
    args = parser.parse_args()

    domains = []
    if args.domain:
        domains = [args.domain]
    else:
        domains_dir = _REPO_ROOT / "domains"
        domains = [d.name for d in sorted(domains_dir.iterdir()) if d.is_dir() and d.name != "_template"]

    any_failure = False
    for domain in domains:
        logger.info("Running evals for domain: %s", domain)
        result = run_evals(domain, api_key=args.api_key)
        logger.info(
            "  %s/%s passed (%.0f%%)",
            result.passed, result.total, result.pass_rate * 100
        )
        if result.pass_rate < 0.8:
            logger.warning("  BELOW THRESHOLD (80%%)")
            any_failure = True

    import sys
    sys.exit(1 if any_failure else 0)


if __name__ == "__main__":
    _main()

