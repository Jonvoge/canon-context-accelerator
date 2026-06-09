"""
Canon interview handler.

Conducts a multi-turn conversational interview with a Data Owner to draft
metric definitions for undocumented measures identified by the scan.

Flow:
  1. Fetch open undocumented-measure issues from GitHub
  2. For each measure, ask 3-5 targeted questions via Claude
  3. Draft a metrics.yaml entry from the answers
  4. Commit the draft to a branch and open a PR when done

State per conversation:
  - list of measures to interview
  - current measure index
  - Q&A transcript for current measure
  - current question index
  - branch name for the session
"""

from __future__ import annotations

import json
import logging
import os
import textwrap
from datetime import datetime, timezone
from typing import Any

import anthropic
import yaml
from botbuilder.core import TurnContext

import bot.git_ops as git_ops

logger = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5"
_ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Questions to ask per measure (rendered into natural language by the LLM)
_INTERVIEW_QUESTIONS = [
    "How would you define this measure in plain business language — what does it actually count or sum?",
    "Are there any exclusions, filters, or conditions built into this number that a business user might not know about?",
    "Which data source should AI agents use for this: the semantic model (RetailSemanticModel) or the Fabric SQL endpoint?",
    "Are there any aliases or common names people use for this measure in reports or conversations?",
    "Is there anything else agents should know about when or how NOT to use this measure?",
]

# Session state: conversation_id → InterviewSession
_sessions: dict[str, "InterviewSession"] = {}


class InterviewSession:
    def __init__(self, measures: list[str], branch: str) -> None:
        self.measures = measures          # list of measure names to interview
        self.current_idx = 0              # which measure we're on
        self.transcript: list[dict] = [] # Q&A for current measure [{q, a}]
        self.question_idx = 0            # which question within current measure
        self.branch = branch
        self.awaiting_answer = False
        self.all_drafts: list[dict] = [] # completed drafted entries
        self.metrics_sha: str | None = None  # SHA of metrics.yaml on the branch

    @property
    def current_measure(self) -> str | None:
        if self.current_idx < len(self.measures):
            return self.measures[self.current_idx]
        return None

    @property
    def current_question(self) -> str | None:
        if self.question_idx < len(_INTERVIEW_QUESTIONS):
            return _INTERVIEW_QUESTIONS[self.question_idx]
        return None

    def record_answer(self, answer: str) -> None:
        self.transcript.append({
            "q": _INTERVIEW_QUESTIONS[self.question_idx],
            "a": answer,
        })
        self.question_idx += 1

    def advance_measure(self) -> None:
        self.current_idx += 1
        self.question_idx = 0
        self.transcript = []

    @property
    def is_complete(self) -> bool:
        return self.current_idx >= len(self.measures)


class InterviewHandler:
    async def is_active(self, conversation_id: str) -> bool:
        return conversation_id in _sessions

    async def handle(self, turn_context: TurnContext) -> None:
        conversation_id = turn_context.activity.conversation.id
        text = (turn_context.activity.text or "").strip()

        if conversation_id not in _sessions:
            await _start_interview(turn_context, conversation_id)
            return

        session = _sessions[conversation_id]

        if session.awaiting_answer:
            session.awaiting_answer = False
            session.record_answer(text)

            # More questions for this measure?
            if session.current_question:
                await _ask_next_question(turn_context, session)
                return

            # Done with this measure — draft it
            await turn_context.send_activity(
                f"Got it. Drafting a definition for **{session.current_measure}**..."
            )
            try:
                draft = await _draft_metric(session.current_measure, session.transcript)
            except Exception as e:
                logger.error("Failed to draft metric: %s", e)
                await turn_context.send_activity(
                    f"Could not draft the definition (LLM error). Moving on.\n_{e}_"
                )
                draft = _minimal_stub(session.current_measure)

            session.all_drafts.append(draft)

            await turn_context.send_activity(
                f"Drafted definition for **{session.current_measure}**:\n\n"
                f"```\n{_preview_metric(draft)}\n```\n\n"
                f"Reply **yes** to keep this, or **edit: <your correction>** to adjust."
            )
            session.awaiting_answer = True  # now waiting for approval
            session.question_idx = -1  # sentinel: waiting for approval
            return

        # Approval / edit step
        if session.question_idx == -1:
            if text.lower().startswith("edit:"):
                correction = text[5:].strip()
                session.all_drafts[-1]["definition"] = correction
                await turn_context.send_activity("Updated. Moving on.")
            elif text.lower() in ("yes", "ok", "keep", "looks good", "good"):
                await turn_context.send_activity("Kept.")
            else:
                await turn_context.send_activity(
                    "Reply **yes** to keep, or **edit: <correction>** to adjust the definition."
                )
                session.awaiting_answer = True
                return

            session.advance_measure()

            if session.is_complete:
                await _finish_interview(turn_context, session)
                del _sessions[conversation_id]
                return

            await turn_context.send_activity(
                f"Next up: **{session.current_measure}** "
                f"({session.current_idx + 1}/{len(session.measures)})"
            )
            await _ask_next_question(turn_context, session)
            return

        # Shouldn't reach here — re-ask
        await _ask_next_question(turn_context, session)


