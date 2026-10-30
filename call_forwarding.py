"""Live call forwarding to the human team.

An admin turns it on and lists the agents' phone numbers (Settings > Call forwarding). When it is on and a caller asks
to talk to a person, the AI says it is connecting them and the call is moved off the AI. If the caller names a configured
team member, only that member is rung; otherwise every available agent rings at once and the first phone to answer is
connected. If nobody answers in time, the caller hears that the team will call back, and the call is saved as a callback
request (Outreach). When it is off, nothing changes: the AI handles the call and a request for a person becomes a callback request.

Works for both voice paths: the custom stack knows the Twilio call itself; for Retell the Twilio call is remembered
when Retell is given the call (remember_retell_call), since Retell only tells us its own call id.
"""

from __future__ import annotations

import html
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from functools import lru_cache
from datetime import datetime
from typing import Any, Dict, List, Optional

import phonenumbers

from mongo_client import mongo_client
from voice_languages import SUPPORTED_LANGUAGES, effective_language, normalize_language

logger = logging.getLogger(__name__)

SETTINGS_ID = "call_forwarding"
MODE = "ring_all_answer"  # transfer_mode on forwarded calls (calls_api keeps their outcome on later saves)
DEFAULT_RING_SECONDS = 20
MIN_RING_SECONDS, MAX_RING_SECONDS = 10, 60
MAX_AGENTS = 10

# What the caller hears. Business-neutral: the business name and details come from the knowledge, not from here.
CONNECTING_LINE = "Connecting you to the team now."
TEAM_BUSY_LINE = ("Sorry, no one on the team could answer right now. I've passed on your request. "
                  "If we have a phone number for you, the team can call you back. Otherwise, please contact us again with a number to reach you.")
AGENT_WHISPER = "A customer is waiting to speak with you. Press 1 now to accept and connect the call."
# Said by the AI voice around a transfer or callback (routers/*_voice_api.py), in the caller's language (caller_line).
TRANSFER_FAILED_LINE = ("Sorry, I couldn't put you through just now. I've noted that you'd like to talk to someone, "
                        "and our team will call you back.")
CALLBACK_NOTED_LINE = "I've noted your callback request. The team will call you on this number."
CALLBACK_NUMBER_LINE = "I've passed on your callback request. What phone number should the team use?"
NO_LIVE_TRANSFER_PREFIX = "A live transfer isn't available right now. "
NOW_OR_LATER_LINE = "Would you like me to connect you with the team now, or have someone call you back later?"
NAMED_REQUEST_LINE = "I have noted your request for {destination}. They or the team will contact you using this number."
ANYTHING_ELSE_LINE = "Is there anything else I can help you with?"
READY_WHEN_YOU_ARE_LINE = "I'm here whenever you're ready. What can I help you with?"
WELCOME_LINE, OF_COURSE_LINE = "You're welcome.", "Of course."
CALLER_LINES = {
    "ur": {
        CONNECTING_LINE: "آپ کو ابھی ٹیم سے ملا رہے ہیں۔",
        TRANSFER_FAILED_LINE: "معذرت، ابھی آپ کو ملایا نہیں جا سکا۔ آپ کی بات کرنے کی درخواست نوٹ کر لی گئی ہے، ہماری ٹیم آپ کو واپس کال کرے گی۔",
        CALLBACK_NOTED_LINE: "آپ کی کال بیک کی درخواست نوٹ کر لی گئی ہے۔ ٹیم آپ کو اسی نمبر پر کال کرے گی۔",
        CALLBACK_NUMBER_LINE: "آپ کی کال بیک کی درخواست آگے بھیج دی گئی ہے۔ ٹیم آپ کو کس نمبر پر کال کرے؟",
        NO_LIVE_TRANSFER_PREFIX: "ابھی براہِ راست ٹیم سے ملانا ممکن نہیں۔ ",
        NOW_OR_LATER_LINE: "کیا آپ ابھی ٹیم سے بات کرنا چاہیں گے، یا بعد میں کوئی آپ کو کال کرے؟",
        NAMED_REQUEST_LINE: "{destination} کے لیے آپ کی درخواست نوٹ کر لی گئی ہے۔ وہ یا ٹیم اسی نمبر پر آپ سے رابطہ کریں گے۔",
        ANYTHING_ELSE_LINE: "کیا کسی اور چیز میں مدد چاہیے؟",
        READY_WHEN_YOU_ARE_LINE: "جب آپ تیار ہوں، بتائیں۔ آپ کو کس چیز میں مدد چاہیے؟",
        WELCOME_LINE: "کوئی بات نہیں۔",
        OF_COURSE_LINE: "جی ضرور۔",
    },
    "ar": {
        CONNECTING_LINE: "أحوّلك إلى الفريق الآن.",
        TRANSFER_FAILED_LINE: "عذرًا، لم أتمكن من تحويلك الآن. سجّلت أنك تريد التحدث مع أحد، وسيتصل بك فريقنا.",
        CALLBACK_NOTED_LINE: "سجّلت طلب معاودة الاتصال. سيتصل بك الفريق على هذا الرقم.",
        CALLBACK_NUMBER_LINE: "أرسلت طلب معاودة الاتصال. ما رقم الهاتف الذي يتصل عليه الفريق؟",
        NO_LIVE_TRANSFER_PREFIX: "التحويل المباشر غير متاح الآن. ",
        NOW_OR_LATER_LINE: "هل تريد أن أحوّلك إلى الفريق الآن، أم يتصل بك أحد لاحقًا؟",
        NAMED_REQUEST_LINE: "سجّلت طلبك للتحدث مع {destination}. سيتواصل معك هو أو الفريق على هذا الرقم.",
        ANYTHING_ELSE_LINE: "هل هناك شيء آخر يمكنني مساعدتك فيه؟",
        READY_WHEN_YOU_ARE_LINE: "أنا هنا متى كنت مستعدًا. كيف يمكنني مساعدتك؟",
        WELCOME_LINE: "على الرحب والسعة.",
        OF_COURSE_LINE: "بالتأكيد.",
    },
}


