#!/usr/bin/env python3
"""
Single entry point for Render deployment
Combines all backend services into one FastAPI application
"""

import logging
import os
import sys
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from env_loader import load_project_env

# Load only this project's environment file before importing application modules.
load_project_env()

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# App-wide log level; set LOG_LEVEL=DEBUG in .env to see debug output.
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
# Third-party libraries are very chatty at DEBUG (e.g. Mongo heartbeats every few seconds).
for _noisy in ("pymongo", "httpx", "httpx2", "httpcore", "httpcore2", "urllib3", "asyncio", "websockets", "multipart", "watchfiles", "hpack", "openai"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

from routers import auth_api, calls_api, config_api, custom_twilio_voice_api, platform_api, retell_api, transfer_api, twilio_api, voice_provider_api
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
