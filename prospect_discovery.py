"""Google Places prospect discovery and website contact enrichment."""

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, List
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from pymongo.errors import DuplicateKeyError

from email_policy import approval_window_open, get_email_policy
from email_templates import get_email_templates, render_email_template, unsupported_template_variables
from brain_service import brain_service
from mongo_client import mongo_client


PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
EMAIL_PATTERN = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
IGNORED_EMAIL_HOSTS = {"example.com", "mysite.com", "yourdomain.com", "domain.com", "sentry.io", "wixpress.com", "wordpress.com"}
ASSET_EMAIL_EXTENSIONS = {"webp", "png", "jpg", "jpeg", "gif", "svg", "ico", "css", "js", "woff", "woff2"}


def _google_places_key() -> str:
    return (os.getenv("GOOGLE_PLACES_API_KEY") or os.getenv("GOOGLE_MAPS_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()


def _clean_website(value: str) -> str:
    if not value:
        return ""
    parsed = urlparse(value if value.startswith("http") else f"https://{value}")
    if not parsed.netloc:
        return value.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")


def _valid_contact_email(email: str) -> bool:
    if not email or "@" not in email:
        return False
    local, host = email.rsplit("@", 1)
    labels = host.lower().split(".")
    return bool(local and len(labels) >= 2 and all(labels) and len(labels[-1]) >= 2 and labels[-1] not in ASSET_EMAIL_EXTENSIONS)


def discovery_status() -> Dict[str, Any]:
    key = _google_places_key()
    last_run = None
    if mongo_client.is_connected():
        last_run = mongo_client.discovery_runs.find_one({}, sort=[("created_at", -1)])
    if last_run:
        last_run = {
            key: value.isoformat() if isinstance(value, datetime) else str(value) if key == "_id" else value
            for key, value in last_run.items()
            if key != "_id"
        }
    return {
        "provider": "google_places",
        "configured": bool(key),
        "environment": "production" if (os.getenv("RENDER") or os.getenv("APP_ENV", "").lower() in {"prod", "production"}) else "local",
        "last_run": last_run or None,
        "description": "Find USA businesses with Google Places, then inspect their public websites for contact emails.",
    }


def search_locations(query: str, limit: int = 8) -> List[Dict[str, str]]:
    """Return dynamic location suggestions from Google Places for campaign filters."""
    api_key = _google_places_key()
    query = str(query or "").strip()
    if not api_key or not query:
        return []
    response = requests.post(
        PLACES_URL,
        headers={"Content-Type": "application/json", "X-Goog-Api-Key": api_key, "X-Goog-FieldMask": "places.displayName,places.formattedAddress"},
        json={"textQuery": query, "pageSize": max(1, min(10, int(limit))), "languageCode": "en"},
        timeout=(5, 10),
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Google Places location search failed ({response.status_code})")
    suggestions = []
    for place in response.json().get("places", []):
        name = str((place.get("displayName") or {}).get("text") or "").strip()
        address = str(place.get("formattedAddress") or "").strip()
        value = address or name
        if value and value not in {item["value"] for item in suggestions}:
            suggestions.append({"value": value, "label": f"{name} — {address}" if name and address and name not in address else value})
    return suggestions


def _website_email(website: str) -> str:
    if not website:
        return ""
    parsed = urlparse(website if website.startswith("http") else f"https://{website}")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    urls = [parsed.geturl()]
    for suffix in ("/contact", "/contact-us", "/about"):
        urls.append(urljoin(parsed.geturl().rstrip("/") + "/", suffix.lstrip("/")))
    for url in urls[:3]:
        try:
            response = requests.get(url, timeout=(3, 5), headers={"User-Agent": "BuildWithRaj-ProspectResearch/1.0"})
            if response.status_code >= 400:
                continue
            soup = BeautifulSoup(response.text[:2_000_000], "html.parser")
            candidates = set(EMAIL_PATTERN.findall(response.text))
            candidates.update(link.get("href", "").replace("mailto:", "") for link in soup.select('a[href^="mailto:"]'))
            for candidate in sorted(candidates):
                email = candidate.strip(" <>.,;:\"'()[]").lower()
                host = email.rsplit("@", 1)[-1]
                ignored_host = host in IGNORED_EMAIL_HOSTS or any(host.endswith(f".{ignored}") for ignored in IGNORED_EMAIL_HOSTS)
                if _valid_contact_email(email) and not ignored_host and not email.startswith(("noreply@", "no-reply@", "donotreply@")):
                    return email
        except requests.RequestException:
            continue
    return ""


def _place_name(place: Dict[str, Any]) -> str:
    display = place.get("displayName") or {}
    return str(display.get("text") or place.get("name") or "Unknown business").strip()


def _make_context(place: Dict[str, Any], website: str, campaign_goal: str = "", shared_context: str = "", scrape_intent: str = "") -> str:
    type_name = str(place.get("primaryType") or "local business").replace("_", " ")
    address = str(place.get("formattedAddress") or "a US market").strip()
    factual = f"{_place_name(place)} appears to be a {type_name} based at {address}. Their public website is {website or 'not listed'}, so the first outreach should be validated before sending."
    parts = [factual]
    if campaign_goal.strip():
        parts.append(f"Campaign goal: {campaign_goal.strip()}")
    if scrape_intent.strip():
        parts.append(f"Why this lead was targeted: {scrape_intent.strip()}")
    if shared_context.strip():
        parts.append(f"Sender context: {shared_context.strip()}")
    return "\n\n".join(parts)


def _brain_shared_context() -> str:
    """Use the canonical AI Brain workspace context for every outreach draft."""
    brain = brain_service.brain_config()
    labels = {
        "company_information": "Company information",
        "business_description": "What we do",
        "services": "Services",
        "locations": "Locations",
        "working_hours": "Working hours",
        "website_content": "Website positioning",
        "policies": "Communication policies",
        "custom_knowledge": "Additional approved knowledge",
    }
    sections = []
    for key, label in labels.items():
        value = brain.get(key)
        if isinstance(value, (list, tuple)):
            value = "\n".join(str(item) for item in value if item)
        elif isinstance(value, dict):
            value = "\n".join(f"{name}: {content}" for name, content in value.items())
        value = str(value or "").strip()
        if value:
            sections.append(f"{label}:\n{value}")
    return "\n\n".join(sections)[:6000]


def _create_draft(prospect: Dict[str, Any], context: str, campaign_name: str = "USA AI automation outreach", campaign_goal: str = "", shared_context: str = "", template_id: str = "") -> str:
    email = str(prospect.get("email", "")).strip().lower()
    if not _valid_contact_email(email):
        return ""
    company_key = str(prospect.get("dedupe_key") or prospect.get("website") or prospect.get("company_name", "")).strip().lower()
    existing = mongo_client.email_outbox.find_one({"$or": [{"recipient_email": email}, {"company_key": company_key}]})
    if existing:
        return ""
    templates = get_email_templates()
    combined_context = f"{prospect.get('company_name', '')} {context}".lower()
    preferred_id = template_id.strip() or "practical-automation-intro"
    if not template_id.strip():
        if any(term in combined_context for term in ("appointment", "roofing", "plumb", "hvac", "cleaning", "clinic", "dental", "contractor", "home service", "call")):
            preferred_id = "voice-ai-appointment-intro"
        elif any(term in combined_context for term in ("lead", "sales", "follow-up", "enquir", "crm")):
            preferred_id = "follow-up-automation-intro"
    template = next((item for item in templates if item.get("id") == preferred_id and item.get("active", True)), None)
    if template_id.strip() and not template:
        return ""
    template = template or next((item for item in templates if item.get("active", True)), None)
    if not template:
        return ""
    rendered = render_email_template(template, {"company_name": prospect["company_name"], "company_context": context, "website": prospect.get("website", ""), "email": email, "campaign_goal": campaign_goal, "shared_context": shared_context})
    policy = get_email_policy()
    approval_needed = bool(policy.get("approval_required")) and approval_window_open(policy)
    now = datetime.utcnow()
    row = {
        "company_name": prospect["company_name"],
        "company_key": company_key,
        "recipient_email": email,
        "website": prospect.get("website", ""),
        "company_context": context,
        "campaign_goal": campaign_goal,
        "shared_context": shared_context,
        "campaign_name": campaign_name,
        "template_id": str(template.get("id", "")),
        "template_name": str(template.get("name", "Automatic outreach template")),
        "subject": rendered["subject"],
        "body": rendered["body"],
        "status": "pending_approval" if approval_needed else "scheduled",
        "source": "google_places_discovery",
        "created_at": now,
        "updated_at": now,
    }
    try:
        result = mongo_client.email_outbox.insert_one(row)
        return str(result.inserted_id)
    except DuplicateKeyError:
        return ""


def discover_prospects(query: str, location: str = "United States", max_results: int = 20, create_drafts: bool = True, target_drafts: int | None = None, campaign_name: str = "USA AI automation outreach", campaign_goal: str = "", shared_context: str = "", template_id: str = "", context_mode: str = "brain", scrape_intent: str = "") -> Dict[str, Any]:
    api_key = _google_places_key()
    if not api_key:
        raise RuntimeError("Google Places discovery is unavailable in the current backend environment.")
    if not mongo_client.is_connected():
        raise RuntimeError("Database is not connected")
    query = query.strip()
    location = location.strip() or "United States"
    if not query:
        raise ValueError("Enter a business type or search query")
    if template_id.strip():
        selected_template = next((item for item in get_email_templates() if str(item.get("id")) == template_id.strip() and item.get("active", True)), None)
        if not selected_template:
            raise ValueError("The selected email template is not available or active")
        unsupported = unsupported_template_variables(selected_template, {"campaign_goal": campaign_goal, "shared_context": shared_context})
        if unsupported:
            raise ValueError(f"The selected template has unsupported variables: {', '.join(unsupported)}")
    shared_context = shared_context.strip() if context_mode == "campaign" else _brain_shared_context()
    if context_mode == "campaign" and not shared_context:
        raise ValueError("Add campaign-specific context or choose AI Brain context")
    max_results = max(1, min(20, int(max_results)))
    target_drafts = max_results if target_drafts is None else max(1, min(60, int(target_drafts)))
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "places.id,places.displayName,places.formattedAddress,places.nationalPhoneNumber,places.internationalPhoneNumber,places.websiteUri,places.primaryType,places.googleMapsUri,places.businessStatus",
    }
    body = {"textQuery": f"{query} in {location}", "pageSize": max_results, "languageCode": "en"}
    found = ready = drafts = 0
    results: List[Dict[str, Any]] = []
    now = datetime.utcnow()
    next_page_token = ""
    for page_number in range(1, 4):
        request_body = {**body, **({"pageToken": next_page_token} if next_page_token else {})}
        response = None
        for attempt in range(1, 4):
            try:
                response = requests.post(PLACES_URL, headers=headers, json=request_body, timeout=(5, 15))
                if response.status_code < 500 or attempt == 3:
                    break
            except requests.RequestException:
                if attempt == 3:
                    raise
            time.sleep(attempt)
        if response.status_code >= 400:
            raise RuntimeError(f"Google Places request failed ({response.status_code}): {response.text[:300]}")
        places = response.json().get("places", [])
        websites = [str(place.get("websiteUri") or "").strip() for place in places]
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(websites)))) as executor:
            emails = list(executor.map(_website_email, websites))
        for place, email in zip(places, emails):
            company_name = _place_name(place)
            website = _clean_website(str(place.get("websiteUri") or "").strip())
            dedupe_key = str(place.get("id") or website or company_name.lower())
            context = _make_context(place, website, campaign_goal, shared_context, scrape_intent)
            prospect = {
            "dedupe_key": dedupe_key,
            "company_name": company_name,
            "email": email,
            "website": website,
            "address": place.get("formattedAddress", ""),
            "phone": place.get("internationalPhoneNumber") or place.get("nationalPhoneNumber", ""),
            "primary_type": place.get("primaryType", ""),
            "google_maps_url": place.get("googleMapsUri", ""),
            "business_status": place.get("businessStatus", ""),
            "source": "google_places",
            "discovery_query": query,
            "discovery_location": location,
            "status": "ready" if email else "missing_email",
            "created_at": now,
            "updated_at": now,
            }
            if email:
                prospect["email_key"] = email
            prospect_update = dict(prospect)
            prospect_update.pop("created_at", None)
            update = {"$set": prospect_update, "$setOnInsert": {"created_at": now}}
            if not email:
                # Sparse unique indexes ignore missing fields, but repeated
                # explicit nulls still collide. Clean up older null values.
                update["$unset"] = {"email_key": ""}
            mongo_client.campaign_prospects.update_one({"dedupe_key": dedupe_key}, update, upsert=True)
            found += 1
            if email:
                ready += 1
            draft_id = _create_draft(prospect, context, campaign_name, campaign_goal, shared_context, template_id) if create_drafts and email else ""
            if draft_id:
                drafts += 1
            results.append({"company_name": company_name, "email": email, "website": website, "status": prospect["status"], "draft_id": draft_id})
            if not create_drafts or drafts >= target_drafts:
                break
        if not create_drafts or drafts >= target_drafts:
            break
        next_page_token = response.json().get("nextPageToken") or ""
        if not next_page_token or not places:
            break
        time.sleep(1)
    run = {"provider": "google_places", "query": query, "location": location, "requested": max_results, "found": found, "ready": ready, "drafts_created": drafts, "created_at": now, "status": "completed", "campaign_name": campaign_name, "campaign_goal": campaign_goal, "scrape_intent": scrape_intent, "template_id": template_id}
    mongo_client.discovery_runs.insert_one(dict(run))
    run["results"] = results
    return run
