"""Stage Dubai review drafts only for emails found on matching official websites.

Uses Google Places to locate each business website, then reads its public pages.
No prospect email is sent. Safe to rerun; existing outreach is never duplicated.
"""

import argparse
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from env_loader import load_project_env  # noqa: E402

load_project_env()

CAMPAIGN = "Dubai voice WhatsApp CRM outreach"
PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
AGENT = {"User-Agent": "BuildWithRaj-ProspectResearch/1.0"}
NAME_OMIT = {"llc", "ltd", "limited", "fze", "fz", "fzc", "the", "dubai", "uae", "international"}
EMAIL_OMIT = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "example.com"}
ROLE_OMIT = ("noreply", "no-reply", "privacy", "careers", "jobs", "media", "press", "admissions", "cv", "reservations", "booking", "book", "ops")


def words(name):
    return [word for word in re.findall(r"[a-z0-9]+", name.lower()) if word not in NAME_OMIT]


def name_score(expected, actual):
    left, right = words(expected), words(actual)
    if not left or not right:
        return 0
    sequence = SequenceMatcher(None, " ".join(left), " ".join(right)).ratio()
    overlap = len(set(left) & set(right)) / len(set(left))
    return max(sequence, overlap if len(left) >= 2 else 0)


def find_website(company, key):
    response = requests.post(
        PLACES_URL,
        headers={"Content-Type": "application/json", "X-Goog-Api-Key": key,
                 "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.websiteUri,places.businessStatus"},
        json={"textQuery": f"{company} Dubai UAE", "pageSize": 5, "languageCode": "en"},
        timeout=(5, 15),
    )
    response.raise_for_status()
    choices = []
    for place in response.json().get("places", []):
        name = str((place.get("displayName") or {}).get("text") or "")
        address = str(place.get("formattedAddress") or "")
        website = str(place.get("websiteUri") or "")
        score = name_score(company, name)
        if score >= 0.82 and website and place.get("businessStatus") != "CLOSED_PERMANENTLY" and ("dubai" in address.lower() or "united arab emirates" in address.lower()):
            choices.append((score, website))
    return max(choices, default=(0, ""))[1]


def official_email(website):
    parsed = urlparse(website)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    host = parsed.hostname.lower().removeprefix("www.")
    base = f"https://{parsed.hostname}"
    pages = [website, urljoin(base, "/contact"), urljoin(base, "/contact-us"), urljoin(base, "/about-us")]
    seen = set()
    for page in pages:
        if page in seen:
            continue
        seen.add(page)
        try:
            response = requests.get(page, headers=AGENT, timeout=(5, 10), allow_redirects=True)
            final_host = (urlparse(response.url).hostname or "").lower().removeprefix("www.")
            if response.status_code >= 400 or final_host != host or "html" not in response.headers.get("Content-Type", "").lower():
                continue
            soup = BeautifulSoup(response.text[:2_000_000], "html.parser")
            for tag in soup(["script", "style", "noscript", "svg"]):
                tag.decompose()
            candidates = []
            for link in soup.select('a[href^="mailto:"]'):
                candidates.append(link.get("href", "").removeprefix("mailto:").split("?", 1)[0])
            candidates.extend(EMAIL.findall(soup.get_text(" ", strip=True)))
            for candidate in candidates:
                email = candidate.strip(" <>.,;:\"'()[]").lower()
                email = re.sub(r"^(?:%20|\s)+", "", email)
                domain = email.rsplit("@", 1)[-1]
                if domain != host and not domain.endswith("." + host):
                    continue
                if domain in EMAIL_OMIT or email.split("@", 1)[0].split(".", 1)[0] in ROLE_OMIT:
                    continue
                if EMAIL.fullmatch(email):
                    return email, response.url
        except requests.RequestException:
            continue
    return None


def research(prospect, key):
    try:
        website = find_website(prospect["company_name"], key)
        if not website:
            return prospect, None, "no_matching_place"
        found = official_email(website)
        return prospect, found, "verified" if found else "no_public_official_email"
    except requests.RequestException:
        return prospect, None, "research_error"


