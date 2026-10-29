"""Retell Custom LLM bridge backed by the existing OpenRouter configuration.

Retell owns telephony, speech recognition, voice synthesis, and turn taking.
This router owns the reasoning layer and returns short, customer-ready responses
from the configured OpenRouter model (gpt-oss-20b by default).
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import memory_care
import os
import re
from datetime import datetime, timezone
from timeutil import utc_iso
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from time import perf_counter
from typing import Any, Dict, List, Optional

from voice_keyterms import business_name
from voice_turns import prepare_turn_meaning, needs_facts
from voice_turns import asks_to_repeat, pause_request, pause_reply, status_after_seconds, status_phrase, is_acknowledgement, contact_email_followup_note, contact_email_from_messages
from voice_wait import with_wait_bridge, wait_message
from voice_turns import wait_bridge_phrase, late_filler, late_filler_seconds
from voice_turns import caller_country_line, caller_region, describe_caller_id, name_note, NumberSpeaker, known_numbers, reply_known_numbers, caller_number_digits, choose_voice_model, contact_check, last_agent_text, place_note, careful_voice_model, earlier_caller_texts, bridges_enabled, bridge_phrase, bridge_note, bridge_delay_seconds, ALL_BRIDGES, FAST_TURN_NOTE, LOOKUP_BRIDGES, bridge_model_seconds, clean_lookup_bridge, fast_voice_model, lookup_bridge_messages, call_is_over, yes_to_offer_note, search_text, bridge_said, spoken_bridge

from fastapi import APIRouter, Depends, Request, Response, WebSocket, WebSocketDisconnect

from auth_service import require_user
from logging_setup import bind_call

from ai_services import AIServices, is_reasoning_model, warm_model_connection
from brain_service import agent_config, brain_service
from conversation_memory import bounded_history, voice_history_token_budget
import call_forwarding
import live_transcript
from routers.calls_api import log_call
from transfer_service import transfer_service
from voice_provider_settings import provider_enabled
from voice_languages import language_for_phone, text_language
from meaning import is_foreign as meaning_foreign

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/retell", tags=["retell"])


def _configure_retell_logger() -> logging.Logger:
    """Write a durable, per-integration trace without relying on Uvicorn stdout."""
    logger = logging.getLogger("retell.integration")
    if logger.handlers:
        return logger
    log_dir = Path(__file__).resolve().parents[1] / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_dir / "retell-debug.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    logger.propagate = False
    return logger


retell_logger = _configure_retell_logger()


def _trace(message: str, *, error: bool = False) -> None:
    """Mirror useful call diagnostics to the console and logs/retell-debug.log."""
    (retell_logger.error if error else retell_logger.info)(message)
    logger.info(message)


def _enabled() -> bool:
    return provider_enabled("retell")


# Repeated as the last instruction before every voice reply: smaller, faster models (e.g. gpt-oss-20b) follow the
# long system prompt's conversation rules much more reliably when the key ones come right before they answer.
VOICE_TURN_CHECKLIST = (
    "Before you reply, check: (1) Answer what the caller just said. If they ask you to repeat, explain again, or say they "
    "did not understand, re-explain your previous answer in simpler words; do not change the subject. (2) Two or three short "
    "spoken sentences, under 50 words; summarise in your own words, never read the knowledge out line by line. "
    "(3) Ask for the caller's name, phone number or email ONLY after they have asked for a callback, meeting, quote or a person, "
    "and then one detail per reply. Otherwise do not ask for their details at all. (4) If they ask whether something is booked "
    "or confirmed, start with 'Not yet'. (5) Never ask for anything they already gave in this conversation. "
    "(6) State only facts from the business knowledge. (7) If you repeat a phone number or email the caller gave, copy it "
    "exactly as it was confirmed earlier in the conversation, digit by digit; if unsure, say 'the number you gave' instead. "
    "(8) You already greeted the caller: never start a reply with Hi, Hello, Hi there, Hey or Welcome."
)


def configure_voice_services(services: AIServices, model: str) -> AIServices:
    """Voice model client: the main model, a fallback model used when the main one fails or stalls before saying
    anything (VOICE_FALLBACK_MODEL, default gpt-4.1-mini), and how long a stall may last (VOICE_MODEL_STALL_SECONDS)."""
    services.config.OPENROUTER_MODEL = model.strip()
    services.fallback_model = os.getenv("VOICE_FALLBACK_MODEL", "openai/gpt-4.1-mini").strip()
    try:
        services.stream_timeout = max(2.0, float(os.getenv("VOICE_MODEL_STALL_SECONDS", "6")))
    except ValueError:
        services.stream_timeout = 6.0
    return services


def _max_response_tokens(model: str = "") -> int:
    """Token limit for one reply request. Reasoning models (gpt-oss) count their hidden thinking against the same
    limit, so the spoken budget (RETELL_MAX_RESPONSE_TOKENS) gets a separate thinking allowance on top;
    otherwise replies are cut off mid-sentence. Ordinary models (gpt-4.1-mini) need no allowance.
    Reply length itself is kept short by the prompt."""
    model = model or os.getenv("RETELL_OPENROUTER_MODEL", "openai/gpt-oss-20b")
    try:
        spoken = max(40, min(240, int(os.getenv("RETELL_MAX_RESPONSE_TOKENS", "120"))))
    except ValueError:
        spoken = 120
    try:
        thinking = max(0, min(2000, int(os.getenv("RETELL_REASONING_TOKEN_ALLOWANCE", "600"))))
    except ValueError:
        thinking = 600
    reasoning_on = os.getenv("RETELL_OPENROUTER_REASONING_EFFORT", "low").strip().lower() in {"low", "medium", "high"} and is_reasoning_model(model)
    return spoken + (thinking if reasoning_on else 0)


def _serialize_messages(messages: List[Dict[str, str]], message_type: str) -> List[Dict[str, str]]:
    return [
        {"content": item["content"], "timestamp": item["timestamp"], "type": message_type}
        for item in messages
        if item.get("content")
    ]


def speech_end_clock(transcript: Any, call_started_clock: float, now: float, max_lag: float = 1.5) -> float:
    """When the caller stopped speaking, on our perf_counter clock: the end of their last word (Retell gives word times
    in seconds from the start of the call) placed on the clock started when the call connected. Retell sends the turn
    ~0.6 s after that, so a wait timed from our receipt left the caller in silence longer than intended. The lag is
    capped (max_lag) and never negative, so a clock mismatch can only shift a wait slightly, never skip it."""
    for item in reversed(transcript if isinstance(transcript, list) else []):
        if isinstance(item, dict) and item.get("role") == "user":
            ends = [word.get("end") for word in item.get("words") or [] if isinstance(word, dict)]
            ends = [float(end) for end in ends if isinstance(end, (int, float))]
            if not ends:
                break
            lag = (now - call_started_clock) - max(ends)
            return now - min(max_lag, max(0.0, lag))
    return now


def _transcript_as_messages(transcript: Any) -> List[Dict[str, str]]:
    messages: List[Dict[str, str]] = []
    for item in transcript or []:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        role = str(item.get("role") or "").lower()
        if content and role in {"user", "agent", "assistant"}:
            messages.append({"role": "assistant" if role in {"agent", "assistant"} else "user", "content": content})
    # The whole call, not just the last few lines: a name, number or email given earlier must stay in view,
    # or the agent asks for it again (it used to keep only the last 10 lines).
    return bounded_history(messages, voice_history_token_budget())


def _last_user_message(messages: List[Dict[str, str]]) -> str:
    for item in reversed(messages):
        if item["role"] == "user":
            return item["content"]
    return ""


def _log_content_enabled() -> bool:
    return os.getenv("RETELL_LOG_CONTENT", "false").strip().lower() in {"1", "true", "yes", "on"}


def _parse_retell_signature(signature: str) -> Optional[tuple[str, str]]:
    match = re.fullmatch(r"v=(\d+),d=([0-9a-fA-F]+)", signature.strip())
    return (match.group(1), match.group(2)) if match else None


def _valid_retell_webhook(raw_body: bytes, signature: str) -> bool:
    """Verify Retell's raw-body HMAC and reject stale replay attempts."""
    api_key = os.getenv("RETELL_API_KEY", "").strip()
    parsed = _parse_retell_signature(signature)
    if not api_key or not parsed:
        return False
    timestamp, supplied_digest = parsed
    try:
        if abs(int(datetime.now().timestamp() * 1000) - int(timestamp)) > 5 * 60 * 1000:
            return False
    except ValueError:
        return False
    expected_digest = hmac.new(
        api_key.encode("utf-8"),
        raw_body + timestamp.encode("utf-8"),
        "sha256",
    ).hexdigest()
    return hmac.compare_digest(expected_digest, supplied_digest)