async def _start_interview(turn_context: TurnContext, conversation_id: str) -> None:
    """Fetch undocumented measures and start the session."""
    try:
        issues = git_ops.get_open_issues(labels=["undocumented-measure"])
    except Exception as e:
        logger.error("Failed to fetch issues: %s", e)
        await turn_context.send_activity(
            "Could not fetch undocumented measures. Check CANON_GITHUB_TOKEN."
        )
        return

    measures = [i["title"].replace("[Drift] ", "").strip() for i in issues]

    if not measures:
        await turn_context.send_activity(
            "No undocumented measures found. Your domain is fully documented!"
        )
        return

    branch = f"canon/interview/{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M')}"
    try:
        git_ops.create_branch(branch)
    except Exception as e:
        logger.error("Could not create branch: %s", e)
        await turn_context.send_activity(f"Could not create interview branch: {e}")
        return

    session = InterviewSession(measures=measures, branch=branch)
    _sessions[conversation_id] = session

    await turn_context.send_activity(
        f"Starting a Canon definition interview for **{len(measures)} measure(s)**:\n"
        + "\n".join(f"- {m}" for m in measures)
        + f"\n\nI'll ask up to {len(_INTERVIEW_QUESTIONS)} questions per measure. "
        f"Answers go into a draft PR on branch `{branch}`.\n\n"
        f"Reply **skip** at any point to skip a question, **stop** to end early."
    )
    await _ask_next_question(turn_context, session)


async def _ask_next_question(turn_context: TurnContext, session: InterviewSession) -> None:
    q = session.current_question
    if not q:
        return
    measure = session.current_measure
    num = session.question_idx + 1
    total = len(_INTERVIEW_QUESTIONS)
    await turn_context.send_activity(
        f"**{measure}** — Question {num}/{total}:\n\n_{q}_"
    )
    session.awaiting_answer = True


