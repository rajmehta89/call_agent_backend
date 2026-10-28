"""Campaign controls, queue processing, approval behavior, and rate limiting."""

import os
import smtplib
from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo
from uuid import uuid4

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from email_policy import approval_window_open, get_email_policy, send_window_open
from mongo_client import mongo_client
import logging

logger = logging.getLogger(__name__)


DEFAULT_EMAIL_CAMPAIGN: Dict[str, Any] = {
    "status": "stopped",
    "max_emails": 20,
    "interval_minutes": 60,
    # Keep headroom below the personal Gmail daily limit for owner notifications
    # and normal mailbox use. The campaign stays running and resumes next day.
    "daily_limit": 450,
    "discovery_enabled": True,
    "discovery_query": "AI automation for home services",
    "discovery_location": "United States",
}

# The configured query is always tried first. These focused fallbacks let a
# campaign keep filling its hourly queue when one Google Places search has
# too few businesses with a public contact email.
# Trades and home services first: they lose jobs to calls missed on site or after hours, the problem the AI
# receptionist fixes, and they get the problem-first trade templates (prospect_discovery.TRADE_TEMPLATES).
DISCOVERY_FALLBACK_QUERIES = (
    "plumbers",
    "electricians",
    "roofing contractors",
    "air conditioning and heating services",
    "pest control",
    "locksmiths",
    "house cleaning services",
    "landscaping and gardening services",
    "painters",
    "builders and renovators",
    "dental clinics",
    "auto repair shops",
)
WORKER_INTERVAL_SECONDS = 30


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
    value["daily_limit"] = max(1, min(500, int(value.get("daily_limit", 450))))
    value["discovery_enabled"] = bool(value.get("discovery_enabled", True))
    value["discovery_query"] = str(value.get("discovery_query") or DEFAULT_EMAIL_CAMPAIGN["discovery_query"]).strip()
    value["discovery_location"] = str(value.get("discovery_location") or DEFAULT_EMAIL_CAMPAIGN["discovery_location"]).strip()
    return value


def save_email_campaign(value: Dict[str, Any]) -> Dict[str, Any]:
    campaign = {**get_email_campaign(), **value}
    campaign["status"] = str(campaign.get("status") or "stopped").lower()
    if campaign["status"] not in {"running", "paused", "stopped"}:
        campaign["status"] = "stopped"
    campaign["max_emails"] = max(1, min(10000, int(campaign.get("max_emails", 20))))
    campaign["interval_minutes"] = max(1, min(10080, int(campaign.get("interval_minutes", 60))))
    campaign["daily_limit"] = max(1, min(500, int(campaign.get("daily_limit", 450))))
    campaign["discovery_enabled"] = bool(campaign.get("discovery_enabled", True))
    campaign["discovery_query"] = str(campaign.get("discovery_query") or DEFAULT_EMAIL_CAMPAIGN["discovery_query"]).strip()
    campaign["discovery_location"] = str(campaign.get("discovery_location") or DEFAULT_EMAIL_CAMPAIGN["discovery_location"]).strip()
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


def _campaign_timezone(policy: Dict[str, Any]):
    timezone_name = str(policy.get("timezone") or "Asia/Kolkata")
    try:
        return ZoneInfo(timezone_name)
    except Exception:
        if timezone_name in {"Asia/Kolkata", "Asia/Calcutta", "IST"}:
            return timezone(timedelta(hours=5, minutes=30))
        return datetime.now().astimezone().tzinfo or timezone.utc


def _daily_window_start(policy: Dict[str, Any]) -> datetime:
    """Return today's midnight in the configured campaign timezone as UTC-naive."""
    campaign_timezone = _campaign_timezone(policy)
    local_now = datetime.now(campaign_timezone)
    local_midnight = datetime.combine(local_now.date(), time.min, tzinfo=campaign_timezone)
    return local_midnight.astimezone(timezone.utc).replace(tzinfo=None)


