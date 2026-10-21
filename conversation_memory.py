"""Bounded conversation memory for long calls: recent turns verbatim, older turns summarised.

The AI always receives at most HISTORY_TOKEN_BUDGET tokens of recent messages plus a short
running summary, so a 15-minute call costs about the same per turn as a 2-minute one while
still remembering details the caller gave at the start (name, company, CRM, budget, ...).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def _int_env(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def message_char_limit() -> int:
    return _int_env("BRAIN_HISTORY_MESSAGE_CHARS", 1200, 200)


def history_token_budget() -> int:
    return _int_env("BRAIN_HISTORY_TOKEN_BUDGET", 1500, 300)


def estimate_tokens(text: str) -> int:
    # About four characters per token for English text; close enough for budgeting.
    return max(1, len(text) // 4)


def clip_message(text: str) -> str:
    text = str(text or "").strip()
    limit = message_char_limit()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " …"


def voice_history_token_budget() -> int:
    """Phone calls keep the whole conversation (about 15-20 minutes of talk) so the agent never forgets a name,
    number or email the caller already gave and asks for it again (VOICE_HISTORY_TOKEN_BUDGET)."""
    return _int_env("VOICE_HISTORY_TOKEN_BUDGET", 5000, 500)


def bounded_history(history: Optional[List[Dict[str, str]]], budget: Optional[int] = None) -> List[Dict[str, str]]:
    """Clip each message, then keep the newest messages that fit the token budget."""
    budget = budget or history_token_budget()
    kept: List[Dict[str, str]] = []
    used = 0
    for item in reversed(history or []):
        content = clip_message(item.get("content", ""))
        cost = estimate_tokens(content)
        if kept and used + cost > budget:
            break
        kept.append({"role": item.get("role", "user"), "content": content})
        used += cost
    kept.reverse()
    return kept


def summary_char_limit() -> int:
    return _int_env("BRAIN_SUMMARY_CHARS", 700, 200)


SUMMARY_PROMPT = (
    "You keep the memory notes for an AI agent on a live customer conversation. Merge the existing notes "
    "with the new messages into updated notes. Record what the CUSTOMER said or decided: name, company, "
    "industry, location, tools or CRM, requirements, numbers, budget, dates, preferences, objections, "
    "agreed next steps, and questions still open. Mention the agent only for commitments it made, in a few "
    "words. Do not restate the agent's explanations. Replace outdated facts instead of repeating them. "
    "Write compact plain text, at most 70 words. Return only the notes."
)


def _clip_summary(text: str) -> str:
    """Hard ceiling so the notes stay small however long the call runs."""
    limit = summary_char_limit()
    if len(text) <= limit:
        return text
    clipped = text[:limit]
    cut = max(clipped.rfind(". "), clipped.rfind("; "))
    return clipped[:cut + 1] if cut > limit // 2 else clipped.rsplit(" ", 1)[0]


def summarize(existing_summary: str, messages: List[Dict[str, str]]) -> str:
    """Blocking LLM call that folds older messages into the running summary."""
    from ai_services import AIServices

    transcript = "\n".join(f"{item['role']}: {item['content']}" for item in messages)
    services = AIServices()
    if not services.is_llm_configured():
        return existing_summary
    reply = services.chat_completion(
        messages=[
            {"role": "system", "content": SUMMARY_PROMPT},
            {"role": "user", "content": f"Existing notes:\n{existing_summary or '(none yet)'}\n\nNew messages:\n{transcript}"},
        ],
        max_tokens=300,
        temperature=0.2,
    )
    return _clip_summary((reply or "").strip()) or existing_summary


class ConversationMemory:
    """Per-conversation memory. Call add() after each turn; compaction runs in the background."""

    def __init__(self, keep_recent_messages: int = 6, max_messages: int = 12, token_budget: Optional[int] = None) -> None:
        self.summary = ""
        self.messages: List[Dict[str, str]] = []
        self.keep_recent = keep_recent_messages
        self.max_messages = max_messages
        self.token_budget = token_budget  # None: the shared chat budget (BRAIN_HISTORY_TOKEN_BUDGET)
        self._compacting: Optional[asyncio.Task[None]] = None

    def history(self) -> List[Dict[str, str]]:
        return bounded_history(self.messages, self.token_budget)

    def add_assistant(self, text: str) -> None:
        """Record something the agent said unprompted (e.g. the greeting) so replies to it have context."""
        self.messages.append({"role": "assistant", "content": clip_message(text)})

    def add(self, user_text: str, assistant_text: str) -> None:
        self.messages.extend([
            {"role": "user", "content": clip_message(user_text)},
            {"role": "assistant", "content": clip_message(assistant_text)},
        ])
        if self._needs_compaction() and not (self._compacting and not self._compacting.done()):
            self._compacting = asyncio.create_task(self._compact())

    def _needs_compaction(self) -> bool:
        used = sum(estimate_tokens(item["content"]) for item in self.messages)
        if len(self.messages) <= self.keep_recent:
            return False
        return len(self.messages) > self.max_messages or used > (self.token_budget or history_token_budget()) * 0.8

    async def _compact(self) -> None:
        older = self.messages[:-self.keep_recent]
        if not older:
            return
        try:
            self.summary = await asyncio.to_thread(summarize, self.summary, older)
            # New turns may have arrived while summarising; drop only what was folded in.
            del self.messages[:len(older)]
            logger.info("conversation memory compacted %d messages; summary ~%d tokens", len(older), estimate_tokens(self.summary))
        except Exception as error:
            # Keep the messages; bounded_history() still caps what the model receives.
            logger.warning("conversation summary failed, keeping recent messages only: %s", error)

    async def close(self) -> None:
        if self._compacting and not self._compacting.done():
            self._compacting.cancel()
