"""Supported lead languages and the small amount of call-routing glue around them."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from mongo_client import mongo_client


DEFAULT_LANGUAGE = "en"
LANGUAGE_SETTINGS_KEY = "voice_supported_languages"
SUPPORTED_LANGUAGES: Dict[str, Dict[str, str]] = {
    "en": {"label": "English", "native_label": "English", "locale": "en-US"},
    "ar": {"label": "Arabic", "native_label": "العربية", "locale": "ar-SA"},
    "ur": {"label": "Urdu", "native_label": "اردو", "locale": "ur-PK"},
}
DEFAULT_ENABLED_LANGUAGES = tuple(SUPPORTED_LANGUAGES.keys())

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


def enabled_language_codes() -> List[str]:
    """Read the workspace language switches, defaulting to all built-in languages."""
    if not mongo_client or not mongo_client.is_connected():
        return list(DEFAULT_ENABLED_LANGUAGES)
    try:
        document = mongo_client.platform_settings.find_one({"key": LANGUAGE_SETTINGS_KEY}) or {}
        raw = (document.get("value") or {}).get("enabled_languages")
        if not isinstance(raw, list):
            return list(DEFAULT_ENABLED_LANGUAGES)
        enabled = [code for code in DEFAULT_ENABLED_LANGUAGES if code in {normalize_language(item) for item in raw}]
        return enabled or [DEFAULT_LANGUAGE]
    except Exception:
        return list(DEFAULT_ENABLED_LANGUAGES)


def language_enabled(value: Any) -> bool:
    return normalize_language(value) in enabled_language_codes()


def effective_language(value: Any) -> str:
    """Use the requested language when enabled, otherwise the first enabled language."""
    code = normalize_language(value)
    enabled = enabled_language_codes()
    return code if code in enabled else enabled[0]


def language_options() -> List[Dict[str, Any]]:
    enabled = set(enabled_language_codes())
    return [{"code": code, **language_config(code), "enabled": code in enabled} for code in SUPPORTED_LANGUAGES]


def set_enabled_language_codes(values: Iterable[Any], updated_by: str = "") -> List[str]:
    enabled = [code for code in DEFAULT_ENABLED_LANGUAGES if code in {normalize_language(item) for item in values}]
    if not enabled:
        raise ValueError("At least one call language must remain enabled")
    if not mongo_client or not mongo_client.is_connected():
        raise RuntimeError("Database not connected")
    mongo_client.platform_settings.update_one(
        {"key": LANGUAGE_SETTINGS_KEY},
        {"$set": {"value": {"enabled_languages": enabled}, "updated_at": datetime.utcnow(), "updated_by": updated_by},
         "$setOnInsert": {"created_at": datetime.utcnow()}},
        upsert=True,
    )
    return enabled


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
        return effective_language(lead.get("preferred_language"))
    except Exception:
        return DEFAULT_LANGUAGE


def language_for_call(form: Dict[str, Any]) -> str:
    """Prefer browser/test-call metadata, then fall back to the saved phone lead."""
    explicit = str(form.get("caller_language") or form.get("preferred_language") or "").strip()
    if explicit:
        return effective_language(explicit)
    return language_for_phone(str(form.get("From") or ""))