def copy_for(prospect):
    company = prospect["company_name"]
    industry = str(prospect.get("industry") or "").lower()
    if any(word in industry for word in ("property", "real estate")):
        idea = "collect buyer and tenant enquiries, route them to the right agent, and log follow-up in your CRM"
    elif any(word in industry for word in ("clinic", "health", "dental")):
        idea = "answer approved administrative questions, collect appointment requests, and hand clinical questions to staff"
    elif any(word in industry for word in ("maintenance", "cleaning", "construction")):
        idea = "capture service requests, route urgent work to staff, and update your CRM or job system"
    elif any(word in industry for word in ("automotive", "rental", "dealership")):
        idea = "collect vehicle enquiry details, arrange a staff handoff, and log follow-up in your CRM"
    elif any(word in industry for word in ("travel", "tour", "hospitality")):
        idea = "collect travel dates and preferences, route complex questions to staff, and record follow-up in your CRM"
    else:
        idea = "capture incoming enquiries, route requests to the right person, and keep CRM follow-up organised"
    subject = f"A voice + WhatsApp enquiry idea for {company}"[:180]
    body = (f"Hi {company} team,\n\nI'm Raj Mehta. I build AI voice and WhatsApp workflows for businesses. "
            f"One possible workflow for {company} could {idea}.\n\nWould a short example be useful?\n\n"
            "Best,\nRaj Mehta\nAI Automation Developer\nhttps://buildwithraj.com/\n\n"
            'If this is not relevant, reply "no thanks" and I won\'t follow up.')
    return subject, body


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="Maximum rows to research; 0 means all")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    key = os.getenv("GOOGLE_PLACES_API_KEY") or os.getenv("GOOGLE_MAPS_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key or not os.getenv("MONGO_URI"):
        raise RuntimeError("Google Places key and MONGO_URI are required")
    client = MongoClient(os.getenv("MONGO_URI"), serverSelectionTimeoutMS=5000)
    client.admin.command("ping")
    db = client[os.getenv("MONGO_DB", "ai_agent_assist")]
    prospects = db.dubai_prospect_review
    outbox = db.email_outbox
    rows = list(prospects.find({"review_status": "needs_verification"}).sort("lead_id", 1).limit(args.limit if args.limit > 0 else 10000))
    counts = {"verified": 0, "no_matching_place": 0, "no_public_official_email": 0, "research_error": 0, "duplicate": 0}
    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 12))) as pool:
        futures = {pool.submit(research, row, key): row for row in rows}
        for number, future in enumerate(as_completed(futures), 1):
            prospect, found, status = future.result()
            if found:
                email, source = found
                lead = prospect["lead_id"]
                if outbox.find_one({"$or": [{"recipient_email": email}, {"company_key": f"dubai:{lead.lower()}"}]}):
                    counts["duplicate"] += 1
                    continue
                subject, body = copy_for(prospect)
                now = datetime.utcnow()
                draft = {"company_name": prospect["company_name"], "company_key": f"dubai:{lead.lower()}",
                         "lead_id": lead, "recipient_email": email, "website": source, "verification_url": source,
                         "company_context": "", "campaign_name": CAMPAIGN, "template_id": "",
                         "template_name": "Dubai owner-reviewed outreach", "subject": subject, "body": body,
                         "status": "awaiting_owner_review", "requires_explicit_approval": True,
                         "source": "official_site_places_verified", "created_at": now, "updated_at": now}
                try:
                    result = outbox.insert_one(draft)
                except DuplicateKeyError:
                    counts["duplicate"] += 1
                    continue
                prospects.update_one({"_id": prospect["_id"], "review_status": "needs_verification"},
                                     {"$set": {"verified_email": email, "source_url": source,
                                               "review_status": "awaiting_owner_review", "updated_at": now}})
                counts["verified"] += 1
                print(f"staged {lead} {email} {result.inserted_id}", flush=True)
            else:
                counts[status] += 1
            if number % 25 == 0 or number == len(rows):
                print(f"progress {number}/{len(rows)} {counts}", flush=True)


if __name__ == "__main__":
    main()
