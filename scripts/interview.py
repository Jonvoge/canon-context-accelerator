"""
Canon interview script — CLI entry point for terminal-based definition interviews.

This is the local CLI counterpart to the Teams bot interview handler.
It runs a simple terminal interview for undocumented measures and writes
drafted definitions directly to domains/{domain}/metrics.yaml.

Usage:
  uv run python -m scripts.cli interview --domain retail
"""

from __future__ import annotations

import json
import logging
import os
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import yaml

logger = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5"

_QUESTIONS = [
    "How would you define this measure in plain business language?",
    "Are there exclusions, filters, or edge cases built in that users might not know about?",
    "Which source should agents use — the semantic model (RetailSemanticModel) or Fabric SQL endpoint?",
    "What common aliases or names do people use for this in reports or conversations?",
    "Anything else agents should know about when NOT to use this measure?",
]


def _draft_metric(client: anthropic.Anthropic, name: str, domain: str, transcript: list[dict]) -> dict:
    today = datetime.now(timezone.utc).date().isoformat()
    qa_text = "\n".join(f"Q: {qa['q']}\nA: {qa['a']}" for qa in transcript)
    prompt = textwrap.dedent(f"""
        Draft a Canon metric definition from this Q&A interview.
        Measure: {name} | Domain: {domain}

        Interview:
        {qa_text}

        Produce ONE metrics.yaml entry as JSON (no markdown):
        {{
            "name": "{name}", "domain": "{domain}",
            "owner": "{domain.title()} Analytics Team",
            "last_reviewed": "{today}", "status": "active",
            "aliases": [], "definition": "<definition>",
            "governed_sources": {{
                "primary": {{
                    "platform": "fabric", "type": "semantic_model",
                    "workspace": "Fabric Demos: Retail Planning",
                    "model": "RetailSemanticModel", "measure": "{name}"
                }}
            }},
            "routing": "<routing>", "depends_on": [], "sensitivity": "internal"
        }}
    """).strip()
    response = client.messages.create(
        model=_MODEL, max_tokens=700, temperature=0.0,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip().strip("```").strip()
    return json.loads(raw)


def start_interview(domain: str, owner_email: str = "", repo_root: Path | None = None) -> None:
    """
    Run a terminal-based interview for undocumented measures in a domain.
    Appends drafted entries to domains/{domain}/metrics.yaml.
    """
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY required for interview")

    client = anthropic.Anthropic(api_key=api_key)
    domain_path = repo_root / "domains" / domain
    metrics_path = domain_path / "metrics.yaml"

    if not metrics_path.exists():
        raise FileNotFoundError(f"metrics.yaml not found at {metrics_path}")

    data = yaml.safe_load(metrics_path.read_text(encoding="utf-8")) or {}
    existing_names = {m["name"] for m in data.get("metrics", [])}

    # Determine undocumented measures from scan cache
    scan_path = repo_root / ".canon-cache" / domain / "scan.json"
    undocumented: list[str] = []

    if scan_path.exists():
        scan_data = json.loads(scan_path.read_text(encoding="utf-8"))
        undocumented = [
            f["subject"] for f in scan_data.get("findings", [])
            if f.get("type") == "undocumented_measure"
        ]

    if not undocumented:
        print(f"No undocumented measures found in scan cache for domain '{domain}'.")
        print("Run `canon scan` first, or all measures are already documented.")
        return

    print(f"\nCanon Interview — {domain}")
    print(f"Found {len(undocumented)} undocumented measures.")
    print("For each, I'll ask up to 5 questions. Type your answers and press Enter.")
    print("Press Ctrl+C at any time to save progress and exit.\n")

    new_entries = []

    for i, measure in enumerate(undocumented, 1):
        print(f"\n── Measure {i}/{len(undocumented)}: {measure} ──")
        transcript = []

        for q in _QUESTIONS:
            print(f"\n{q}")
            try:
                answer = input("> ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nInterrupted — saving progress.")
                break
            if answer.lower() in ("skip", "s", ""):
                continue
            transcript.append({"q": q, "a": answer})

        if not transcript:
            print(f"  Skipped {measure}")
            continue

        print(f"\nDrafting definition for {measure}...")
        try:
            entry = _draft_metric(client, measure, domain, transcript)
        except Exception as e:
            print(f"  LLM error: {e}. Writing stub.")
            entry = {
                "name": measure, "domain": domain,
                "owner": f"{domain.title()} Analytics Team",
                "last_reviewed": datetime.now(timezone.utc).date().isoformat(),
                "status": "active", "aliases": [],
                "definition": "# TODO: add definition",
                "governed_sources": {
                    "primary": {
                        "platform": "fabric", "type": "semantic_model",
                        "workspace": "Fabric Demos: Retail Planning",
                        "model": "RetailSemanticModel", "measure": measure,
                    }
                },
                "routing": "# TODO: add routing", "depends_on": [], "sensitivity": "internal",
            }

        print(f"\nDraft:\n  definition: {str(entry.get('definition',''))[:200]}")
        confirm = input("Keep? [Y/n]: ").strip().lower()
        if confirm in ("n", "no"):
            print("  Skipped.")
            continue

        new_entries.append(entry)

    if new_entries:
        data.setdefault("metrics", []).extend(new_entries)
        header = "# yaml-language-server: $schema=../../schemas/metrics.schema.json\n"
        metrics_path.write_text(
            header + yaml.dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False),
            encoding="utf-8",
        )
        print(f"\n✓ Added {len(new_entries)} definition(s) to {metrics_path}")
    else:
        print("\nNo new definitions written.")

