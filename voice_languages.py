"""Supported lead languages and the small amount of call-routing glue around them."""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from mongo_client import mongo_client


DEFAULT_LANGUAGE = "en"
LANGUAGE_SETTINGS_KEY = "voice_supported_languages"
# "also_called": what callers may name the language when asking for it. Spoken Urdu and Hindi are understood by the
# same callers, so a caller asking for Hindi is answered in Urdu.
SUPPORTED_LANGUAGES: Dict[str, Dict[str, Any]] = {
    "en": {"label": "English", "native_label": "English", "locale": "en-US", "also_called": []},
    "ar": {"label": "Arabic", "native_label": "العربية", "locale": "ar-SA", "also_called": []},
    "ur": {"label": "Urdu", "native_label": "اردو", "locale": "ur-PK", "also_called": ["Hindi"]},
}
# The switches are read on every voice turn (the language rule); a short cache keeps that off the database.
_ENABLED_CACHE_SECONDS = 30.0
_enabled_cache: Dict[str, Any] = {"codes": None, "at": 0.0}
_enabled_lock = threading.Lock()
DEFAULT_ENABLED_LANGUAGES = tuple(SUPPORTED_LANGUAGES.keys())

_ALIASES = {
    "english": "en", "en-us": "en", "en-in": "en", "en-gb": "en", "en": "en",
    "arabic": "ar", "العربية": "ar", "ar-sa": "ar", "ar-ae": "ar", "ar": "ar",
    "urdu": "ur", "اردو": "ur", "ur-pk": "ur", "ur-in": "ur", "ur": "ur",
    # Spoken Hindi and Urdu are the same for callers: speech recognition reporting Hindi ("hi") answers in Urdu.
    "hindi": "ur", "हिन्दी": "ur", "हिंदी": "ur", "hi": "ur", "hi-in": "ur",
}


def normalize_language(value: Any) -> str:
    """Return one of the stable CRM language codes, defaulting safely to English."""
    key = str(value or "").strip().lower()
    return _ALIASES.get(key, DEFAULT_LANGUAGE)


def spoken_language(value: Any) -> str:
    """The CRM language for a language code from speech recognition, or "" for one we don't serve."""
    return _ALIASES.get(str(value or "").strip().lower(), "")


# Letters Urdu writes and Arabic does not (ٹ ڈ ڑ ں ے ہ ھ ک گ پ چ ژ ی): Arabic writes ك ي ه, Urdu ک ی ہ.
_URDU_LETTERS = frozenset("ٹڈڑںےہھکگپچژی")


def text_language(text: Any) -> str:
    """Which enabled language a caller's words are written in, from their alphabet (Retell gives no language per
    turn): Hindi (Devanagari) script = Urdu; Arabic script with Urdu's own letters = Urdu, without = Arabic; Latin =
    English. The script only, never the words' meaning. "" when there are no letters."""
    devanagari = arabic = latin = 0
    urdu = False
    for character in str(text or ""):
        if "ऀ" <= character <= "ॿ":
            devanagari += 1
        elif "؀" <= character <= "ۿ":
            arabic += 1
            urdu = urdu or character in _URDU_LETTERS
        elif character.isascii() and character.isalpha():
            latin += 1
    if not (devanagari or arabic or latin):
        return ""
    if latin >= devanagari + arabic:
        code = "en"
    else:
        code = "ur" if devanagari >= arabic or urdu else "ar"
    return code if code in enabled_language_codes() else DEFAULT_LANGUAGE


def language_config(value: Any) -> Dict[str, str]:
    code = normalize_language(value)
    return {"code": code, **SUPPORTED_LANGUAGES[code]}


def language_label(value: Any) -> str:
    return language_config(value)["label"]


def enabled_language_codes() -> List[str]:
    """The workspace language switches (cached for a few seconds), defaulting to all built-in languages."""
    with _enabled_lock:
        if _enabled_cache["codes"] is not None and time.monotonic() - _enabled_cache["at"] < _ENABLED_CACHE_SECONDS:
            return list(_enabled_cache["codes"])
    codes = _read_enabled_language_codes()
    with _enabled_lock:
        _enabled_cache.update(codes=list(codes), at=time.monotonic())
    return codes


def _read_enabled_language_codes() -> List[str]:
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
    with _enabled_lock:
        _enabled_cache.update(codes=list(enabled), at=time.monotonic())
    return enabled


def _spoken_list(labels: List[str], joiner: str = "or") -> str:
    return labels[0] if len(labels) == 1 else f"{', '.join(labels[:-1])} {joiner} {labels[-1]}"


def language_rule(current: Any = "", follow_caller: bool = True) -> str:
    """The language instruction sent last before every voice reply ("" when only one language is switched on).

    follow_caller=True (Retell: its speech recognition and voice handle every enabled language): reply in the
    language of the caller's latest turn. False (custom stack: recognition and voice are set to one language at a
    time): reply in the call's current language; the call system switches when the caller asks for another one.
    A business-written voice prompt may say "use English"; this rule comes after it and says how both fit together."""
    enabled = enabled_language_codes()
    if len(enabled) < 2:
        return ""
    labels = [language_label(code) for code in enabled]
    script = [language_label(code) for code in enabled if code != "en"]
    script_note = f" Write {_spoken_list(script, 'and')} in {'its' if len(script) == 1 else 'their'} own script, never in Latin letters." if script else ""
    if "ur" in enabled:
        # Speech recognition writes spoken Urdu in Hindi (Devanagari) script; the Urdu voice reads only Urdu script,
        # and a copied Hindi-script word came out wrong (2026-10-29).
        script_note += (" A caller speaking Hindi or Urdu may come through written in Hindi (Devanagari) script: "
                        "understand it, and write every word of your Urdu reply in Urdu script, none in Devanagari.")
    unclear = ("If the caller's words make no sense, they may be speaking another language: say briefly that you did "
               "not catch that and ask which language they prefer, naming {options}.")
    if follow_caller:
        return (f"Language: callers may speak {_spoken_list(labels)}. Reply in the language of the caller's latest turn, "
                "even where the instructions above say English: those describe how to speak English, not a limit. "
                f"If they mix languages, reply in the one they used most in that turn.{script_note} "
                + unclear.format(options=_spoken_list(labels)))
    code = effective_language(current)
    others = [language_label(other) for other in enabled if other != code]
    return (f"Language: this call is in {language_label(code)} right now. Reply only in {language_label(code)}, even where "
            "the instructions above say English: those describe how to speak English, not a limit."
            f"{script_note if code != 'en' else ''} "
            + unclear.format(options=_spoken_list([language_label(code), *others]))
            + " When the caller names a language, the call system switches to it; never say you cannot speak it.")


def requested_language(text: str, current: Any = "") -> str:
    """The enabled language the caller asks to talk in ("Can you speak Urdu?", "English please"), decided by meaning
    (turn_meaning.LANGUAGE_REQUEST); "" when they ask for none, ask for the current one, or the meaning is unclear."""
    from turn_meaning import LANGUAGE_REQUEST, LANGUAGE_REQUEST_LEAD
    ranked = LANGUAGE_REQUEST.rank(text)
    if not ranked:
        return ""
    best, score = ranked[0]
    none_score = next((value for kind, value in ranked if kind == "none"), 0.0)
    if best == "none" or score < LANGUAGE_REQUEST.min_score or score - none_score < LANGUAGE_REQUEST_LEAD:
        return ""
    code = normalize_language(best)
    return code if code != effective_language(current) and language_enabled(code) else ""


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
