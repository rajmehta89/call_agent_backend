#!/usr/bin/env python3
"""
Single entry point for Render deployment
Combines all backend services into one FastAPI application
"""

import logging
import os
import sys
import uvicorn
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from env_loader import load_project_env

# Load only this project's environment file before importing application modules.
load_project_env()

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from logging_setup import bind_call, setup_logging

# Console + logs/app.log; set LOG_LEVEL=DEBUG in .env for more detail.
setup_logging()
logger = logging.getLogger("relaydesk.http")

from datetime import datetime

import fastapi.encoders
from timeutil import api_time

# Stored datetimes are UTC (timeutil.utc_now); send them with "Z" so every browser shows its own local time.
fastapi.encoders.ENCODERS_BY_TYPE[datetime] = api_time

from routers import auth_api, call_forwarding_api, calls_api, config_api, custom_twilio_voice_api, data_reset_api, inbox_api, overview_api, platform_api, retell_api, shopify_api, transfer_api, twilio_api, voice_provider_api
from routers.leads_api_mongo import public_router as leads_public_router, router as leads_router, sms_router
import realtime

# Create FastAPI app
app = FastAPI(
    title="AI Agent Backend",
    description="Combined backend services for AI Agent application",
    version="1.0.0"
)


# Add CORS middleware
# Browsers may only call this API from the dashboard's own origins. Set CORS_ORIGINS
# (comma-separated) in production, e.g. "https://app.example.com".
_environment = (os.getenv("APP_ENV") or os.getenv("ENVIRONMENT") or "development").lower()
_local_cors_origins = "http://localhost:3000,http://127.0.0.1:3000,http://192.168.56.1:3000"
_production_cors_origin = os.getenv("RELAYDESK_PUBLIC_APP_URL", "").strip().rstrip("/")
DEFAULT_CORS_ORIGINS = _production_cors_origin if _environment in {"production", "prod"} and _production_cors_origin else _local_cors_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in os.getenv("CORS_ORIGINS", DEFAULT_CORS_ORIGINS).split(",") if origin.strip()],
    allow_credentials=False,  # the dashboard authenticates with a bearer token, not cookies
    allow_methods=["*"],
    allow_headers=["*"],
    max_age=7200,  # browsers remember the preflight for 2 hours (Chrome's cap) instead of 10 minutes: one round trip less per request
)

# Polling endpoints would drown out everything else at INFO.
QUIET_PATHS = ("/health", "/api/platform/notifications")


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """One line per request with status and duration; unexpected errors are logged with their traceback."""
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:8]
    bind_call(request_id)
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("%s %s crashed after %.0fms", request.method, request.url.path, (time.perf_counter() - started) * 1000)
        return JSONResponse({"detail": "Internal server error", "request_id": request_id}, status_code=500)
    elapsed = (time.perf_counter() - started) * 1000
    level = logging.DEBUG if request.url.path.startswith(QUIET_PATHS) else logging.WARNING if response.status_code >= 500 else logging.INFO
    logger.log(level, "%s %s -> %s in %.0fms", request.method, request.url.path, response.status_code, elapsed)
    response.headers["X-Request-ID"] = request_id
    return response


# Health check endpoint
@app.get("/")
async def root():
    return {
        "message": "Inbound Call CRM backend is running",
        "services": ["Inbound Twilio voice", "Call history", "Leads CRM"]
    }

@app.get("/health")
async def health_check():
    return {"status": "healthy", "release": "2026-10-27-voice-bridge-async-18"}

# Register all routers
app.include_router(config_api.router)
app.include_router(auth_api.router)
app.include_router(calls_api.router)
app.include_router(inbox_api.router)
app.include_router(overview_api.router)
app.include_router(platform_api.router)
app.include_router(data_reset_api.router)
app.include_router(shopify_api.router)
app.include_router(shopify_api.public_router)
app.include_router(twilio_api.router)
app.include_router(custom_twilio_voice_api.router)
app.include_router(retell_api.router)
app.include_router(transfer_api.router)
app.include_router(call_forwarding_api.router)
app.include_router(leads_router)
app.include_router(leads_public_router)
app.include_router(sms_router)
app.include_router(voice_provider_api.router)
app.include_router(realtime.router)


@app.on_event("startup")
async def attach_realtime() -> None:
    """Live dashboard updates publish from worker threads onto this loop."""
    import asyncio
    realtime.attach_loop(asyncio.get_running_loop())


