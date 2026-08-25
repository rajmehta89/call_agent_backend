#!/usr/bin/env python3
"""
Start the Twilio-first FastAPI backend for local development.
"""

import os
import sys

import uvicorn

from env_loader import load_project_env


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    env_path = load_project_env()
    port = int(os.getenv("PORT", "8000"))

    print("Starting Twilio backend")
    print(f"Loaded env source: {env_path or 'none'}")
    print(f"Backend URL: http://127.0.0.1:{port}")
    print("Twilio voice webhook: /api/twilio/inbound")
    print("Twilio ConversationRelay websocket: /api/twilio/ws")

    if not os.getenv("MONGO_URI"):
        print("Warning: MONGO_URI is not set. Call logs and leads will not persist.")

    if not os.getenv("TWILIO_ACCOUNT_SID") or not os.getenv("TWILIO_AUTH_TOKEN"):
        print("Warning: Twilio credentials are not set. Live transfer updates will be skipped.")

    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)


if __name__ == "__main__":
    main()
