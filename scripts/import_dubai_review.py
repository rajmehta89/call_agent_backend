"""Import the Dubai workbook as research candidates and stage verified email copy.

Safe to rerun. Drafts use awaiting_owner_review, which the campaign worker never
selects; only the owner's Approve & send action can release one.
"""

import argparse
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from pathlib import Path

from pymongo.errors import DuplicateKeyError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mongo_client import mongo_client  # noqa: E402


CAMPAIGN = "Dubai voice WhatsApp CRM outreach"
NAMESPACE = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

# Only emails on current official company pages belong here. No address is
# inferred from a company name or domain. Other workbook rows remain research.
VERIFIED = {
    "DX-008": {
        "email": "info@med7clinic.ae",
        "website": "https://med7clinic.ae/clinic/med7_lotus_clinic/",
        "source": "https://med7clinic.ae/clinic/med7_lotus_clinic/",
        "subject": "Routine appointment enquiries at MED7 Lotus",
        "body": (
            "Hi MED7 Lotus team,\n\n"
            "I'm Raj Mehta. I build AI voice and WhatsApp receptionist workflows for clinics. "
            "A workflow can answer approved administrative questions, collect appointment preferences, "
            "pass clinical or sensitive questions to staff, and record requests in an existing CRM or booking system.\n\n"
            "I saw that MED7 Lotus offers several medical services in Al Quoz. "
            "Would a short example of an appointment-enquiry handoff be useful?\n\n"
            "Best,\nRaj Mehta\nAI Automation Developer\nhttps://buildwithraj.com/\n\n"
            'If this is not relevant, reply "no thanks" and I won\'t follow up.'
        ),
    },
}
CONTACTED_LEADS = {"DX-002", "DX-004", "DX-006", "DX-007", "DX-009", "DX-013", "DX-014", "DX-015", "DX-017", "DX-018"}


def workbook_rows(path: Path):
    with zipfile.ZipFile(path) as archive:
        strings = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            strings = ["".join(node.text or "" for node in item.iter(NAMESPACE + "t")) for item in root]
        root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        for row in root.findall(".//" + NAMESPACE + "sheetData/" + NAMESPACE + "row")[1:]:
            fields = {}
            for cell in row.findall(NAMESPACE + "c"):
                col = re.match(r"[A-Z]+", cell.attrib["r"]).group()
                value = cell.find(NAMESPACE + "v")
                inline = cell.find(NAMESPACE + "is")
                raw = value.text if value is not None else "".join(node.text or "" for node in inline.iter(NAMESPACE + "t")) if inline is not None else ""
                fields[col] = strings[int(raw)] if cell.attrib.get("t") == "s" and raw else raw
            if fields.get("A"):
                yield fields


def import_workbook(path: Path):
    if not mongo_client.is_connected():
        raise RuntimeError("CRM database is unavailable")
    prospects = mongo_client.db["dubai_prospect_review"]
    prospects.create_index("lead_id", unique=True)
    inserted = staged = existing = 0
    for row in workbook_rows(path):
        lead_id = row["A"]
        company = (row.get("B") or "").strip()
        verification = VERIFIED.get(lead_id)
        now = datetime.utcnow()
        fields = {
                "company_name": company,
                "industry": (row.get("C") or "").strip(),
                "priority": (row.get("D") or "").strip(),
                "suggested_angle": (row.get("J") or "").strip(),
                "updated_at": now,
        }
        if verification:
            fields.update({"verified_email": verification["email"], "source_url": verification["source"]})
        result = prospects.update_one(
            {"lead_id": lead_id},
            {"$set": fields, "$setOnInsert": {"lead_id": lead_id, "review_status": "needs_verification", "created_at": now}},
            upsert=True,
        )
        inserted += bool(result.upserted_id)
        if lead_id in CONTACTED_LEADS:
            prospects.update_one({"lead_id": lead_id}, {"$set": {"review_status": "already_contacted"}})
            continue
        if not verification:
            continue
        email = verification["email"]
        prior = mongo_client.email_outbox.find_one({"recipient_email": email})
        if prior:
            existing += 1
            prospects.update_one({"lead_id": lead_id}, {"$set": {"review_status": "awaiting_owner_review" if prior.get("status") == "awaiting_owner_review" else "already_contacted"}})
            continue
        draft = {
            "company_name": company,
            "company_key": f"dubai:{lead_id.lower()}",
            "lead_id": lead_id,
            "recipient_email": email,
            "website": verification["website"],
            "verification_url": verification["source"],
            "company_context": "",
            "campaign_name": CAMPAIGN,
            "template_id": "",
            "template_name": "Dubai owner-reviewed outreach",
            "subject": verification["subject"],
            "body": verification["body"],
            "status": "awaiting_owner_review",
            "requires_explicit_approval": True,
            "source": "dubai_workbook_verified",
            "created_at": now,
            "updated_at": now,
        }
        try:
            mongo_client.email_outbox.insert_one(draft)
            staged += 1
        except DuplicateKeyError:
            existing += 1
        prospects.update_one({"lead_id": lead_id}, {"$set": {"review_status": "awaiting_owner_review"}})
    return {"source_rows": prospects.count_documents({}), "new_rows": inserted, "drafts_staged": staged, "existing_contacts": existing}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("workbook", type=Path)
    args = parser.parse_args()
    print(import_workbook(args.workbook))
