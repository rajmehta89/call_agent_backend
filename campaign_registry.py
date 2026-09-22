"""Reusable campaign definitions.

The email sender still uses the legacy email_campaign settings for delivery,
while this registry gives the workspace a stable place for multiple campaign
types and future channel-specific workers.
"""

from datetime import datetime
from typing import Any, Dict, List
from uuid import uuid4

from mongo_client import mongo_client


CAMPAIGN_TYPES = {
    "email_outreach": {"label": "Email outreach", "description": "Personalised prospect emails with review and sending limits.", "channel": "Email"},
    "whatsapp_followup": {"label": "WhatsApp follow-up", "description": "Follow up with leads and customers through WhatsApp.", "channel": "WhatsApp"},
    "voice_outreach": {"label": "Voice outreach", "description": "Call prospects with a voice AI agent and track outcomes.", "channel": "Voice"},
    "lead_nurture": {"label": "Lead nurture", "description": "Move leads through timed multi-step follow-up sequences.", "channel": "Omnichannel"},
}

DEFAULT_CAMPAIGNS = [
    {
        "id": "usa-ai-automation-outreach",
        "name": "USA AI automation outreach",
        "type": "email_outreach",
        "status": "draft",
        "audience": "USA home-service businesses",
        "steps": ["Find prospects", "Personalise email", "Review and send"],
        "description": "The existing review-first Gmail outreach campaign.",
        "scrape": {"query": "AI automation for home services", "location": "United States", "max_results": 20, "create_drafts": True},
    }
]


def _normalise(item: Dict[str, Any]) -> Dict[str, Any]:
    campaign = {**item}
    campaign["id"] = str(campaign.get("id") or uuid4().hex)
    campaign["name"] = str(campaign.get("name") or "Untitled campaign").strip()[:120]
    campaign["type"] = str(campaign.get("type") or "email_outreach").strip()
    if campaign["type"] not in CAMPAIGN_TYPES:
        campaign["type"] = "email_outreach"
    campaign["status"] = str(campaign.get("status") or "draft").lower()
    if campaign["status"] not in {"draft", "ready", "active", "paused", "completed"}:
        campaign["status"] = "draft"
    campaign["audience"] = str(campaign.get("audience") or "").strip()[:200]
    campaign["description"] = str(campaign.get("description") or "").strip()[:500]
    campaign["steps"] = [str(step).strip() for step in (campaign.get("steps") or []) if str(step).strip()][:12]
    scrape = campaign.get("scrape") if isinstance(campaign.get("scrape"), dict) else {}
    campaign["scrape"] = {
        "query": str(scrape.get("query") or "").strip()[:200],
        "location": str(scrape.get("location") or "United States").strip()[:120],
        "max_results": max(1, min(20, int(scrape.get("max_results", 20)))),
        "create_drafts": bool(scrape.get("create_drafts", True)),
    }
    campaign["last_scrape"] = campaign.get("last_scrape") or None
    campaign["updated_at"] = campaign.get("updated_at") or datetime.utcnow().isoformat() + "Z"
    return campaign


def get_campaigns() -> List[Dict[str, Any]]:
    campaigns = None
    if mongo_client.is_connected():
        stored = mongo_client.platform_settings.find_one({"key": "campaign_registry"})
        if stored and isinstance(stored.get("value"), list):
            campaigns = stored["value"]
    return [_normalise(item) for item in (campaigns or DEFAULT_CAMPAIGNS)]


def save_campaigns(campaigns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result = [_normalise(item) for item in campaigns]
    if mongo_client.is_connected():
        mongo_client.platform_settings.update_one(
            {"key": "campaign_registry"},
            {"$set": {"value": result, "updated_at": datetime.utcnow()}, "$setOnInsert": {"created_at": datetime.utcnow()}},
            upsert=True,
        )
    return result


def create_campaign(value: Dict[str, Any]) -> Dict[str, Any]:
    campaign = _normalise(value)
    campaign["updated_at"] = datetime.utcnow().isoformat() + "Z"
    save_campaigns([*get_campaigns(), campaign])
    return campaign


def update_campaign(campaign_id: str, value: Dict[str, Any]) -> Dict[str, Any]:
    campaigns = get_campaigns()
    for index, current in enumerate(campaigns):
        if current["id"] == campaign_id:
            campaigns[index] = _normalise({**current, **value, "id": campaign_id, "updated_at": datetime.utcnow().isoformat() + "Z"})
            save_campaigns(campaigns)
            return campaigns[index]
    raise KeyError("Campaign not found")


def delete_campaign(campaign_id: str) -> None:
    campaigns = get_campaigns()
    if not any(item["id"] == campaign_id for item in campaigns):
        raise KeyError("Campaign not found")
    save_campaigns([item for item in campaigns if item["id"] != campaign_id])