def caller_line(line: str, language: str = "en", **values: Any) -> str:
    """One of the lines above in the caller's language (English when it has no translation); {destination} etc.
    are filled in after."""
    from voice_languages import normalize_language
    code = normalize_language(language)
    template = CALLER_LINES.get(code, {}).get(line, line) if code != "en" else line
    return template.format(**values) if values else template
# Twilio <Say> voice for the lines spoken outside the AI voice (on hold, and to the agent). Change with FORWARDING_SAY_VOICE.
SAY_VOICE = os.getenv("FORWARDING_SAY_VOICE", "Polly.Joanna-Neural")
# TEAM_BUSY_LINE is said by Twilio, not the AI voice, after the team's phones stop ringing: in the caller's language.
# Twilio has no Urdu voice, so Urdu callers hear it in Hindi (spoken the same; Polly.Aditi reads Devanagari).
TEAM_BUSY_SAY = {
    "ar": (os.getenv("FORWARDING_SAY_VOICE_AR", "Polly.Zeina"),
           "عذرًا، لم يتمكن أحد من الفريق من الرد الآن. أوصلت طلبك. إذا كان لدينا رقم هاتفك فسيتصل بك الفريق، وإلا فيرجى التواصل معنا مجددًا مع رقم للتواصل."),
    "ur": (os.getenv("FORWARDING_SAY_VOICE_UR", "Polly.Aditi"),
           "माफ़ कीजिए, अभी टीम में कोई जवाब नहीं दे सका। आपकी बात आगे पहुँचा दी गई है। अगर हमारे पास आपका नंबर है तो टीम आपको "
           "वापस कॉल करेगी, नहीं तो कृपया अपने नंबर के साथ दोबारा संपर्क करें।"),
}


def _region() -> str:
    return (os.getenv("VOICE_DEFAULT_REGION") or "AU").strip().upper() or "AU"


def normalize_number(raw: str) -> str:
    """An agent's number in E.164 (+61412345678), or "" when it is not a valid phone number. Local numbers are read in
    the business's region (VOICE_DEFAULT_REGION)."""
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        number = phonenumbers.parse(text, None if text.startswith("+") else _region())
    except phonenumbers.NumberParseException:
        return ""
    if not phonenumbers.is_valid_number(number):
        return ""
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


def _clean_agents(agents: Any) -> List[Dict[str, Any]]:
    cleaned: List[Dict[str, Any]] = []
    for item in agents if isinstance(agents, list) else []:
        if not isinstance(item, dict):
            continue
        aliases = item.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [part.strip() for part in aliases.split(",") if part.strip()]
        raw_languages = item.get("preferred_languages") or item.get("languages") or []
        if isinstance(raw_languages, str):
            raw_languages = [part.strip() for part in raw_languages.split(",") if part.strip()]
        preferred_languages = []
        for value in raw_languages if isinstance(raw_languages, list) else []:
            code = normalize_language(value)
            if str(value or "").strip().lower() in SUPPORTED_LANGUAGES and code not in preferred_languages:
                preferred_languages.append(code)
        cleaned_item = {"name": str(item.get("name") or "").strip()[:80], "phone": str(item.get("phone") or "").strip()[:32],
                        "enabled": bool(item.get("enabled", True))}
        if preferred_languages:
            cleaned_item["preferred_languages"] = preferred_languages
        cleaned_aliases = [str(alias).strip()[:80] for alias in aliases if str(alias).strip()][:8]
        if cleaned_aliases:
            cleaned_item["aliases"] = cleaned_aliases
        cleaned.append(cleaned_item)
    return cleaned[:MAX_AGENTS]


def get_settings() -> Dict[str, Any]:
    """Saved settings (defaults when nothing is saved yet: off, no agents)."""
    saved: Dict[str, Any] = {}
    if mongo_client.is_connected():
        saved = mongo_client.platform_settings.find_one({"_id": SETTINGS_ID}) or {}
    ring = saved.get("ring_seconds")
    return {
        "enabled": bool(saved.get("enabled", False)),
        "agents": _clean_agents(saved.get("agents")),
        "ring_seconds": ring if isinstance(ring, int) and MIN_RING_SECONDS <= ring <= MAX_RING_SECONDS else DEFAULT_RING_SECONDS,
        "updated_by_name": saved.get("updated_by_name"),
        "updated_at": saved.get("updated_at"),
    }


def has_saved_settings() -> bool:
    """Whether the admin Call forwarding screen has saved its shared settings document."""
    if not mongo_client.is_connected():
        return False
    try:
        return bool(mongo_client.platform_settings.find_one({"_id": SETTINGS_ID}, {"_id": 1}))
    except Exception:
        return False


