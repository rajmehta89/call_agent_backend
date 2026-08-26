#!/usr/bin/env python3
"""
Single entry point for Render deployment
Combines all backend services into one FastAPI application
"""

import os
import sys
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routers import auth_api, config_api, calls_api, omnichannel_api, platform_api, transfer_api, twilio_api, whatsapp_api

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

optional_routers = []
for module_name in ("leads_api_mongo", "webhook_api", "websocket_api", "inbound_api"):
    try:
        module = __import__(f"routers.{module_name}", fromlist=["router"])
        optional_routers.append(module.router)
    except Exception as exc:
        print(f"Skipping optional router {module_name}: {exc}")

# Create FastAPI app
app = FastAPI(
    title="AI Agent Backend",
    description="Combined backend services for AI Agent application",
    version="1.0.0"
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure this properly for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Health check endpoint
@app.get("/")
async def root():
    return {
        "message": "AI Agent Backend is running",
        "services": [
            "Configuration API",
            "Leads Management API",
            "Calls API",
            "WebSocket Server",
            "Webhook Server",
            "SMS API",
            "Outbound Caller API",
            "Inbound Call API"
        ]
    }

@app.get("/health")
async def health_check():
    return {"status": "healthy"}

# Register all routers
app.include_router(config_api.router)
app.include_router(auth_api.router)
app.include_router(calls_api.router)
app.include_router(omnichannel_api.router)
app.include_router(platform_api.router)
app.include_router(transfer_api.router)
app.include_router(twilio_api.router)
app.include_router(whatsapp_api.router)
for router in optional_routers:
    app.include_router(router)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=False  # Set to False for production
    )
