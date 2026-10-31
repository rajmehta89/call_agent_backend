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

import memory_care

memory_care.limit_malloc_arenas()  # before the routers start their worker threads

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
QUIET_PATHS = ("/health", "/ready", "/api/platform/notifications")


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
    return {"status": "healthy", "release": "2026-10-27-voice-fast-first-audio-21"}


# Set when the startup warm-up (embedding model, turn classifiers, product catalogue) has finished.
_voice_warm = __import__("threading").Event()
_started_at = time.monotonic()


@app.get("/ready")
async def ready_check():
    """Health check for deploys (Render: Settings > Health Check Path = /ready). 503 until the warm-up has finished,
    so a new server takes calls only once it is fast: a call 11 minutes after a push had turn checks of up to 5.8 s
    while the warm-up still ran. After READY_MAX_SECONDS (default 240) it reports ready anyway, so a failed warm-up can
    never block a deploy. /health stays a plain liveness check."""
    try:
        ceiling = max(0.0, float(os.getenv("READY_MAX_SECONDS", "240")))
    except ValueError:
        ceiling = 240.0
    waited = time.monotonic() - _started_at
    # Which embeddings this server uses: "local" (the model in this process, ~35 ms a check) or "network" (an API
    # call per new text, 1-1.5 s): the main reason turn checks are fast or slow. No settings values are shown.
    from brain_service import _LOCAL_EMBEDDER
    embeddings = "local" if _LOCAL_EMBEDDER is not None else "network"
    # memory_mb: this process's resident memory (Linux; null elsewhere). A 512 MB instance near its limit crashes mid-call.
    memory = memory_care.resident_mb()
    # Render supplies a commit SHA. Report only a validated identifier so a
    # pushed fix can be distinguished from the version actually taking calls.
    revision = (os.getenv("RENDER_GIT_COMMIT") or "").strip().lower()
    revision = revision[:12] if len(revision) == 40 and all(char in "0123456789abcdef" for char in revision) else None
    if _voice_warm.is_set() or waited >= ceiling:
        return {"ready": True, "warm": _voice_warm.is_set(), "seconds": round(waited), "embeddings": embeddings, "memory_mb": memory, "revision": revision}
    return JSONResponse(status_code=503, content={"ready": False, "warming_up_seconds": round(waited), "embeddings": embeddings, "memory_mb": memory, "revision": revision})

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
async def check_voice_security() -> None:
    import asyncio
    from twilio_security import validate_production_voice_security
    await asyncio.to_thread(validate_production_voice_security)


@app.on_event("startup")
async def watch_event_loop() -> None:
    """Log and keep what holds up the event loop during calls (loop_watch.py)."""
    import loop_watch
    loop_watch.start()


@app.on_event("startup")
async def attach_realtime() -> None:
    """Live dashboard updates publish from worker threads onto this loop."""
    import asyncio
    realtime.attach_loop(asyncio.get_running_loop())


@app.on_event("startup")
async def start_email_campaign_worker() -> None:
    """Process campaign drafts while the backend is running. Production (Render) only answers calls: discovery
    and sending there competed with live Retell calls (pings up to 9.5 s), so the campaign runs on the local
    machine unless EMAIL_CAMPAIGN_WORKER=true."""
    import asyncio
    from email_campaign import email_campaign_worker

    app.state.email_campaign_task = None
    configured = os.getenv("EMAIL_CAMPAIGN_WORKER", "").strip().lower()
    production = bool(os.getenv("RENDER")) or _environment in {"prod", "production"}
    if configured in {"0", "false", "no", "off"} or (production and configured not in {"1", "true", "yes", "on"}):
        logging.getLogger(__name__).info("email campaign worker off on this server (production voice only)")
        return
    app.state.email_campaign_task = asyncio.create_task(email_campaign_worker())


@app.on_event("shutdown")
async def stop_email_campaign_worker() -> None:
    if app.state.email_campaign_task is not None:
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
            from thread_summaries import ensure_summaries
            if __import__("mongo_client").mongo_client.is_connected():
                ensure_summaries()  # stream/backfill complete history before serving Inbox work
            from voice_provider_settings import provider_snapshot
            provider_snapshot()  # which voice provider is on: read once now, not while a call is starting
            from voice_runtime import warm_runtime
            try:
                warm_runtime()  # optional Pipecat audio imports happen before accepting ready traffic
            except Exception as error:
                logger.warning("Optional Pipecat warmup failed (%s); ordinary voice warmup continues", type(error).__name__)
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
            from voice_presence import is_connection_check
            is_connection_check("Hello, can you hear me?")
            from voice_control import has_followup_request, CALLER_PAUSE, HUMAN_REQUEST
            has_followup_request("No callback. What time do you open?")
            CALLER_PAUSE.rank("hello")
            HUMAN_REQUEST.rank("hello")
            # The product catalogue copy is built in the background: product questions are only fast once it exists.
            deadline = time.monotonic() + 120
            while shopify_service.configured and not shopify_service.catalog_index() and time.monotonic() < deadline:
                time.sleep(1)
            logging.getLogger(__name__).info("startup warm-up finished in %.0fs", time.monotonic() - _started_at)
        except Exception as error:
            logging.getLogger(__name__).warning("startup warm-up failed: %s", error)
        finally:
            # Before /ready reports ready: the one full collection runs now, not during a call.
            memory_care.freeze_startup_objects()
            _voice_warm.set()

    threading.Thread(target=warm, name="warm-voice", daemon=True).start()

    def recover_call_summaries() -> None:
        # Calls cut short by a restart never got Retell's summary: fetch it, once the warm-up has finished.
        _voice_warm.wait(timeout=300)
        try:
            from routers.retell_api import recover_retell_outcomes
            recover_retell_outcomes()
        except Exception as error:
            logging.getLogger(__name__).warning("call summary recovery failed: %s", error)

    threading.Thread(target=recover_call_summaries, name="recover-summaries", daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=False  # Set to False for production
    )