def validate(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Check and normalise settings from the admin page. Raises ValueError with a message the admin can act on."""
    agents = []
    for index, agent in enumerate(_clean_agents(payload.get("agents")), start=1):
        if not agent["name"] and not agent["phone"]:
            continue  # an empty row left in the form
        number = normalize_number(agent["phone"])
        if not number:
            raise ValueError(f"Agent {index} ({agent['name'] or 'no name'}): '{agent['phone']}' is not a valid phone number")
        saved_agent = {"name": agent["name"] or f"Agent {index}", "phone": number, "enabled": agent["enabled"]}
        if agent.get("preferred_languages"):
            saved_agent["preferred_languages"] = agent["preferred_languages"]
        if agent.get("aliases"):
            saved_agent["aliases"] = agent["aliases"]
        agents.append(saved_agent)
    numbers = [agent["phone"] for agent in agents]
    if len(numbers) != len(set(numbers)):
        raise ValueError("The same phone number is listed twice")
    ring = payload.get("ring_seconds", DEFAULT_RING_SECONDS)
    try:
        ring = int(ring)
    except (TypeError, ValueError):
        raise ValueError("Ring time must be a number of seconds")
    if not MIN_RING_SECONDS <= ring <= MAX_RING_SECONDS:
        raise ValueError(f"Ring time must be between {MIN_RING_SECONDS} and {MAX_RING_SECONDS} seconds")
    enabled = bool(payload.get("enabled"))
    if enabled and not any(agent["enabled"] for agent in agents):
        raise ValueError("Add at least one active agent before turning call forwarding on")
    return {"enabled": enabled, "agents": agents, "ring_seconds": ring}


def save_settings(payload: Dict[str, Any], user: Dict[str, Any]) -> Dict[str, Any]:
    settings = validate(payload)
    mongo_client.platform_settings.update_one(
        {"_id": SETTINGS_ID},
        {"$set": {**settings, "updated_by": str(user.get("_id") or ""), "updated_by_name": user.get("name"), "updated_at": datetime.utcnow()}},
        upsert=True)
    _ON_CACHE.update(on=settings["enabled"] and bool(active_agents(settings)), at=time.monotonic())  # live calls see it next turn
    logger.info("call forwarding %s by %s: %d agent(s), %d active, ring %ss", "on" if settings["enabled"] else "off", user.get("email"),
                len(settings["agents"]), sum(agent["enabled"] for agent in settings["agents"]), settings["ring_seconds"])
    return get_settings()


def active_agents(settings: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    settings = settings or get_settings()
    return [agent for agent in settings["agents"] if agent["enabled"] and agent["phone"]]


def language_matched_agents(agents: List[Dict[str, Any]], preferred_language: str = "") -> List[Dict[str, Any]]:
    """Prefer every active teammate who speaks the caller's language, or everyone when no match exists."""
    active = [agent for agent in agents if agent.get("enabled", True) and (agent.get("phone") or agent.get("phone_number"))]
    if not active:
        return []
    code = effective_language(preferred_language or "en")
    matches = [agent for agent in active if code in {
        normalize_language(value) for value in (agent.get("preferred_languages") or agent.get("languages") or [])
        if str(value or "").strip().lower() in SUPPORTED_LANGUAGES
    }]
    return matches or active


def forwarding_targets(settings: Optional[Dict[str, Any]] = None, requested_text: str = "", preferred_language: str = "") -> List[Dict[str, Any]]:
    """Return the exact phones to ring: a named person first, then language affinity, then the whole team."""
    settings = settings or get_settings()
    matched = requested_agent(requested_text, settings)
    if matched:
        return [matched]
    return language_matched_agents(active_agents(settings), preferred_language)


def requested_agent(caller_text: str, settings: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Return the enabled agent named by the caller, using a full name, first name, or configured alias."""
    normalized = re.sub(r"[^a-z0-9\s]", " ", str(caller_text or "").lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    agents = active_agents(settings)
    candidates: List[tuple[int, Dict[str, Any], str]] = []
    for agent in agents:
        name = str(agent.get("name") or "").strip().lower()
        aliases = agent.get("aliases") or []
        terms = [name, *[str(alias).strip().lower() for alias in aliases]]
        parts = name.split()
        if len(parts) > 1 and parts[0] not in {"team", "human", "agent"}:
            # A first name is convenient in a live call, but only wins if it is unique among active agents.
            same_first_name = sum(1 for other in agents if str(other.get("name") or "").strip().lower().split()[:1] == parts[:1])
            if same_first_name == 1:
                terms.append(parts[0])
        for term in {term for term in terms if len(term) >= 2}:
            if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", normalized):
                candidates.append((len(term), agent, term))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def explicit_callback_request(caller_text: str) -> bool:
    """Semantic callback check, including when live forwarding is disabled."""
    return handoff_intent(caller_text) == "callback"


def assign_lead_to_requested_agent(
    caller: str,
    requested_text: str,
    settings: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Assign the caller's lead to the registered user named in a handoff request.

    Call forwarding settings contain the phone used to ring an agent, while lead
    ownership uses the authenticated user's id. Resolve the configured phone to
    that user before writing ``assigned_to`` so the normal team-member scope and
    dashboard counts continue to work. A small inbound lead is created when the
    live transfer happens before the end-of-call webhook has saved one.
    """
    matched = requested_agent(requested_text, settings)
    if not matched:
        return None

    try:
        from routers.calls_api import caller_identity

        identity = caller_identity(caller)
        if not identity or identity.lower() in {"unknown", "anonymous", "web caller"}:
            logger.info("call forwarding: cannot assign named request without a caller identity")
            return {"agent": matched, "user": None, "lead": None}
        if not mongo_client.is_connected() or not hasattr(mongo_client, "users") or not hasattr(mongo_client, "leads"):
            logger.warning("call forwarding: cannot assign %s; users or leads collection is unavailable", matched.get("name"))
            return {"agent": matched, "user": None, "lead": None}

        # Only active authenticated users can own a lead. A forwarding number
        # may still be a non-login phone, in which case the notification remains
        # useful but no invalid lead owner is written.
        users = list(mongo_client.users.find({"active": {"$ne": False}}))
        target_phone = normalize_number(matched.get("phone"))
        user = next(
            (candidate for candidate in users
             if target_phone and normalize_number(candidate.get("phone")) == target_phone),
            None,
        )
        if not user:
            target_name = re.sub(r"\s+", " ", str(matched.get("name") or "").strip().lower())
            user = next(
                (candidate for candidate in users
                 if target_name and re.sub(r"\s+", " ", str(candidate.get("name") or "").strip().lower()) == target_name),
                None,
            )
        if not user:
            logger.warning("call forwarding: named agent %s is not a registered active user; lead stays unassigned", matched.get("name"))
            return {"agent": matched, "user": None, "lead": None}

        lead = mongo_client.leads.find_one({"phone": identity})
        if not lead:
            now = datetime.utcnow()
            draft = {
                "name": "Inbound caller",
                "phone": identity,
                "email": "",
                "company": "",
                "notes": "Automatically captured from an inbound voice call.",
                "status": "new",
                "source": "voice",
                "call_attempts": 0,
                "last_call": now,
                "created_at": now,
                "updated_at": now,
            }
            try:
                inserted = mongo_client.leads.insert_one(draft)
                draft["_id"] = inserted.inserted_id
                lead = draft
            except Exception:
                # A concurrent webhook may have created the unique phone row.
                lead = mongo_client.leads.find_one({"phone": identity})
        if not lead:
            logger.warning("call forwarding: could not find or create lead for %s", identity)
            return {"agent": matched, "user": user, "lead": None}

        assigned_name = user.get("name") or matched.get("name") or "Team member"
        mongo_client.leads.update_one(
            {"_id": lead["_id"]},
            {"$set": {
                "assigned_to": str(user["_id"]),
                "assigned_to_name": assigned_name,
                "updated_at": datetime.utcnow(),
            }},
        )
        lead.update({"assigned_to": str(user["_id"]), "assigned_to_name": assigned_name})
        logger.info("call forwarding: assigned lead %s to %s (%s)", identity, assigned_name, user.get("_id"))
        return {"agent": matched, "user": user, "lead": lead}
    except Exception as error:
        # Assignment must never stop a live transfer or callback notification.
        logger.warning("call forwarding: could not assign named lead to %s: %s", matched.get("name"), error)
        return {"agent": matched, "user": None, "lead": None}


_ON_CACHE: Dict[str, Any] = {"at": -1e9, "on": False, "refreshing": False}
ON_CACHE_SECONDS = 10  # re-read in the background at most this often; saving the settings page updates it at once


def _refresh_on() -> None:
    try:
        settings = get_settings()
        _ON_CACHE.update(on=settings["enabled"] and bool(active_agents(settings)), at=time.monotonic())
    except Exception as error:
        logger.debug("call forwarding: settings refresh failed (keeping %s): %s", _ON_CACHE["on"], error)
    finally:
        _ON_CACHE["refreshing"] = False


def forwarding_on() -> bool:
    """Forwarding is on and someone can take the call. Asked on every caller turn inside the live call, so it never
    waits for the database: it answers from the last reading and refreshes that in the background when stale (a slow
    database or network would otherwise hold up the call's audio)."""
    if time.monotonic() - _ON_CACHE["at"] > ON_CACHE_SECONDS and not _ON_CACHE["refreshing"]:
        _ON_CACHE["refreshing"] = True
        import threading
        threading.Thread(target=_refresh_on, name="forwarding-settings", daemon=True).start()
    return _ON_CACHE["on"]


def should_forward(caller_text: str) -> bool:
    """Forwarding is on and the caller asked for a person now. The meaning check runs only when forwarding is on;
    call it off the event loop (it embeds the text)."""
    try:
        return forwarding_on() and wants_person(caller_text)
    except Exception as error:
        logger.warning("call forwarding check failed (the AI carries on): %s", error)
        return False


def explicit_live_request(caller_text: str) -> bool:
    """Embedding-based live handoff check for a partial caller turn."""
    return handoff_intent(caller_text) == "live"


def followup_live_request(caller_text: str, earlier: str = "") -> bool:
    """Read a short reply together with the earlier team request."""
    return handoff_intent(caller_text, earlier) == "live"


def team_handoff_request(caller_text: str) -> bool:
    """An unclear handoff request, suitable for a now-or-later clarification."""
    return handoff_intent(caller_text) == "unclear"


def ambiguous_team_call(caller_text: str) -> bool:
    """A request to arrange a team call without clear live or later timing."""
    return handoff_intent(caller_text) == "unclear"


def handoff_intent(caller_text: str, earlier: str = "", agent_text: str = "") -> str:
    """Classify the full turn, optionally with the preceding team request: live, callback, unclear or other.
    agent_text: our last reply, which the language-model check reads with the turn ("I just talk with you" after
    "Would you like details on those handles?" is about talking to us)."""
    from turn_meaning import HANDOFF_ACTION
    ranked = HANDOFF_ACTION.rank(caller_text, earlier)
    if not ranked:
        return ""
    scores = dict(ranked)
    live, callback = scores["live"], scores["callback"]
    unclear, other = scores["unclear"], scores["other"]
    # Embeddings sometimes put "arrange a call now" and "arrange a call tomorrow"
    # almost on top of each other. Ask a small language model to resolve only
    # that close live/callback tie, preserving the rest of the fast classifier.
    if (max(live, callback) >= HANDOFF_ACTION.min_score and max(live, callback) >= other + 0.02
            and abs(live - callback) <= 0.02 and unclear <= max(live, callback) + 0.03):
        from turn_meaning import HANDOFF_REPLY
        replies = HANDOFF_REPLY.rank(caller_text)
        if (replies and replies[0][0] in {"live", "callback"} and replies[0][1] >= 0.85
                and replies[0][1] - replies[1][1] >= 0.07):
            return replies[0][0]
        decided = _adjudicate_handoff(caller_text, earlier, agent_text=agent_text)
        if decided:
            return decided
        return "unclear"  # ask now-or-later instead of guessing after a provider error
    # Live transfers are consequential. A barely-leading live score over `other`
    # can turn ordinary questions into an unwanted transfer, so require a clear
    # semantic margin over non-handoff intent while preserving explicit requests.
    if live >= HANDOFF_ACTION.min_score and live >= callback + 0.025 and live >= unclear - 0.02 and live >= other + 0.04:
        result = "live"
    elif callback >= HANDOFF_ACTION.min_score and callback >= live + 0.02 and callback >= unclear + 0.02 and callback >= other + 0.02:
        result = "callback"
    elif unclear >= HANDOFF_ACTION.min_score and unclear >= other + 0.02:
        result = "unclear"
    else:
        result = ""
    # Callers can put a request for a person in any words, and a sentence about talking to *us* can sound like one:
    # "...whichever there are two, I just talk with you." scored live 0.78 against other 0.68 and rang the team
    # (production call 2026-10-28 09:24). Example sentences cannot cover every wording, so a possible transfer that
    # is not clearly one is read by a language model, in full and with the earlier request, before anything happens.
    # Clear requests ("I want to speak to a person": live 0.95, other 0.76) still go at once, and turns that are
    # clearly not about a person never wait for it. The caller is never asked.
    clear_live = result == "live" and live >= other + HANDOFF_CLEAR_MARGIN
    # Right after we offered the team ("now, or should they call you back?"), any reply may be the answer, in any
    # words: "Later is fine." matched no example and booked nothing. Then the model reads the reply with our offer.
    answers_offer = not result and bool(agent_text) and _offered_team(agent_text)
    if (result in {"live", "unclear"} and not clear_live or answers_offer) and _llm_handoff_check_enabled():
        decided = _adjudicate_handoff_cached(caller_text, earlier, agent_text)
        if decided:
            result = "" if decided == "other" else decided
        elif result == "live":
            result = "unclear"  # the check failed: ask now-or-later rather than ring the team on a borderline score
    logger.debug("handoff intent %r -> %s (%s)", caller_text[:80], result or "other", ", ".join(f"{k} {v:.2f}" for k, v in ranked))
    return result


# How far "live" must lead "other" to transfer without the language-model check (see handoff_intent).
HANDOFF_CLEAR_MARGIN = 0.15
_HANDOFF_VERDICTS: "OrderedDict[tuple, str]" = OrderedDict()
_HANDOFF_VERDICTS_LOCK = threading.Lock()


def _offered_team(agent_text: str) -> bool:
    """Our reply offered a live transfer or a callback, by meaning (turn_meaning.HANDOFF_OFFER)."""
    from turn_meaning import HANDOFF_OFFER
    offers = HANDOFF_OFFER.close(agent_text)
    return bool(offers) and "other" not in offers


def _llm_handoff_check_enabled() -> bool:
    """HANDOFF_LLM_CHECK=false decides by the embedding scores alone (no model call, as before)."""
    return os.getenv("HANDOFF_LLM_CHECK", "true").strip().lower() not in {"0", "false", "no", "off"}


def _adjudicate_handoff_cached(caller_text: str, earlier: str = "", agent_text: str = "") -> str:
    """One model call per turn: the same turn is checked by several helpers (wants a person? a callback? unclear?)."""
    key = (" ".join(caller_text.split()), " ".join((earlier or "").split()), " ".join((agent_text or "").split()))
    with _HANDOFF_VERDICTS_LOCK:
        if key in _HANDOFF_VERDICTS:
            return _HANDOFF_VERDICTS[key]
    decided = _adjudicate_handoff(caller_text, earlier, agent_text=agent_text)
    if decided:  # a failed check is not remembered: the next helper may try again
        with _HANDOFF_VERDICTS_LOCK:
            _HANDOFF_VERDICTS[key] = decided
            while len(_HANDOFF_VERDICTS) > 256:
                _HANDOFF_VERDICTS.popitem(last=False)
    return decided


def contextual_handoff_intent(caller_text: str, agent_text: str = "", callback_requested: bool = False) -> Optional[str]:
    """Resolve the caller's meaning against our offer and completed callback state.

    Run off the event loop. None leaves substantive requests to handoff_intent;
    an empty string means no new handoff. No lexical intent rules are used.
    """
    from turn_meaning import CALLBACK_ACK_CONTEXT, HANDOFF_OFFER, HANDOFF_REPLY, AGENT_ASKED
    from meaning import vectors
    try:
        vectors([part for part in (caller_text, agent_text) if part.strip()])
    except Exception:
        return None
    ranked = HANDOFF_REPLY.rank(caller_text)
    if not ranked or ranked[0][1] < HANDOFF_REPLY.min_score or ranked[0][1] - ranked[1][1] < HANDOFF_REPLY.close_call:
        return None
    reply = ranked[0][0]
    if reply not in {"accept", "decline", "live", "callback"}:
        return None
    if reply == "decline" or (callback_requested and reply == "accept"):
        from voice_control import has_followup_request
        if has_followup_request(caller_text) is not False:
            return ""  # answer the whole turn; a refusal/acknowledgement must not erase its question
    if callback_requested and reply == "accept":
        # Agreement is not a phone number when we still need a reachable number.
        if AGENT_ASKED.best(agent_text) == "phone":
            return ""
        return "callback_ack" if CALLBACK_ACK_CONTEXT.best(agent_text) == "confirmed" else None
    # Only a clear offer counts. From a production call: a business description ending "...and friendly customer
    # support." scored live 0.590 against other 0.574 (a close call), and the caller's "Okay." started a live transfer.
    # A close call between two kinds of offer is still an offer (which kind is unclear: ask, via "choice").
    offers = HANDOFF_OFFER.close(agent_text) if agent_text else ["other"]
    offer = "other" if not offers or "other" in offers else offers[0] if len(offers) == 1 else "choice"
    if offer not in {"live", "callback", "choice"}:
        return "" if reply in {"accept", "decline"} else None
    if reply == "decline":
        return "declined"
    if reply in {"live", "callback"}:
        return reply
    return "unclear" if offer == "choice" else offer


@lru_cache(maxsize=256)
def _adjudicate_handoff(caller_text: str, earlier: str = "", agent_text: str = "") -> str:
    """Resolve an embedding tie using the actual caller wording and context."""
    try:
        from ai_services import AIServices
        services = AIServices()
        # The main voice model, not the fast one: llama-3.1-8b read "Put me through to sales please." and "Can somebody
        # phone me this arvo?" as no request for a person. Ringing the team is consequential enough for the better reader.
        model = (os.getenv("HANDOFF_CLASSIFIER_MODEL") or os.getenv("RETELL_OPENROUTER_MODEL")
                 or ("openai/gpt-4.1-mini" if services.provider == "openrouter" else "gpt-4.1-mini"))
        message = "\n".join(part for part in (f"Assistant just said: {agent_text}" if agent_text else "",
                                               f"Earlier caller request: {earlier}" if earlier else "",
                                               f"Current caller turn: {caller_text}") if part)
        response = services.openai_client.with_options(timeout=2, max_retries=0).chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "The caller is on the phone with an AI assistant. Classify whether the caller "
                 "wants to speak with a human team member. Reply with exactly one word: LIVE, CALLBACK, UNCLEAR, or OTHER. "
                 "LIVE means connect during this ongoing phone call. CALLBACK means the team should contact them later. "
                 "UNCLEAR means a human is requested but timing is not clear. OTHER means no human is requested. "
                 "Read the caller's meaning and timing, even when the wording is indirect or the transcript is garbled. "
                 "Wanting to keep talking with the assistant itself (\"you\") is OTHER, and so are questions about "
                 "products, services or staff that do not ask for a person to join the call or call back."},
                {"role": "user", "content": message},
            ], max_tokens=8, temperature=0)
        answer = (response.choices[0].message.content or "").strip().lower()
        return answer if answer in {"live", "callback", "unclear", "other"} else ""
    except Exception as error:
        logger.warning("handoff tie check failed: %s", error)
        return ""


