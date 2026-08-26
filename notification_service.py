from datetime import datetime
from typing import Any, Dict, Iterable, Optional

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
    mongo_client.notifications.insert_one({
        "type": "human_assignment",
        "channel": channel,
        "title": f"New {channel} handoff",
        "message": message or f"{customer or 'A customer'} is waiting for human assistance.",
        "customer": customer,
        "recipient": recipient or "Human team",
        "conversation_id": conversation_id,
        "call_session_id": call_session_id,
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