def _next_daily_reset(policy: Dict[str, Any]) -> datetime:
    return _daily_window_start(policy) + timedelta(days=1)


def _daily_count(policy: Dict[str, Any]) -> int:
    return mongo_client.email_outbox.count_documents({"status": "sent", "sent_at": {"$gte": _daily_window_start(policy)}})


def campaign_status() -> Dict[str, Any]:
    campaign = get_email_campaign()
    policy = get_email_policy()
    used = _rate_count(campaign) if mongo_client.is_connected() else 0
    daily_used = _daily_count(policy) if mongo_client.is_connected() else 0
    daily_remaining = max(0, int(campaign["daily_limit"]) - daily_used)
    return {
        **campaign,
        "rate_used": used,
        "rate_remaining": max(0, int(campaign["max_emails"]) - used),
        "daily_used": daily_used,
        "daily_remaining": daily_remaining,
        "daily_limit_reached": daily_remaining == 0,
        "daily_reset_at": _next_daily_reset(policy).isoformat() + "Z",
        "approval_window_open": approval_window_open(policy),
        "send_window_open": send_window_open(policy),
        "can_process": campaign["status"] == "running" and send_window_open(policy) and used < int(campaign["max_emails"]) and daily_remaining > 0,
    }


def _send_draft(draft: Dict[str, Any]) -> Dict[str, Any]:
    from brain_service import brain_service
    from email_templates import normalize_draft_copy
    from gmail_service import gmail_service
    from private_email_service import private_email_service

    recipient = str(draft.get("recipient_email") or "").strip().lower()
    domain = recipient.rsplit("@", 1)[-1]
    suppression = mongo_client.db.email_suppressions.find_one({"$or": [{"email": recipient}, {"domain": domain}]})
    if suppression:
        mongo_client.email_outbox.update_one({"_id": draft["_id"]}, {"$set": {"status": "excluded", "exclusion_reason": suppression.get("reason", "suppressed"), "updated_at": datetime.utcnow()}})
        return {"status": "excluded", "reason": "Recipient is suppressed"}
    use_private_email = (draft.get("campaign_name") == "Dubai voice WhatsApp CRM outreach"
                         or draft.get("sender_provider") == "namecheap_private_email"
                         or (not draft.get("sender_provider") and get_email_campaign().get("sender_provider") == "namecheap_private_email"))
    sender = private_email_service if use_private_email else gmail_service
    if not use_private_email and not brain_service.tools().get("send_gmail_email", False):
        return {"status": "error", "reason": "The Gmail tool is disabled"}
    if not sender.configured:
        return {"status": "error", "reason": "Private Email is not configured" if use_private_email else "Gmail is not configured"}
    safe_copy = normalize_draft_copy(draft)
    if safe_copy["body"] != draft.get("body") or safe_copy["company_context"] != draft.get("company_context"):
        mongo_client.email_outbox.update_one(
            {"_id": draft["_id"]},
            {"$set": {
                "subject": safe_copy["subject"],
                "body": safe_copy["body"],
                "company_context": safe_copy["company_context"],
                "updated_at": datetime.utcnow(),
            }},
        )
        draft = {**draft, **safe_copy}
    claimed = mongo_client.email_outbox.find_one_and_update(
        {"_id": draft["_id"], "status": {"$in": ["pending_approval", "scheduled", "approved"]}},
        {"$set": {"status": "sending", "updated_at": datetime.utcnow()}},
    )
    if not claimed:
        return {"status": "skipped", "reason": "Draft is already being processed"}
    try:
        result = sender.send([draft.get("recipient_email", "")], draft.get("subject", ""), draft.get("body", ""))
        if result.get("status") != "sent":
            mongo_client.email_outbox.update_one({"_id": draft["_id"]}, {"$set": {"status": "error", "error": result.get("reason", "Email was not sent"), "updated_at": datetime.utcnow()}})
            return {"status": "error", "reason": result.get("reason", "Email was not sent")}
        now = datetime.utcnow()
        mongo_client.email_outbox.update_one({"_id": draft["_id"]}, {"$set": {"status": "sent", "sent_at": now, "updated_at": now, "delivery": result}})
        return {"status": "sent", "id": str(draft["_id"])}
    except smtplib.SMTPResponseException as exc:
        if "too many messages from sender in last" in str(exc).lower():
            retry_after = datetime.utcnow() + timedelta(hours=1)
            mongo_client.email_outbox.update_one(
                {"_id": draft["_id"]},
                {"$set": {"status": "approved", "last_delivery_error": str(exc),
                          "delivery_retry_after": retry_after, "updated_at": datetime.utcnow()}},
            )
            mongo_client.db.email_provider_backoff.update_one(
                {"_id": "namecheap_private_email" if use_private_email else "gmail"},
                {"$set": {"retry_after": retry_after, "reason": "Provider hourly sender limit",
                          "updated_at": datetime.utcnow()}}, upsert=True,
            )
            return {"status": "provider_rate_limited", "retry_after": retry_after.isoformat() + "Z"}
        mongo_client.email_outbox.update_one({"_id": draft["_id"]}, {"$set": {"status": "error", "error": str(exc), "updated_at": datetime.utcnow()}})
        return {"status": "error", "reason": "Private Email delivery failed" if use_private_email else "Gmail delivery failed"}
    except Exception as exc:
        mongo_client.email_outbox.update_one({"_id": draft["_id"]}, {"$set": {"status": "error", "error": str(exc), "updated_at": datetime.utcnow()}})
        return {"status": "error", "reason": "Private Email delivery failed" if use_private_email else "Gmail delivery failed"}