def wants_person(caller_text: str, earlier: str = "", agent_text: str = "") -> bool:
    return handoff_intent(caller_text, earlier, agent_text) == "live"


def asks_for_team(caller_text: str) -> str:
    """Retell's live-person or later-callback decision from the same embedding model."""
    result = handoff_intent(caller_text)
    return "person" if result == "live" else result if result == "callback" else ""


# ---- Retell: its call id -> the Twilio call carrying it (Retell calls arrive as Twilio calls bridged over SIP).
_RETELL_CALLS: Dict[str, Dict[str, Any]] = {}
_RETELL_KEEP_SECONDS = 4 * 3600


def remember_retell_call(retell_call_id: str, call_sid: str, caller: str) -> None:
    now = time.monotonic()
    for key in [key for key, item in _RETELL_CALLS.items() if now - item["at"] > _RETELL_KEEP_SECONDS]:
        _RETELL_CALLS.pop(key, None)
    if retell_call_id and call_sid:
        _RETELL_CALLS[retell_call_id] = {"call_sid": call_sid, "caller": caller, "at": now}


def twilio_call_for_retell(retell_call_id: str) -> Optional[Dict[str, Any]]:
    return _RETELL_CALLS.get(retell_call_id)


# ---- TwiML
def _base_url() -> str:
    return (os.getenv("RENDER_EXTERNAL_URL") or os.getenv("TWILIO_PUBLIC_BASE_URL") or "").rstrip("/")


