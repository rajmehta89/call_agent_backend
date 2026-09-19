"""Campaign controls, queue processing, approval behavior, and rate limiting."""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from bson import ObjectId

from email_policy import approval_window_open, get_email_policy, send_window_open
from mongo_client import mongo_client


DEFAULT_EMAIL_CAMPAIGN: Dict[str, Any] = {
    "status": "stopped",
    "max_emails": 20,
    "interval_minutes": 60,
}


def get_email_campaign() -> Dict[str, Any]:
    value = DEFAULT_EMAIL_CAMPAIGN.copy()
    if mongo_client.is_connected():
        stored = mongo_client.platform_settings.find_one({"key": "email_campaign"})
        if stored and isinstance(stored.get("value"), dict):
            value.update(stored["value"])
    value["status"] = str(value.get("status") or "stopped").lower()
    if value["status"] not in {"running", "paused", "stopped"}:
        value["status"] = "stopped"
    value["max_emails"] = max(1, min(10000, int(value.get("max_emails", 20))))
    value["interval_minutes"] = max(1, min(10080, int(value.get("interval_minutes", 60))))
    return value


def save_email_campaign(value: Dict[str, Any]) -> Dict[str, Any]:
    campaign = {**get_email_campaign(), **value}
    campaign["status"] = str(campaign.get("status") or "stopped").lower()
    if campaign["status"] not in {"running", "paused", "stopped"}:
        campaign["status"] = "stopped"
    campaign["max_emails"] = max(1, min(10000, int(campaign.get("max_emails", 20))))
    campaign["interval_minutes"] = max(1, min(10080, int(campaign.get("interval_minutes", 60))))
    if mongo_client.is_connected():
        mongo_client.platform_settings.update_one(
            {"key": "email_campaign"},
            {"$set": {"value": campaign, "updated_at": datetime.utcnow()}, "$setOnInsert": {"created_at": datetime.utcnow()}},
            upsert=True,
        )
    return campaign


def campaign_action(action: str) -> Dict[str, Any]:
    normalized = action.strip().lower()
    if normalized in {"start", "resume"}:
        status = "running"
    elif normalized == "pause":
        status = "paused"
    elif normalized == "stop":
        status = "stopped"
    else:
        raise ValueError("Campaign action must be start, pause, resume, or stop")
    return save_email_campaign({"status": status})


def _rate_count(campaign: Dict[str, Any]) -> int:
    cutoff = datetime.utcnow() - timedelta(minutes=int(campaign["interval_minutes"]))
    return mongo_client.email_outbox.count_documents({"status": "sent", "sent_at": {"$gte": cutoff}})


def campaign_status() -> Dict[str, Any]:
    campaign = get_email_campaign()
    policy = get_email_policy()
    used = _rate_count(campaign) if mongo_client.is_connected() else 0
    return {
        **campaign,
        "rate_used": used,
        "rate_remaining": max(0, int(campaign["max_emails"]) - used),
        "approval_window_open": approval_window_open(policy),
        "send_window_open": send_window_open(policy),
        "can_process": campaign["status"] == "running" and send_window_open(policy) and used < int(campaign["max_emails"]),
    }


def _send_draft(draft: Dict[str, Any]) -> Dict[str, Any]:
    from brain_service import brain_service
    from gmail_service import gmail_service

    if not brain_service.tools().get("send_gmail_email", False):
        return {"status": "error", "reason": "The Gmail tool is disabled"}
    if not gmail_service.configured:
        return {"status": "error", "reason": "Gmail is not configured"}
    claimed = mongo_client.email_outbox.find_one_and_update(
        {"_id": draft["_id"], "status": {"$in": ["pending_approval", "scheduled", "approved"]}},
        {"$set": {"status": "sending", "updated_at": datetime.utcnow()}},
    )
    if not claimed:
        return {"status": "skipped", "reason": "Draft is already being processed"}
    try:
        result = gmail_service.send([draft.get("recipient_email", "")], draft.get("subject", ""), draft.get("body", ""))
        if result.get("status") != "sent":
            mongo_client.email_outbox.update_one({"_id": draft["_id"]}, {"$set": {"status": "error", "error": result.get("reason", "Email was not sent"), "updated_at": datetime.utcnow()}})
            return {"status": "error", "reason": result.get("reason", "Email was not sent")}
        now = datetime.utcnow()
        mongo_client.email_outbox.update_one({"_id": draft["_id"]}, {"$set": {"status": "sent", "sent_at": now, "updated_at": now, "delivery": result}})
        return {"status": "sent", "id": str(draft["_id"])}
    except Exception as exc:
        mongo_client.email_outbox.update_one({"_id": draft["_id"]}, {"$set": {"status": "error", "error": str(exc), "updated_at": datetime.utcnow()}})
        return {"status": "error", "reason": "Gmail delivery failed"}


def process_email_outbox(draft_ids: Optional[List[str]] = None, limit: int = 100) -> Dict[str, Any]:
    if not mongo_client.is_connected():
        return {"processed": 0, "sent": 0, "status": "database_unavailable"}
    campaign = get_email_campaign()
    policy = get_email_policy()
    if campaign["status"] != "running" or not send_window_open(policy):
        return {"processed": 0, "sent": 0, "status": "waiting", "campaign_status": campaign["status"]}
    query: Dict[str, Any] = {"status": {"$in": ["pending_approval", "scheduled", "approved"]}}
    if draft_ids:
        valid_ids = [ObjectId(item) for item in draft_ids if ObjectId.is_valid(str(item))]
        query["_id"] = {"$in": valid_ids}
    drafts = list(mongo_client.email_outbox.find(query).sort("created_at", 1).limit(max(1, min(100, limit))))
    sent = 0
    skipped_for_approval = 0
    for draft in drafts:
        current_campaign = get_email_campaign()
        if current_campaign["status"] != "running" or not send_window_open(policy):
            break
        if _rate_count(current_campaign) >= int(current_campaign["max_emails"]):
            break
        if draft.get("status") == "pending_approval" and current_campaign and policy.get("approval_required") and approval_window_open(policy):
            skipped_for_approval += 1
            continue
        result = _send_draft(draft)
        if result.get("status") == "sent":
            sent += 1
    return {"processed": len(drafts), "sent": sent, "skipped_for_approval": skipped_for_approval, "status": "processed"}


async def email_campaign_worker() -> None:
    import asyncio
    while True:
        try:
            process_email_outbox()
        except Exception:
            pass
        await asyncio.sleep(30)