def _timestamp_from_ms(value: Any) -> Optional[str]:
    try:
        return datetime.fromtimestamp(float(value) / 1000, timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _duration_seconds(call: Dict[str, Any]) -> float:
    try:
        if call.get("duration_ms") is not None:
            return max(float(call["duration_ms"]) / 1000, 0)
        return max((float(call.get("end_timestamp", 0)) - float(call.get("start_timestamp", 0))) / 1000, 0)
    except (TypeError, ValueError):
        return 0


def _retell_messages(call: Dict[str, Any]) -> tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    callers: List[Dict[str, str]] = []
    agents: List[Dict[str, str]] = []
    started_at = call.get("start_timestamp")
    for item in call.get("transcript_object") or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").lower()
        content = str(item.get("content") or "").strip()
        if not content or role not in {"user", "agent", "assistant"}:
            continue
        timestamp = _timestamp_from_ms(float(started_at) + float(item.get("time_sec", 0)) * 1000) if started_at else None
        message = {"content": content, "timestamp": timestamp or utc_iso(), "type": "user" if role == "user" else "bot"}
        (callers if role == "user" else agents).append(message)
    return callers, agents


def _retell_human_request(call: Dict[str, Any], event: str) -> bool:
    analysis = call.get("call_analysis") or {}
    custom = analysis.get("custom_analysis_data") or {}
    requested_keys = {"human_agent_requested", "human_follow_up_required", "callback_requested", "needs_human"}
    return event.startswith("transfer_") or any(bool(custom.get(key)) for key in requested_keys)


def _upsert_retell_call(call: Dict[str, Any], event: str) -> None:
    call_id = str(call.get("call_id") or "").strip()
    if not call_id:
        raise ValueError("Webhook payload did not include call.call_id")

    analysis = call.get("call_analysis") or {}
    callers, agents = _retell_messages(call)
    direction = str(call.get("direction") or "inbound").lower()
    phone_number = str(
        (call.get("from_number") if direction == "inbound" else call.get("to_number"))
        or "Web caller"
    )
    handoff = transfer_service.get_call(call_id) or {}
    human_requested = bool(handoff.get("transfer_requested")) or _retell_human_request(call, event)
    transfer_status = handoff.get("transfer_status") or ("requested" if human_requested else None)
    call_successful = analysis.get("call_successful")
    outcome_status = "transfer_requested" if human_requested and transfer_status not in {"completed", "accepted"} else "completed"
    if str(call.get("call_status", "")).lower() == "error":
        outcome_status = "failed"
    elif str(call.get("call_status", "")).lower() in {"registered", "ongoing"}:
        outcome_status = live_transcript.LIVE_STATUS  # transcript_updated arrives while the call is still running

    latency = call.get("latency") or {}
    e2e = latency.get("e2e") or {}
    data = {
        "direction": direction,
        "status": outcome_status,
        "duration": _duration_seconds(call),
        "transcription": callers,
        "ai_responses": agents,
        "summary": analysis.get("call_summary") or "Retell call completed.",
        "summary_language": "en",
        "sentiment": analysis.get("user_sentiment", "neutral"),
        "transfer_requested": human_requested,
        "transfer_status": transfer_status,
        "transfer_destination": handoff.get("transfer_destination") or call.get("transfer_destination") or ("Callback queue" if human_requested else None),
        "transfer_error": handoff.get("transfer_error"),
        "transfer_succeeded": transfer_status in {"connected", "completed", "accepted"},
        "accepted_by": handoff.get("accepted_by"),
        "handled_by": handoff.get("handled_by"),
        "call_session_id": call_id,
        "provider": "retell",
        "provider_call_id": call_id,
        "channel_type": call.get("call_type", "phone_call"),
        "agent_id": call.get("agent_id"),
        "agent_name": call.get("agent_name"),
        "agent_version": call.get("agent_version"),
        "agent_tag": call.get("agent_tag"),
        "to_number": call.get("to_number"),
        "provider_call_status": call.get("call_status"),
        "disconnection_reason": call.get("disconnection_reason"),
        "recording_url": call.get("recording_url") or call.get("scrubbed_recording_url"),
        "recording_multi_channel_url": call.get("recording_multi_channel_url") or call.get("scrubbed_recording_multi_channel_url"),
        "public_log_url": call.get("public_log_url"),
        "call_cost": call.get("call_cost"),
        "latency": latency,
        "end_to_end_latency_ms": e2e.get("p50") or e2e.get("average") or e2e.get("mean"),
        "provider_metadata": call.get("metadata") or {},
        "dynamic_variables": call.get("collected_dynamic_variables") or call.get("retell_llm_dynamic_variables") or {},
        "custom_analysis_data": analysis.get("custom_analysis_data") or {},
        "call_successful": call_successful,
        "in_voicemail": analysis.get("in_voicemail"),
        "human_callback_requested": human_requested,
        "analysis_status": "complete" if event == "call_analyzed" else "pending",
        "transcript_with_tool_calls": call.get("transcript_with_tool_calls") or [],
        "messages": live_transcript.ordered_from_retell(
            call.get("transcript_object") or [],
            datetime.fromtimestamp(float(call["start_timestamp"]) / 1000, timezone.utc) if call.get("start_timestamp") else None,
        ),
        "provider_started_at": _timestamp_from_ms(call.get("start_timestamp")),
        "provider_ended_at": _timestamp_from_ms(call.get("end_timestamp")),
    }
    result = log_call(phone_number, call_data=data)
    if not result.get("success"):
        raise RuntimeError(result.get("error", "Failed to store Retell call"))
    _trace(
        f"retell_webhook_stored event={event} call={call_id} provider_status={call.get('call_status')} "
        f"transcript_lines={len(callers) + len(agents)} human_callback={human_requested} "
        f"analysis_status={data['analysis_status']}"
    )


def _embed_budget_ms() -> int:
    """Longest a turn waits for the knowledge-search embedding before using keyword search (RETELL_EMBED_BUDGET_MS)."""
    try:
        return max(100, int(os.getenv("RETELL_EMBED_BUDGET_MS", "600")))
    except ValueError:
        return 600


_PROMPT_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="retell-prompt")


def _knowledge_limit() -> int:
    try:
        return max(2, int(os.getenv("VOICE_KNOWLEDGE_LIMIT", "6")))
    except ValueError:
        return 6


def conversation_knowledge(caller_text: str, history: Optional[List[Dict[str, str]]] = None) -> List[Dict[str, Any]]:
    """Knowledge for this turn, found from the conversation and not only the last words: a follow-up such as
    "what about the white ones?" or "how much is it?" says nothing about the topic on its own. Facts matching the
    latest words come first, then facts matching the recent conversation (the caller's last two turns plus this one)."""
    if not caller_text:
        return []
    budget = _embed_budget_ms()
    earlier = [str(item.get("content") or "").strip() for item in (history or []) if item.get("role") == "user"]
    earlier = [text for text in earlier if text and text != caller_text.strip()][-2:]
    if not earlier:
        return brain_service.retrieve_knowledge(caller_text, embed_budget_ms=budget)
    # Both searches at once (the local embedding model releases the GIL): ~100 ms instead of ~180 ms one after the other.
    recent_search = _PROMPT_POOL.submit(brain_service.retrieve_knowledge, " ".join([*earlier, caller_text]), 4, budget)
    current = brain_service.retrieve_knowledge(caller_text, embed_budget_ms=budget)
    recent = recent_search.result(timeout=5)
    merged: List[Dict[str, Any]] = []
    seen: set = set()
    for item in [*current, *recent]:
        text = str(item.get("text") or "")
        if text and text not in seen:
            seen.add(text)
            merged.append(item)
    return merged[:_knowledge_limit()]


def _system_prompt(caller_text: str, caller_number: str, summary: str = "", history: Optional[List[Dict[str, str]]] = None,
                   with_knowledge: bool = True, language: Optional[str] = None) -> str:
    """with_knowledge=False: a turn without a question (a name, a number, "okay") gets no business facts at all,
    so a small fast model has nothing to misquote and a shorter prompt to read."""
    try:
        phone = caller_number if caller_number != "unknown" else None
        # The caller lookup and the knowledge search are both network reads: run them side by side.
        caller_lookup = _PROMPT_POOL.submit(brain_service.customer_context, phone) if phone else None
        # Live store data (products, stock, orders, policies) when the store is connected and the caller asks for it.
        store_lookup = _PROMPT_POOL.submit(_store_facts, caller_text, phone, history) if with_knowledge else None
        knowledge = conversation_knowledge(caller_text, history) if with_knowledge else NO_FACTS_NEEDED
        if store_lookup is not None:
            try:
                store = store_lookup.result(timeout=STORE_LOOKUP_SECONDS)
            except Exception as error:
                logger.warning("voice: store lookup skipped (%s)", str(error) or "too slow")
                store = ""
            if store:
                knowledge = [{"text": store}, *knowledge]
        caller = caller_lookup.result(timeout=5) if caller_lookup is not None else ""
        if not language and phone:
            from voice_languages import language_for_phone
            language = language_for_phone(phone)
        business = agent_config.get_system_prompt().strip()
        channel = brain_service.channel_config("voice")
        if language:
            from voice_languages import language_label, normalize_language
            channel = {**channel, "language": language_label(normalize_language(language))}
    except Exception:
        knowledge, business, channel, caller = [], "", {}, ""
    custom = agent_config.get_voice_prompt()
    handoff_rules = (
        "\n\n## Call-system handoff rules\n"
        "Live transfers are handled by the call system. When the caller accepts your offer to connect them now, "
        "the system connects them; do not ask a second now-versus-later question. Never require their name, phone "
        "number or email before a live transfer. For callbacks, collect only a missing reachable phone number; "
        "a name and email are optional and must not delay the request. After the system confirms a callback "
        "request, do not offer the same transfer choice again when the caller says okay or thanks."
    )
    if phone and re.fullmatch(r"\+?[0-9 ()-]{8,}", phone):
        handoff_rules += " A reachable caller-ID phone number is already available. Use it for the callback unless the caller asks for a different number; do not ask them to provide or reconfirm it."
    else:
        handoff_rules += " Caller ID is unavailable. Ask for a reachable number only if the caller requests a callback and has not already supplied one."
    if custom:
        return _business_voice_prompt(custom, caller, phone, knowledge, summary) + handoff_rules
    return _voice_prompt(business, channel, caller, phone, knowledge, summary) + handoff_rules


STORE_LOOKUP_SECONDS = float(os.getenv("VOICE_STORE_LOOKUP_SECONDS", "2.5"))  # never hold a reply longer for Shopify


def _no_lookup_turn(caller_text: str) -> bool:
    """"Sorry?", "Yes.", "Thanks, bye": nothing to look up in the store (the reply re-explains or carries on from the
    call so far). Judged by meaning; a store lookup otherwise costs up to a second or two on these turns."""
    from turn_meaning import CALLER_TURN
    from voice_turns import kind_of_turn_text, store_question
    if store_question(caller_text):
        return False  # "Okay. ... the handle and the basin ... is it possible?" is a product question, not an "okay"
    if pause_request(caller_text) or is_acknowledgement(caller_text):
        return True
    try:
        generic = kind_of_turn_text(caller_text)
        kind = CALLER_TURN.best(generic)
        if kind in {"clarify", "acknowledge", "closing"}:
            return True
        import store_intent
        return kind == "question" and store_intent.wanted(generic) == []
    except Exception:
        return False


def _store_facts(caller_text: str, phone: Optional[str], history: Optional[List[Dict[str, str]]]) -> str:
    """What the connected Shopify store says about this question, as one fact block ("" when not connected or not a
    store question). The earlier turns go along so an order number said before an email still verifies the caller."""
    if _no_lookup_turn(caller_text):
        return ""
    earlier = " ".join(str(item.get("content") or "") for item in (history or [])[-8:] if item.get("role") == "user")
    context, tool = brain_service._shopify_context(caller_text, phone, f"{earlier} {caller_text}".strip())
    if not context:
        return ""
    logger.info("voice: store lookup %s for %r", tool, caller_text[:80])
    return f"Live store data from Shopify, current right now (use it as fact):\n{context}"


KNOWLEDGE_PLACEHOLDER = "{{business_knowledge}}"
NO_FACTS_NEEDED = [{"text": "(not needed for this reply: the caller asked no question. Do not state any product, price, "
                            "or policy details in this reply.)"}]