def _caller_id(caller: str) -> str:
    """Use the configured business identity for the new outbound team leg.

    A browser caller's CRM contact number is not their Twilio source identity.
    Reusing it as caller ID caused error 13247 even for a verified contact.
    """
    return (os.getenv("TWILIO_PHONE_NUMBER") or os.getenv("TWILIO_CALLER_ID") or "").strip()


def transfer_twiml(caller: str, settings: Optional[Dict[str, Any]] = None, requested_text: str = "", preferred_language: str = "") -> str:
    """Ring the named agent when matched; otherwise prefer teammates who speak the caller's language."""
    settings = settings or get_settings()
    base = _base_url()
    agents = active_agents(settings)
    targets = forwarding_targets(settings, requested_text, preferred_language)
    if not targets or not base:
        raise RuntimeError("call forwarding has no active agents" if not targets else "TWILIO_PUBLIC_BASE_URL is not set")
    numbers = "".join(f'<Number>{html.escape(agent["phone"])}</Number>' for agent in targets)
    caller_id = _caller_id(caller)
    caller_attr = f' callerId="{html.escape(caller_id, quote=True)}"' if caller_id else ""
    # The caller's language rides on the action URL, for TEAM_BUSY_LINE in it (dial_result_twiml).
    result = html.escape(f"{base}/api/call-forwarding/dial-result?language={effective_language(preferred_language or 'en')}", quote=True)
    recording_result = html.escape(f"{base}/api/call-forwarding/recording-result", quote=True)
    return ('<?xml version="1.0" encoding="UTF-8"?><Response>'
            f'<Dial{caller_attr} answerOnBridge="true" '
            f'record="record-from-answer-dual" recordingStatusCallback="{recording_result}" '
            f'recordingStatusCallbackMethod="POST" timeout="{settings["ring_seconds"]}" action="{result}" method="POST">{numbers}</Dial>'
            "</Response>")


