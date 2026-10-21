"""Words the speech recogniser should listen for, taken from the business knowledge (AI Brain).

Speech recognition mishears names and product words it does not expect ("Home DFO" -> "Home Depot", "sink" -> "sync").
Both Deepgram (keyterm prompting) and Retell (boosted keywords) can be told which words to expect. Instead of a
hand-written list per business, the list is extracted from the knowledge whenever it changes:

- refresh_keyterms(): one model call reads the knowledge and picks the business, brand, product, series, category
  and place names a caller is likely to say (plain-word fallback if the model fails). Stored with the knowledge
  version in platform_settings, then pushed to the Retell agent when the list changed.
- current_keyterms(): the stored list (memory-cached, never waits on the model), plus any extra terms from .env.
  A knowledge version newer than the list starts a refresh in the background.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections import Counter
from typing import Any, Dict, List

from mongo_client import mongo_client

logger = logging.getLogger(__name__)

SETTING_KEY = "voice_keyterms"
_REFRESH_LOCK = threading.Lock()
_STOP_WORDS = {"the", "and", "for", "with", "our", "your", "you", "are", "can", "all", "any", "from", "this", "that", "will",
               "have", "has", "not", "but", "per", "via", "into", "over", "more", "most", "also", "only", "each", "yes", "no",
               "what", "how", "when", "where", "who", "why", "which", "do", "does", "is", "it", "we", "a", "an", "of", "to",
               "in", "on", "at", "by", "or", "if", "as", "be", "free", "new", "home", "page", "terms", "policy", "privacy",
               "contact", "about", "shop", "faq", "faqs", "services", "products", "australia", "australian"}


def _limit() -> int:
    try:
        return max(10, min(100, int(os.getenv("VOICE_KEYTERMS_LIMIT", "60"))))
    except ValueError:
        return 60


def _extra_terms() -> List[str]:
    """Hand-added terms from .env (VOICE_KEYTERMS_EXTRA; CUSTOM_DEEPGRAM_KEYTERMS is read too, for older setups)."""
    raw = ",".join(os.getenv(name, "") for name in ("VOICE_KEYTERMS_EXTRA", "CUSTOM_DEEPGRAM_KEYTERMS"))
    return [term.strip() for term in raw.split(",") if term.strip()]


def _clean(terms: List[Any]) -> List[str]:
    seen, out = set(), []
    for term in terms:
        term = re.sub(r"\s+", " ", str(term or "")).strip(" .,;:-'\"")
        if not term or len(term) > 40 or len(term.split()) > 3 or not re.search(r"[A-Za-z]", term) or re.search(r"[/@]|www\.|\.com", term):
            continue
        if term.lower() in seen or term.lower() in _STOP_WORDS:
            continue
        seen.add(term.lower())
        out.append(term)
    return out


def _knowledge_text(max_chars: int = 60000) -> str:
    parts, size = [], 0
    for item in mongo_client.brain_documents.find({}, {"text": 1, "source_name": 1}).limit(5000):
        text = f"{item.get('source_name') or ''}\n{item.get('text') or ''}".strip()
        if size + len(text) > max_chars:
            break
        parts.append(text)
        size += len(text)
    return "\n\n".join(parts)


def _model_terms(knowledge: str, limit: int) -> List[str]:
    from ai_services import AIServices
    prompt = (
        "Below is a business's knowledge base. Callers phone this business and speech recognition turns their words into "
        "text. Pick the words callers are likely to SAY that speech recognition could get wrong, in four lists (at most 40, 20, 10 and 15 items):\n"
        "- product_types: EVERY distinct kind of product the business sells, each as a short singular noun the way a "
        "caller says it (a furniture store would list chair, table, sofa, bed, wardrobe, ...). Go through the knowledge "
        "line by line and leave none out; two similar products (for example a cup and a mug) are listed separately.\n"
        "- names: the business name, then brand, product and series names.\n"
        "- places: suburbs, streets, cities and other place names.\n"
        "- other: other unusual words or short phrases (1 to 3 words).\n"
        "Skip prices, numbers, URLs and email addresses.\n\n"
        f"Knowledge:\n{knowledge}\n\n"
        'Answer with JSON only: {"product_types": [], "names": [], "places": [], "other": []}'
    )
    services = AIServices()
    response = services.openai_client.chat.completions.create(
        model=os.getenv("VOICE_KEYTERMS_MODEL", os.getenv("CALL_STATUS_MODEL", "openai/gpt-4.1-mini")).strip(),
        messages=[{"role": "user", "content": prompt}], max_tokens=2000, temperature=0,
        response_format={"type": "json_object"},
    )
    result = json.loads(response.choices[0].message.content or "{}")
    names, products = list(result.get("names") or []), list(result.get("product_types") or [])
    places, other = list(result.get("places") or []), list(result.get("other") or [])
    # Business name, the main product words, then brand/series names and places, then longer product variants.
    # Deepgram takes the first 50, so the most important of every kind come first.
    return [*names[:1], *products[:25], *names[1:20], *places[:8], *products[25:], *other][:limit]


def _plain_terms(knowledge: str, limit: int) -> List[str]:
    """Fallback without a model: capitalised names that appear in the knowledge more than once and never in lower
    case (so ordinary words that start a sentence, like "The", are left out)."""
    lower = set(re.findall(r"\b[a-z][a-z]+\b", knowledge))
    ordinary = lambda word: word.lower() in lower or word.lower() in _STOP_WORDS
    names: Counter = Counter()
    for match in re.finditer(r"\b[A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+){0,2}\b", knowledge):
        words = match.group(0).split()
        while words and ordinary(words[0]):  # "The Flora" -> "Flora"
            words.pop(0)
        if words:
            names[" ".join(words)] += 1
    return [name for name, count in names.most_common(limit * 3) if count > 1 and not all(ordinary(word) for word in name.split())][:limit]


def _version() -> int:
    from brain_service import _brain_version
    return _brain_version()


def refresh_keyterms(force: bool = False) -> List[str]:
    """Rebuild the list from the knowledge (if the knowledge changed), store it, and update the Retell agent."""
    if not mongo_client.is_connected():
        return []
    with _REFRESH_LOCK:
        version = _version()
        stored = mongo_client.platform_settings.find_one({"key": SETTING_KEY}) or {}
        if not force and stored.get("version") == version and stored.get("terms"):
            return list(stored["terms"])
        knowledge = _knowledge_text()
        limit = _limit()
        terms: List[str] = []
        source = "model"
        if knowledge:
            try:
                terms = _clean(_model_terms(knowledge, limit))[:limit]
            except Exception as error:
                logger.warning("voice key terms: model extraction failed (%s); using plain extraction", error)
            if not terms:
                terms, source = _clean(_plain_terms(knowledge, limit))[:limit], "plain"
        mongo_client.platform_settings.update_one(
            {"key": SETTING_KEY}, {"$set": {"key": SETTING_KEY, "version": version, "terms": terms, "source": source}}, upsert=True)
        _forget_cached()
        logger.info("voice key terms: %d terms from knowledge version %s (%s)", len(terms), version, source)
        if terms != list(stored.get("terms") or []):
            sync_retell(terms)
        return terms


def _merged(terms: List[str]) -> List[str]:
    return _clean([*_extra_terms(), *terms])[:100]


def sync_retell(terms: List[str]) -> bool:
    """Set the Retell agent's boosted keywords to the list and publish it (VOICE_KEYTERMS_SYNC_RETELL=false: off)."""
    if os.getenv("VOICE_KEYTERMS_SYNC_RETELL", "true").strip().lower() in {"0", "false", "no", "off"}:
        return False
    key, agent_id = os.getenv("RETELL_API_KEY", "").strip(), os.getenv("RETELL_AGENT_ID", "").strip()
    if not key or not agent_id:
        return False
    import httpx
    headers = {"Authorization": f"Bearer {key}"}
    try:
        httpx.patch(f"https://api.retellai.com/update-agent/{agent_id}", headers=headers, timeout=20,
                    json={"boosted_keywords": _merged(terms)}).raise_for_status()
        httpx.post(f"https://api.retellai.com/publish-agent/{agent_id}", headers=headers, timeout=20).raise_for_status()
        logger.info("voice key terms: Retell agent boosted keywords updated (%d)", len(_merged(terms)))
        return True
    except Exception as error:
        logger.warning("voice key terms: Retell update failed: %s", error)
        return False


