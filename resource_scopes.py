"""Shared record visibility for alternate APIs as well as the primary lists."""

from typing import Any, Dict
from auth_service import can_assign_conversations


def lead_scope(user: Dict[str, Any]) -> Dict[str, Any]:
    return {} if can_assign_conversations(user) else {"assigned_to": str(user.get("_id") or user.get("id"))}


def call_scope(user: Dict[str, Any], database: Any) -> Dict[str, Any]:
    if can_assign_conversations(user):
        return {}
    ids = [lead["_id"] for lead in database.leads.find(lead_scope(user), {"_id": 1})]
    return {"lead_id": {"$in": ids + [str(value) for value in ids]}}


def phone_scope(user: Dict[str, Any], database: Any, field: str = "phone") -> Dict[str, Any]:
    if can_assign_conversations(user):
        return {}
    phones = [lead["phone"] for lead in database.leads.find(lead_scope(user), {"phone": 1}) if lead.get("phone")]
    return {field: {"$in": phones}}


def intersect(*queries: Dict[str, Any]) -> Dict[str, Any]:
    parts = [query for query in queries if query]
    return {"$and": parts} if len(parts) > 1 else parts[0] if parts else {}
