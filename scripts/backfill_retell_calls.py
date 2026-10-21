"""Finish Retell calls whose end-of-call webhook never arrived (they stay "in progress" with no summary).

Happens when Retell's webhook_url pointed at an old ngrok address (scripts/sync_public_url.py now keeps it current).
Fetches each stuck call from Retell and stores it exactly as the webhook would:

    python scripts/backfill_retell_calls.py            # list stuck calls
    python scripts/backfill_retell_calls.py --apply    # fetch from Retell and save
"""

import logging
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.disable(logging.CRITICAL)
from env_loader import load_project_env  # noqa: E402

load_project_env()
import httpx  # noqa: E402

from mongo_client import mongo_client  # noqa: E402
from routers.retell_api import _upsert_retell_call  # noqa: E402
from timeutil import utc_now  # noqa: E402

STUCK_AFTER = timedelta(minutes=10)


def main(apply: bool) -> None:
    key = os.getenv("RETELL_API_KEY", "").strip()
    if not key:
        sys.exit("RETELL_API_KEY is not set")
    stuck = list(mongo_client.calls.find({"provider": "retell", "status": {"$in": ["in_progress", "in-progress", "ongoing", "registered"]},
                                          "updated_at": {"$lt": utc_now() - STUCK_AFTER}}, {"call_session_id": 1, "call_date": 1}))
    print(f"{len(stuck)} stuck Retell call(s)")
    for row in stuck:
        call_id = row.get("call_session_id")
        if not call_id or not apply:
            print(f"  {call_id} ({row.get('call_date')})")
            continue
        response = httpx.get(f"https://api.retellai.com/v2/get-call/{call_id}", headers={"Authorization": f"Bearer {key}"}, timeout=30)
        if response.status_code != 200:
            print(f"  {call_id}: Retell answered {response.status_code}; left as is")
            continue
        call = response.json()
        event = "call_analyzed" if call.get("call_analysis") else "call_ended"
        _upsert_retell_call(call, event)
        print(f"  {call_id}: saved as {event} (status {call.get('call_status')})")


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
