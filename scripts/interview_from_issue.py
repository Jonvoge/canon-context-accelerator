from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

import yaml

from scripts.interview import _QUESTIONS, _draft_metric


def _extract_field(text: str, name: str) -> str:
    match = re.search(rf"(?im)^{name}:\s*(.+)$", text)
    return match.group(1).strip() if match else ""


def _extract_numbered_answers(body: str) -> list[str]:
    matches = list(re.finditer(r"(?ms)^\s*(\d+)\.\s*(.+?)(?=^\s*\d+\.\s|\Z)", body))
    answers = [""] * len(_QUESTIONS)
    for match in matches:
        idx = int(match.group(1)) - 1
        if 0 <= idx < len(_QUESTIONS):
            answers[idx] = match.group(2).strip()
    return answers


def _detect_domain(issue: dict, comment_body: str) -> str:
    domain = _extract_field(comment_body, "Domain") or _extract_field(issue.get("body", ""), "Domain")
    if domain:
        return domain
    for label in issue.get("labels", []):
        name = label.get("name", "")
        if name.startswith("domain:"):
            return name.split(":", 1)[1]
    return ""


def _detect_metric_name(issue: dict, comment_body: str) -> str:
    metric = _extract_field(comment_body, "Metric") or _extract_field(issue.get("body", ""), "Metric")
    if metric:
        return metric
    title = issue.get("title", "")
    if ":" in title:
        return title.split(":", 1)[1].strip()
    return title.strip()


def _draft_stub(name: str, domain: str) -> dict:
    today = datetime.now(UTC).date().isoformat()
    return {
        "name": name,
        "domain": domain,
        "owner": f"{domain.title()} Analytics Team",
        "last_reviewed": today,
        "status": "active",
        "aliases": [],
        "definition": "# TODO: add definition",
        "governed_sources": {
            "primary": {
                "platform": "fabric",
                "type": "semantic_model",
                "workspace": "Fabric Demos: Retail Planning",
                "model": "RetailSemanticModel",
                "measure": name,
            }
        },
        "routing": "# TODO: add routing",
        "depends_on": [],
        "sensitivity": "internal",
    }


def process_issue_comment(repo_root: Path) -> dict:
    event_path = os.environ.get("GITHUB_EVENT_PATH", "")
    if not event_path:
        raise ValueError("GITHUB_EVENT_PATH is required")
    event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    issue = event.get("issue", {})
    comment = event.get("comment", {})
    comment_body = comment.get("body", "")

    domain = _detect_domain(issue, comment_body)
    metric_name = _detect_metric_name(issue, comment_body)
    if not domain or not metric_name:
        raise ValueError("Could not determine domain and metric from issue/comment content")

    answers = _extract_numbered_answers(comment_body)
    transcript = [{"q": question, "a": answer} for question, answer in zip(_QUESTIONS, answers, strict=False) if answer]
    if not transcript:
        raise ValueError("No Q&A answers found in comment body")

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    entry = None
    if api_key:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        try:
            entry = _draft_metric(client, metric_name, domain, transcript)
        except Exception:
            entry = None
    if entry is None:
        entry = _draft_stub(metric_name, domain)

    metrics_path = repo_root / "domains" / domain / "metrics.yaml"
    data = yaml.safe_load(metrics_path.read_text(encoding="utf-8")) or {}
    data.setdefault("metrics", []).append(entry)
    header = "# yaml-language-server: $schema=../../schemas/metrics.schema.json\n"
    metrics_path.write_text(
        header + yaml.dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    result = {"domain": domain, "metric": metric_name, "path": str(metrics_path), "transcript_count": len(transcript)}
    print(json.dumps(result))
    return result


if __name__ == "__main__":
    process_issue_comment(Path(__file__).resolve().parent.parent)