def process_email_outbox(draft_ids: Optional[List[str]] = None, limit: int = 100) -> Dict[str, Any]:
    if not mongo_client.is_connected():
        return {"processed": 0, "sent": 0, "status": "database_unavailable"}
    # Manual approvals and the background worker share a lease so they cannot
    # both observe the last free hourly slot and send different messages.
    owner = uuid4().hex
    now = datetime.utcnow()
    locks = mongo_client.db.campaign_delivery_locks
    try:
        lease = locks.find_one_and_update(
            {"_id": "email_outbox", "$or": [{"expires_at": {"$lte": now}}, {"expires_at": {"$exists": False}}]},
            {"$set": {"owner": owner, "expires_at": now + timedelta(minutes=3)}},
            upsert=True, return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        return {"processed": 0, "sent": 0, "status": "delivery_in_progress"}
    if not lease:
        return {"processed": 0, "sent": 0, "status": "delivery_in_progress"}
    try:
        return _process_email_outbox_locked(draft_ids, limit, owner)
    finally:
        locks.delete_one({"_id": "email_outbox", "owner": owner})


def _process_email_outbox_locked(draft_ids: Optional[List[str]], limit: int, owner: str) -> Dict[str, Any]:
    campaign = get_email_campaign()
    policy = get_email_policy()
    if campaign["status"] != "running" or not send_window_open(policy):
        return {"processed": 0, "sent": 0, "status": "waiting", "campaign_status": campaign["status"]}
    provider = str(campaign.get("sender_provider") or "gmail")
    backoff = mongo_client.db.email_provider_backoff.find_one({"_id": provider})
    if isinstance(backoff, dict) and isinstance(backoff.get("retry_after"), datetime) and backoff["retry_after"] > datetime.utcnow():
        return {"processed": 0, "sent": 0, "status": "provider_rate_limited", "retry_after": backoff["retry_after"].isoformat() + "Z"}
    if _daily_count(policy) >= int(campaign["daily_limit"]):
        return {"processed": 0, "sent": 0, "status": "daily_limit_reached", "daily_limit": campaign["daily_limit"]}
    # A process/network failure can leave a draft claimed as "sending". Requeue
    # only claims older than 15 minutes so active deliveries are not duplicated.
    stale_cutoff = datetime.utcnow() - timedelta(minutes=15)
    if hasattr(mongo_client.email_outbox, "update_many"):
        mongo_client.email_outbox.update_many(
            {"status": "sending", "updated_at": {"$lt": stale_cutoff}},
            {"$set": {"status": "scheduled", "error": "Previous delivery attempt timed out; queued for retry", "updated_at": datetime.utcnow()}},
        )
    query: Dict[str, Any] = {"status": {"$in": ["pending_approval", "scheduled", "approved"]}}
    if draft_ids:
        valid_ids = [ObjectId(item) for item in draft_ids if ObjectId.is_valid(str(item))]
        query["_id"] = {"$in": valid_ids}
    drafts = list(mongo_client.email_outbox.find(query).sort("created_at", 1).limit(max(1, min(100, limit))))
    sent = 0
    skipped_for_approval = 0
    for draft in drafts:
        renewed = mongo_client.db.campaign_delivery_locks.update_one(
            {"_id": "email_outbox", "owner": owner, "expires_at": {"$gt": datetime.utcnow()}},
            {"$set": {"expires_at": datetime.utcnow() + timedelta(minutes=3)}},
        )
        if not renewed.matched_count:
            break
        current_campaign = get_email_campaign()
        if current_campaign["status"] != "running" or not send_window_open(policy):
            break
        if _rate_count(current_campaign) >= int(current_campaign["max_emails"]):
            break
        if _daily_count(policy) >= int(current_campaign["daily_limit"]):
            break
        if draft.get("status") == "pending_approval" and (
            draft.get("requires_explicit_approval")
            or (current_campaign and policy.get("approval_required") and approval_window_open(policy))
        ):
            skipped_for_approval += 1
            continue
        result = _send_draft(draft)
        if result.get("status") == "provider_rate_limited":
            return {"processed": len(drafts), "sent": sent, "status": "provider_rate_limited", "retry_after": result["retry_after"]}
        if result.get("status") == "sent":
            sent += 1
    return {"processed": len(drafts), "sent": sent, "skipped_for_approval": skipped_for_approval, "status": "processed"}


def _pending_campaign_candidates() -> int:
    return mongo_client.email_outbox.count_documents({"status": {"$in": ["pending_approval", "scheduled", "approved", "sending"]}})


def _discovery_queries(configured_query: str) -> List[str]:
    queries: List[str] = []
    for value in (configured_query, *DISCOVERY_FALLBACK_QUERIES):
        query = str(value or "").strip()
        if query and query.lower() not in {item.lower() for item in queries}:
            queries.append(query)
    return queries


def _discovery_locations(configured_location: str) -> List[str]:
    """Semicolons separate markets; commas remain part of a city address."""
    locations = []
    for value in str(configured_location or "United States").split(";"):
        value = value.strip()
        if value and value.casefold() not in {item.casefold() for item in locations}:
            locations.append(value)
    return locations or ["United States"]


def refill_campaign_candidates() -> Dict[str, Any]:
    """Keep enough unique prospect drafts ready for the next rate window."""
    if not mongo_client.is_connected():
        return {"status": "database_unavailable", "needed": 0}
    campaign = get_email_campaign()
    if campaign["status"] != "running" or not campaign.get("discovery_enabled"):
        return {"status": "disabled", "needed": 0}
    if not (os.getenv("GOOGLE_PLACES_API_KEY") or os.getenv("GOOGLE_MAPS_API_KEY") or os.getenv("GOOGLE_API_KEY")):
        return {"status": "provider_unconfigured", "needed": 0}
    daily_remaining = max(0, int(campaign["daily_limit"]) - _daily_count(get_email_policy()))
    if daily_remaining == 0:
        return {"status": "daily_limit_reached", "needed": 0, "queued": _pending_campaign_candidates(), "daily_limit": campaign["daily_limit"]}
    queued = _pending_campaign_candidates()
    # Keep a complete next-batch buffer ready even when the current hourly
    # sending window has already used some of its allowance. Sending itself
    # remains protected by max_emails; discovery can prepare the next batch.
    target_buffer = min(int(campaign["max_emails"]), daily_remaining)
    needed = max(0, target_buffer - queued)
    if needed == 0:
        return {"status": "buffer_ready", "needed": 0, "queued": queued}
    # Discovery searches Places and scrapes ~20 websites inside the server that also answers live calls. With a
    # large hourly limit it ran twice a minute (190 runs in 90 min) and stalled Retell calls for up to 15 s, so
    # every worker together waits CAMPAIGN_DISCOVERY_COOLDOWN_MINUTES after the latest run.
    cooldown = timedelta(minutes=max(1.0, float(os.getenv("CAMPAIGN_DISCOVERY_COOLDOWN_MINUTES", "10") or 10)))
    latest = mongo_client.discovery_runs.find_one({}, sort=[("created_at", -1)])
    if latest and isinstance(latest.get("created_at"), datetime) and datetime.utcnow() - latest["created_at"] < cooldown:
        return {"status": "cooldown", "needed": needed, "queued": queued}

    configured_query = campaign.get("discovery_query") or DEFAULT_EMAIL_CAMPAIGN["discovery_query"]
    location = campaign.get("discovery_location") or DEFAULT_EMAIL_CAMPAIGN["discovery_location"]
    campaign_definition = None
    try:
        from campaign_registry import get_campaigns
        campaign_definition = next((item for item in get_campaigns() if item.get("type") == "email_outreach"), None)
    except Exception:
        campaign_definition = None
    if campaign_definition:
        scrape = campaign_definition.get("scrape") or {}
        configured_query = str(scrape.get("query") or configured_query)
        location = str(scrape.get("location") or location)
    locations = _discovery_locations(location)
    def last_discovered(value):
        last = mongo_client.discovery_runs.find_one({"location": value}, sort=[("created_at", -1)])
        return last.get("created_at", datetime.min) if last else datetime.min
    # Rotate to the least recently researched market instead of asking Places
    # to interpret the whole country list as a single geographic location.
    location = min(locations, key=last_discovered)

    def last_searched(candidate):
        last = mongo_client.discovery_runs.find_one({"query": candidate, "location": location}, sort=[("created_at", -1)])
        return last.get("created_at", datetime.min) if last else datetime.min
    # The trade least recently searched in this market, never searched ones first in list order. Picking "the first
    # query not run in the last 10 minutes" chose the same one every time (each market comes round only every
    # ~70 minutes): 22 runs of "plumbing contractors" on 2026-10-28 found the same, already emailed, businesses
    # and made no drafts, while the other trades were never searched.
    query = min(_discovery_queries(configured_query), key=last_searched)

    from prospect_discovery import discover_prospects
    result = discover_prospects(
        query,
        location,
        min(20, max(1, needed)),
        True,
        target_drafts=needed,
        campaign_name=str(campaign_definition.get("name") if campaign_definition else "USA AI automation outreach"),
        campaign_goal=str((campaign_definition or {}).get("goal") or (campaign_definition or {}).get("description") or (campaign_definition or {}).get("audience") or ""),
        scrape_intent=str((campaign_definition or {}).get("scrape_intent") or ""),
        shared_context=str((campaign_definition or {}).get("campaign_context") or "") if (campaign_definition or {}).get("context_mode") == "campaign" else "",
        context_mode=str((campaign_definition or {}).get("context_mode") or "brain"),
        template_id=str(((campaign_definition or {}).get("scrape") or {}).get("template_id") or (campaign_definition or {}).get("template_id") or ""),
    )
    return {"status": "refilled", "needed": needed, "queued": queued, "result": result}


def notify_pending_approval_digest() -> Dict[str, Any]:
    """Automatically email Raj when new drafts need review; never sends to prospects."""
    if not mongo_client.is_connected():
        return {"status": "database_unavailable", "count": 0}
    from gmail_service import gmail_service
    if not gmail_service.configured:
        return {"status": "gmail_unconfigured", "count": 0}
    drafts = list(mongo_client.email_outbox.find({"status": "pending_approval"}).sort("created_at", 1).limit(50))
    drafts = [draft for draft in drafts if not draft.get("approval_notified_at") or draft.get("approval_notified_at") < draft.get("updated_at", draft.get("created_at"))]
    if not drafts:
        return {"status": "no_new_approvals", "count": 0}
    owner_email = (os.getenv("GMAIL_APPROVAL_EMAIL") or gmail_service.address).strip()
    app_url = (os.getenv("PUBLIC_APP_URL") or "https://bussinessautomate.vercel.app").rstrip("/")
    lines = [
        "Hi Raj,",
        "",
        f"The AI outreach campaign prepared {len(drafts)} new prospect email(s) for your review.",
        f"Open the Campaigns workspace: {app_url}/campaigns",
        "",
        "Approve & send only the messages you want. Exclude anything you do not want mailed.",
        "Nothing in this review email contacts a prospect.",
        "",
    ]
    for index, draft in enumerate(drafts, 1):
        lines.extend([
            f"{index}. {draft.get('company_name', 'Unknown company')} <{draft.get('recipient_email', '')}>",
            f"Subject: {draft.get('subject', '')}",
            f"Context: {draft.get('company_context', '') or 'No company context supplied'}",
            f"Review: {app_url}/campaigns?draft_id={draft.get('_id')}",
            "",
        ])
    lines.extend(["Raj Mehta", "AI Automation Developer", "https://buildwithraj.com/"])
    try:
        result = gmail_service.send([owner_email], f"Review {len(drafts)} AI outreach email(s)", "\n".join(lines))
    except Exception as exc:
        logger.error(f"Campaign approval digest failed: {exc}")
        return {"status": "error", "count": len(drafts), "reason": "Gmail delivery failed"}
    if result.get("status") != "sent":
        return {"status": "error", "count": len(drafts), "reason": result.get("reason", "Digest was not sent")}
    now = datetime.utcnow()
    mongo_client.email_outbox.update_many({"_id": {"$in": [draft["_id"] for draft in drafts]}}, {"$set": {"approval_notified_at": now, "updated_at": now}})
    return {"status": "sent", "count": len(drafts), "recipient": owner_email}


async def email_campaign_worker() -> None:
    import asyncio
    while True:
        # Let FastAPI finish startup before the first synchronous Mongo/provider
        # work. Without this initial yield, an active campaign can block the
        # server's startup event while discovery is running.
        await asyncio.sleep(WORKER_INTERVAL_SECONDS)
        try:
            # These functions perform synchronous Mongo/provider/SMTP work;
            # keep them off FastAPI's event loop so health and UI requests
            # remain responsive during discovery and delivery.
            await asyncio.to_thread(refill_campaign_candidates)
            await asyncio.to_thread(notify_pending_approval_digest)
            # The background worker delivers drafts approved in the outbox and drafts created while approval
            # was off ("scheduled": approved by default). "pending_approval" and "awaiting_owner_review"
            # drafts still wait for the owner.
            approved_ids = await asyncio.to_thread(
                lambda: [str(item["_id"]) for item in mongo_client.email_outbox.find(
                    {"status": {"$in": ["approved", "scheduled"]}}, {"_id": 1}).sort("created_at", 1).limit(100)]
            )
            if approved_ids:
                await asyncio.to_thread(process_email_outbox, approved_ids)
        except Exception as exc:
            logger.error(f"Email campaign worker cycle failed: {exc}")
