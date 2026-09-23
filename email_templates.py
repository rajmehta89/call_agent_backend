"""Reusable outreach templates for owner-reviewed Gmail campaigns."""

from datetime import datetime
from typing import Any, Dict, List
import re
from uuid import uuid4

from mongo_client import mongo_client


DEFAULT_EMAIL_TEMPLATES: List[Dict[str, Any]] = [
    {
        "id": "practical-automation-intro",
        "name": "Practical AI automation intro",
        "description": "Short, specific first-touch email with one practical automation idea and a low-pressure CTA.",
        "subject": "A practical idea for {{company_name}}'s lead follow-up",
        "body": "Hi,\n\nI’m Raj Mehta, an AI Automation Developer. I help service businesses use AI agents and practical workflows to handle enquiries, qualify leads, follow up consistently, and keep their CRM updated.\n\nI noticed {{company_name}} and had one practical idea: a lightweight AI workflow could respond to new enquiries without changing your current team.\n\n{{company_context}}\n\nWould a short example be useful? If it is not relevant, no worries.\n\nBest,\nRaj Mehta\nAI Automation Developer\nhttps://buildwithraj.com/",
        "built_in": True,
        "active": True,
        "variables": ["company_name", "company_context", "website"],
    },
    {
        "id": "voice-ai-appointment-intro",
        "name": "Voice AI and appointment booking",
        "description": "For service businesses handling calls, enquiries, estimates, or appointments.",
        "subject": "Could {{company_name}} reduce missed enquiries with Voice AI?",
        "body": "Hi,\n\nI’m Raj Mehta, an AI Automation Developer. I build Voice AI systems that help service businesses answer common questions, qualify enquiries, book appointments, and update their CRM.\n\nI noticed {{company_name}} serves customers who may call or request an appointment, so this may be relevant to your team.\n\n{{company_context}}\n\nWould a short example be useful?\n\nBest,\nRaj Mehta\nAI Automation Developer\nhttps://buildwithraj.com/",
        "built_in": True,
        "active": True,
        "variables": ["company_name", "company_context", "website"],
    },
    {
        "id": "follow-up-automation-intro",
        "name": "Lead follow-up automation",
        "description": "For companies where new enquiries need faster, consistent follow-up.",
        "subject": "A quick follow-up idea for {{company_name}}",
        "body": "Hi,\n\nI’m Raj Mehta, an AI Automation Developer. I help teams automate repetitive customer and lead workflows with AI agents, integrations, and reliable follow-up systems.\n\nA quick idea for {{company_name}}: automate new-lead follow-up so every enquiry gets a timely response and your CRM stays updated.\n\n{{company_context}}\n\nWorth sending a short outline?\n\nBest,\nRaj Mehta\nAI Automation Developer\nhttps://buildwithraj.com/",
        "built_in": True,
        "active": True,
        "variables": ["company_name", "company_context", "website"],
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
            return [_clean_template_text(template) for template in merged]
    return [_clean_template_text(dict(template)) for template in DEFAULT_EMAIL_TEMPLATES]


def _clean_template_text(template: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize common encoding artifacts before templates reach the editor or sender."""
    cleaned = dict(template)
    for field in ("name", "description", "subject", "body"):
        if field in cleaned:
            cleaned[field] = str(cleaned[field]).replace("â€™", "'").replace("�", "'").replace("â€“", "-").replace("â€”", "-")
    return cleaned


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
        "variables": [str(item).strip().lower().replace(" ", "_") for item in (value.get("variables") or []) if str(item).strip()],
        "variable_descriptions": {str(key): str(val).strip() for key, val in (value.get("variable_descriptions") or {}).items() if str(key).strip() and str(val).strip()},
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
        "campaign_goal": context.get("campaign_goal") or "",
        "shared_context": context.get("shared_context") or "",
    }

    def render(value: Any) -> str:
        result = str(value or "")
        for key, replacement in replacements.items():
            result = result.replace("{{" + key + "}}", str(replacement))
        return re.sub(r"{{\s*[a-zA-Z_][a-zA-Z0-9_]*\s*}}", "", result).strip()

    return {"subject": render(template.get("subject")), "body": render(template.get("body"))}


def unsupported_template_variables(template: Dict[str, Any], context: Dict[str, Any]) -> List[str]:
    """Return placeholders that cannot be resolved for this draft context."""
    available = {
        "company_name", "company_context", "website", "recipient_email", "name",
        "customer_name", "message", "status", "campaign_goal", "shared_context",
        *[str(key) for key in context.keys()],
    }
    tokens = re.findall(r"{{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}}", f"{template.get('subject', '')}\n{template.get('body', '')}")
    return sorted({token for token in tokens if token not in available})
