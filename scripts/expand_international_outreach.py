"""Research each configured market, stage verified contacts, and send on request.

Uses the existing outbox deduplication, sender selection, and sending limits.
Run without --send to research and stage only. Reports are stored under logs/.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mongo_client import mongo_client as mongo
from campaign_registry import get_campaigns, update_campaign
from email_campaign import campaign_status, process_email_outbox
from prospect_discovery import discover_prospects
from private_email_service import private_email_service
from pymongo.errors import DuplicateKeyError
from scripts.prepare_dubai_review import copy_for


MARKETS = [
    ("Dubai, United Arab Emirates", "United Arab Emirates", "property management companies", "dental clinics"),
    ("United States", "United States", "home cleaning services in Austin Texas", "real estate agencies in Raleigh North Carolina"),
    ("Canada", "Canada", "property management companies in Calgary", "dental clinics in Ottawa"),
    ("United Kingdom", "United Kingdom", "real estate agencies in Manchester", "home cleaning services in Bristol"),
    ("Australia", "Australia", "property management companies in Brisbane", "dental clinics in Perth"),
    ("New Zealand", "New Zealand", "real estate agencies in Wellington", "home cleaning services in Christchurch"),
    ("Ireland", "Ireland", "property management companies in Cork", "dental clinics in Galway"),
    ("Singapore", "Singapore", "home cleaning services", "real estate agencies"),
    ("India", "India", "real estate agencies in Pune", "dental clinics in Bengaluru"),
    ("South Africa", "South Africa", "property management companies in Cape Town", "home cleaning services in Durban"),
]
PROFILE = (
    "Raj Mehta is an AI Developer and AI Automation Engineer at BuildWithRaj. "
    "Website: https://buildwithraj.com/. Builds AI agents and LLM applications, "
    "Voice AI receptionists, WhatsApp workflows, lead qualification and follow-up, "
    "CRM integrations, and full-stack SaaS applications. Technologies include "
    "Python, Node.js, Next.js, n8n, Make, GoHighLevel, APIs and webhooks. "
    "Offer a short relevant example; do not invent results, pricing or customer facts."
)
GLOBAL_CAMPAIGN = "BuildWithRaj international AI automation outreach"
DUBAI_CAMPAIGN = "Dubai voice WhatsApp CRM outreach"


def deliver_existing(run_id):
    """Approve this researched batch only, then use normal delivery controls."""
    private_email_service.verify_connection()
    rows = list(mongo.email_outbox.find({"research_run_id": run_id, "status": "awaiting_owner_review"}))
    if not rows:
        print(json.dumps({"run_id": run_id, "status": "no_pending_drafts"}), flush=True)
        return
    countries = list(dict.fromkeys(row["country"] for row in rows))
    groups = [[row for row in rows if row["country"] == country] for country in countries]
    ordered = [items[i] for i in range(max(map(len, groups))) for items in groups if i < len(items)]
    now = datetime.utcnow()
    ids = []
    for position, row in enumerate(ordered):
        assert row.get("sender_provider") == "namecheap_private_email" and row.get("email_source_url")
        prospect = mongo.campaign_prospects.find_one({"email": row["recipient_email"]})
        industry = " ".join(str((prospect or {}).get(key) or "") for key in ("primary_type", "discovery_query"))
        subject, body = copy_for({"company_name": row["company_name"], "industry": industry})
        mongo.email_outbox.update_one({"_id": row["_id"], "status": "awaiting_owner_review"}, {"$set": {"subject": subject, "body": body, "status": "approved", "approved_at": now, "approval_source": "user_requested_buildwithraj_mailbox_outreach", "created_at": now + timedelta(milliseconds=position), "updated_at": now}})
        ids.append(str(row["_id"]))
    mongo.audit_logs.insert_one({"action": "approve", "resource": "email_drafts", "created_at": now, "source": "user_requested_buildwithraj_mailbox_outreach", "after": {"run_id": run_id, "draft_ids": ids, "count": len(ids), "sender": private_email_service.address}})
    print(json.dumps({"approved": len(ids), "sender": private_email_service.address}), flush=True)
    result = process_email_outbox(ids, limit=100)
    path = Path(__file__).resolve().parents[1] / "logs" / (run_id + ".json")
    report = json.loads(path.read_text(encoding="utf-8"))
    report["process_result"] = result
    report["delivery"] = list(mongo.email_outbox.find({"research_run_id": run_id}, {"company_name": 1, "recipient_email": 1, "country": 1, "status": 1, "sent_at": 1, "delivery": 1, "email_source_url": 1, "subject": 1}))
    report["delivery_after"] = campaign_status()
    report["sender"] = private_email_service.address
    report["finished_at"] = datetime.utcnow().isoformat() + "Z"
    path.write_text(json.dumps(report, default=str, indent=2), encoding="utf-8")
    print(json.dumps({"run_id": run_id, "processing": result, "statuses": {state: sum(row["status"] == state for row in report["delivery"]) for state in {row["status"] for row in report["delivery"]}}, "report": str(path)}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--deliver-run", help="Deliver an already researched batch without repeating research")
    parser.add_argument("--per-market", type=int, default=8)
    parser.add_argument("--markets-json", type=Path, help="JSON list of location, country, and business search queries")
    args = parser.parse_args()
    if not mongo.is_connected():
        raise RuntimeError("Database unavailable")
    if args.deliver_run:
        deliver_existing(args.deliver_run)
        return
    markets = json.loads(args.markets_json.read_text(encoding="utf-8")) if args.markets_json else MARKETS
    if not isinstance(markets, list) or not markets or len(markets) > 30:
        raise ValueError("Supply between one and thirty markets")
    if not 1 <= args.per_market <= 50:
        raise ValueError("Select between one and fifty drafts per market")
    if any(not isinstance(row, (list, tuple)) or len(row) < 3 or not all(isinstance(value, str) and value.strip() for value in row) for row in markets):
        raise ValueError("Each market requires a location, country, and search query")
    now = datetime.utcnow()
    run_id = "international-" + now.strftime("%Y%m%dT%H%M%SZ")
    log_dir = Path(__file__).resolve().parents[1] / "logs"
    log_dir.mkdir(exist_ok=True)
    report_path = log_dir / (run_id + ".json")
    report = {"run_id": run_id, "started_at": now.isoformat() + "Z", "profile_source": "https://buildwithraj.com/", "markets": [], "new_draft_ids": [], "before": {name: mongo.db[name].count_documents({}) for name in ("campaign_prospects", "email_outbox")}, "status": "researching"}

    def save():
        report_path.write_text(json.dumps(report, default=str, indent=2), encoding="utf-8")

    # Preserve the original campaign definition before correcting its audience
    # and context; store knowledge used by inbound calls is left intact.
    campaigns = get_campaigns()
    report["campaign_before"] = campaigns
    save()
    for campaign in campaigns:
        if campaign["id"] == "usa-ai-automation-outreach":
            update_campaign(campaign["id"], {
                "name": GLOBAL_CAMPAIGN, "status": "active",
                "audience": "Service businesses across UAE, USA, Canada, UK, Australia, New Zealand, Ireland, Singapore, India and South Africa",
                "description": "Introduce Raj Mehta's Voice AI, WhatsApp, AI agent and CRM automation services to relevant businesses across ten markets.",
                "goal": "Start relevant conversations about enquiry handling, lead qualification, appointments and CRM follow-up.",
                "scrape_intent": "Find distinct businesses with public business contact emails on their official websites.",
                "context_mode": "campaign", "campaign_context": PROFILE,
                "scrape": {**campaign["scrape"], "location": "Dubai, UAE; USA; Canada; UK; Australia; New Zealand; Ireland; Singapore; India; South Africa", "query": "property management companies"},
            })
    # Research only: nothing can be delivered until the whole batch is checked.
    jobs = [(location, country, query) for location, country, *queries in markets for query in queries]
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(discover_prospects, query, location, 20, False, context_mode="campaign", shared_context=PROFILE, campaign_name=GLOBAL_CAMPAIGN): (location, country, query) for location, country, query in jobs}
        for future in as_completed(futures):
            location, country, query = futures[future]
            try:
                run = future.result()
                report["markets"].append({"location": location, "country": country, "query": query, "found": run["found"], "ready": run["ready"]})
                print(json.dumps(report["markets"][-1]), flush=True)
            except Exception as exc:
                # Provider errors can contain request details, so retain only
                # the exception class in the public operational report.
                report["markets"].append({"location": location, "country": country, "query": query, "error": type(exc).__name__})
                print(json.dumps(report["markets"][-1]), flush=True)
            save()

    selected_by_country = []
    for location, country, *_ in markets:
        chosen = []
        candidates = mongo.campaign_prospects.find({"discovery_location": location, "email_checked_at": {"$gte": now}, "status": "ready", "email_source_url": {"$nin": ["", None]}, "business_status": {"$ne": "CLOSED_PERMANENTLY"}}).sort("updated_at", -1)
        for prospect in candidates:
            email = prospect.get("email", "").lower()
            if mongo.db.email_suppressions.find_one({"$or": [{"email": email}, {"domain": email.rsplit("@", 1)[-1]}]}):
                continue
            company_key = str(prospect.get("dedupe_key") or prospect.get("website") or prospect["company_name"]).lower()
            if mongo.email_outbox.find_one({"$or": [{"recipient_email": email}, {"company_key": company_key}, {"website": prospect["website"]}]}):
                continue
            subject, body = copy_for({**prospect, "industry": prospect.get("primary_type", "")})
            draft = {
                "company_name": prospect["company_name"], "company_key": company_key,
                "recipient_email": email, "website": prospect["website"],
                "email_source_url": prospect["email_source_url"], "email_checked_at": prospect["email_checked_at"],
                "country": country, "location": location,
                "campaign_name": DUBAI_CAMPAIGN if country == "United Arab Emirates" else GLOBAL_CAMPAIGN,
                "campaign_goal": "Offer a relevant Voice AI, WhatsApp and CRM workflow example.",
                "shared_context": PROFILE, "company_context": "", "template_id": "",
                "template_name": "Verified international business introduction",
                "sender_provider": "namecheap_private_email",
                "subject": subject, "body": body, "status": "awaiting_owner_review",
                "requires_explicit_approval": True, "source": "official_website_verified",
                "research_run_id": run_id, "created_at": now, "updated_at": datetime.utcnow(),
            }
            assert "{{" not in body and "Home DFO" not in body and "https://buildwithraj.com/" in body
            try:
                draft_id = mongo.email_outbox.insert_one(draft).inserted_id
            except DuplicateKeyError:
                continue
            chosen.append(draft_id)
            report["new_draft_ids"].append(str(draft_id))
            mongo.campaign_prospects.update_one({"_id": prospect["_id"]}, {"$set": {"country": country, "outreach_draft_id": str(draft_id)}})
            if len(chosen) >= args.per_market:
                break
        selected_by_country.append(chosen)
        print(json.dumps({"country": country, "new_drafts": len(chosen)}), flush=True)
    # Interleave countries so the first rate-limited batch has broad coverage.
    ids = [items[i] for i in range(args.per_market) for items in selected_by_country if i < len(items)]
    report["delivery_before"] = campaign_status()
    save()
    report["delivery"] = list(mongo.email_outbox.find({"research_run_id": run_id}, {"company_name": 1, "recipient_email": 1, "country": 1, "status": 1, "sent_at": 1, "delivery": 1, "email_source_url": 1, "subject": 1}))
    report["after"] = {name: mongo.db[name].count_documents({}) for name in ("campaign_prospects", "email_outbox")}
    report["delivery_after"] = campaign_status()
    report["status"] = "completed"
    report["finished_at"] = datetime.utcnow().isoformat() + "Z"
    save()
    print(json.dumps({"report": str(report_path), "new_prospects": report["after"]["campaign_prospects"] - report["before"]["campaign_prospects"], "new_drafts": len(ids), "statuses": {state: sum(row["status"] == state for row in report["delivery"]) for state in {row["status"] for row in report["delivery"]}}, "processing": report.get("process_result")}), flush=True)
    if args.send and ids:
        deliver_existing(run_id)


if __name__ == "__main__":
    main()
