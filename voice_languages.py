"""Supported lead languages and the small amount of call-routing glue around them."""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from mongo_client import mongo_client


DEFAULT_LANGUAGE = "en"
SUPPORTED_LANGUAGES: Dict[str, Dict[str, str]] = {
    "en": {"label": "English", "native_label": "English", "locale": "en-US"},
    "ar": {"label": "Arabic", "native_label": "العربية", "locale": "ar-SA"},
    "ur": {"label": "Urdu", "native_label": "اردو", "locale": "ur-PK"},
}

_ALIASES = {
    "english": "en", "en-us": "en", "en-in": "en", "en-gb": "en", "en": "en",
    "arabic": "ar", "العربية": "ar", "ar-sa": "ar", "ar-ae": "ar", "ar": "ar",
    "urdu": "ur", "اردو": "ur", "ur-pk": "ur", "ur-in": "ur", "ur": "ur",
}


def normalize_language(value: Any) -> str:
    """Return one of the stable CRM language codes, defaulting safely to English."""
    key = str(value or "").strip().lower()
    return _ALIASES.get(key, DEFAULT_LANGUAGE)


def language_config(value: Any) -> Dict[str, str]:
    code = normalize_language(value)
    return {"code": code, **SUPPORTED_LANGUAGES[code]}


def language_label(value: Any) -> str:
    return language_config(value)["label"]


def language_greeting(value: Any, configured_english: str) -> str:
    """Keep the configured English greeting, with safe multilingual defaults for the other choices.

    Teams can override these with VOICE_GREETING_AR / VOICE_GREETING_UR without changing code.
    """
    code = normalize_language(value)
    if code == "en":
        return configured_english
    import os

    if code == "ar":
        return os.getenv("VOICE_GREETING_AR", "مرحبًا، هذا مساعد راج الذكي. كيف يمكنني مساعدتك اليوم?").strip()
    return os.getenv("VOICE_GREETING_UR", "السلام علیکم، یہ راج کا AI معاون ہے۔ میں آج آپ کی کیسے مدد کر سکتا ہوں؟").strip()


def language_for_phone(phone: Optional[str]) -> str:
    """Read a lead's saved language for a phone caller, without making call setup fragile."""
    if not phone or not mongo_client.is_connected():
        return DEFAULT_LANGUAGE
    raw = str(phone).strip()
    candidates = {raw}
    digits = re.sub(r"\D", "", raw)
    if digits:
        candidates.update({f"+{digits}", digits})
    try:
        lead = mongo_client.leads.find_one({"phone": {"$in": list(candidates)}}, {"preferred_language": 1}) or {}
        return normalize_language(lead.get("preferred_language"))
    except Exception:
        return DEFAULT_LANGUAGE


def language_for_call(form: Dict[str, Any]) -> str:
    """Prefer browser/test-call metadata, then fall back to the saved phone lead."""
    explicit = str(form.get("caller_language") or form.get("preferred_language") or "").strip()
    if explicit:
        return normalize_language(explicit)
    return language_for_phone(str(form.get("From") or ""))
