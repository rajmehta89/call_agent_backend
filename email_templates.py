"""Reusable outreach templates for owner-reviewed Gmail campaigns."""

from datetime import datetime
from typing import Any, Dict, List
from uuid import uuid4

from mongo_client import mongo_client


DEFAULT_EMAIL_TEMPLATES: List[Dict[str, Any]] = [
    {
        "id": "practical-automation-intro",
        "name": "Practical AI automation intro",
        "description": "A concise first-touch email for businesses that may have repetitive customer work.",
        "subject": "A practical AI automation idea for {{company_name}}",
        "body": "Hi,\n\nI work with businesses like {{company_name}} to automate customer conversations, lead qualification, follow-ups, appointment booking, and CRM updates using reliable AI agents.\n\n{{company_context}}\n\nIf improving this process is relevant for your team, I would be happy to share a practical approach.\n\nBest,\nRaj Mehta\nAI Automation Developer\nhttps://buildwithraj.com/",
        "built_in": True,
        "active": True,
    },
    {
        "id": "voice-ai-appointment-intro",
        "name": "Voice AI and appointment booking",
        "description": "For service businesses that handle calls, enquiries, or bookings.",
        "subject": "Could {{company_name}} reduce missed calls with Voice AI?",
        "body": "Hi,\n\nI help businesses like {{company_name}} handle inbound calls, qualify enquiries, book appointments, and update their CRM with Voice AI.\n\n{{company_context}}\n\nWould it be useful if I mapped a simple call-to-booking workflow for your business?\n\nBest,\nRaj Mehta\nAI Automation Developer\nhttps://buildwithraj.com/",
        "built_in": True,
        "active": True,
    },
    {
        "id": "follow-up-automation-intro",
        "name": "Lead follow-up automation",
        "description": "For companies that may lose leads because follow-up is manual or delayed.",
        "subject": "An idea for faster lead follow-up at {{company_name}}",
        "body": "Hi,\n\nI build AI workflows that respond to new leads, qualify them, send timely follow-ups, and keep the CRM updated automatically.\n\n{{company_context}}\n\nIf follow-up is currently handled manually, I can suggest a small workflow that your team can review before anything is automated.\n\nBest,\nRaj Mehta\nAI Automation Developer\nhttps://buildwithraj.com/",
        "built_in": True,
        "active": True,
    },
]


def get_email_templates() -> List[Dict[str, Any]]:
    if mongo_client.is_connected():
        stored = mongo_client.platform_settings.find_one({"key": "email_templates"})
        if stored and isinstance(stored.get("value"), list):
            return stored["value"]
    return [dict(template) for template in DEFAULT_EMAIL_TEMPLATES]


def save_email_templates(templates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if mongo_client.is_connected():
        now = datetime.utcnow()
        mongo_client.platform_settings.update_one(
            {"key": "email_templates"},
            {"$set": {"value": templates, "updated_at": now}, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
    return templates


def add_email_template(value: Dict[str, Any]) -> Dict[str, Any]:
    template = {
        "id": f"custom-{uuid4().hex[:12]}",
        "name": str(value.get("name") or "Custom outreach template").strip(),
        "description": str(value.get("description") or "Custom owner-reviewed outreach template").strip(),
        "subject": str(value.get("subject") or "A practical AI idea for {{company_name}}").strip(),
        "body": str(value.get("body") or "Hi,\n\nI would like to share a practical automation idea for {{company_name}}.\n\nBest,\nRaj").strip(),
        "built_in": False,
        "active": True,
    }
    templates = get_email_templates()
    templates.append(template)
    save_email_templates(templates)
    return template


def render_email_template(template: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, str]:
    """Render a template with campaign or automation context."""
    replacements = {
        "company_name": context.get("company_name") or context.get("company") or context.get("customer_name") or "the business",
        "company_context": context.get("company_context") or context.get("message") or "I noticed there may be an opportunity to make this process faster and easier for your team.",
        "website": context.get("website") or "",
        "recipient_email": context.get("recipient_email") or context.get("email") or "",
        "name": context.get("name") or context.get("customer_name") or "there",
        "customer_name": context.get("customer_name") or context.get("name") or "there",
        "message": context.get("message") or "",
        "status": context.get("status") or "",
    }

    def render(value: Any) -> str:
        result = str(value or "")
        for key, replacement in replacements.items():
            result = result.replace("{{" + key + "}}", str(replacement))
        return result.strip()

    return {"subject": render(template.get("subject")), "body": render(template.get("body"))}
