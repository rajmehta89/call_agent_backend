"""Runtime-safe provider controls shared by the voice routes and dashboard."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict

from mongo_client import mongo_client


SETTINGS_KEY = "voice_provider_controls"
SUPPORTED_PROVIDERS = {"retell", "custom"}


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _env_default(provider: str) -> bool:
    if provider == "retell":
        return _as_bool(os.getenv("RETELL_CUSTOM_LLM_ENABLED"), False)
    return _as_bool(os.getenv("CUSTOM_TWILIO_VOICE_ENABLED"), False)


def _saved_controls() -> Dict[str, bool]:
    if not mongo_client or not mongo_client.is_connected():
        return {}
    document = mongo_client.platform_settings.find_one({"key": SETTINGS_KEY}) or {}
    value = document.get("value") or {}
    return {provider: _as_bool(value[provider]) for provider in SUPPORTED_PROVIDERS if provider in value}


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
    retell_missing = [
        name for name in ("RETELL_API_KEY", "RETELL_AGENT_ID", "OPENROUTER_API_KEY") if not os.getenv(name, "").strip()
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
    }