def _business_voice_prompt(template: str, caller: str, phone: Optional[str], knowledge: List[Dict[str, Any]], summary: str = "") -> str:
    """The voice prompt written by the business (Settings → voice_prompt), used as the whole instruction set.
    Only data is added: the knowledge found for this question (at {{business_knowledge}}, or at the end) and the
    live call details the prompt refers to (caller ID, CRM record, earlier part of the call)."""
    facts = "\n".join(f"- {item['text']}" for item in knowledge) or "- (nothing relevant was found for this question)"
    details = [
        f"Caller ID: {describe_caller_id(phone) if phone else 'not available (web call or hidden number)'}",
        caller_country_line(phone or ""),
        f"Existing CRM context for this caller (use these saved details; do not ask for them again):\n{caller}" if caller else "This caller is not in the CRM yet.",
        f"Earlier in this call (facts the caller already told you):\n{summary}" if summary else "",
    ]
    live = "\n\n## Live call details (filled in automatically)\n" + "\n".join(item for item in details if item)
    if _knowledge_at_end():
        # The model host reuses work for a prompt start it has seen before (prompt caching): with the facts for this
        # question last, the whole written prompt is identical on every turn and starts answering sooner.
        prompt = template.replace(KNOWLEDGE_PLACEHOLDER, "(see the Business knowledge section at the end of these instructions)")
        return f"{prompt}{live}\n\n## Business knowledge (the facts for this question)\n{facts}"
    prompt = template.replace(KNOWLEDGE_PLACEHOLDER, facts) if KNOWLEDGE_PLACEHOLDER in template else f"{template}\n\n## Business knowledge\n{facts}"
    return prompt + live


def _knowledge_at_end() -> bool:
    """VOICE_KNOWLEDGE_AT_END: put the per-question facts at the end of the prompt instead of at {{business_knowledge}}."""
    return os.getenv("VOICE_KNOWLEDGE_AT_END", "false").strip().lower() in {"1", "true", "yes", "on"}


def voice_turn_reminder(language: str = "", follow_caller: bool = True) -> str:
    """Last instruction sent after the conversation on every turn. With a business-written voice prompt this is that
    prompt's own final-rules section (a heading containing "Final"); otherwise the built-in checklist. Both end with
    the language rule (voice_languages.language_rule): a written prompt that says "use English" otherwise kept
    replies in English while the caller spoke Urdu (call of 2026-10-28)."""
    from voice_languages import language_rule
    rule = language_rule(language, follow_caller)
    custom = agent_config.get_voice_prompt()
    if not custom:
        return f"{VOICE_TURN_CHECKLIST}\n\n{rule}".strip()
    final = _final_rules_section(custom)
    # Small fast models (gpt-oss-20b) drift from long prompts; these few concrete rules, tested on real calls,
    # keep them on track. VOICE_TURN_SAFETY=false sends only the business prompt's own final rules.
    if os.getenv("VOICE_TURN_SAFETY", "true").strip().lower() not in {"0", "false", "no", "off"}:
        final = f"{final}\n\n{VOICE_TURN_SAFETY_RULES}".strip()
    # Keeps replies sounding like one flowing conversation rather than a fresh start each turn.
    # VOICE_TURN_CONVERSATION=false leaves them out.
    if os.getenv("VOICE_TURN_CONVERSATION", "true").strip().lower() not in {"0", "false", "no", "off"}:
        final = f"{final}\n\n{VOICE_CONVERSATION_RULES}".strip()
    return f"{final}\n\n{rule}".strip()


VOICE_CONVERSATION_RULES = (
    "Conversation: (1) Speak as the business: say 'we' and 'our'. Do not start a reply with the business name and do not "
    "repeat the business name in your replies; the caller knows who they called. (2) Continue the conversation: build on what "
    "you and the caller already said, refer back to it ('those ones', 'as I mentioned'), and never re-introduce the business "
    "or repeat an earlier answer word for word. (3) The caller's words come from speech recognition and can be misheard: read "
    "them in the context of the conversation and the business knowledge (a word that sounds like a product or the business "
    "name means that), and answer what they meant without pointing out the mistake. (4) If the caller's words stop mid-sentence, "
    "answer what they clearly mean from the conversation so far instead of asking them to clarify. (5) Describe products only "
    "with the types, sizes, finishes and features written in the business knowledge. When the knowledge only names a product "
    "category, say we stock it and point the caller to the website or catalogue for the styles; never add details of your own, "
    "and keep the knowledge's meaning exact (if it says 'many products', do not say 'most' or 'all'). (6) Never say we do NOT have, sell or "
    "offer something unless the knowledge says so: say it is not something you have details on, mention the closest thing the "
    "knowledge does cover, and offer to have the team confirm. (7) When the caller names a city, suburb or area (for delivery, "
    "installation or stores), never answer yes, no or 'should be fine' for that place unless the knowledge names it: say what the "
    "knowledge says in general (for example 'nationwide' or 'most of Australia') and that the team will confirm for their area. "
    "(8) The business's own phone number, email addresses, address and website written in the knowledge are public contact "
    "details: when the caller asks for them, give them exactly as written (the privacy rules are about the caller's details). "
    "(9) Sound like a friendly person on the phone, not a brochure: usually one or two short sentences (under about 30 words). "
    "React to what the caller just said, give the gist in plain everyday words instead of reading policies or lists out (name "
    "two or three things at most and say there are more; leave out fine print the caller didn't ask about), and use contractions. Then, unless the caller is wrapping up or giving their details, keep "
    "the conversation going with one short, natural question about what they need (for example what they are working on or "
    "looking for), so it feels like a conversation, not questions and answers. Vary how you start; do not begin every reply the same way. "
    "(10) If the caller says 'hello?' or 'are you there?' in the middle of the call, they did not hear you: say you're here "
    "and briefly repeat what you last asked or said. (11) If the caller is vague or unsure what to ask ('so what?', 'yes' "
    "with nothing to act on, 'what can you do?'), do not just say you're here to help: say in one sentence what the business "
    "can help with (from the knowledge) and ask one concrete question about what they need. (12) You already greeted the "
    "caller at the start of the call: never greet again. Do not start a reply with Hi, Hello, Hi there, Hey or Welcome, even "
    "when the caller opens with 'hi'; go straight to answering what they asked."
)


VOICE_TURN_SAFETY_RULES = (
    "Also: (a) If the caller asks you to repeat, explain again, tell or 'text' them again, or says they did not understand, "
    "re-explain your previous answer in simpler words; do not ask for their details. (b) Ask for name, phone or email only "
    "after the caller asked for a callback, quote, meeting or a person, one detail per reply. (c) If asked whether something is "
    "booked or confirmed, start with 'Not yet'. (d) When repeating a phone number or email, copy it exactly as the caller "
    "confirmed it, digit by digit, or say 'the number you gave'. (e) If the live call details show an Existing CRM lead "
    "or saved customer name, use it and never ask for the caller's name just to identify them again. (f) Under 50 words; "
    "summarise, never read the knowledge out."
)


def _final_rules_section(custom: str) -> str:
    """The prompt's own final-rules section: from a heading containing "Final" to the next heading at its level."""
    lines = custom.splitlines()
    for start, line in enumerate(lines):
        heading = re.match(r"^(#+)\s+(.*)$", line.strip())
        if heading and "final" in heading.group(2).lower():
            level = len(heading.group(1))
            end = next((index for index in range(start + 1, len(lines)) if (other := re.match(r"^(#+)\s", lines[index].strip())) and len(other.group(1)) <= level), len(lines))
            return "\n".join(lines[start:end]).strip()
    return ""


def _voice_prompt(business: str, channel: Dict[str, Any], caller: str, phone: Optional[str], knowledge: List[Dict[str, Any]], summary: str = "") -> str:
    """The whole instruction set for one spoken reply. Written for the phone only (the chat channels use
    brain_service.system_context): one voice, one set of rules, no conflicting chat formatting advice."""
    instructions = str(channel.get("instructions") or "").strip()
    facts = "\n".join(f"- {item['text']}" for item in knowledge) or "- (nothing relevant was found for this question)"
    sections = [
        business or "You are the business's AI phone assistant.",
        f"Channel instructions: {instructions}" if instructions and instructions != business else "",
        f"Personality: {channel.get('personality') or 'Professional and helpful'}. Tone: {channel.get('tone') or 'Warm and concise'}. "
        f"Language: {channel.get('language') or 'English'} (reply in the caller's language if they switch).",
        "You are answering a live inbound phone call. Everything you write is spoken aloud by a voice, word for word.",
        "# How to speak\n"
        "- Answer the caller's actual question in your first sentence, then add one or two sentences of useful detail "
        "(normally two to three natural sentences, about 25 to 50 words). Broad questions such as what you sell or what you offer deserve "
        "the fuller answer; a simple yes or no question can be shorter.\n"
        "- Every detail you add must be written in the business knowledge. Never add offers, discounts, free delivery, thresholds, "
        "times, or promises that are not written there; if there is nothing more to add, a shorter answer is better than an invented one.\n"
        "- Start straight with the answer. Never open with filler such as Sure, Great, Absolutely, Certainly, Of course, Got it, Perfect, or I'd love to.\n"
        "- Ask at most one question per reply, and only when you need it to help them. Never ask for something they already told you.\n"
        "- Plain spoken words only: no lists, bullet points, symbols, emojis, links, or formatting.\n"
        "- You are the assistant, not the owner: refer to the owner by name in the third person, never 'I build' or 'my rate'.\n"
        "- If the caller only says hello, yes, okay, or are you there, reply in a few words and invite them to go on.",
        "# Facts and honesty\n"
        "- State only facts written in the business knowledge section at the end. If the answer is not there, say you don't have that detail "
        "and the team can confirm it on a callback. Never guess hours, availability, timelines, guarantees, or prices.\n"
        "- Prices: when the caller asks about price, cost, or rates and the knowledge states a rate (for example an hourly rate), "
        "say that rate with its number in your first sentence, then say final project pricing is confirmed after scoping, and offer a callback. "
        "Stating a rate written in the knowledge is not guessing; never say you don't have pricing when a rate is written there.\n"
        "- Services: name only the two or three that fit what the caller described, not the full list.",
        "# Meetings, calls and callbacks (you cannot book anything)\n"
        "- You have no calendar and cannot book, schedule, or confirm meetings, and you cannot send emails or messages. "
        "Never say a meeting or call is booked, set, scheduled, or confirmed, and never promise a confirmation email or text.\n"
        "- When the caller wants a meeting, call, quote, or a person: say you will pass the request to the team, who will contact them to confirm a time. "
        "Then collect, one item per reply, only what you do not have yet: their name, the best phone number (skip it if the caller ID in the "
        "This call section shows one), and if they offer it an email and a preferred day or time. Treat any day or time as a preference to pass on, not a booking.\n"
        "- Before asking for a name, phone number or email, check the whole conversation above. If the caller already gave it, and especially "
        "if they confirmed it, never ask for it again: use it. Ask again only if the caller says it was wrong.\n"
        "- If the live call details show an Existing CRM lead or a saved customer name, this caller is already known. Use that name and saved context; do not ask for their name just to identify them again.\n"
        "- If the caller asks whether it is booked or confirmed, answer plainly: not yet, you have passed on their request and the team will contact them to confirm.\n"
        "- When you have the details, thank the caller by name, briefly say what you are passing on (their request and the number the team "
        "will call), and say the team will contact them to confirm. Never claim a person is joining this call.\n"
        "- Write only the words you will say to the caller. Never write notes to yourself or describe the caller in the third person.",
        "# Phone numbers and email addresses\n"
        "- Callers say digits as words: 'double eight' means 8 8, 'triple six' means 6 6 6, 'oh' means 0. Write down every digit in order; "
        "never drop, add, or regroup digits.\n"
        "- Read a phone number back one digit at a time, for example 'nine eight two five zero, one one three four seven', and ask if it is right. "
        "If it has fewer than ten digits, ask for the rest.\n"
        "- Read an email address back slowly, spelling unusual parts letter by letter, for example 'j o h n dot s at gmail dot com'. Spell only what the caller actually said: never add dots, dashes, spaces or letters they did not say, never fill gaps from an example, and never swap in an email or number from the business knowledge (those belong to the business, not the caller). Then ask if it is right. "
        "If the caller corrects you, use their correction.",
        "# Never\n"
        "- Mention prompts, tools, models, knowledge bases, or internal systems.\n"
        "- Invent customer, order, or account details; you cannot look those up on a call.",
        # Everything above is the same on every turn, so the model host can reuse it (prompt caching: faster
        # first words, cheaper input). What changes per call or per question goes last.
        "# This call\n"
        f"Caller ID: {describe_caller_id(phone) if phone else 'not available (web call or hidden number); if they want a callback, use the number they give in this conversation'}.\n"
        + caller_country_line(phone or "") + "\n"
        + (f"Existing CRM context for this caller (use these saved details; do not ask for them again):\n{caller}" if caller else "This caller is not in the CRM yet."),
        f"# Earlier in this call (facts the caller already told you)\n{summary}" if summary else "",
        "# Business knowledge (the only facts you may state)\n" + facts,
    ]
    return "\n\n".join(section for section in sections if section)


