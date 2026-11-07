import logging
import os
from datetime import datetime
from typing import Any, Dict, Iterable, Optional
from urllib.parse import quote

from mongo_client import mongo_client

logger = logging.getLogger(__name__)


def resolve_inbound_workspace_id(preferred: Optional[str] = None) -> str:
    """Resolve an unauthenticated voice/SMS event without crossing workspace boundaries.

    Provider webhooks have no signed-in user. Use an explicit inbound workspace setting when
    the database serves several workspaces; otherwise infer the only active workspace.
    """
    value = str(preferred or os.getenv("INBOUND_WORKSPACE_ID") or "").strip()
    if value:
        return value
    try:
        if mongo_client.is_connected() and hasattr(mongo_client, "users") and hasattr(mongo_client.users, "distinct"):
            workspaces = {
                str(item).strip()
                for item in mongo_client.users.distinct("workspace_id", {"active": {"$ne": False}})
                if str(item or "").strip()
            }
            missing_workspace = mongo_client.users.count_documents({
                "active": {"$ne": False},
                "$or": [{"workspace_id": {"$exists": False}}, {"workspace_id": None}, {"workspace_id": ""}],
            })
            if missing_workspace:
                workspaces.add("default")
            if len(workspaces) == 1:
                return next(iter(workspaces))
            if len(workspaces) > 1:
                logger.error("notifications: inbound workspace is ambiguous; configure INBOUND_WORKSPACE_ID")
    except Exception:
        logger.exception("notifications: could not resolve the inbound workspace")
    return "default"


def notify_human_assignment(
    *,
    channel: str,
    customer: str,
    recipient: str,
    conversation_id: Optional[str] = None,
    call_session_id: Optional[str] = None,
    message: Optional[str] = None,
) -> bool:
    """Create one durable in-app notification for a human assignment."""
    if not mongo_client.is_connected() or not hasattr(mongo_client, "notifications"):
        return False
    now = datetime.utcnow()
    # Clicking it opens the caller's inbox conversation (live transcript included); same key as inbox_api._thread_key.
    from routers.calls_api import caller_identity
    identity = caller_identity(customer or "")
    link = f"/inbox?open={quote(identity, safe='')}" if channel == "voice" and identity.startswith(("+", "client:")) else "/inbox" if channel == "voice" else None
    mongo_client.notifications.insert_one({
        "type": "human_assignment",
        "channel": channel,
        "title": f"New {channel} handoff",
        "message": message or f"{customer or 'A customer'} is waiting for human assistance.",
        "customer": customer,
        "recipient": recipient or "Human team",
        "conversation_id": conversation_id,
        "call_session_id": call_session_id,
        "link": link,
        "created_at": now,
        "read": False,
    })
    return True


def notify_recipients(
    *,
    channel: str,
    customer: str,
    recipients: Iterable[str],
    conversation_id: Optional[str] = None,
    call_session_id: Optional[str] = None,
    message: Optional[str] = None,
) -> int:
    count = 0
    seen = set()
    for recipient in recipients:
        name = str(recipient or "Human team").strip()
        if name in seen:
            continue
        seen.add(name)
        count += int(notify_human_assignment(
            channel=channel,
            customer=customer,
            recipient=name,
            conversation_id=conversation_id,
            call_session_id=call_session_id,
            message=message,
        ))
    return count


def notify_team_member_registered(*, name: str, email: str, role: str, link: str = "/settings/team") -> bool:
    """Create an admin-only notification after an invited member completes registration."""
    if not mongo_client.is_connected() or not hasattr(mongo_client, "notifications"):
        return False
    mongo_client.notifications.insert_one({
        "type": "team_member_registered",
        "channel": "admin",
        "recipient": "admins",
        "title": "New team member registered",
        "message": f"{name} ({email}) joined the workspace as {role}.",
        "link": link,
        "created_at": datetime.utcnow(),
        "read": False,
    })
    return True


