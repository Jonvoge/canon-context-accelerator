"""
Drift review conversation handler.

Flow:
  1. Bot fetches open drift issues from GitHub
  2. Presents each finding conversationally
  3. User responds: "document", "defer", "ignore"
  4. Bot takes action: draft PR / close issue / add comment
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from botbuilder.core import TurnContext

import bot.git_ops as git_ops

logger = logging.getLogger(__name__)

# Simple in-memory state: conversation_id → list of pending findings
_active_sessions: dict[str, list[dict]] = {}


async def is_active(conversation_id: str) -> bool:
    return conversation_id in _active_sessions and bool(_active_sessions[conversation_id])


async def handle(turn_context: TurnContext) -> None:
    conversation_id = turn_context.activity.conversation.id
    text = (turn_context.activity.text or "").strip().lower()

    # Start or continue review session
    if conversation_id not in _active_sessions:
        await _start_review(turn_context, conversation_id)
        return

    findings = _active_sessions[conversation_id]
    if not findings:
        del _active_sessions[conversation_id]
        await turn_context.send_activity("All findings reviewed. Great work!")
        return

    current = findings[0]
    await _process_response(turn_context, conversation_id, current, text)


async def _start_review(turn_context: TurnContext, conversation_id: str) -> None:
    """Fetch open drift issues and start the review conversation."""
    try:
        issues = git_ops.get_open_issues(labels=["drift"])
    except Exception as e:
        logger.error("Failed to fetch drift issues: %s", e)
        await turn_context.send_activity("Could not fetch drift findings. Check that CANON_GITHUB_TOKEN is configured.")
        return

    if not issues:
        await turn_context.send_activity("No open drift findings. Your domain definitions are in sync!")
        return

    _active_sessions[conversation_id] = issues
    count = len(issues)
    await turn_context.send_activity(
        f"Found **{count}** open drift finding{'s' if count > 1 else ''}. Let's walk through them.\n\n"
        f"For each, reply with:\n"
        f"- **document** — I'll draft a definition update PR\n"
        f"- **defer** — add a comment and leave open\n"
        f"- **ignore** — close the issue as intentionally undocumented\n"
    )
    await _present_finding(turn_context, issues[0])


async def _present_finding(turn_context: TurnContext, issue: dict) -> None:
    title = issue.get("title", "Unknown")
    body = issue.get("body", "")[:400]
    labels = [l["name"] for l in issue.get("labels", [])]
    label_str = " | ".join(l for l in labels if l not in ("canon", "drift"))

    await turn_context.send_activity(
        f"**Finding:** {title}\n"
        f"**Type:** {label_str}\n\n"
        f"{body}\n\n"
        f"_What would you like to do? (document / defer / ignore)_"
    )


async def _process_response(
    turn_context: TurnContext,
    conversation_id: str,
    issue: dict,
    text: str,
) -> None:
    issue_number = issue["number"]
    subject = issue.get("title", "")

    if "document" in text:
        await _draft_definition(turn_context, issue)
    elif "defer" in text:
        try:
            git_ops.close_issue(
                issue_number,
                comment=f"Deferred by Data Owner via Canon bot on {datetime.now(timezone.utc).date()}."
            )
        except Exception:
            pass
        await turn_context.send_activity(f"Got it — '{subject}' deferred and commented.")
    elif "ignore" in text:
        try:
            git_ops.close_issue(
                issue_number,
                comment="Closed by Data Owner via Canon bot — intentionally undocumented."
            )
        except Exception:
            pass
        await turn_context.send_activity(f"Closed '{subject}' as intentionally undocumented.")
    else:
        await turn_context.send_activity(
            "Please reply with **document**, **defer**, or **ignore**."
        )
        return

    # Move to next finding
    findings = _active_sessions.get(conversation_id, [])
    if findings:
        findings.pop(0)
    remaining = len(findings)
    if remaining > 0:
        await turn_context.send_activity(f"{remaining} finding{'s' if remaining > 1 else ''} remaining.")
        await _present_finding(turn_context, findings[0])
    else:
        _active_sessions.pop(conversation_id, None)
        await turn_context.send_activity("All findings reviewed!")


async def _draft_definition(turn_context: TurnContext, issue: dict) -> None:
    """Draft a stub definition PR for an undocumented measure."""
    issue_number = issue["number"]
    title = issue.get("title", "")
    # Extract measure name from title format: "[Drift] Undocumented: MeasureName"
    measure_name = title.replace("[Drift]", "").replace("Undocumented:", "").strip()

    # For now, generate a stub and open a PR
    branch = f"canon/drift/retail/issue-{issue_number}"
    stub_yaml = (
        f"  - name: {measure_name}\n"
        f"    domain: retail\n"
        f"    owner: \"# TODO: assign owner\"\n"
        f"    last_reviewed: {datetime.now(timezone.utc).date()}\n"
        f"    status: draft\n"
        f"    definition: \"# TODO: add definition\"\n"
        f"    governed_sources:\n"
        f"      primary:\n"
        f"        platform: fabric\n"
        f"        type: semantic_model\n"
        f"        workspace: \"Fabric Demos\"\n"
        f"        model: \"Retail Planning\"\n"
        f"        measure: \"{measure_name}\"\n"
        f"    routing: \"# TODO: add routing instructions\"\n"
        f"    sensitivity: internal\n"
    )

    try:
        git_ops.create_branch(branch)
        existing = git_ops.get_file("domains/retail/metrics.yaml", ref="main")
        updated = existing.content + "\n" + stub_yaml
        git_ops.commit_file(
            path="domains/retail/metrics.yaml",
            content=updated,
            message=f"canon(retail): draft stub for {measure_name} — refs #{issue_number}",
            branch=branch,
            sha=existing.sha,
        )
        pr = git_ops.open_pr(
            title=f"Draft: define {measure_name}",
            body=(
                f"Auto-generated stub from Canon bot drift review.\n\n"
                f"**Please fill in:**\n"
                f"- [ ] definition\n- [ ] owner\n- [ ] routing\n\n"
                f"Closes #{issue_number}"
            ),
            head_branch=branch,
            labels=["needs-review", "canon"],
        )
        await turn_context.send_activity(
            f"PR opened: **{pr['title']}** — {pr['html_url']}\n\n"
            f"Edit the stub to add the full definition, then request review."
        )
    except Exception as e:
        logger.error("Failed to draft definition PR: %s", e)
        await turn_context.send_activity(
            f"Could not open PR automatically. Manual step: add '{measure_name}' to domains/retail/metrics.yaml."
        )