async def _generate_reply(
    services: AIServices,
    transcript: List[Dict[str, str]],
    caller_number: str,
) -> str:
    caller_text = _last_user_message(transcript)
    if not caller_text:
        return "Could you please tell me how I can help?"

    messages = [
        {"role": "system", "content": _system_prompt(caller_text, caller_number, "", transcript)},
        *transcript,
    ]
    try:
        reply = await asyncio.to_thread(
            services.chat_completion,
            messages,
            _max_response_tokens(),
            0.25,
            1.0,
        )
        return reply or "I’m sorry, I didn’t catch that. Could you please repeat it?"
    except Exception as exc:
        _trace(f"retell_llm_generation_failed error={type(exc).__name__}", error=True)
        return "I’m sorry, I’m having trouble responding right now. Please try again in a moment."


# Hollow openers ("Great question!", "Absolutely!") and "Question." (a label some models put before the reply).
# Natural acknowledgements ("Yeah, we do", "Sure, ...", "Got it") stay: they make the call sound like a conversation.
# Only when the word stands on its own ("Great!", "Absolutely, ..."): "Great to hear you're planning ..." keeps its "Great"
# (stripping it once left "To hear you're planning ...").
_FILLER_OPENER = re.compile(r"^\s*(?:(?:(?:that's a |that is a )?(?:good|great) question)[\s,.!—–-]*|(?:great|absolutely|certainly|of course|perfect)[,.!—–-]+\s*|question[.!:]\s*)", re.I)
# Short acknowledgements; dropped from the start of a reply when a filler like "Yeah." was already spoken before it.
_ECHO_WORDS = {"okay", "ok", "sure", "yeah", "yes", "yep", "right", "alright", "got", "it", "no", "problem", "so", "great",
               "of", "course", "absolutely", "certainly", "i", "can", "do", "that", "for", "you"}
ACKNOWLEDGEMENT_OPENER = re.compile(r"^\s*(?:yeah|yep|sure|okay|ok|alright|right|got it|no problem|mm-?hmm)[,.!—–-]+\s*", re.I)
_SENTENCE_END = re.compile(r"[.!](?=\s|[A-Z])|\?")
_GLUED_SENTENCE = re.compile(r"[.!](?=[A-Z][a-z])")


class SpokenReplyGuard:
    """Keeps a streamed reply speakable, whatever the model writes:
    - drops a filler opener ("Sure,", "Great!") and capitalises what follows;
    - ends the reply at its first question, so the agent never answers its own question for the caller;
    - ends it at the first sentence break after max_words, so a reply never rambles on."""

    def __init__(self, max_words: int = 45, strip_acknowledgement: bool = False, echo_of: str = "") -> None:
        self.max_words = max_words
        # A bridge phrase ("Sure, let me check.") was already said: drop the model's own "Sure," / "Yeah," too, and a
        # first sentence that only repeats the bridge ("I can sort that out." after "Yeah, I can sort that out.").
        self.strip_acknowledgement = strip_acknowledgement
        self.echo_words = set(re.findall(r"[a-z']+", echo_of.lower())) | _ECHO_WORDS if echo_of else set()
        self.raw = ""
        self.skip = 0          # characters of filler removed from the start
        self.started = False   # opener checked
        self.sent = 0          # characters of the cleaned text already sent
        self.done = False

    def _opener(self) -> Optional[re.Match]:
        match = _FILLER_OPENER.match(self.raw)
        if not match and self.strip_acknowledgement:
            match = ACKNOWLEDGEMENT_OPENER.match(self.raw)
        return match

    def _cleaned(self) -> str:
        text = self.raw[self.skip:]
        return text[:1].upper() + text[1:] if text else text

    def _drop_echo(self, final: bool = False) -> bool:
        """Drop leading sentences that only repeat the bridge. False while the first sentence is still incomplete."""
        while self.echo_words:
            end = _SENTENCE_END.search(self.raw)
            if not end:
                if final or len(self.raw) >= 80:
                    break
                return False
            words = re.findall(r"[a-z']+", self.raw[:end.end()].lower())
            if not words or len(words) > 8 or not all(word in self.echo_words for word in words):
                break
            self.raw = self.raw[end.end():].lstrip()
        self.echo_words = set()
        return True

    def feed(self, chunk: str) -> str:
        if self.done:
            return ""
        self.raw += chunk
        if not self.started and not self._drop_echo():
            return ""
        if not self.started:
            # Wait for the first few words before deciding whether they are filler.
            if len(self.raw) < 14 and not _SENTENCE_END.search(self.raw):
                return ""
            match = self._opener()
            if match and match.end() >= len(self.raw):
                return ""  # only the filler so far: wait for the words after it
            if match:
                self.skip = match.end()
            self.started = True
        text = self._cleaned()
        cut = len(text)
        question = text.find("?", self.sent)
        glued = _GLUED_SENTENCE.search(text, max(0, self.sent - 1))
        if glued and (question == -1 or glued.start() < question):
            # "...number.User didn't..." – a sentence glued on with no space is the model's private notes or a
            # second reply leaking into the spoken text; end the reply before it.
            cut, self.done = glued.start() + 1, True
        elif question != -1:
            cut, self.done = question + 1, True
        elif len(text.split()) >= self.max_words:
            words_seen = 0
            for match in _SENTENCE_END.finditer(text):
                words_seen = len(text[:match.end()].split())
                if words_seen >= self.max_words:
                    cut, self.done = match.end(), True
                    break
        if not self.done:
            # Hold back a trailing "." or ".U" until the next characters show whether a glued sentence follows.
            pending = re.search(r"[.!][A-Z]?$", text)
            if pending:
                cut = min(cut, max(self.sent, pending.start()))
        out = text[self.sent:cut]
        self.sent = cut
        return out

    def flush(self) -> str:
        if self.done:
            return ""
        if not self.started:
            self._drop_echo(final=True)
            match = self._opener()
            if match and match.end() < len(self.raw):
                self.skip = match.end()
        text = self._cleaned()
        out = text[self.sent:]
        self.sent = len(text)
        return out


def sounds_unfinished(text: str) -> bool:
    """Whether the caller's meaning still needs a continuation."""
    from turn_meaning import TURN_COMPLETION
    ranked = TURN_COMPLETION.rank(text)
    return bool(ranked and ranked[0][0] == "unfinished" and ranked[0][1] >= TURN_COMPLETION.min_score
                and ranked[0][1] - ranked[1][1] >= 0.04)


def unfinished_wait_seconds() -> float:
    """VOICE_UNFINISHED_WAIT_SECONDS: how long to wait for the rest of a sentence the caller broke off (0 = never)."""
    try:
        return max(0.0, float(os.getenv("VOICE_UNFINISHED_WAIT_SECONDS", "0.7")))
    except ValueError:
        return 0.7


class _SemanticPrefetch:
    """Prepare stable caller words early, with one worker and no partial-text queue.

    The pause before a round adapts to how long a round takes on this server. On a fast CPU (a round ~50 ms) it
    starts after STABLE_SECONDS of unchanged words. On Render's small CPU a round took ~1.5 s, so rounds started at
    every short pause were stale before they finished: the CPU stayed busy with old partial sentences all through the
    caller's speech, and the real turn check then waited behind them (1.6-1.8 s per turn on the call of 2026-10-28
    08:22, growing as the call went on). Now a round starts only after the caller has been quiet about as long as a
    round takes (at most MAX_PAUSE_SECONDS)."""

    STABLE_SECONDS = 0.25
    MAX_PAUSE_SECONDS = 2.0

    def __init__(self, prepare=None):
        self.prepare = prepare or prepare_turn_meaning
        self.pending = None
        self.last = None
        self.updated_at = 0.0
        self.task = None
        self.round_seconds = 0.0  # how long the last round took

    def pause_seconds(self) -> float:
        return max(self.STABLE_SECONDS, min(self.MAX_PAUSE_SECONDS, self.round_seconds))

    def schedule(self, caller_text: str, agent_text: str, earlier: str = "") -> None:
        payload = (caller_text, agent_text, earlier) if earlier else (caller_text, agent_text)
        # Words in Urdu, Hindi or Arabic are read in English through a translation (meaning.english_text): not for
        # every partial transcript, only for the finished turn.
        if not caller_text or payload == self.last or meaning_foreign(caller_text):
            return
        self.last = self.pending = payload
        self.updated_at = perf_counter()
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run())

    def discard_pending(self) -> None:
        self.pending = None

    async def _run(self) -> None:
        while self.pending is not None:
            remaining = self.pause_seconds() - (perf_counter() - self.updated_at)
            if remaining > 0:
                await asyncio.sleep(remaining)
                continue
            payload, self.pending = self.pending, None
            started = perf_counter()
            try:
                await asyncio.to_thread(self.prepare, *payload)
            except Exception as error:
                logger.debug("semantic prefetch failed: %s", type(error).__name__)
            self.round_seconds = perf_counter() - started

    async def close(self) -> None:
        self.discard_pending()
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


