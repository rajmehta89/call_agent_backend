from datetime import datetime
from typing import Any, Dict, Iterable, Optional
from urllib.parse import quote

from mongo_client import mongo_client


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


def notify_new_lead(lead: Dict[str, Any], *, workspace_id: str = "default", source: str = "inbound") -> int:
    """Post a new lead to the shared channel and notify each active sales user."""
    if not mongo_client.is_connected() or not all(hasattr(mongo_client, name) for name in ("notifications", "team_messages", "users")):
        return 0

    lead_id = str(lead.get("_id") or "")
    if not lead_id:
        return 0
    workspace = str(workspace_id or lead.get("workspace_id") or "default")
    name = str(lead.get("name") or "New caller").strip()[:120]
    phone = str(lead.get("phone") or "").strip()[:60]
    email = str(lead.get("email") or "").strip()[:180]
    source_label = {"voice": "phone call", "retell": "phone call", "sms": "SMS", "manual": "CRM entry"}.get(source.lower(), source.replace("_", " "))
    now = datetime.utcnow()
    link = "/inbox/channel"
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
            "link": link,
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
                    "link": link,
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
