"""
Weekly digest handler.

Fetches open drift issues and sends a structured summary.
"""

from __future__ import annotations

import logging

from botbuilder.core import TurnContext

import bot.git_ops as git_ops

logger = logging.getLogger(__name__)


async def send_digest(turn_context: TurnContext) -> None:
    try:
        issues = git_ops.get_open_issues(labels=["drift"])
    except Exception as e:
        logger.error("Digest fetch failed: %s", e)
        await turn_context.send_activity("Could not fetch drift findings.")
        return

    if not issues:
        await turn_context.send_activity("**Canon Weekly Digest** — No open drift findings.")
        return

    # Group by type label
    by_type: dict[str, list[dict]] = {}
    for issue in issues:
        for label in issue.get("labels", []):
            lname = label["name"]
            if lname in ("undocumented-measure", "orphaned-definition", "missing-source", "dimension-values"):
                by_type.setdefault(lname, []).append(issue)
                break

    lines = ["**📋 Canon Weekly Drift Digest**\n"]
    label_display = {
        "undocumented-measure": "Undocumented measures",
        "orphaned-definition": "Orphaned definitions",
        "missing-source": "Missing sources",
        "dimension-values": "Dimension value changes",
    }

    for lname, label_title in label_display.items():
        if lname in by_type:
            count = len(by_type[lname])
            lines.append(f"- **{label_title}**: {count}")

    lines.append(f"\n_Total: {len(issues)} open findings_")
    lines.append('\nReply **review** to walk through them.')

    await turn_context.send_activity("\n".join(lines))