def voice_temperature() -> float:
    """VOICE_TEMPERATURE: how freely the model words a spoken reply (0 = most literal, sticks closest to the knowledge)."""
    try:
        return max(0.0, min(1.0, float(os.getenv("VOICE_TEMPERATURE", "0.25"))))
    except ValueError:
        return 0.25


def _max_spoken_words(fast: bool = False) -> int:
    """Longest reply in words (RETELL_MAX_SPOKEN_WORDS); fast-model turns stay short and natural."""
    name, default = ("VOICE_FAST_MAX_WORDS", 25) if fast else ("RETELL_MAX_SPOKEN_WORDS", 60)
    try:
        return max(15, int(os.getenv(name, str(default))))
    except ValueError:
        return default


async def _stream_reply(
    services: AIServices,
    transcript: List[Dict[str, str]],
    caller_number: str,
    call_id: str,
    response_id: int,
    reminder: bool = False,
    started_at: Optional[float] = None,
    timings: Optional[Dict[str, Any]] = None,
    inline_bridge: bool = True,
    facts_prompt: Optional["asyncio.Future[str]"] = None,
):
    """Yield model deltas while recording the latency of each voice turn.
    facts_prompt: the prompt with business facts, already being built (started with the turn's handoff check)."""
    request_started = started_at if started_at is not None else perf_counter()
    caller_text = _last_user_message(transcript)
    if not caller_text:
        yield "Could you please tell me how I can help?"
        return
    if facts_prompt is None and not reminder:
        facts_prompt = start_facts_prompt(caller_text, transcript, caller_number)
    try:
        async for chunk in _stream_reply_with_facts(services, transcript, caller_number, call_id, response_id, reminder,
                                                    request_started, timings, inline_bridge, caller_text, facts_prompt):
            yield chunk
    finally:
        if facts_prompt is not None and not facts_prompt.done():
            facts_prompt.cancel()


def start_facts_prompt(caller_text: str, transcript: List[Dict[str, str]], caller_number: str) -> "asyncio.Future[str]":
    """Start building the prompt with business facts now. Most turns need it (questions, and every main-model turn);
    built only after the turn checks it added 0.7-2.2 s on production calls. A fast turn that needs no facts drops it."""
    return asyncio.ensure_future(asyncio.to_thread(_system_prompt, search_text(caller_text, transcript), caller_number, "", transcript, True))


async def _stream_reply_with_facts(services, transcript, caller_number, call_id, response_id, reminder, request_started,
                                   timings, inline_bridge, caller_text, facts_prompt):
    if _log_content_enabled():
        _trace(f"retell_llm_request call={call_id} response_id={response_id} caller={caller_text!r}")

    prompt_started = perf_counter()
    question = False
    earlier_text = " ".join(" ".join(earlier_caller_texts(transcript, caller_text)[-8:]).split()[-25:])
    if reminder:
        turn_model, (details_note, details_problem), places = None, ("", False), ""
    else:
        # Semantic decisions can use network embeddings in production. Keep
        # them off the receive loop and overlap independent work.
        def check_places():
            return place_note(caller_text, brain_service.knowledge_text(), last_agent_text(transcript))

        # The prompt with facts is already being built (facts_prompt); these checks only decide whether it is used.
        turn_model, (details_note, details_problem), places, question = await asyncio.gather(
            asyncio.to_thread(choose_voice_model, caller_text, last_agent_text(transcript), earlier_text),
            asyncio.to_thread(contact_check, caller_text, last_agent_text(transcript),
                              earlier_caller_texts(transcript, caller_text), caller_region(caller_number)),
            asyncio.to_thread(check_places),
            asyncio.to_thread(needs_facts, caller_text),
        )
    if not reminder and await asyncio.to_thread(sounds_unfinished, caller_text):
        # Preprocessing already gave the caller time to continue. Only wait for
        # the remainder, instead of adding another full pause after that work.
        remaining = unfinished_wait_seconds() - (perf_counter() - request_started)
        if remaining > 0:
            await asyncio.sleep(remaining)
    if details_problem:
        turn_model = None  # a wrong number or email needs the careful model
    bridge = ""
    if inline_bridge and not reminder and bridges_enabled() and turn_model is None:
        # A short acknowledgement while the model works; the prompt keeps building while this phrase is selected.
        said = [str(item.get("content") or "") for item in transcript if item.get("role") == "assistant"][-3:]
        recent = [phrase for text in said if (phrase := bridge_said(text))]
        language = text_language(caller_text) or "en"
        bridge = await asyncio.to_thread(bridge_phrase, caller_text, recent, " ".join(earlier_caller_texts(transcript, caller_text)[-8:]))
        if bridge and language != "en":
            # In the caller's language; the lookup line is not model-written (its checks read English).
            if bridge in LOOKUP_BRIDGES:
                await asyncio.sleep(bridge_delay_seconds())
            bridge = spoken_bridge(bridge, language)
        elif bridge:
            if bridge in LOOKUP_BRIDGES:
                bridge = await _lookup_bridge(services, caller_text, last_agent_text(transcript), bridge, call_id, response_id)
            else:
                await asyncio.sleep(bridge_delay_seconds())  # caller carries on: Retell cancels this reply before it is said
        if bridge:
            yield bridge + " "
    if places:
        turn_model = careful_voice_model(services.fallback_model) or None  # the main model tends to say "yes, covered"
    if facts_prompt is not None and (question or turn_model is None or places):
        # Questions (also on the fast model) and every main-model turn get the relevant business facts.
        system_prompt = await facts_prompt
    else:
        if facts_prompt is not None:
            facts_prompt.cancel()
        system_prompt = await asyncio.to_thread(_system_prompt, search_text(caller_text, transcript), caller_number, "", transcript, turn_model is None or bool(places))
    retrieval_ms = (perf_counter() - prompt_started) * 1000
    if timings is not None:
        timings.update(preparation_ms=round(retrieval_ms), model=turn_model or services.config.OPENROUTER_MODEL,
                       requested_model=turn_model or services.config.OPENROUTER_MODEL)
    reminder_text = voice_turn_reminder()
    messages = [{"role": "system", "content": system_prompt}, *transcript] + ([{"role": "system", "content": reminder_text}] if reminder_text else [])
    if details_note:
        messages.append({"role": "system", "content": details_note})
    email_note = contact_email_followup_note(transcript, caller_text)
    if email_note:
        messages.append({"role": "system", "content": email_note})
    if places:
        messages.append({"role": "system", "content": places})
    offered = "" if reminder else yes_to_offer_note(caller_text, last_agent_text(transcript))
    if offered:
        messages.append({"role": "system", "content": offered})
    misheard = "" if reminder else name_note(caller_text, business_name())
    if misheard:
        messages.append({"role": "system", "content": misheard})
    if bridge:
        messages.append({"role": "system", "content": bridge_note(bridge)})
    elif turn_model is not None and not places:
        messages.append({"role": "system", "content": FAST_TURN_NOTE})
    if reminder:
        # Retell asks for a nudge after the caller has been silent; without this the model may repeat its last answer.
        messages.append({"role": "system", "content": "The caller has gone quiet. Check in with one short, friendly sentence, "
                         "for example asking if they are still there or if there is anything else you can help with. Do not repeat your previous answer."})
    if not inline_bridge and bridges_enabled():
        messages.append({"role": "system", "content": "The call system handles any short wait acknowledgement. Start directly with the actual answer; do not add or repeat a wait/checking phrase."})
    _trace(
        f"retell_llm_started call={call_id} response_id={response_id} "
        f"model={turn_model or services.config.OPENROUTER_MODEL} retrieval_ms={retrieval_ms:.0f}"
    )

    iterator = None
    model_started = perf_counter()
    try:
        iterator = services.stream_chat_completion(
            messages,
            _max_response_tokens(turn_model or ""),
            voice_temperature(),
            1.0,
            model=turn_model,
        )
        first_token_logged = False
        guard = SpokenReplyGuard(_max_spoken_words(fast=turn_model is not None and not places), strip_acknowledgement=bool(bridge), echo_of=bridge)
        caller_digits = caller_number_digits(transcript)
        numbers = NumberSpeaker(caller_digits, reply_known_numbers(brain_service.knowledge_text(), system_prompt, caller_digits))
        while True:
            chunk = await asyncio.to_thread(next, iterator, None)
            if chunk is None:
                break
            spoken = numbers.feed(guard.feed(chunk))
            if spoken and not first_token_logged:
                first_token_logged = True
                if timings is not None:
                    timings.update(first_model_text_ms=round((perf_counter() - request_started) * 1000),
                                   model_wait_ms=round((perf_counter() - model_started) * 1000))
                _trace(
                    f"retell_llm_first_token call={call_id} response_id={response_id} "
                    f"elapsed_ms={(perf_counter() - request_started) * 1000:.0f} "
                    f"model_wait_ms={(perf_counter() - model_started) * 1000:.0f}"
                )
            if spoken:
                yield spoken
            if guard.done:
                _trace(f"retell_llm_reply_trimmed call={call_id} response_id={response_id} kept={guard.sent} chars")
                break
        rest = numbers.feed(guard.flush()) + numbers.flush()
        if rest:
            yield rest
        _trace(
            f"retell_llm_complete call={call_id} response_id={response_id} "
            f"elapsed_ms={(perf_counter() - request_started) * 1000:.0f}"
        )
    except Exception as exc:
        _trace(f"retell_llm_stream_failed call={call_id} response_id={response_id} error={type(exc).__name__}: {exc}", error=True)
        yield "I’m sorry, I’m having trouble responding right now. Please try again in a moment."
    finally:
        # Also when this reply was superseded and cancelled: stop the model request instead of letting it run on.
        close = getattr(iterator, "close", None)
        if close:
            try:
                close()
            except Exception:
                pass


