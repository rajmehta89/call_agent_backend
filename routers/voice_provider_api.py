"""Administrator controls for enabled inbound voice providers."""

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth_service import require_permission
from voice_languages import language_options, set_enabled_language_codes
from voice_provider_settings import SUPPORTED_PROVIDERS, provider_snapshot, set_provider_enabled


router = APIRouter(prefix="/api/voice-providers", tags=["voice providers"])


class ProviderTogglePayload(BaseModel):
    enabled: bool


class LanguageSettingsPayload(BaseModel):
    enabled_languages: list[str]


@router.get("")
def list_voice_providers(user: Dict[str, Any] = Depends(require_permission("manage_integrations"))):
    return {"success": True, "data": {"providers": list(provider_snapshot().values())}}


@router.get("/languages")
def list_voice_languages(user: Dict[str, Any] = Depends(require_permission("manage_integrations"))):
    return {"success": True, "data": {"languages": language_options()}}


@router.put("/languages")
def update_voice_languages(
    payload: LanguageSettingsPayload,
    user: Dict[str, Any] = Depends(require_permission("manage_integrations")),
):
    try:
        set_enabled_language_codes(payload.enabled_languages, str(user.get("_id", "")))
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return {"success": True, "data": {"languages": language_options()}}


@router.put("/{provider}")
def update_voice_provider(
    provider: str,
    payload: ProviderTogglePayload,
    user: Dict[str, Any] = Depends(require_permission("manage_integrations")),
):
    provider = provider.strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(status_code=404, detail="Unknown voice provider")
    if payload.enabled:
        missing = provider_snapshot()[provider]["missing_configuration"]
        if missing:
            raise HTTPException(status_code=400, detail="Configure this provider before enabling it: " + ", ".join(missing))
    try:
        set_provider_enabled(provider, payload.enabled, str(user.get("_id", "")))
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    snapshot = provider_snapshot()
    return {"success": True, "data": snapshot[provider], "providers": list(snapshot.values())}