def recording_finished(call_sid: str, recording_sid: str, recording_url: str, status: str) -> None:
    """Twilio sends this only when the bridge recording is available (or absent)."""
    if not call_sid or not _find_call(call_sid):
        logger.warning("call forwarding: recording callback has no matching call %s", call_sid)
        return
    if status == "completed" and recording_sid.startswith("RE") and recording_url.startswith("https://"):
        _record(call_sid, {"recording_sid": recording_sid, "recording_url": recording_url,
                           "human_recording_sid": recording_sid, "human_recording_status": "completed",
                           "human_transcription_status": "pending" if (os.getenv("DEEPGRAM_API_KEY") or os.getenv("DG_API_KEY")) else "unavailable"})
        logger.info("call forwarding: recording ready for %s", call_sid)
    elif status == "absent":
        _record(call_sid, {"human_recording_status": "absent"})
        logger.warning("call forwarding: Twilio recording absent for %s", call_sid)


def full_recording_finished(call_sid: str, recording_sid: str, recording_url: str, status: str, duration: str = "") -> None:
    """Save the parent-call audio that starts before the AI greeting."""
    if not call_sid:
        return
    # Retell's final webhook and Twilio's recording callback can arrive in either order.
    for attempt in range(6):
        if _find_call(call_sid):
            break
        if attempt < 5:
            time.sleep(2)
    else:
        logger.warning("call forwarding: full recording callback has no matching call %s", call_sid)
        return
    if status == "completed" and recording_sid.startswith("RE") and recording_url.startswith("https://"):
        fields = {"full_recording_sid": recording_sid, "full_recording_url": recording_url,
                  "full_recording_status": "completed"}
        if str(duration).isdigit():
            fields["full_recording_duration"] = int(duration)
            current = _find_call(call_sid) or {}
            fields["duration"] = max(int(duration), int(current.get("duration") or 0))
        _record(call_sid, fields)
        logger.info("call forwarding: full recording ready for %s", call_sid)
    elif status == "absent":
        _record(call_sid, {"full_recording_status": "absent"})
        logger.warning("call forwarding: full recording absent for %s", call_sid)