async def _lookup_bridge(services: AIServices, caller_text: str, agent_text: str, fallback: str, call_id: str, response_id: int,
                         delay_seconds: Optional[float] = None) -> str:
    """What to say while the store lookup runs, phrased for this question by the small fast model ("Let me check the
    Lydia mixer for you."): no facts, no reasoning, just the line. Never said before bridge_delay_seconds (if the caller
    carries on, Retell cancels this reply first); slower than bridge_model_seconds or unsafe, the fixed phrase."""
    started = perf_counter()
    model = fast_voice_model()
    client = getattr(services, "openai_client", None)
    line = ""
    if model and client is not None:
        def ask() -> str:
            response = client.chat.completions.create(
                model=model, messages=lookup_bridge_messages(caller_text, agent_text), max_tokens=24, temperature=0.5,
                extra_body=services._openrouter_extra_body(model), timeout=bridge_model_seconds(),
            )
            return response.choices[0].message.content or ""
        try:
            line = clean_lookup_bridge(await asyncio.wait_for(asyncio.to_thread(ask), timeout=bridge_model_seconds()))
        except Exception as error:
            logger.info("voice: lookup bridge from %s skipped (%s)", model, type(error).__name__)
    waited = perf_counter() - started
    delay = bridge_delay_seconds() if delay_seconds is None else delay_seconds
    if waited < delay:
        await asyncio.sleep(delay - waited)
    _trace(f"retell_lookup_bridge call={call_id} response_id={response_id} source={'model' if line else 'fixed'} "
           f"ms={waited * 1000:.0f} said={(line or fallback)!r}")
    return line or fallback


@router.get("/config", dependencies=[Depends(require_user)])
async def retell_config_status() -> Dict[str, Any]:
    """Safe readiness data for the dashboard; secrets are never returned."""
    return {
        "custom_llm_enabled": _enabled(),
        "openrouter_configured": bool(os.getenv("OPENROUTER_API_KEY")),
        "model": os.getenv("RETELL_OPENROUTER_MODEL", "openai/gpt-oss-20b"),
        "websocket_path": "/api/retell/llm-websocket/{call_id}",
        "webhook_path": "/api/retell/webhook",
        "webhook_ready": bool(os.getenv("RETELL_API_KEY", "").strip()),
    }


# Retell writes the call summary a little after the call ends and sends it in its call_analyzed webhook. When that
# webhook does not reach us (it went to a server that was restarting, or was refused), the call kept our placeholder
# "Retell inbound AI call completed." So after every call we also ask Retell for it: at these waits (seconds,
# about four minutes in all), stopping as soon as the summary is stored by either path.
RETELL_OUTCOME_WAITS = (20, 40, 60, 120)
_OUTCOME_TASKS: set = set()  # keeps a reference to each running fetch (asyncio keeps only weak ones)


def _retell_analysis_stored(call_id: str) -> bool:
    from mongo_client import mongo_client
    return bool(mongo_client.is_connected() and mongo_client.calls.find_one({"call_session_id": call_id, "analysis_status": "complete"}, {"_id": 1}))


def _retell_call_record(call_id: str) -> Optional[Dict[str, Any]]:
    """Retell's own record of a call (the same object its webhooks carry); None when it cannot be read."""
    import httpx
    key = os.getenv("RETELL_API_KEY", "").strip()
    if not key or not call_id:
        return None
    try:
        response = httpx.get(f"https://api.retellai.com/v2/get-call/{call_id}", headers={"Authorization": f"Bearer {key}"}, timeout=15)
        return response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError) as error:
        _trace(f"retell_outcome_fetch_failed call={call_id} error={type(error).__name__}")
        return None


def _store_retell_outcome(call_id: str, final: bool = False) -> bool:
    """Store Retell's record of a finished call if it has its summary (or, when final, whatever it has). True when
    nothing more is to come."""
    if _retell_analysis_stored(call_id):
        return True
    call = _retell_call_record(call_id)
    if not call or str(call.get("call_status") or "").lower() in {"registered", "ongoing"}:
        return False
    if (call.get("call_analysis") or {}).get("call_summary"):
        _upsert_retell_call(call, "call_analyzed")
        _trace(f"retell_outcome_fetched call={call_id} summary=yes")
        return True
    if final:
        _upsert_retell_call(call, "call_ended")
        _trace(f"retell_outcome_fetched call={call_id} summary=no (Retell has none)")
    return False


async def _fetch_retell_outcome(call_id: str) -> None:
    for index, wait in enumerate(RETELL_OUTCOME_WAITS):
        await asyncio.sleep(wait)
        try:
            if await asyncio.to_thread(_store_retell_outcome, call_id, index == len(RETELL_OUTCOME_WAITS) - 1):
                return
        except Exception as error:
            _trace(f"retell_outcome_store_failed call={call_id} error={type(error).__name__}: {error}", error=True)
            return


def fetch_retell_outcome_later(call_id: str) -> None:
    """After a Retell call ends: fetch its summary in the background unless the webhook stores it first."""
    if not call_id or not os.getenv("RETELL_API_KEY", "").strip():
        return
    task = asyncio.get_running_loop().create_task(_fetch_retell_outcome(call_id))
    _OUTCOME_TASKS.add(task)
    task.add_done_callback(_OUTCOME_TASKS.discard)


def recover_retell_outcomes(hours: int = 24) -> int:
    """At startup: Retell calls of the last day still without Retell's summary (the server restarted before it was
    fetched, as during the call of 2026-10-28 10:05) get it now. Returns how many were completed."""
    from mongo_client import mongo_client
    from datetime import timedelta
    if not os.getenv("RETELL_API_KEY", "").strip() or not mongo_client.is_connected():
        return 0
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)
    rows = mongo_client.calls.find({"provider": "retell", "call_date": {"$gte": since}, "analysis_status": {"$ne": "complete"}},
                                   {"call_session_id": 1}).limit(50)
    done = 0
    for row in rows:
        try:
            done += bool(_store_retell_outcome(str(row.get("call_session_id") or ""), final=True))
        except Exception as error:
            _trace(f"retell_outcome_recovery_failed call={row.get('call_session_id')} error={type(error).__name__}", error=True)
    if done:
        _trace(f"retell_outcome_recovered calls={done}")
    return done


@router.post("/webhook", status_code=204)
async def retell_webhook(request: Request) -> Response:
    """Persist Retell lifecycle, transcript, and post-call analysis events."""
    raw_body = await request.body()
    if not os.getenv("RETELL_API_KEY", "").strip():
        _trace("retell_webhook_unavailable reason=RETELL_API_KEY_missing", error=True)
        return Response(status_code=503, content="RETELL_API_KEY is not configured")
    if not _valid_retell_webhook(raw_body, request.headers.get("x-retell-signature", "")):
        _trace("retell_webhook_rejected reason=invalid_signature", error=True)
        return Response(status_code=401, content="Invalid Retell webhook signature")

    try:
        payload = json.loads(raw_body)
        event = str(payload.get("event") or "")
        call = payload.get("call")
        if not isinstance(call, dict):
            raise ValueError("Webhook payload did not include a call object")
        _trace(f"retell_webhook_received event={event or 'missing'} call={call.get('call_id') or 'missing'}")

        if event == "call_started":
            transfer_service.register_call(
                str(call.get("call_id") or ""),
                str(call.get("from_number") or "Web caller"),
                direction=str(call.get("direction") or "inbound"),
            )
        elif event in {
            "call_ended",
            "call_analyzed",
            "transcript_updated",
            "transfer_started",
            "transfer_bridged",
            "transfer_cancelled",
            "transfer_ended",
        }:
            _upsert_retell_call(call, event)
        else:
            _trace(f"retell_webhook_ignored event={event or 'missing'}")
        return Response(status_code=204)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        _trace(f"retell_webhook_invalid_payload error={type(exc).__name__}: {exc}", error=True)
        return Response(status_code=400, content="Invalid Retell webhook payload")
    except Exception as exc:
        _trace(f"retell_webhook_processing_failed error={type(exc).__name__}: {exc}", error=True)
        return Response(status_code=500, content="Retell webhook processing failed")


# Long enough for Retell to say CONNECTING_LINE (about 4 s of speech) before the call leaves the AI.
FORWARD_AFTER_SECONDS = 5.0


async def _hand_to_team(call_id: str, call_sid: str, caller: str, requested_text: str = "", preferred_language: str = "") -> bool:
    await asyncio.sleep(FORWARD_AFTER_SECONDS)
    language = preferred_language or await asyncio.to_thread(language_for_phone, caller)
    if not await asyncio.to_thread(call_forwarding.transfer, call_sid, caller, call_id, requested_text, language):
        _trace(f"retell_forwarding_failed call={call_id}", error=True)
        transfer_service.request_transfer(call_id, "Caller asked for a person; the transfer could not start", "retell_callback", 0.95,
                                          phone_number=caller)
        return False
    else:
        _trace(f"retell_forwarded call={call_id} twilio={call_sid}")
        return True


