"""Runtime-safe provider controls shared by the voice routes and dashboard."""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from typing import Any, Dict

from mongo_client import mongo_client


SETTINGS_KEY = "voice_provider_controls"
# "custom" and "custom_deepgram" are the same Twilio + Deepgram + AI pipeline with a different voice:
# ElevenLabs, or Deepgram Aura-2 (cheaper; no ElevenLabs account needed).
SUPPORTED_PROVIDERS = {"retell", "custom", "custom_deepgram"}


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _env_default(provider: str) -> bool:
    if provider == "retell":
        return _as_bool(os.getenv("RETELL_CUSTOM_LLM_ENABLED"), False)
    if provider == "custom_deepgram":
        return False
    return _as_bool(os.getenv("CUSTOM_TWILIO_VOICE_ENABLED"), False)


def custom_stack_enabled() -> bool:
    """True when either custom pipeline variant (ElevenLabs or Deepgram voice) is switched on."""
    return provider_enabled("custom") or provider_enabled("custom_deepgram")


def custom_voice_engine() -> str:
    """Which text-to-speech the custom pipeline speaks with: "deepgram" or "elevenlabs"."""
    return "deepgram" if provider_enabled("custom_deepgram") else "elevenlabs"


def deepgram_voice(language: str = "en") -> str:
    """Return the configured Deepgram voice/model for a call language.

    Aura-2 is used for English. Urdu has a supported Flux model by default;
    Arabic falls back to the configured English Aura model because Deepgram's
    current Flux TTS catalog does not list an Arabic voice.
    """
    code = str(language or "en").strip().lower()
    variable = {"ar": "DEEPGRAM_TTS_VOICE_AR", "ur": "DEEPGRAM_TTS_VOICE_UR"}.get(code, "DEEPGRAM_TTS_VOICE")
    configured = os.getenv(variable, "").strip()
    if configured:
        return configured
    if code == "ur":
        return "flux-ambreen-ur"
    return os.getenv("DEEPGRAM_TTS_VOICE", "aura-2-theia-en").strip() or "aura-2-theia-en"


def _load_controls() -> Dict[str, bool]:
    if not mongo_client or not mongo_client.is_connected():
        return {}
    document = mongo_client.platform_settings.find_one({"key": SETTINGS_KEY}) or {}
    value = document.get("value") or {}
    return {provider: _as_bool(value[provider]) for provider in SUPPORTED_PROVIDERS if provider in value}


# Which provider is switched on is read several times while a call is being set up, before the caller hears the
# greeting; each database read can take 0.4-2 s on a slow connection. Kept in memory: refreshed in the background
# every CACHE_SECONDS, and updated at once when a provider is switched here.
_CACHE: Dict[str, Any] = {"value": None, "at": 0.0, "refreshing": False}
_CACHE_LOCK = threading.Lock()
CACHE_SECONDS = 15


def _refresh_controls() -> None:
    try:
        value = _load_controls()
        with _CACHE_LOCK:
            _CACHE.update(value=value, at=time.monotonic())
    except Exception:
        pass
    finally:
        with _CACHE_LOCK:
            _CACHE["refreshing"] = False


def _saved_controls() -> Dict[str, bool]:
    with _CACHE_LOCK:
        value, age, refreshing = _CACHE["value"], time.monotonic() - _CACHE["at"], _CACHE["refreshing"]
        if value is not None and age >= CACHE_SECONDS and not refreshing:
            _CACHE["refreshing"] = True
            threading.Thread(target=_refresh_controls, name="voice-provider-controls", daemon=True).start()
    if value is not None:
        return value
    _refresh_controls()  # first use only
    with _CACHE_LOCK:
        return _CACHE["value"] or {}


def provider_enabled(provider: str) -> bool:
    """Return a workspace override when one exists, otherwise the env default."""
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(f"Unsupported voice provider: {provider}")
    return _saved_controls().get(provider, _env_default(provider))