def _forget_cached() -> None:
    """Drop the memory copy so the next call reads the new list at once."""
    import brain_service
    with brain_service._TTL_LOCK:
        brain_service._TTL_CACHE.pop("voice_keyterms", None)


def schedule_refresh(force: bool = False) -> None:
    threading.Thread(target=lambda: _safe_refresh(force), name="voice-keyterms", daemon=True).start()


def _safe_refresh(force: bool) -> None:
    try:
        refresh_keyterms(force)
    except Exception as error:
        logger.warning("voice key terms: refresh failed: %s", error)


def current_keyterms() -> List[str]:
    """Terms for this call: the stored list plus the .env extras. Never waits on the model; a newer knowledge
    version starts a background refresh and this call uses the previous list."""
    from brain_service import _swr_cached
    try:
        stored = _swr_cached("voice_keyterms", 30, lambda: (mongo_client.platform_settings.find_one({"key": SETTING_KEY}) or {}) if mongo_client.is_connected() else {})
        if stored.get("version") != _version() and not _REFRESH_LOCK.locked():
            schedule_refresh()
        return _merged(list(stored.get("terms") or []))
    except Exception as error:
        logger.debug("voice key terms unavailable: %s", error)
        return _merged([])


def business_name() -> str:
    """The business's name as the key-term extraction found it (its first term), for spotting mishearings of it."""
    from brain_service import _swr_cached
    try:
        stored = _swr_cached("voice_keyterms", 30, lambda: (mongo_client.platform_settings.find_one({"key": SETTING_KEY}) or {}) if mongo_client.is_connected() else {})
        terms = list(stored.get("terms") or [])
        return str(terms[0]) if terms else ""
    except Exception:
        return ""