@router.websocket("/llm-websocket/{call_id}")
async def retell_custom_llm_websocket(websocket: WebSocket, call_id: str) -> None:
    """Implement Retell's Custom LLM WebSocket protocol for one live call."""
    if not _enabled():
        await websocket.close(code=1008, reason="Retell custom LLM is disabled")
        return

    expected_secret = os.getenv("RETELL_CUSTOM_LLM_SECRET", "").strip()
    # Retell does not forward arbitrary auth headers for Custom LLM sockets.
    # Configure the secret in the Custom LLM URL instead:
    # wss://api.example.com/api/retell/llm-websocket?token=... .
    # Retell appends the call_id to the path before opening the socket.
    supplied_secret = websocket.query_params.get("token", "")
    if expected_secret and not hmac.compare_digest(supplied_secret, expected_secret):
        await websocket.close(code=1008, reason="Unauthorized")
        return

    await websocket.accept()
    bind_call(call_id)
    _trace(f"retell_ws_connected call={call_id}")
    started_at = datetime.now(timezone.utc)  # aware: transcript line times are derived from it
    started_clock = perf_counter()
    caller_number = "unknown"
    callback_requested = False
    handoff_pending = False
    user_messages: List[Dict[str, str]] = []
    agent_messages: List[Dict[str, str]] = []
    live_messages: List[Dict[str, str]] = []  # ordered transcript for the inbox, saved after every turn
    prefetched_text, prefetched_at = "", 0.0  # last caller words sent for an early knowledge-search embedding
    semantic_prefetch = _SemanticPrefetch()
    voice_turn_timings: List[Dict[str, Any]] = []
    live_saves: set[asyncio.Task[None]] = set()
    services = configure_voice_services(AIServices(), os.getenv("RETELL_OPENROUTER_MODEL", "openai/gpt-oss-20b"))

    if not services.is_llm_configured():
        await websocket.send_json(
            {
                "response_type": "response",
                "response_id": 0,
                "content": "I’m sorry, the AI service is not configured right now.",
                "content_complete": True,
                "end_call": True,
            }
        )
        await websocket.close(code=1011, reason="OpenRouter is not configured")
        return

    await websocket.send_json(
        {
            "response_type": "config",
            "config": {"auto_reconnect": True, "call_details": True},
        }
    )
    # The greeting set in Settings (agent config), unless RETELL_BEGIN_MESSAGE overrides it.
    begin_message = (os.getenv("RETELL_BEGIN_MESSAGE", "").strip() or agent_config.get_greeting_message() or "Hello, how can I help you today?").strip()
    await websocket.send_json(
        {
            "response_type": "response",
            "response_id": 0,
            "content": begin_message,
            "content_complete": True,
        }
    )
    if begin_message:
        agent_messages.append({"content": begin_message, "timestamp": utc_iso()})

    # Retell pings every two seconds while a response is being generated. Keep
    # the receive loop free so it can return those pings and accept newer turns.
    send_lock = asyncio.Lock()
    response_tasks: set[asyncio.Task[None]] = set()
    latest_response_id = -1

    async def handoff_to_team(call_sid: str, caller: str, requested_text: str) -> None:
        nonlocal handoff_pending
        try:
            if not await _hand_to_team(call_id, call_sid, caller, requested_text):
                handoff_pending = False
        except Exception:
            handoff_pending = False
            _trace(f"retell_forwarding_task_failed call={call_id}", error=True)

    async def send_event(payload: Dict[str, Any]) -> None:
        async with send_lock:
            await websocket.send_json(payload)

    def save_progress(transcript: Any, reply: str = "") -> None:
        """Rebuild the ordered transcript from Retell's (it is always complete and in order) and save it.
        Lines keep the time they were first seen; Retell's live transcript carries no timings."""
        nonlocal live_messages
        now = utc_iso()
        lines = live_transcript.ordered_from_retell(transcript)
        if reply:
            lines.append({"role": "agent", "text": reply, "at": ""})
        for index, line in enumerate(lines):
            previous = live_messages[index] if index < len(live_messages) else None
            line["at"] = previous["at"] if previous and previous["role"] == line["role"] else now
        if lines == live_messages:
            return
        live_messages = lines
        phone = caller_number if caller_number != "unknown" else "Web caller"
        task = asyncio.create_task(asyncio.to_thread(live_transcript.save_live, call_id, phone, "retell", list(lines)))
        live_saves.add(task)
        task.add_done_callback(live_saves.discard)

    async def handle_response(event: Dict[str, Any], response_id: int) -> None:
        nonlocal callback_requested, user_messages, handoff_pending
        turn_started = perf_counter()
        timing: Dict[str, Any] = {"response_id": response_id, "pipeline": "retell"}
        # Waits (the filler) are timed from when the caller stopped speaking, not from when Retell sent the turn.
        speech_ended = speech_end_clock(event.get("transcript"), started_clock, turn_started)
        timing["speech_end_lag_ms"] = round((turn_started - speech_ended) * 1000)
        if handoff_pending:
            await send_event({"response_type": "response", "response_id": response_id,
                              "content": "", "content_complete": True})
            return
        transcript = _transcript_as_messages(event.get("transcript"))
        caller_text = _last_user_message(transcript)
        now = utc_iso()

        if caller_text:
            user_messages = [
                {"content": item["content"], "timestamp": now}
                for item in transcript
                if item["role"] == "user"
            ]
            transfer_service.append_message(call_id, "user", caller_text)

        # Retell handles barge-in itself, so this is a finished turn: a "Sorry?" here asks to hear the reply again.
        if caller_text and await asyncio.to_thread(pause_request, caller_text) and not await asyncio.to_thread(asks_to_repeat, caller_text):
            reply = pause_reply(caller_text)
            await send_event({"response_type": "response", "response_id": response_id,
                              "content": reply, "content_complete": True})
            agent_messages.append({"content": reply, "timestamp": now})
            save_progress(event.get("transcript"), reply)
            return

        streamed = False
        bridge_prelude = ""
        recent = [phrase for item in transcript if item.get("role") == "assistant"
                  if (phrase := bridge_said(item.get("content", "")))][-3:]
        bridge_language = text_language(caller_text) or "en"  # bridges are said in the caller's language

        async def prepare_notice():
            if not caller_text or not bridges_enabled():
                return ""
            bridge_started = perf_counter()
            earlier = " ".join(" ".join(earlier_caller_texts(transcript, caller_text)[-8:]).split()[-25:])
            await asyncio.to_thread(prepare_turn_meaning, caller_text, last_agent_text(transcript), earlier)
            candidate = await asyncio.to_thread(wait_bridge_phrase, caller_text, recent,
                " ".join(earlier_caller_texts(transcript, caller_text)[-8:]))
            if candidate:
                timing.update(bridge_candidate=True, bridge_eligibility_ms=round((perf_counter() - bridge_started) * 1000))
                selected = await wait_message(services, candidate, caller_text, last_agent_text(transcript), recent, timing)
                timing["bridge_ready_ms"] = round((perf_counter() - bridge_started) * 1000)
                return spoken_bridge(selected, bridge_language)
            else:
                timing.update(bridge_candidate=False, bridge_eligibility_ms=round((perf_counter() - bridge_started) * 1000),
                              bridge_ready_ms=round((perf_counter() - bridge_started) * 1000),
                              bridge_selector="none")
            return candidate

        bridge_ready = asyncio.create_task(prepare_notice())
        asyncio.current_task().add_done_callback(lambda _: bridge_ready.cancel())
        # The prompt with business facts builds while the handoff check runs: on production it took 0.7-2.2 s.
        facts_prompt = (start_facts_prompt(caller_text, transcript, caller_number)
                        if caller_text and event.get("interaction_type") == "response_required" else None)
        if facts_prompt is not None:
            asyncio.current_task().add_done_callback(lambda _: facts_prompt.cancel())
        # The answer also starts now, alongside the handoff check (as the custom pipeline does): waiting for that check
        # first added 0.5-1 s per turn. A turn routed to a transfer, callback or goodbye drops it unheard.
        early_reply = early_first = None
        if facts_prompt is not None:
            early_reply = _stream_reply(services, transcript, caller_number, call_id, response_id, reminder=False,
                                        started_at=turn_started, timings=timing, inline_bridge=False, facts_prompt=facts_prompt)
            early_first = asyncio.ensure_future(anext(early_reply))
            asyncio.current_task().add_done_callback(lambda _: early_first.cancel())

        async def routing_notice():
            line = await asyncio.shield(bridge_ready)
            # A live lookup has not started until routing completes.
            return "I'm working through the details now." if line in LOOKUP_BRIDGES else line

        async def resolve():
            direct = await asyncio.to_thread(call_forwarding.contextual_handoff_intent, caller_text, last_agent_text(transcript), callback_requested)
            timing["handoff_source"] = "semantic_context" if direct is not None else "semantic"
            if direct in {"live", "callback", "callback_ack", "declined", "unclear"}:
                result = direct, False
            else:
                semantic_started = perf_counter()
                # The same texts the prefetch made while the caller spoke (with the earlier conversation), so they
                # are cache hits. Without `earlier` this built one more combined text that no decision used: an
                # extra embedding every turn, 0.3-0.5 s on Render's CPU.
                await asyncio.to_thread(prepare_turn_meaning, caller_text, last_agent_text(transcript),
                                        " ".join(" ".join(earlier_caller_texts(transcript, caller_text)[-8:]).split()[-25:]))
                timing["semantic_ms"] = round((perf_counter() - semantic_started) * 1000)
                result = await asyncio.gather(
                    asyncio.to_thread(call_forwarding.handoff_intent, caller_text, "", last_agent_text(transcript)) if direct is None else asyncio.sleep(0, result=direct),
                    asyncio.to_thread(call_is_over, caller_text, last_agent_text(transcript)),
                )
            yield tuple(result)

        if caller_text:
            async with contextlib.aclosing(with_wait_bridge(resolve(), routing_notice, "",
                    status_after_seconds() if bridges_enabled() else 0, speech_ended)) as routed:
                async for item in routed:
                    if isinstance(item, tuple):
                        transfer_intent, ending = item
                    else:
                        bridge_prelude = item
                        timing["bridge_ms"] = round((perf_counter() - turn_started) * 1000)
                        await send_event({"response_type": "response", "response_id": response_id,
                                          "content": item, "content_complete": False})
        else:
            transfer_intent, ending = "", False
        _trace(f"retell_turn_intent call={call_id} response_id={response_id} elapsed_ms={(perf_counter() - turn_started) * 1000:.0f}")
        specific_agent = await asyncio.to_thread(call_forwarding.requested_agent, caller_text) if caller_text and transfer_intent in {"callback", "unclear"} else None
        if event.get("interaction_type") == "response_required" and caller_text and ending:
            # We said goodbye and the caller only said bye back: hang up instead of another goodbye per fragment.
            _trace(f"retell_call_over call={call_id} response_id={response_id} caller={caller_text!r}")
            await send_event({"response_type": "response", "response_id": response_id, "content": "",
                              "content_complete": True, "end_call": True})
            return
        if event.get("interaction_type") == "reminder_required" and not caller_text:
            reply = "Is there anything else I can help you with?"
        elif event.get("interaction_type") == "response_required" and transfer_intent in {"callback_ack", "declined"}:
            reply = "You're welcome." if transfer_intent == "callback_ack" else "Of course."
            timing["action"] = transfer_intent
        elif event.get("interaction_type") == "response_required" and caller_text and call_forwarding.forwarding_on()                 and call_forwarding.twilio_call_for_retell(call_id) and transfer_intent == "live":
            # Call forwarding is on and the caller asked for a person: say so, then ring the team once it has been said.
            reply = call_forwarding.CONNECTING_LINE
            twilio_call = call_forwarding.twilio_call_for_retell(call_id) or {}
            handoff_pending = True
            callback_requested = False
            asyncio.create_task(handoff_to_team(twilio_call.get("call_sid", ""), twilio_call.get("caller") or caller_number, caller_text))
        elif (event.get("interaction_type") == "response_required" and callback_requested
              and (transfer_intent in {"live", "callback"} or specific_agent)):
            reply = "Your callback request is already with the team."
            timing["action"] = "callback_ack"
        elif event.get("interaction_type") == "response_required" and caller_text and (
            transfer_intent in {"live", "callback"} or specific_agent
        ):
            callback_requested = True
            transfer_service.request_transfer(
                call_id,
                "Caller requested a human callback",
                "retell_callback",
                0.95,
                phone_number=caller_number,
            )
            destination = await asyncio.to_thread(
                call_forwarding.create_callback_outreach,
                caller_number,
                caller_text,
                call_id,
                specific_agent.get("name") if specific_agent else "",
                "The caller requested a callback. Please follow up using the caller's phone number.",
                contact_email=contact_email_from_messages(transcript),
            )
            transfer_service.update_transfer(call_id, "requested", destination)
            _trace(f"retell_human_callback_requested call={call_id} response_id={response_id}")
            reply = (f"I have marked your request for {destination}. Our team will contact you using this number as soon as possible."
                     if re.fullmatch(r"\+?[0-9 ()-]{8,}", caller_number)
                     else f"I have marked your request for {destination}. What phone number should the team use?")
        else:
            answer_parts: List[str] = []
            reminder = event.get("interaction_type") == "reminder_required"
            if early_first is not None:
                async def continue_early_reply():
                    try:
                        yield await early_first
                    except StopAsyncIteration:
                        return
                    async for chunk in early_reply:
                        yield chunk
                source = continue_early_reply()
            else:
                source = _stream_reply(services, transcript, caller_number, call_id, response_id, reminder=reminder,
                                       started_at=turn_started, timings=timing, inline_bridge=False, facts_prompt=facts_prompt)
            async def make_bridge():
                return await asyncio.shield(bridge_ready)

            fallback = ""  # make_bridge supplies a phrase only after this turn has qualified.
            def record_bridge(line):
                timing["bridge_ms"] = round((perf_counter() - turn_started) * 1000)
            stream = with_wait_bridge(source, make_bridge, fallback,
                                      status_after_seconds() if bridges_enabled() and not reminder and not bridge_prelude else 0, speech_ended,
                                      on_bridge=record_bridge,
                                      late_line="" if reminder or bridge_prelude or not bridges_enabled() else spoken_bridge(late_filler(recent), bridge_language),
                                      late_after=late_filler_seconds())
            async with contextlib.aclosing(stream):
                async for chunk in stream:
                    if response_id != latest_response_id:
                        _trace(f"retell_llm_stale_response call={call_id} response_id={response_id}")
                        return
                    answer_parts.append(chunk)
                    if not streamed:
                        timing["first_text_ms"] = round((perf_counter() - turn_started) * 1000)
                    streamed = True
                    await send_event(
                        {
                            "response_type": "response",
                            "response_id": response_id,
                            "content": chunk,
                            "content_complete": False,
                        }
                    )
            reply = "".join(answer_parts).strip() or "I’m sorry, I didn’t catch that. Could you please repeat it?"

        if response_id != latest_response_id:
            _trace(f"retell_llm_stale_response call={call_id} response_id={response_id}")
            return

        outgoing = "" if streamed else reply
        reply = (bridge_prelude + reply).strip()
        agent_messages.append({"content": reply, "timestamp": utc_iso()})
        save_progress(event.get("transcript"), reply)
        transfer_service.append_message(call_id, "assistant", reply)
        if _log_content_enabled():
            _trace(f"retell_llm_answer call={call_id} response_id={response_id} answer={reply!r}")
        await send_event(
            {
                "response_type": "response",
                "response_id": response_id,
                "content": outgoing,
                "content_complete": True,
            }
        )
        timing.setdefault("first_text_ms", round((perf_counter() - turn_started) * 1000))
        voice_turn_timings.append(timing)

    def consume_response_task(task: asyncio.Task[None]) -> None:
        response_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            _trace(f"retell_llm_response_task_failed call={call_id} error={type(exc).__name__}: {exc}", error=True)

    try:
        while True:
            event = json.loads(await websocket.receive_text())
            interaction_type = event.get("interaction_type")
            transcript = event.get("transcript") or []
            _trace(
                f"retell_ws_event call={call_id} type={interaction_type or 'missing'} "
                f"response_id={event.get('response_id')} transcript_lines={len(transcript)}"
            )
            if _log_content_enabled() and transcript:
                _trace(
                    f"retell_ws_transcript call={call_id} type={interaction_type or 'missing'} "
                    f"data={json.dumps(transcript[-4:], ensure_ascii=False)}"
                )

            if interaction_type == "ping_pong":
                await send_event({"response_type": "ping_pong", "timestamp": event.get("timestamp")})
                continue

            if interaction_type == "call_details":
                call = event.get("call") or {}
                caller_number = str(call.get("from_number") or "unknown")
                transfer_service.register_call(call_id, caller_number, direction=call.get("direction") or "inbound")
                _trace(f"retell_call_details call={call_id} direction={call.get('direction') or 'inbound'} caller_known={caller_number != 'unknown'}")
                try:
                    sensitivity = min(1.0, max(0.0, float(os.getenv("RETELL_INTERRUPTION_SENSITIVITY", "0.85"))))
                except ValueError:
                    sensitivity = 0.85
                try:
                    responsiveness = min(1.0, max(0.0, float(os.getenv("RETELL_RESPONSIVENESS", "1.0"))))
                except ValueError:
                    responsiveness = 1.0
                try:
                    reminder_trigger_ms = max(1000, int(os.getenv("VOICE_REMINDER_TRIGGER_MS", "7000")))
                except ValueError:
                    reminder_trigger_ms = 7000
                try:
                    reminder_max_count = max(0, int(os.getenv("VOICE_REMINDER_MAX_COUNT", "1")))
                except ValueError:
                    reminder_max_count = 1
                await send_event({"response_type": "update_agent", "agent_config": {
                    "interruption_sensitivity": sensitivity,
                    "responsiveness": responsiveness,
                    "reminder_trigger_ms": reminder_trigger_ms,
                    "reminder_max_count": reminder_max_count,
                }})
                # While the greeting plays: open the model connection and pre-build the prompt (cold first turns
                # could otherwise take seconds).
                asyncio.create_task(asyncio.to_thread(warm_model_connection, services))
                asyncio.create_task(asyncio.to_thread(_system_prompt, "hello", caller_number, ""))
                asyncio.create_task(asyncio.to_thread(prepare_turn_meaning, "", begin_message))
                continue

            if interaction_type == "update_only":
                if event.get("turntaking") == "user_turn" and not handoff_pending:
                    latest_response_id = -1
                    for older in list(response_tasks):
                        older.cancel()
                # The caller is still talking: start the knowledge-search embedding for what they have said so
                # far, so it is usually ready (cached) when Retell asks for the reply.
                spoken = _last_user_message(_transcript_as_messages(transcript))
                if not handoff_pending:
                    messages = _transcript_as_messages(transcript)
                    semantic_prefetch.schedule(spoken, last_agent_text(messages),
                        " ".join(" ".join(earlier_caller_texts(messages, spoken)[-8:]).split()[-25:]))
                if spoken and spoken != prefetched_text and perf_counter() - prefetched_at > 0.3:
                    prefetched_text, prefetched_at = spoken, perf_counter()
                    brain_service.prefetch_query_embedding(spoken)

            if interaction_type not in {"response_required", "reminder_required"}:
                if interaction_type not in {"update_only", "call_details"}:
                    _trace(f"retell_ws_ignored_event call={call_id} type={interaction_type or 'missing'}")
                continue

            save_progress(transcript)
            response_id = event.get("response_id")
            if not isinstance(response_id, int):
                continue
            latest_response_id = response_id
            semantic_prefetch.discard_pending()
            for older in list(response_tasks):
                # Retell only plays the newest response; finishing older ones would waste a model call each.
                older.cancel()
            response_task = asyncio.create_task(handle_response(event, response_id))
            response_tasks.add(response_task)
            response_task.add_done_callback(consume_response_task)
            continue
    except WebSocketDisconnect as exc:
        _trace(f"retell_ws_closed call={call_id} code={exc.code}")
    except Exception as exc:
        _trace(f"retell_ws_error call={call_id} error={type(exc).__name__}: {exc}", error=True)
    finally:
        await semantic_prefetch.close()
        for response_task in response_tasks:
            response_task.cancel()
        if response_tasks:
            await asyncio.gather(*response_tasks, return_exceptions=True)
        handoff = transfer_service.get_call(call_id) or {}
        transfer_service.close_call(call_id)
        if caller_number != "unknown" and (user_messages or agent_messages):
            duration = max((perf_counter() - started_clock), 0)
            transfer_status = handoff.get("transfer_status") or ("requested" if callback_requested else None)
            log_call(
                caller_number,
                call_data={
                    "direction": "inbound",
                    "status": "transfer_requested" if callback_requested else "completed",
                    "duration": duration,
                    "transcription": _serialize_messages(user_messages, "user"),
                    "ai_responses": _serialize_messages(agent_messages, "bot"),
                    "summary": (
                        "Human callback requested from the Retell AI call."
                        if callback_requested
                        else "Retell inbound AI call completed."
                    ),
                    "provisional": True,  # Retell's own record (summary, sentiment) replaces this one when it arrives
                    "sentiment": "neutral",
                    "transfer_requested": callback_requested,
                    "transfer_status": transfer_status,
                    "transfer_destination": handoff.get("transfer_destination") or ("Callback queue" if callback_requested else None),
                    "transfer_succeeded": False,
                    "call_session_id": call_id,
                    "provider": "retell",
                    "started_at": started_at.isoformat(),
                    "messages": live_messages,
                    "voice_turn_timings": voice_turn_timings[-200:],
                },
            )
            _trace(f"retell_ws_fallback_call_saved call={call_id} messages={len(user_messages) + len(agent_messages)} callback={callback_requested}")
        else:
            _trace(f"retell_ws_fallback_call_skipped call={call_id} caller_known={caller_number != 'unknown'} messages={len(user_messages) + len(agent_messages)}")
        fetch_retell_outcome_later(call_id)  # Retell's summary, even when its webhook never reaches us
        # The call's objects are garbage now: give their memory back before the next call (memory_care).
        asyncio.get_running_loop().run_in_executor(None, memory_care.release_after_call)