def set_provider_enabled(provider: str, enabled: bool, updated_by: str = "") -> None:
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(f"Unsupported voice provider: {provider}")
    if not mongo_client or not mongo_client.is_connected():
        raise RuntimeError("Database not connected")
    values = {f"value.{provider}": bool(enabled)}
    if enabled:
        # Only one provider may be live at a time: turning one on turns the others off.
        values.update({f"value.{other}": False for other in SUPPORTED_PROVIDERS if other != provider})
    mongo_client.platform_settings.update_one(
        {"key": SETTINGS_KEY},
        {
            "$set": {
                **values,
                "updated_at": datetime.utcnow(),
                "updated_by": updated_by,
            },
            "$setOnInsert": {"created_at": datetime.utcnow()},
        },
        upsert=True,
    )
    _refresh_controls()  # the next call uses the new choice at once


def provider_snapshot() -> Dict[str, Dict[str, Any]]:
    """Safe provider data for the control page. Never expose credentials."""
    custom_missing = [
        name
        for name, value in {
            "TWILIO_ACCOUNT_SID": os.getenv("TWILIO_ACCOUNT_SID", "").strip(),
            "TWILIO_AUTH_TOKEN": os.getenv("TWILIO_AUTH_TOKEN", "").strip(),
            "TWILIO_PHONE_NUMBER": (os.getenv("TWILIO_PHONE_NUMBER") or os.getenv("TWILIO_CALLER_ID") or "").strip(),
            "TWILIO_PUBLIC_BASE_URL": os.getenv("TWILIO_PUBLIC_BASE_URL", "").strip(),
            "DEEPGRAM_API_KEY": os.getenv("DEEPGRAM_API_KEY", "").strip(),
            "ELEVENLABS_API_KEY": os.getenv("ELEVENLABS_API_KEY", "").strip(),
            "ELEVENLABS_VOICE_ID": os.getenv("ELEVENLABS_VOICE_ID", "").strip(),
        }.items()
        if not value
    ]
    twilio_deepgram_missing = [name for name in custom_missing if not name.startswith("ELEVENLABS_")]
    retell_missing = [
        name for name in ("RETELL_API_KEY", "RETELL_AGENT_ID", "OPENROUTER_API_KEY", "RETELL_CUSTOM_LLM_SECRET") if not os.getenv(name, "").strip()
    ]
    return {
        "retell": {
            "id": "retell",
            "name": "Retell AI",
            "description": "Managed telephony, speech, and Custom LLM bridge.",
            "enabled": provider_enabled("retell"),
            "ready": not retell_missing,
            "missing_configuration": retell_missing,
            "model": os.getenv("RETELL_OPENROUTER_MODEL", "openai/gpt-oss-20b"),
            "activation_note": "When on, calls to your Twilio number are passed to this Retell agent. Active calls are not interrupted.",
        },
        "custom": {
            "id": "custom",
            "name": "Custom Twilio voice stack",
            "description": "Twilio Media Streams with Deepgram transcription, your AI/knowledge base, and ElevenLabs voice.",
            "enabled": provider_enabled("custom"),
            "ready": not custom_missing,
            "missing_configuration": custom_missing,
            "provider": "twilio + deepgram + elevenlabs",
            "activation_note": "Applies to new calls sent to the custom Twilio webhook. When off or not ready, calls fall back to the default Twilio AI agent. Active calls are not interrupted.",
        },
        "custom_deepgram": {
            "id": "custom_deepgram",
            "name": "Custom voice stack — Deepgram voice",
            "description": f"The same custom pipeline, speaking with Deepgram Aura-2 ({deepgram_voice()}) instead of ElevenLabs. Lowest cost per minute.",
            "enabled": provider_enabled("custom_deepgram"),
            "ready": not twilio_deepgram_missing,
            "missing_configuration": twilio_deepgram_missing,
            "provider": "twilio + deepgram (speech-to-text and voice)",
            "activation_note": "Applies to new calls sent to the custom Twilio webhook. Turning it on turns the ElevenLabs custom stack and Retell off. Active calls are not interrupted.",
        },
    }
