"""Administrator controls for enabled inbound voice providers."""

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth_service import require_permission
from voice_provider_settings import SUPPORTED_PROVIDERS, provider_snapshot, set_provider_enabled


router = APIRouter(prefix="/api/voice-providers", tags=["voice providers"])


class ProviderTogglePayload(BaseModel):
    enabled: bool


@router.get("")
async def list_voice_providers(user: Dict[str, Any] = Depends(require_permission("manage_integrations"))):
    return {"success": True, "data": {"providers": list(provider_snapshot().values())}}


@router.put("/{provider}")
async def update_voice_provider(
    provider: str,
    payload: ProviderTogglePayload,
    user: Dict[str, Any] = Depends(require_permission("manage_integrations")),
):
    provider = provider.strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(status_code=404, detail="Unknown voice provider")
    try:
        set_provider_enabled(provider, payload.enabled, str(user.get("_id", "")))
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    snapshot = provider_snapshot()
    return {"success": True, "data": snapshot[provider], "providers": list(snapshot.values())}
