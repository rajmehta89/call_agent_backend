"""Dashboard overview: one request with everything the home page shows.

Admins see the whole workspace; a team member sees their own work (their leads' calls and the conversations
assigned to them). Days and hours follow the browser's time zone (tz_offset = minutes east of UTC).
"""

import logging
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query

from auth_service import require_permission, require_user
from mongo_client import mongo_client
from routers import inbox_api
from routers.calls_api import call_scope
from timeutil import utc_now
from voice_provider_settings import provider_snapshot

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/overview", tags=["overview"], dependencies=[Depends(require_permission("view_dashboard"))])

DAYS = 14
FINISHED_OK = {"completed", "ended"}


def _change(now: float, before: float) -> float | None:
    """Percent change vs the previous period; None when there is nothing to compare with."""
    return None if not before else round((now - before) / before * 100, 1)


def _thread_row(thread: Dict[str, Any], kind: str, text: str) -> Dict[str, Any]:
    return {"kind": kind, "key": thread["key"], "name": thread["name"], "at": thread["last_at"], "text": text,
            "assigned_to_name": thread.get("assigned_to_name"), "stage": thread.get("stage")}


@router.get("")
def overview(tz_offset: int = Query(0, ge=-14 * 60, le=14 * 60), user: Dict[str, Any] = Depends(require_user)):
    if not mongo_client.is_connected():
        raise HTTPException(status_code=503, detail="Database not connected")
    is_admin = inbox_api._can_assign(user)
    me = inbox_api._user_id(user)
    shift = timedelta(minutes=tz_offset)
    now = utc_now()
    local_today = (now + shift).replace(hour=0, minute=0, second=0, microsecond=0)
    day_start = lambda days_ago: local_today - timedelta(days=days_ago) - shift  # local midnight, as UTC

    # Calls in the last 28 days (two 14-day windows for the comparisons), scoped to what this person may see.
    scope = call_scope(user)
    calls = list(mongo_client.calls.find({**scope, "direction": {"$ne": "outbound"}, "call_date": {"$gte": day_start(DAYS * 2 - 1)}},
                                         {"call_date": 1, "status": 1, "duration": 1}))
    local_day = lambda call: (call["call_date"] + shift).date()
    by_day = Counter(local_day(call) for call in calls if isinstance(call.get("call_date"), datetime))
    failed_by_day = Counter(local_day(call) for call in calls if str(call.get("status", "")).lower() in inbox_api.FAILED_STATUSES)
    daily = [{"date": (local_today - timedelta(days=offset)).date().isoformat(), "calls": by_day[(local_today - timedelta(days=offset)).date()],
              "failed": failed_by_day[(local_today - timedelta(days=offset)).date()]} for offset in range(DAYS - 1, -1, -1)]

    def window(start_days_ago: int, length: int) -> List[Dict[str, Any]]:
        start, end = day_start(start_days_ago), day_start(start_days_ago - length)
        return [call for call in calls if isinstance(call.get("call_date"), datetime) and start <= call["call_date"] < end]

    this_week, last_week = window(6, 7), window(13, 7)
    durations = lambda rows: [float(row.get("duration") or 0) for row in rows if row.get("duration")]
    avg = lambda values: round(sum(values) / len(values), 1) if values else 0
    answered = lambda rows: sum(1 for row in rows if str(row.get("status", "")).lower() in FINISHED_OK)
    hours = Counter(int(((call["call_date"] + shift).hour)) for call in window(DAYS * 2 - 1, DAYS * 2))

    # Conversations: everything for admins; a team member's own.
    threads = [thread for thread in inbox_api._threads() if is_admin or thread.get("assigned_to") == me]
    open_threads = [thread for thread in threads if not thread.get("closed")]
    live = [_thread_row(thread, "live", "On a call now") for thread in open_threads if thread.get("live")]
    unassigned = [_thread_row(thread, "unassigned", thread.get("preview") or "Waiting for an owner") for thread in open_threads
                  if not thread.get("assigned_to") and not thread.get("failed")] if is_admin else []
    failed = [_thread_row(thread, "failed", "Call did not connect") for thread in open_threads if thread.get("failed")]
    mentions = mongo_client.team_messages.count_documents({**inbox_api._mention_query(user), "read_by": {"$ne": me}})
    attention = (live + failed + unassigned)[:8]

    recent_calls = list(mongo_client.calls.find({**scope, "direction": {"$ne": "outbound"}}, inbox_api.LIST_FIELDS).sort("call_date", -1).limit(6))
    recent = [{"id": str(call["_id"]), "key": inbox_api._thread_key(call), "at": call.get("call_date"), "status": call.get("status"),
               "duration": call.get("duration") or 0, "provider": call.get("provider"), "summary": call.get("call_summary") or "",
               "name": next((thread["name"] for thread in threads if thread["key"] == inbox_api._thread_key(call)), inbox_api._thread_key(call)),
               "failed": str(call.get("status", "")).lower() in inbox_api.FAILED_STATUSES} for call in recent_calls]

    stage_counts = Counter(thread.get("stage") for thread in open_threads)
    pipeline = [{"id": stage, "label": label, "count": stage_counts.get(stage, 0)} for stage, label in inbox_api.STAGES]

    team: List[Dict[str, Any]] = []
    if is_admin:
        lead_counts = Counter(lead.get("assigned_to") for lead in mongo_client.leads.find({"assigned_to": {"$exists": True}}, {"assigned_to": 1}))
        for member in inbox_api._members(user):
            if not member.get("assignable"):
                continue
            mine = [thread for thread in open_threads if thread.get("assigned_to") == member["id"]]
            team.append({"id": member["id"], "name": member["name"], "open": len(mine), "live": sum(1 for thread in mine if thread.get("live")),
                         "leads": lead_counts.get(member["id"], 0)})

    lead_scope = {} if is_admin else {"assigned_to": me}
    leads_new = mongo_client.leads.count_documents({**lead_scope, "created_at": {"$gte": day_start(6)}})
    providers = provider_snapshot()
    active_provider = next((name for name, info in providers.items() if info.get("enabled")), None)

    data = {
        "scope": "workspace" if is_admin else "mine",
        "kpis": {
            "calls_today": by_day[local_today.date()], "calls_yesterday": by_day[(local_today - timedelta(days=1)).date()],
            "calls_7d": len(this_week), "calls_7d_change": _change(len(this_week), len(last_week)),
            "avg_duration_7d": avg(durations(this_week)), "avg_duration_change": _change(avg(durations(this_week)), avg(durations(last_week))),
            "answered_rate_7d": round(answered(this_week) / len(this_week) * 100) if this_week else None,
            "failed_7d": sum(1 for row in this_week if str(row.get("status", "")).lower() in inbox_api.FAILED_STATUSES),
            "live_now": len(live), "open_conversations": len(open_threads), "unassigned": len(unassigned), "mentions": mentions,
            "leads_new_7d": leads_new, "leads_total": mongo_client.leads.count_documents(lead_scope),
        },
        "daily": daily,
        "hours": [hours.get(hour, 0) for hour in range(24)],
        "attention": attention,
        "recent": recent,
        "pipeline": pipeline,
        "team": team,
        "voice": {"active_provider": providers[active_provider]["name"] if active_provider else "Default Twilio AI agent",
                  "ready": bool(providers[active_provider].get("ready")) if active_provider else True},
    }
    logger.info("overview: %s (%s) %d calls in window, %d open conversations", user.get("email"), data["scope"], len(calls), len(open_threads))
    return {"success": True, "data": inbox_api._serialize(data)}