def screen_twiml() -> str:
    """Played to an agent who picked up: accept with 1, otherwise their leg hangs up and the others keep ringing."""
    action = html.escape(f"{_base_url()}/api/call-forwarding/screen-result", quote=True)
    say = html.escape(AGENT_WHISPER)
    return ('<?xml version="1.0" encoding="UTF-8"?><Response>'
            f'<Gather numDigits="1" timeout="15" action="{action}" method="POST"><Say voice="{SAY_VOICE}">{say}</Say>'
            f'<Say voice="{SAY_VOICE}">Press 1 now to connect.</Say></Gather>'
            "<Hangup/></Response>")


def screen_result_twiml(digits: str) -> str:
    """1: an empty response connects the agent to the caller. Anything else: this agent's leg ends."""
    if str(digits or "").strip() == "1":
        return '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
    return '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'


def dial_result_twiml(accepted: bool, language: str = "en") -> str:
    """After the agents' phones: hang up when an agent took the call (they talked and it is over), otherwise tell the
    caller, in their language, that the team will call back."""
    if accepted:
        return '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'
    voice, line = TEAM_BUSY_SAY.get(normalize_language(language), (SAY_VOICE, TEAM_BUSY_LINE))
    return (f'<?xml version="1.0" encoding="UTF-8"?><Response><Say voice="{html.escape(voice, quote=True)}">{html.escape(line)}</Say>'
            "<Hangup/></Response>")


# ---- Moving a live call to the team, and recording what happened
def transfer(call_sid: str, caller: str, session_id: str = "", requested_text: str = "", preferred_language: str = "") -> bool:
    """Take the live call away from the AI and ring the team. False when it could not be started (the AI carries on).
    `session_id`: the id the call is stored under when it is not the Twilio call (Retell)."""
    from routers.twilio_api import _get_twilio_client
    client = _get_twilio_client()
    if client is None or not call_sid:
        logger.warning("call forwarding: Twilio is not configured; cannot transfer %s", call_sid)
        create_callback_outreach(caller, requested_text, session_id or call_sid,
                                 message="A caller asked to speak with a team member, but the live transfer was unavailable.")
        return False
    settings = get_settings()
    matched = requested_agent(requested_text, settings)
    if not preferred_language:
        try:
            from voice_languages import language_for_phone
            preferred_language = language_for_phone(caller)
        except Exception:
            preferred_language = "en"
    targets = forwarding_targets(settings, requested_text, preferred_language)
    destination = matched.get("name") if matched else "Human team"
    try:
        twiml = transfer_twiml(caller, settings, requested_text, preferred_language)
        client.calls(call_sid).update(twiml=twiml)
    except Exception as error:
        logger.warning("call forwarding: could not transfer %s: %s", call_sid, error)
        create_callback_outreach(caller, requested_text, session_id or call_sid, destination=destination,
                                 message=f"A caller asked to speak with {destination}, but the live transfer could not start.")
        return False
    targets = [{"name": agent["name"], "phone": agent["phone"], "preferred_languages": agent.get("preferred_languages", []), "status": "requested"}
               for agent in targets]
    _record(call_sid, {"transfer_requested": True, "transfer_status": "ringing", "transfer_mode": MODE, "twilio_call_sid": call_sid,
                       "transfer_targets": targets, "transfer_language": effective_language(preferred_language),
                       "status": "transfer_requested", "transfer_destination": destination, "requested_agent": destination if matched else None,
                       "transfer_started_at": datetime.utcnow()},
            session_id or call_sid)
    _timeline(call_sid, caller, f"Caller asked for {destination}: ringing {destination if matched else f'{len(targets)} team member(s)'}")
    try:
        create_callback_outreach(caller, requested_text, session_id or call_sid, destination=destination,
                                 message=f"A caller is being connected to {destination}.", notify_only=True)
    except Exception as error:
        logger.warning("call forwarding: could not notify %s about %s: %s", destination, call_sid, error)
    logger.info("call forwarding: %s moved to %s (%d agent(s), %ss)", call_sid, destination, len(targets), settings["ring_seconds"])
    return True


def create_callback_outreach(
    caller: str,
    requested_text: str,
    session_id: str = "",
    destination: str = "",
    message: str = "",
    notify_only: bool = False,
    contact_email: str = "",
) -> str:
    """Create the in-app outreach notification for a named request or the general human team."""
    settings = get_settings()
    matched = requested_agent(requested_text, settings)
    recipient = destination or (matched.get("name") if matched else "Human team")
    if matched:
        assign_lead_to_requested_agent(caller, requested_text, settings)
    from notification_service import notify_human_assignment
    notify_human_assignment(
        channel="voice",
        customer=caller or "caller",
        recipient=recipient,
        call_session_id=session_id or None,
        message=(message or f"A caller asked to speak with {recipient}. Please follow up.")
                + (f" Caller email: {contact_email}." if contact_email else ""),
    )
    if not notify_only:
        logger.info("call forwarding: outreach created for %s (%s)", recipient, session_id)
    return recipient


