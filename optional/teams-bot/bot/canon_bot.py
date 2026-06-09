"""
Canon bot activity handler.

Intents (detected from message text):
  - "interview" / "document" / "define"  → interview mode (multi-turn definition drafting)
  - "review" / "drift" / "scan"          → drift review conversation
  - "digest" / "weekly" / "summary"      → weekly digest
  - anything else                        → ad-hoc definition update
"""

from __future__ import annotations

import logging
import os
import re

from botbuilder.core import ActivityHandler, TurnContext
from botbuilder.schema import Activity, ActivityTypes

from bot.handlers.drift_review import DriftReviewHandler
from bot.handlers.digest import send_digest
from bot.handlers.interview import InterviewHandler

logger = logging.getLogger(__name__)

_REVIEW_PATTERN = re.compile(r"\b(review|drift|scan|findings)\b", re.I)
_DIGEST_PATTERN = re.compile(r"\b(digest|weekly|summary)\b", re.I)
_INTERVIEW_PATTERN = re.compile(r"\b(interview|document\s+measures?|define\s+measures?|bootstrap)\b", re.I)


class CanonBot(ActivityHandler):
    def __init__(self) -> None:
        self._drift_handler = DriftReviewHandler()
        self._interview_handler = InterviewHandler()

    async def on_message_activity(self, turn_context: TurnContext) -> None:
        text = (turn_context.activity.text or "").strip()
        conversation_id = turn_context.activity.conversation.id

        logger.info("Received message from %s: %s", conversation_id, text[:100])

        if _DIGEST_PATTERN.search(text):
            await send_digest(turn_context)
        elif await self._interview_handler.is_active(conversation_id):
            await self._interview_handler.handle(turn_context)
        elif _INTERVIEW_PATTERN.search(text):
            await self._interview_handler.handle(turn_context)
        elif _REVIEW_PATTERN.search(text) or await self._drift_handler.is_active(conversation_id):
            await self._drift_handler.handle(turn_context)
        else:
            await self._handle_ad_hoc(turn_context, text)

    async def on_members_added_activity(self, members_added, turn_context: TurnContext) -> None:
        for member in members_added:
            if member.id != turn_context.activity.recipient.id:
                await turn_context.send_activity(
                    "Hi! I'm the Canon bot.\n\n"
                    "I can help you:\n"
                    "- **interview** — walk through undocumented measures and draft definitions\n"
                    "- **review** — walk through this week's drift findings\n"
                    "- **update a definition** — just tell me what changed\n"
                    "- **digest** — show the weekly summary\n\n"
                    "What would you like to do?"
                )

    async def _handle_ad_hoc(self, turn_context: TurnContext, text: str) -> None:
        """Handle ad-hoc definition update requests."""
        from bot.handlers.ad_hoc_update import AdHocUpdateHandler
        handler = AdHocUpdateHandler()
        await handler.handle(turn_context, text)
