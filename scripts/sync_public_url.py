"""Point Twilio, Retell and .env at the backend's current public URL (ngrok).

Free ngrok URLs change on every restart, which silently breaks calls ("application error").
Run after starting ngrok; only out-of-date settings are changed.

    python scripts/sync_public_url.py              # discover the URL from ngrok's local API
    python scripts/sync_public_url.py https://x.ngrok-free.app
    python scripts/sync_public_url.py --local-only # update local .env without touching providers
    python scripts/sync_public_url.py --check      # report only, change nothing
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from env_loader import load_project_env  # noqa: E402

load_project_env()


def ngrok_url() -> str:
    with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=5) as response:
        tunnels = json.load(response)["tunnels"]
    https = [t["public_url"] for t in tunnels if t["public_url"].startswith("https://")]
    if not https:
        raise SystemExit("ngrok is running but has no https tunnel")
    return https[0]


def update_env_file(base: str, check: bool) -> str:
    path = BACKEND / ".env"
    text = path.read_text(encoding="utf-8")
    current = re.search(r"^TWILIO_PUBLIC_BASE_URL=(.*)$", text, re.M)
    if current and current.group(1).strip() == base:
        return "ok"
    if check:
        return f"stale ({current.group(1).strip() if current else 'missing'})"
    line = f"TWILIO_PUBLIC_BASE_URL={base}"
    text = re.sub(r"^TWILIO_PUBLIC_BASE_URL=.*$", line, text, flags=re.M) if current else text.rstrip() + f"\n{line}\n"
    path.write_text(text, encoding="utf-8")
    return "updated"


def sync_twilio(base: str, check: bool) -> list[str]:
    from twilio.rest import Client

    sid, token = os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN")
    if not sid or not token:
        return ["Twilio: skipped (no credentials)"]
    client, report = Client(sid, token), []
    status_url = f"{base}/api/twilio/status"

    app_sid = os.getenv("TWILIO_BROWSER_TWIML_APP_SID")
    if app_sid:
        app = client.applications(app_sid).fetch()
        wanted = f"{base}/api/twilio/browser-connect"
        if app.voice_url == wanted and app.status_callback == status_url:
            report.append("Twilio browser app: ok")
        elif check:
            report.append(f"Twilio browser app: stale ({app.voice_url})")
        else:
            client.applications(app_sid).update(voice_url=wanted, voice_method="POST", status_callback=status_url, status_callback_method="POST")
            report.append("Twilio browser app: updated")

    number = (os.getenv("TWILIO_PHONE_NUMBER") or os.getenv("TWILIO_CALLER_ID") or "").strip()
    wanted = f"{base}/api/custom-voice/inbound"  # single router: Retell / custom stack / default agent
    for record in client.incoming_phone_numbers.list(phone_number=number) if number else []:
        if record.voice_url == wanted and record.status_callback == status_url:
            report.append(f"Twilio number {record.phone_number}: ok")
        elif check:
            report.append(f"Twilio number {record.phone_number}: stale ({record.voice_url})")
        else:
            record.update(voice_url=wanted, voice_method="POST", status_callback=status_url, status_callback_method="POST")
            report.append(f"Twilio number {record.phone_number}: updated")
    return report


def sync_retell(base: str, check: bool) -> str:
    import httpx

    key, agent_id = os.getenv("RETELL_API_KEY", "").strip(), os.getenv("RETELL_AGENT_ID", "").strip()
    if not key or not agent_id:
        return "Retell: skipped (no RETELL_API_KEY / RETELL_AGENT_ID)"
    headers = {"Authorization": f"Bearer {key}"}
    secret = os.getenv("RETELL_CUSTOM_LLM_SECRET", "").strip()
    wanted = "wss://" + base.split("://", 1)[1] + "/api/retell/llm-websocket" + (f"?token={secret}" if secret else "")
    # The webhook brings call_ended / call_analyzed (final status, summary); without it Retell calls stay "in progress".
    webhook = f"{base}/api/retell/webhook"
    agent = httpx.get(f"https://api.retellai.com/get-agent/{agent_id}", headers=headers, timeout=20).json()
    if (agent.get("response_engine") or {}).get("llm_websocket_url") == wanted and agent.get("webhook_url") == webhook:
        return "Retell agent: ok"
    if check:
        return "Retell agent: stale"
    httpx.patch(f"https://api.retellai.com/update-agent/{agent_id}", headers=headers, timeout=20,
                json={"response_engine": {"type": "custom-llm", "llm_websocket_url": wanted}, "webhook_url": webhook}).raise_for_status()
    httpx.post(f"https://api.retellai.com/publish-agent/{agent_id}", headers=headers, timeout=20).raise_for_status()
    return "Retell agent: updated and published"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", nargs="?", help="public https URL; defaults to the running ngrok tunnel")
    parser.add_argument("--check", action="store_true", help="report what is out of date without changing it")
    parser.add_argument("--local-only", action="store_true", help="update only local .env; leave production provider webhooks intact")
    args = parser.parse_args()
    base = (args.url or ngrok_url()).rstrip("/")
    print(f"Public URL: {base}")
    results = [f".env TWILIO_PUBLIC_BASE_URL: {update_env_file(base, args.check)}"]
    steps = () if args.local_only else (lambda: sync_twilio(base, args.check), lambda: [sync_retell(base, args.check)])
    for step in steps:
        try:
            results.extend(step())
        except Exception as error:  # keep going: one provider failing should not block the others
            results.append(f"failed: {error}")
    for line in results:
        print(f"  {line}")
    return 1 if any("failed" in line for line in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
