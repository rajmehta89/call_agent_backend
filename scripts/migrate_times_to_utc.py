"""One-time move of calls / leads / customers timestamps from server-local time to UTC (see timeutil.py).

Before 2026-10-20 these collections were written with datetime.now() (the server's local time); everything is UTC now.
Run it once per database, with the backend stopped so no new rows are written meanwhile:

    python scripts/migrate_times_to_utc.py            # dry run: counts only
    python scripts/migrate_times_to_utc.py --apply    # writes; backs up the old values to logs/ first

Each converted document gets times_utc: true, so running it again never shifts a row twice.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.disable(logging.CRITICAL)
from env_loader import load_project_env  # noqa: E402

load_project_env()
from mongo_client import mongo_client  # noqa: E402

COLLECTIONS = ("calls", "leads", "customers")
MARKER = "times_utc_migrated"


def to_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None)  # naive values are read as this machine's local time


def main(apply: bool) -> None:
    if not mongo_client.is_connected():
        sys.exit("Database not connected")
    done = mongo_client.platform_settings.find_one({"key": MARKER})
    if done:  # rows written since then are already UTC and carry no times_utc flag: never run twice
        sys.exit(f"Already migrated on {done.get('value')}; nothing to do.")
    backup, total = {}, 0
    for name in COLLECTIONS:
        collection = getattr(mongo_client, name)
        changed = 0
        for document in collection.find({"times_utc": {"$ne": True}}):
            fields = {key: value for key, value in document.items() if isinstance(value, datetime)}
            if not fields:
                collection.update_one({"_id": document["_id"]}, {"$set": {"times_utc": True}}) if apply else None
                continue
            backup.setdefault(name, {})[str(document["_id"])] = {key: value.isoformat() for key, value in fields.items()}
            if apply:
                collection.update_one({"_id": document["_id"]}, {"$set": {**{key: to_utc(value) for key, value in fields.items()}, "times_utc": True}})
            changed += 1
        print(f"{name}: {changed} document(s) {'converted' if apply else 'to convert'}")
        total += changed
    if apply and total:
        os.makedirs("logs", exist_ok=True)
        path = os.path.join("logs", f"times_backup_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(backup, handle, indent=1)
        print(f"old values saved to {path}")
    if apply:
        mongo_client.platform_settings.update_one({"key": MARKER}, {"$set": {"value": datetime.now(timezone.utc).isoformat()}}, upsert=True)


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