@app.on_event("startup")
async def start_email_campaign_worker() -> None:
    """Process approved campaign drafts while the backend is running."""
    import asyncio
    from email_campaign import email_campaign_worker

    app.state.email_campaign_task = asyncio.create_task(email_campaign_worker())


@app.on_event("shutdown")
async def stop_email_campaign_worker() -> None:
    app.state.email_campaign_task.cancel()


@app.on_event("startup")
async def keep_connections_warm() -> None:
    """Open every connection a call needs as the server starts, and keep them open between calls, so no call waits on
    a secure handshake (0.1-0.5 s each, from here to the US): the voice model (OpenRouter), the voices (ElevenLabs,
    Deepgram), Shopify and Retell. Each ping is a free request (key info, model or project list, the shop's name),
    every KEEP_WARM_SECONDS (default 25: Retell dropped idle connections after 30-60 s; 0 = only once at startup)."""
    import asyncio
    from ai_services import AIServices, warm_model_connection
    from routers.custom_twilio_voice_api import _warm_tts_connection, warm_retell_connection
    from routers.retell_api import configure_voice_services
    from shopify_service import shopify_service

    try:
        interval = max(0.0, float(os.getenv("KEEP_WARM_SECONDS", "25")))
    except ValueError:
        interval = 25.0
    services = None

    async def ping_all() -> None:
        nonlocal services
        if services is None:
            services = await asyncio.to_thread(configure_voice_services, AIServices(), os.getenv("RETELL_OPENROUTER_MODEL", "openai/gpt-oss-20b"))
        voices = [engine for engine, key in (("elevenlabs", "ELEVENLABS_API_KEY"), ("deepgram", "DEEPGRAM_API_KEY")) if os.getenv(key, "").strip()]
        await asyncio.gather(
            asyncio.to_thread(warm_model_connection, services),
            asyncio.to_thread(shopify_service.keep_warm),
            warm_retell_connection(),
            *(_warm_tts_connection(engine) for engine in voices),
            return_exceptions=True,
        )

    async def keep_warm() -> None:
        while True:
            try:
                await ping_all()
            except Exception as error:  # never stop keeping the others warm
                logging.getLogger(__name__).debug("keep-warm round failed: %s", error)
            if not interval:
                return
            await asyncio.sleep(interval)

    asyncio.get_running_loop().create_task(keep_warm())


@app.on_event("startup")
async def warm_local_embeddings() -> None:
    """Warm everything a voice turn needs, in the background, so the first call after a restart is not slow:
    the local embedding model (and vectors for any new knowledge), the knowledge and settings caches, and the
    connection to the voice model."""
    import os
    import threading
    from ai_services import AIServices, warm_model_connection
    from brain_service import brain_service
    from routers.retell_api import _system_prompt, configure_voice_services

    def warm() -> None:
        try:
            import live_transcript
            live_transcript.close_orphaned_calls()  # custom-pipeline calls cut off by the restart
            from voice_provider_settings import provider_snapshot
            provider_snapshot()  # which voice provider is on: read once now, not while a call is starting
            from voice_turns import regions_where_valid
            regions_where_valid("0412345678")  # loads every country's number formats now (~0.1 s), not mid-call
            brain_service.ensure_local_embeddings()
            _system_prompt("hello", "unknown", "")
            warm_model_connection(configure_voice_services(AIServices(), os.getenv("RETELL_OPENROUTER_MODEL", "openai/gpt-oss-20b")))
            # Speech-recognition key terms from the knowledge (rebuilt only if the knowledge changed).
            from voice_keyterms import current_keyterms
            current_keyterms()
            # Words naming what the connected store sells, so product questions in any words get a live lookup.
            from shopify_service import shopify_service
            shopify_service.catalog_words()
            shopify_service.catalog_index()  # the whole catalogue in memory, for instant product searches
            if shopify_service.configured:
                shopify_service.access_scopes()  # checked on store lookups: ~0.5 s on the first caller's question otherwise
            import store_intent
            store_intent.wanted("hello")  # the meaning examples for understanding store questions
            import call_forwarding
            call_forwarding._refresh_on()  # the on/off flag calls read without waiting
            # Every turn decision's examples (caller turn, person request, contact details...): each is prepared the
            # first time it is used, which the first caller after a restart would otherwise wait for.
            import turn_meaning
            from meaning import Classifier
            for classifier in vars(turn_meaning).values():
                if isinstance(classifier, Classifier):
                    classifier.rank("hello")
        except Exception as error:
            logging.getLogger(__name__).warning("startup warm-up failed: %s", error)

    threading.Thread(target=warm, name="warm-voice", daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=False  # Set to False for production
    )
