"""Reusable outreach templates for owner-reviewed Gmail campaigns."""

from datetime import datetime
from typing import Any, Dict, List
from uuid import uuid4

from mongo_client import mongo_client


DEFAULT_EMAIL_TEMPLATES: List[Dict[str, Any]] = [
    {
        "id": "practical-automation-intro",
        "name": "Practical AI automation intro",
        "description": "Short, specific first-touch email with one practical automation idea and a low-pressure CTA.",
        "subject": "A practical idea for {{company_name}}'s lead follow-up",
        "body": "Hi,\n\nI noticed {{company_name}} and had one practical idea: a lightweight AI workflow could respond to new enquiries, qualify leads, and follow up automatically without changing your current team.\n\n{{company_context}}\n\nWould a short example be useful? If it is not relevant, no worries.\n\nBest,\nRaj Mehta\nAI Automation Developer\nhttps://buildwithraj.com/",
        "built_in": True,
        "active": True,
    },
    {
        "id": "voice-ai-appointment-intro",
        "name": "Voice AI and appointment booking",
        "description": "For service businesses handling calls, enquiries, estimates, or appointments.",
        "subject": "Could {{company_name}} reduce missed enquiries with Voice AI?",
        "body": "Hi,\n\nI noticed {{company_name}} serves customers who may call or request an appointment. I build Voice AI that answers common questions, qualifies enquiries, books appointments, and updates the CRM.\n\n{{company_context}}\n\nWould a short example be useful?\n\nBest,\nRaj Mehta\nAI Automation Developer\nhttps://buildwithraj.com/",
        "built_in": True,
        "active": True,
    },
    {
        "id": "follow-up-automation-intro",
        "name": "Lead follow-up automation",
        "description": "For companies where new enquiries need faster, consistent follow-up.",
        "subject": "A quick follow-up idea for {{company_name}}",
        "body": "Hi,\n\nA quick idea for {{company_name}}: automate new-lead follow-up so every enquiry gets a timely response and your CRM stays updated.\n\n{{company_context}}\n\nWorth sending a short outline?\n\nBest,\nRaj Mehta\nAI Automation Developer\nhttps://buildwithraj.com/",
        "built_in": True,
        "active": True,
    },
]


def get_email_templates() -> List[Dict[str, Any]]:
    if mongo_client.is_connected():
        stored = mongo_client.platform_settings.find_one({"key": "email_templates"})
        if stored and isinstance(stored.get("value"), list):
            defaults = {template["id"]: template for template in DEFAULT_EMAIL_TEMPLATES}
            merged = []
            seen = set()
            for template in stored["value"]:
                template_id = str(template.get("id") or "")
                if template.get("built_in") and template_id in defaults:
                    merged.append({**template, **defaults[template_id]})
                else:
                    merged.append(template)
                seen.add(template_id)
            merged.extend(template for template in DEFAULT_EMAIL_TEMPLATES if template["id"] not in seen)
            return merged
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
