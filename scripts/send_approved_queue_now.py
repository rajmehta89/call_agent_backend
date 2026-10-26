"""One-time delivery of the approved queue explicitly requested by the owner.

Temporarily expands campaign limits for this bounded snapshot, then restores
them. Suppressions, delivery leases and duplicate protection remain in force.
"""

from datetime import datetime
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from email_campaign import campaign_status, get_email_campaign, process_email_outbox, save_email_campaign
from mongo_client import mongo_client as mongo
from private_email_service import private_email_service


def main():
    if not mongo.is_connected():
        raise RuntimeError("Database unavailable")
    if not private_email_service.verify_connection():
        raise RuntimeError("Business mailbox verification failed")
    now = datetime.utcnow()
    run_id = "send-all-" + now.strftime("%Y%m%dT%H%M%SZ")
    rows = list(mongo.email_outbox.find({"status": "approved"}).sort("created_at", 1))
    if any(row.get("sender_provider") != "namecheap_private_email" for row in rows):
        raise RuntimeError("Approved snapshot contains a different sender")
    ids = [row["_id"] for row in rows]
    if not ids:
        print(json.dumps({"status": "queue_empty"}), flush=True)
        return
    original = get_email_campaign()
    current = campaign_status()
    required_daily_limit = current["daily_used"] + len(ids)
    if required_daily_limit > 500:
        raise RuntimeError("Snapshot would exceed the campaign's supported daily ceiling")
    path = Path(__file__).resolve().parents[1] / "logs" / (run_id + ".json")
    report = {"run_id": run_id, "started_at": now, "sender": private_email_service.address,
              "selected": len(ids), "draft_ids": [str(item) for item in ids],
              "original_campaign": original, "before": current,
              "authorization": "User explicitly requested send for all now",
              "status": "preparing"}

    def save():
        path.write_text(json.dumps(report, default=str, indent=2), encoding="utf-8")

    def snapshot():
        return list(mongo.email_outbox.find({"_id": {"$in": ids}}, {
            "company_name": 1, "recipient_email": 1, "country": 1, "status": 1,
            "subject": 1, "sent_at": 1, "delivery": 1, "delivery_outcome": 1,
            "error": 1, "email_source_url": 1, "exclusion_reason": 1,
        }))

    def counts(items):
        return {state: sum(item["status"] == state for item in items)
                for state in sorted({item["status"] for item in items})}

    save()
    changed = False
    try:
        save_email_campaign({"status": "running", "discovery_enabled": False,
                             "max_emails": max(original["max_emails"], current["rate_used"] + len(ids)),
                             "daily_limit": max(original["daily_limit"], required_daily_limit)})
        changed = True
        mongo.audit_logs.insert_one({"action": "send_all_approved", "resource": "email_outbox",
                                     "created_at": now, "source": "explicit_user_request",
                                     "after": {"run_id": run_id, "count": len(ids), "sender": private_email_service.address,
                                               "draft_ids": [str(item) for item in ids]}})
        report["status"] = "sending"
        save()
        print(json.dumps({"run_id": run_id, "selected": len(ids), "sender": private_email_service.address}), flush=True)
        while True:
            items = snapshot()
            state = counts(items)
            if not any(item["status"] in {"approved", "scheduled", "sending", "pending_approval"} for item in items):
                break
            result = process_email_outbox([str(item) for item in ids], limit=100)
            report["progress"] = counts(snapshot())
            report["last_processing_result"] = result
            save()
            print(json.dumps({"progress": report["progress"], "processing": result}), flush=True)
            if result["status"] in {"waiting", "daily_limit_reached", "database_unavailable", "provider_rate_limited"}:
                report["status"] = "incomplete"
                break
            if not result.get("sent"):
                # Another sender may hold the shared lease. Let it finish;
                # each recipient is still claimed atomically by the processor.
                time.sleep(5)
        report["delivery"] = snapshot()
        report["counts"] = counts(report["delivery"])
        if report["status"] != "incomplete":
            report["status"] = "completed"
    except BaseException as exc:
        report["status"] = "interrupted"
        report["exception_type"] = type(exc).__name__
        raise
    finally:
        if changed:
            save_email_campaign({key: original[key] for key in
                                 ("status", "discovery_enabled", "max_emails", "daily_limit")})
            report["limits_restored"] = True
        report["finished_at"] = datetime.utcnow()
        save()
    print(json.dumps({"run_id": run_id, "selected": len(ids), "counts": report.get("counts"),
                      "limits_restored": report.get("limits_restored"), "report": str(path)}), flush=True)


if __name__ == "__main__":
    main()
