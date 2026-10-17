"""Test settings: no database, no real provider credentials, no network.

Values set here win over .env because env_loader never overrides existing variables.
"""

import os
import sys
from pathlib import Path

os.environ.update({
    "MONGO_URI": "",
    "AUTH_SECRET": "test-auth-secret",
    "TWILIO_AUTH_TOKEN": "test-twilio-token",
    "TWILIO_VALIDATE_SIGNATURES": "true",
    "RETELL_API_KEY": "test-retell-key",
    "RETELL_AGENT_ID": "agent_test",
    "LOG_LEVEL": "WARNING",
})

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
