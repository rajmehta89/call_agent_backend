"""Verification for requests that come from Twilio rather than a signed-in user.

* HTTP webhooks carry an X-Twilio-Signature header, checked against TWILIO_AUTH_TOKEN.
* Media Stream websockets cannot carry that header through our TwiML, so the TwiML we return
  embeds a short-lived HMAC of the call SID that the websocket checks in its "start" message.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time

from fastapi import HTTPException, Request
from twilio.request_validator import RequestValidator

logger = logging.getLogger(__name__)
STREAM_TOKEN_TTL_SECONDS = 300


def validate_production_voice_security() -> None:
    """Do not start a public voice deployment with verification silently disabled."""
    environment = (os.getenv("APP_ENV") or os.getenv("ENVIRONMENT") or "development").lower()
    if environment not in {"production", "prod"}:
        return
    if not _enabled() or not os.getenv("TWILIO_AUTH_TOKEN", "").strip():
        raise RuntimeError("Production voice requires TWILIO_AUTH_TOKEN and TWILIO_VALIDATE_SIGNATURES=true")
    from voice_provider_settings import provider_enabled
    if provider_enabled("retell") and not os.getenv("RETELL_CUSTOM_LLM_SECRET", "").strip():
        raise RuntimeError("The enabled Retell provider requires RETELL_CUSTOM_LLM_SECRET")


def _enabled() -> bool:
    return os.getenv("TWILIO_VALIDATE_SIGNATURES", "true").strip().lower() not in {"0", "false", "no", "off"}


def public_request_url(request: Request) -> str:
    """The URL Twilio actually called, as seen through ngrok or another proxy."""
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc).split(",")[0].strip()
    query = f"?{request.url.query}" if request.url.query else ""
    return f"{proto}://{host}{request.url.path}{query}"


async def verify_twilio_request(request: Request) -> None:
    """FastAPI dependency: reject webhook calls that were not signed by Twilio."""
    if not _enabled():
        return
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    if not auth_token:
        logger.error("Twilio webhook rejected: signature verification is not configured")
        raise HTTPException(status_code=503, detail="Twilio signature verification is not configured")
    signature = request.headers.get("x-twilio-signature", "")
    params = dict(await request.form())  # Starlette caches the form, so the endpoint can read it again
    url = public_request_url(request)
    if not signature or not RequestValidator(auth_token).validate(url, params, signature):
        logger.warning("rejected unsigned or invalid Twilio request: %s %s (signature present: %s)", request.method, url, bool(signature))
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")


def _secret() -> bytes:
    secret = (os.getenv("AUTH_SECRET") or os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
    if not secret:
        raise RuntimeError("A secret is required to sign voice stream tokens")
    return secret.encode()


def stream_token(call_sid: str) -> str:
    issued = str(int(time.time()))
    digest = hmac.new(_secret(), f"{call_sid}:{issued}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{issued}.{digest}"


def valid_stream_token(call_sid: str, token: str) -> bool:
    if not _enabled():
        return True
    try:
        issued, digest = token.split(".", 1)
        fresh = 0 <= time.time() - int(issued) <= STREAM_TOKEN_TTL_SECONDS
    except (ValueError, AttributeError):
        return False
    try:
        expected = hmac.new(_secret(), f"{call_sid}:{issued}".encode(), hashlib.sha256).hexdigest()[:32]
    except RuntimeError:
        return False
    return fresh and hmac.compare_digest(digest, expected)
