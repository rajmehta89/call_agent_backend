"""Durable, bounded-size per-caller summaries for Inbox, counts and Tasks.

Backfill streams the entire history once per process. Subsequent writes refresh
only the affected caller, outside the voice event loop. Metadata such as human
stage, assignment and notes is never replaced by this derived cache.
"""

import logging
import threading
from datetime import datetime, timedelta
from typing import Any

from bson import ObjectId
from pymongo import ReturnDocument, UpdateOne
from pymongo.errors import BulkWriteError

from mongo_client import mongo_client
from timeutil import utc_now

logger = logging.getLogger(__name__)
_ready = False
_backfill_lock = threading.Lock()
_condition = threading.Condition()
_pending: set[str] = set()
_rebuild = False
_worker = None
MAX_PENDING = 1000


def _summarize(calls) -> dict:
    from routers.inbox_api import _thread_key, _when, _utc, _is_live, LIVE_WINDOW
    groups = {}
    for call in calls:
        key = _thread_key(call)
        row = groups.setdefault(key, {"latest": call, "call_count": 0, "lead_id": None, "live_until": None})
        row["call_count"] += 1
        if (_utc(_when(call)), str(call["_id"])) >= (_utc(_when(row["latest"])), str(row["latest"]["_id"])):
            row["latest"] = call
        if call.get("lead_id") and (not row.get("lead_at") or _utc(_when(call)) >= row["lead_at"]):
            row["lead_id"] = call["lead_id"]
            row["lead_at"] = _utc(_when(call))
        if _is_live(call):
            until = _utc(call["updated_at"]) + LIVE_WINDOW
            row["live_until"] = max(row["live_until"], until) if row["live_until"] else until
    return groups


def ensure_summaries() -> None:
    global _ready
    if _ready:
        return
    with _backfill_lock:
        if _ready:
            return
        from routers.inbox_api import LIST_FIELDS
        began = utc_now()
        grouped = _summarize(mongo_client.calls.find({"direction": {"$ne": "outbound"}}, LIST_FIELDS))
        operations = []
        for key, summary in grouped.items():
            # A live refresh that finished after this scan began must win over the backfill.
            operations.append(UpdateOne(
                {"_id": key, "$or": [{"summary_refreshed_at": {"$exists": False}}, {"summary_refreshed_at": {"$lt": began}}]},
                {"$set": {"call_summary_cache": summary, "summary_refreshed_at": began}}, upsert=True))
            if len(operations) == 500:
                _write_backfill(operations)
                operations = []
        if operations:
            _write_backfill(operations)
        _ready = True
        logger.info("inbox: backfilled summaries for %d caller threads from complete history", len(grouped))


def _write_backfill(operations) -> None:
    try:
        mongo_client.inbox_threads.bulk_write(operations, ordered=False)
    except BulkWriteError as error:
        # A concurrent refresh created a fresher row: its _id rejects our stale upsert.
        details = error.details
        if details.get("writeConcernErrors") or not details.get("writeErrors") or any(row.get("code") != 11000 for row in details["writeErrors"]):
            raise


def refresh_key(key: str) -> None:
    from routers.inbox_api import LIST_FIELDS, _calls_query
    state = mongo_client.inbox_threads.find_one_and_update(
        {"_id": key}, {"$inc": {"summary_revision": 1}}, upsert=True, return_document=ReturnDocument.AFTER)
    grouped = _summarize(mongo_client.calls.find(_calls_query(key), LIST_FIELDS))
    summary = grouped.get(key)
    if summary is None:
        return
    changed = mongo_client.inbox_threads.update_one(
        {"_id": key, "summary_revision": state["summary_revision"]},
        {"$set": {"call_summary_cache": summary, "summary_refreshed_at": utc_now()}})
    if changed.matched_count:
        import realtime
        realtime.publish("thread", key=key)


def schedule_refresh(identifier: Any) -> None:
    """Nonblocking enqueue from call writes; no database work on the caller's loop."""
    global _worker, _rebuild
    if getattr(mongo_client, "client", None) is None:
        return
    with _condition:
        if not identifier or len(_pending) >= MAX_PENDING:
            _rebuild = True
        else:
            _pending.add(str(identifier))
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_work, daemon=True, name="inbox-summary")
            _worker.start()
        _condition.notify()


def _work() -> None:
    global _rebuild, _ready
    while True:
        with _condition:
            _condition.wait_for(lambda: _pending or _rebuild)
            rebuild, _rebuild = _rebuild, False
            identifier = _pending.pop() if _pending else None
        try:
            if not mongo_client.is_connected():
                raise RuntimeError("Database not connected")
            if rebuild:
                _ready = False
                ensure_summaries()
                import realtime
                realtime.publish("thread")
            if identifier:
                query = [{"call_session_id": identifier}, {"twilio_call_sid": identifier}]
                if ObjectId.is_valid(identifier):
                    query.append({"_id": ObjectId(identifier)})
                call = mongo_client.calls.find_one({"$or": query}, {"phone_number": 1, "call_session_id": 1, "direction": 1})
                if call and call.get("direction") != "outbound":
                    from routers.inbox_api import _thread_key
                    refresh_key(_thread_key(call))
        except Exception as error:
            logger.warning("inbox summary refresh deferred: %s", type(error).__name__)
            with _condition:
                _rebuild = True  # complete restart/recovery also repairs missed writes
                _condition.wait(timeout=5)