def notify_new_lead(lead: Dict[str, Any], *, workspace_id: Optional[str] = None, source: str = "inbound") -> int:
    """Post a new lead to the shared channel and notify each active sales user."""
    if not mongo_client.is_connected() or not all(hasattr(mongo_client, name) for name in ("notifications", "team_messages", "users")):
        return 0

    lead_id = str(lead.get("_id") or "")
    if not lead_id:
        return 0
    workspace = str(workspace_id or lead.get("workspace_id") or resolve_inbound_workspace_id())
    name = str(lead.get("name") or "New caller").strip()[:120]
    phone = str(lead.get("phone") or "").strip()[:60]
    email = str(lead.get("email") or "").strip()[:180]
    source_label = {"voice": "phone call", "retell": "phone call", "sms": "SMS", "manual": "CRM entry", "csv_import": "CSV import"}.get(source.lower(), source.replace("_", " "))
    now = datetime.utcnow()
    channel_link = "/inbox/channel"
    lead_link = "/leads"
    details = [f"Name: {name}"]
    if phone:
        details.append(f"Phone: {phone}")
    if email:
        details.append(f"Email: {email}")
    details.append(f"Source: {source_label}")
    notes = str(lead.get("notes") or "").strip()
    if notes:
        details.append(f"Notes: {notes[:400]}")
    message_text = ("New lead added" if source.lower() == "manual" else "New inbound lead received") + "\n" + "\n".join(details)

    try:
        mongo_client.team_messages.insert_one({
            "workspace_id": workspace,
            "author_id": "system",
            "author_name": "RelayDesk",
            "text": message_text[:4000],
            "mentions": [],
            "mention_names": [],
            "conversation_id": None,
            "read_by": [],
            "type": "new_lead",
            "lead_id": lead_id,
            "link": channel_link,
            "created_at": now,
        })
    except Exception:
        logger.exception("notifications: could not post new lead %s in the team channel", lead_id)

    # Agents who can work leads receive their own durable notification. Keep this
    # independent from the channel write so one alert path can still succeed alone.
    count = 0
    try:
        from auth_service import has_permission

        members = mongo_client.users.find({"workspace_id": workspace, "active": {"$ne": False}})
        for member in members:
            try:
                if not has_permission(member, "manage_assigned_leads"):
                    continue
                recipient = str(member.get("email") or member.get("name") or "").strip()
                if not recipient:
                    continue
                mongo_client.notifications.insert_one({
                    "type": "new_lead",
                    "channel": "lead",
                    "title": f"New lead: {name}",
                    "message": (f"{name} was added from a {source_label}. Please follow up." if source.lower() == "manual"
                                else f"{name}{f' ({phone})' if phone else ''} contacted us by {source_label}. Please follow up."),
                    "recipient": recipient,
                    "recipient_id": str(member.get("_id") or ""),
                    "workspace_id": workspace,
                    "lead_id": lead_id,
                    "link": lead_link,
                    "created_at": now,
                    "read": False,
                })
                count += 1
            except Exception:
                logger.exception("notifications: could not notify a sales user about lead %s", lead_id)
    except Exception:
        logger.exception("notifications: could not load sales users for lead %s", lead_id)

    try:
        import realtime
        realtime.publish("lead", key=lead_id, workspace=workspace)
    except Exception:
        logger.exception("notifications: could not publish live lead alert for %s", lead_id)
    logger.info("notifications: shared new lead %s with %d sales user(s)", lead_id, count)
    return count


def notify_new_leads_imported(count: int, *, workspace_id: Optional[str] = None) -> int:
    """Notify once per CSV import batch so a large upload does not flood the channel or bell."""
    if count <= 0 or not mongo_client.is_connected() or not all(hasattr(mongo_client, name) for name in ("notifications", "team_messages", "users")):
        return 0
    workspace = str(workspace_id or resolve_inbound_workspace_id())
    now = datetime.utcnow()
    noun = "lead" if count == 1 else "leads"
    title = f"{count} new {noun} imported"
    message = f"{count} {noun} were imported from a CSV. Review them in Leads."
    try:
        mongo_client.team_messages.insert_one({
            "workspace_id": workspace,
            "author_id": "system",
            "author_name": "RelayDesk",
            "text": f"{title}\n{message}",
            "mentions": [],
            "mention_names": [],
            "conversation_id": None,
            "read_by": [],
            "type": "new_lead",
            "link": "/leads",
            "created_at": now,
        })
    except Exception:
        logger.exception("notifications: could not post the CSV lead import in the team channel")

    notified = 0
    try:
        from auth_service import has_permission

        members = mongo_client.users.find({"workspace_id": workspace, "active": {"$ne": False}})
        for member in members:
            try:
                if not has_permission(member, "manage_assigned_leads"):
                    continue
                recipient = str(member.get("email") or member.get("name") or "").strip()
                if not recipient:
                    continue
                mongo_client.notifications.insert_one({
                    "type": "new_lead",
                    "channel": "lead",
                    "title": title,
                    "message": message,
                    "recipient": recipient,
                    "recipient_id": str(member.get("_id") or ""),
                    "workspace_id": workspace,
                    "lead_count": count,
                    "link": "/leads",
                    "created_at": now,
                    "read": False,
                })
                notified += 1
            except Exception:
                logger.exception("notifications: could not notify a sales user about the CSV lead import")
    except Exception:
        logger.exception("notifications: could not load sales users for the CSV lead import")

    try:
        import realtime
        realtime.publish("lead", key=f"csv-import:{int(now.timestamp())}", workspace=workspace)
    except Exception:
        logger.exception("notifications: could not publish the CSV lead import")
    logger.info("notifications: shared CSV import of %d leads with %d sales user(s)", count, notified)
    return notified
