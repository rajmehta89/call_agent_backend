"""Test settings: no database, no real provider credentials, no network.

Values set here win over .env because env_loader never overrides existing variables.
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ.update({
    "MONGO_URI": "",
    "AUTH_SECRET": "test-auth-secret",
    "TWILIO_AUTH_TOKEN": "test-twilio-token",
    "TWILIO_VALIDATE_SIGNATURES": "true",
    "RETELL_API_KEY": "test-retell-key",
    "RETELL_AGENT_ID": "agent_test",
    "LOG_LEVEL": "WARNING",
    "HANDOFF_LLM_CHECK": "false",  # no model calls; tests/test_handoff_llm_check.py covers that path with a stand-in
    "MEANING_TRANSLATION": "false",  # no model calls; tests/test_meaning_translation.py covers it with a stand-in
    "LOG_DIR": tempfile.mkdtemp(prefix="relaydesk-test-logs-"),  # never write into the real logs/app.log
})

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
