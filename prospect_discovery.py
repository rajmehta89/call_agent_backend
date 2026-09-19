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
from email_templates import get_email_templates, render_email_template
from mongo_client import mongo_client


PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
EMAIL_PATTERN = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
IGNORED_EMAIL_HOSTS = {"example.com", "sentry.io", "wixpress.com", "wordpress.com"}


def discovery_status() -> Dict[str, Any]:
    key = (os.getenv("GOOGLE_PLACES_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
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
        "last_run": last_run or None,
        "description": "Find USA businesses with Google Places, then inspect their public websites for contact emails.",
    }


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
                if "@" in email and host not in IGNORED_EMAIL_HOSTS:
                    return email
        except requests.RequestException:
            continue
    return ""


def _place_name(place: Dict[str, Any]) -> str:
    display = place.get("displayName") or {}
    return str(display.get("text") or place.get("name") or "Unknown business").strip()


def _make_context(place: Dict[str, Any], website: str) -> str:
    type_name = str(place.get("primaryType") or "local business").replace("_", " ")
    address = str(place.get("formattedAddress") or "a US market").strip()
    return f"{_place_name(place)} appears to be a {type_name} based at {address}. Their public website is {website or 'not listed'}, so the first outreach should be validated before sending."


def _create_draft(prospect: Dict[str, Any], context: str) -> str:
    email = str(prospect.get("email", "")).strip().lower()
    if not email:
        return ""
    existing = mongo_client.email_outbox.find_one({"recipient_email": email})
    if existing:
        return ""
    templates = get_email_templates()
    combined_context = f"{prospect.get('company_name', '')} {context}".lower()
    preferred_id = "practical-automation-intro"
    if any(term in combined_context for term in ("appointment", "roofing", "plumb", "hvac", "cleaning", "clinic", "dental", "contractor", "home service", "call")):
        preferred_id = "voice-ai-appointment-intro"
    elif any(term in combined_context for term in ("lead", "sales", "follow-up", "enquir", "crm")):
        preferred_id = "follow-up-automation-intro"
    template = next((item for item in templates if item.get("id") == preferred_id and item.get("active", True)), None)
    template = template or next((item for item in templates if item.get("active", True)), None)
    if not template:
        return ""
    rendered = render_email_template(template, {"company_name": prospect["company_name"], "company_context": context, "website": prospect.get("website", ""), "email": email})
    policy = get_email_policy()
    approval_needed = bool(policy.get("approval_required")) and approval_window_open(policy)
    now = datetime.utcnow()
    row = {
        "company_name": prospect["company_name"],
        "recipient_email": email,
        "website": prospect.get("website", ""),
        "company_context": context,
        "campaign_name": "USA AI automation outreach",
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


def discover_prospects(query: str, location: str = "United States", max_results: int = 20, create_drafts: bool = True, target_drafts: int | None = None) -> Dict[str, Any]:
    api_key = (os.getenv("GOOGLE_PLACES_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("Google Places is not configured. Add GOOGLE_PLACES_API_KEY to the backend environment.")
    if not mongo_client.is_connected():
        raise RuntimeError("Database is not connected")
    query = query.strip()
    location = location.strip() or "United States"
    if not query:
        raise ValueError("Enter a business type or search query")
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
            website = str(place.get("websiteUri") or "").strip()
            dedupe_key = str(place.get("id") or website or company_name.lower())
            context = _make_context(place, website)
            prospect = {
            "dedupe_key": dedupe_key,
            "company_name": company_name,
            "email": email,
            "email_key": email or None,
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
            prospect_update = dict(prospect)
            prospect_update.pop("created_at", None)
            mongo_client.campaign_prospects.update_one({"dedupe_key": dedupe_key}, {"$set": prospect_update, "$setOnInsert": {"created_at": now}}, upsert=True)
            found += 1
            if email:
                ready += 1
            draft_id = _create_draft(prospect, context) if create_drafts and email else ""
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
    run = {"provider": "google_places", "query": query, "location": location, "requested": max_results, "found": found, "ready": ready, "drafts_created": drafts, "created_at": now, "status": "completed"}
    mongo_client.discovery_runs.insert_one(dict(run))
    run["results"] = results
    return run
