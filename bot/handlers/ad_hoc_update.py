"""
Ad-hoc definition update handler.

When a Data Owner sends a free-text update request (e.g. "Revenue should now exclude X"),
the bot:
  1. Reads the current definition from GitHub
  2. Uses claude-haiku to draft the updated definition
  3. Opens a PR on a new branch
  4. Confirms with the user
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone

import anthropic
import yaml
from botbuilder.core import TurnContext

import bot.git_ops as git_ops

logger = logging.getLogger(__name__)

_MODEL = "claude-3-haiku-20240307"
_ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


def _find_metric_in_yaml(text: str, metric_name: str) -> dict | None:
    data = yaml.safe_load(text)
    for m in data.get("metrics", []):
        if m["name"].lower() == metric_name.lower():
            return m
    return None


def _detect_metric_name(user_text: str, metrics_yaml_content: str) -> str | None:
    """Ask the LLM to identify which metric the user is referring to."""
    data = yaml.safe_load(metrics_yaml_content) or {}
    names = [m["name"] for m in data.get("metrics", [])]
    aliases_map = {}
    for m in data.get("metrics", []):
        for alias in m.get("aliases", []):
            aliases_map[alias.lower()] = m["name"]

    user_lower = user_text.lower()
    # Direct name match
    for name in names:
        if name.lower() in user_lower:
            return name
    # Alias match
    for alias, name in aliases_map.items():
        if alias in user_lower:
            return name
    return None


async def handle(turn_context: TurnContext, text: str) -> None:
    """Handle an ad-hoc definition update request."""
    if not _ANTHROPIC_API_KEY:
        await turn_context.send_activity(
            "I can't draft updates right now (ANTHROPIC_API_KEY not configured).\n"
            "Please open a PR directly in GitHub."
        )
        return

    # Load current metrics
    try:
        metrics_file = git_ops.get_file("domains/retail/metrics.yaml")
    except Exception as e:
        logger.error("Failed to load metrics: %s", e)
        await turn_context.send_activity("Could not load the current definitions. Please try again.")
        return

    metric_name = _detect_metric_name(text, metrics_file.content)
    if not metric_name:
        await turn_context.send_activity(
            "I couldn't identify which metric you're referring to. "
            "Could you name it explicitly? (e.g. 'Revenue should now exclude X')"
        )
        return

    current_metric = _find_metric_in_yaml(metrics_file.content, metric_name)
    if not current_metric:
        await turn_context.send_activity(f"Metric '{metric_name}' not found in the retail domain.")
        return

    await turn_context.send_activity(f"Got it — updating **{metric_name}**. Drafting change...")

    # Use LLM to draft the updated definition
    client = anthropic.Anthropic(api_key=_ANTHROPIC_API_KEY)
    current_def = current_metric.get("definition", "")
    prompt = (
        f"The user wants to update the Canon definition for '{metric_name}'.\n\n"
        f"Current definition:\n{current_def}\n\n"
        f"User's requested change:\n{text}\n\n"
        f"Write ONLY the updated definition text (1-3 sentences, business language). "
        f"No YAML, no code, no preamble."
    )
    try:
        response = client.messages.create(
            model=_MODEL,
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        new_definition = response.content[0].text.strip()
    except Exception as e:
        logger.error("LLM draft failed: %s", e)
        await turn_context.send_activity("Could not draft the update. Please edit the definition manually in GitHub.")
        return

    # Build updated YAML
    data = yaml.safe_load(metrics_file.content) or {}
    for m in data.get("metrics", []):
        if m["name"].lower() == metric_name.lower():
            m["definition"] = new_definition
            m["last_reviewed"] = str(datetime.now(timezone.utc).date())
            break

    updated_yaml = yaml.dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)

    # Open PR
    slug = re.sub(r"[^a-z0-9-]", "-", metric_name.lower())[:30]
    branch = f"canon/domain/retail/{slug}-update-{datetime.now(timezone.utc).strftime('%Y%m%d')}"

    try:
        git_ops.create_branch(branch)
        git_ops.commit_file(
            path="domains/retail/metrics.yaml",
            content=updated_yaml,
            message=f"canon(retail): update {metric_name} definition",
            branch=branch,
            sha=metrics_file.sha,
        )
        pr = git_ops.open_pr(
            title=f"Update: {metric_name} definition",
            body=(
                f"**Requested by:** Data Owner via Canon bot\n\n"
                f"**Change:** {text}\n\n"
                f"**New definition:**\n> {new_definition}\n\n"
                f"**Old definition:**\n> {current_def}"
            ),
            head_branch=branch,
            labels=["needs-review", "canon"],
        )
        await turn_context.send_activity(
            f"PR opened: **{pr['title']}**\n"
            f"{pr['html_url']}\n\n"
            f"**Proposed definition:**\n> {new_definition}\n\n"
            f"Review and merge to make it official."
        )
    except Exception as e:
        logger.error("Failed to open PR: %s", e)
        await turn_context.send_activity(
            f"Drafted update but couldn't open PR. The new definition would be:\n\n"
            f"> {new_definition}"
        )