def agent_accepted(parent_call_sid: str, agent_number: str) -> None:
    """An agent pressed 1: remember who took the call."""
    name = next((agent["name"] for agent in get_settings()["agents"] if agent["phone"] == agent_number), agent_number)
    _record(parent_call_sid, {"transfer_status": "connected", "transfer_destination": name, "handled_by": name,
                              "transfer_succeeded": True, "status": "transferred", "accepted_by": name})
    _timeline(parent_call_sid, "", f"Connected to {name}")
    logger.info("call forwarding: %s accepted by %s", parent_call_sid, name)


def dial_finished(call_sid: str, dial_status: str, caller: str, dial_call_sid: str = "", dial_duration: str = "") -> bool:
    """Record a completed bridge, or request a callback when nobody answered.

    Old press-1 transfers still require the screen-result webhook to have marked them connected.
    """
    status = str(dial_status or "").lower()
    leg = {"human_call_status": status, "human_call_ended_at": datetime.utcnow()}
    if dial_call_sid:
        leg["human_call_sid"] = dial_call_sid
    if str(dial_duration).isdigit():
        leg["human_call_duration"] = int(dial_duration)
    call = _find_call(call_sid)
    if call and call.get("transfer_mode") == MODE:
        try:
            from routers.twilio_api import _get_twilio_client
            client = _get_twilio_client()
            legs = client.calls.list(parent_call_sid=call_sid, limit=MAX_AGENTS) if client else []
            by_number = {str(leg.to): leg for leg in legs}
            targets = call.get("transfer_targets") or [
                {"name": agent["name"], "phone": agent["phone"]} for agent in active_agents(get_settings())
            ]
            targets = [
                {**target, "status": str(by_number[target["phone"]].status), "call_sid": str(by_number[target["phone"]].sid)}
                if target["phone"] in by_number else {**target, "status": "not-started"}
                for target in targets
            ]
            leg["transfer_targets"] = targets
        except Exception as error:
            logger.warning("call forwarding: could not fetch team call legs for %s: %s", call_sid, error)
    if call and call.get("transfer_status") in {"connected", "completed"}:
        _record(call_sid, {"transfer_status": "completed", **leg})
        return True
    if call and call.get("transfer_mode") == MODE and status == "completed":
        name = "Human team"
        if dial_call_sid:
            try:
                from routers.twilio_api import _get_twilio_client
                client = _get_twilio_client()
                answered_number = str(client.calls(dial_call_sid).fetch().to or "") if client else ""
                name = next((agent["name"] for agent in get_settings()["agents"] if agent["phone"] == answered_number), name)
            except Exception as error:
                logger.warning("call forwarding: could not identify answering agent for %s: %s", call_sid, error)
        _record(call_sid, {"transfer_status": "completed", "transfer_destination": name, "handled_by": name, **leg,
                           "accepted_by": name, "transfer_succeeded": True, "status": "transferred", "transfer_error": None})
        _timeline(call_sid, caller, f"Connected to {name}")
        logger.info("call forwarding: %s connected to %s", call_sid, name)
        return True
    _record(call_sid, {"transfer_status": "no-answer" if status in {"", "completed", "answered"} else status, "transfer_succeeded": False, **leg,
                       "transfer_destination": "Callback queue", "human_callback_requested": True, "status": "transfer_requested",
                       "transfer_error": f"No team member accepted ({status or 'no-answer'})"})
    _timeline(call_sid, caller, "No team member was free: the caller was told the team will call back")
    try:
        from notification_service import notify_human_assignment
        number = caller or (call or {}).get("phone_number") or ""
        destination = str((call or {}).get("transfer_destination") or "Human team")
        notify_human_assignment(channel="voice", customer=number, recipient=destination, call_session_id=call_sid,
                                message=f"{number or 'A caller'} asked to speak with {destination} but nobody was free: please call them back.")
    except Exception as error:
        logger.warning("call forwarding: could not notify the team about %s: %s", call_sid, error)
    logger.info("call forwarding: %s not accepted (%s); callback requested", call_sid, status)
    return False


def _find_call(call_sid: str) -> Optional[Dict[str, Any]]:
    """The stored call for a Twilio call: custom calls are stored under it, Retell calls under Retell's id with the
    Twilio one saved when the transfer started (twilio_call_sid)."""
    if not mongo_client.is_connected() or not call_sid:
        return None
    found = mongo_client.calls.find_one({"$or": [{"call_session_id": call_sid}, {"twilio_call_sid": call_sid}]})
    if found:
        return found
    retell_id = next((key for key, value in _RETELL_CALLS.items() if value.get("call_sid") == call_sid), "")
    return mongo_client.calls.find_one({"call_session_id": retell_id}) if retell_id else None


def _record(call_sid: str, fields: Dict[str, Any], session_id: str = "") -> None:
    if not mongo_client.is_connected() or not call_sid:
        return
    found = _find_call(call_sid)
    where = {"call_session_id": session_id} if session_id else ({"_id": found["_id"]} if found.get("_id") else {"call_session_id": found["call_session_id"]}) if found else None
    if where is None:
        return
    mongo_client.calls.update_one(where, {"$set": {**fields, "updated_at": datetime.utcnow()}})
    try:
        import realtime
        realtime.publish("call", key=call_sid)
    except Exception:
        pass


def _timeline(call_sid: str, caller: str, text: str) -> None:
    """A line on the caller's conversation timeline (the inbox shows it between the calls)."""
    if not mongo_client.is_connected():
        return
    try:
        from routers import inbox_api
        call = _find_call(call_sid) or ({"_id": call_sid, "phone_number": caller} if caller else None)
        if not call:
            return
        key = inbox_api._thread_key(call)
        mongo_client.inbox_threads.update_one({"_id": key}, {"$push": {"events": {"type": "transfer", "text": text, "by_name": "AI assistant",
                                                                                  "at": datetime.utcnow()}}}, upsert=True)
    except Exception as error:
        logger.debug("call forwarding: timeline note skipped: %s", error)
