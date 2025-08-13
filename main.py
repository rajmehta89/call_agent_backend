#!/usr/bin/env python3
"""
Single entry point for Render deployment
Combines all backend services into one FastAPI application
"""

import os
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routers import config_api, calls_api, webhook_api, websocket_api, leads_api_mongo, inbound_api

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
app.include_router(calls_api.router)
app.include_router(webhook_api.router)
app.include_router(websocket_api.router)
app.include_router(leads_api_mongo.router)
app.include_router(inbound_api.router)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=False  # Set to False for production
    )