async def _draft_metric(measure_name: str, transcript: list[dict]) -> dict:
    """Use Claude to synthesise a metrics.yaml entry from the Q&A transcript."""
    if not _ANTHROPIC_API_KEY:
        return _minimal_stub(measure_name)

    client = anthropic.Anthropic(api_key=_ANTHROPIC_API_KEY)

    qa_text = "\n".join(f"Q: {qa['q']}\nA: {qa['a']}" for qa in transcript)

    prompt = textwrap.dedent(f"""
        You are drafting a Canon metric definition from a Q&A interview.
        The metric name is: {measure_name}
        
        Interview transcript:
        {qa_text}
        
        Produce a Python dict representing ONE metrics.yaml entry in this exact structure:
        {{
            "name": "<name>",
            "domain": "retail",
            "owner": "Retail Analytics Team",
            "last_reviewed": "{datetime.now(timezone.utc).date().isoformat()}",
            "status": "active",
            "aliases": ["<alias1>", ...],
            "definition": "<clear 1-3 sentence business definition>",
            "governed_sources": {{
                "primary": {{
                    "platform": "fabric",
                    "type": "semantic_model",
                    "workspace": "Fabric Demos: Retail Planning",
                    "model": "RetailSemanticModel",
                    "measure": "<measure name in model>"
                }}
            }},
            "routing": "<when and how to query>",
            "depends_on": [],
            "sensitivity": "internal"
        }}
        
        Return ONLY valid JSON (no prose, no markdown, no code fences).
    """).strip()

    response = client.messages.create(
        model=_MODEL,
        max_tokens=800,
        temperature=0.0,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = "\n".join(raw.split("\n")[1:])
    if raw.endswith("```"):
        raw = "\n".join(raw.split("\n")[:-1])
    return json.loads(raw.strip())


def _minimal_stub(measure_name: str) -> dict:
    return {
        "name": measure_name,
        "domain": "retail",
        "owner": "Retail Analytics Team",
        "last_reviewed": datetime.now(timezone.utc).date().isoformat(),
        "status": "active",
        "aliases": [],
        "definition": "# TODO: add definition",
        "governed_sources": {
            "primary": {
                "platform": "fabric",
                "type": "semantic_model",
                "workspace": "Fabric Demos: Retail Planning",
                "model": "RetailSemanticModel",
                "measure": measure_name,
            }
        },
        "routing": "# TODO: add routing instructions",
        "depends_on": [],
        "sensitivity": "internal",
    }


def _preview_metric(entry: dict) -> str:
    lines = [
        f"name: {entry.get('name')}",
        f"definition: {str(entry.get('definition', ''))[:200]}",
        f"routing: {str(entry.get('routing', ''))[:200]}",
        f"aliases: {entry.get('aliases', [])}",
    ]
    return "\n".join(lines)


async def _finish_interview(turn_context: TurnContext, session: InterviewSession) -> None:
    """Commit all drafted definitions to the branch and open a PR."""
    await turn_context.send_activity(
        "All measures interviewed. Committing definitions and opening a PR..."
    )

    try:
        # Load existing metrics.yaml from branch
        try:
            existing_file = git_ops.get_file("domains/retail/metrics.yaml", ref=session.branch)
            existing_content = existing_file.content
            existing_sha = existing_file.sha
        except Exception:
            # Fall back to main
            existing_file = git_ops.get_file("domains/retail/metrics.yaml")
            existing_content = existing_file.content
            existing_sha = None

        # Append new entries
        data = yaml.safe_load(existing_content) or {}
        existing_names = {m["name"] for m in data.get("metrics", [])}

        added = []
        for draft in session.all_drafts:
            if draft["name"] not in existing_names:
                data.setdefault("metrics", []).append(draft)
                added.append(draft["name"])

        if not added:
            await turn_context.send_activity("All measures were already documented. No changes needed.")
            return

        new_content = yaml.dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
        # Preserve header comment
        new_content = "# yaml-language-server: $schema=../../schemas/metrics.schema.json\n" + new_content

        git_ops.commit_file(
            path="domains/retail/metrics.yaml",
            content=new_content,
            message=f"canon(retail): interview-drafted definitions for {', '.join(added)}",
            branch=session.branch,
            sha=existing_sha,
        )

        pr_body = (
            "## Canon Interview Drafts\n\n"
            "Definitions drafted via Teams bot interview with Data Owner.\n\n"
            "**Measures added:**\n"
            + "\n".join(f"- {n}" for n in added)
            + "\n\n**Review notes:**\n"
            "- Verify definitions match business intent\n"
            "- Add SQL usage patterns if known\n"
            "- Run `canon validate` before merging\n"
        )

        pr = git_ops.open_pr(
            title=f"canon(retail): interview-drafted definitions for {len(added)} measure(s)",
            body=pr_body,
            head_branch=session.branch,
            base_branch="main",
            labels=["canon"],
        )

        await turn_context.send_activity(
            f"Done! PR #{pr['number']} opened: {pr['html_url']}\n\n"
            f"Added definitions for: {', '.join(added)}\n\n"
            "Review, adjust, and merge when ready."
        )

    except Exception as e:
        logger.error("Failed to commit interview results: %s", e)
        await turn_context.send_activity(
            f"Interview complete but failed to create PR: {e}\n"
            "Definitions are in memory — please open a PR manually."
        )
