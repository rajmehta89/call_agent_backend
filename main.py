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

from routers import auth_api, calls_api, config_api, custom_twilio_voice_api, platform_api, retell_api, shopify_api, transfer_api, twilio_api, voice_provider_api
from routers.leads_api_mongo import router as leads_router

# Create FastAPI app
app = FastAPI(
    title="AI Agent Backend",
    description="Combined backend services for AI Agent application",
    version="1.0.0"
)


# Add CORS middleware
# Browsers may only call this API from the dashboard's own origins. Set CORS_ORIGINS
# (comma-separated) in production, e.g. "https://app.example.com".
DEFAULT_CORS_ORIGINS = "http://localhost:3000,http://127.0.0.1:3000,http://192.168.56.1:3000"
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in os.getenv("CORS_ORIGINS", DEFAULT_CORS_ORIGINS).split(",") if origin.strip()],
    allow_credentials=False,  # the dashboard authenticates with a bearer token, not cookies
    allow_methods=["*"],
    allow_headers=["*"],
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
    return {"status": "healthy"}

# Register all routers
app.include_router(config_api.router)
app.include_router(auth_api.router)
app.include_router(calls_api.router)
app.include_router(platform_api.router)
app.include_router(shopify_api.router)
app.include_router(twilio_api.router)
app.include_router(custom_twilio_voice_api.router)
app.include_router(retell_api.router)
app.include_router(transfer_api.router)
app.include_router(leads_router)
app.include_router(voice_provider_api.router)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=False  # Set to False for production
    )
