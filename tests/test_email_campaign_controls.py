import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import email_campaign


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = docs or []

    def count_documents(self, query):
        total = 0
        for doc in self.docs:
            if query.get("status") and isinstance(query["status"], dict):
                if doc.get("status") not in query["status"].get("$in", []):
                    continue
            elif query.get("status") and doc.get("status") != query["status"]:
                continue
            sent_at = query.get("sent_at", {}).get("$gte")
            if sent_at and (not doc.get("sent_at") or doc["sent_at"] < sent_at):
                continue
            total += 1
        return total

    def find(self, query):
        rows = [doc for doc in self.docs if doc.get("status") in query.get("status", {}).get("$in", [])]
        return FakeCursor(rows)


class FakeCursor(list):
    def sort(self, *args, **kwargs):
        return self

    def limit(self, count):
        return self[:count]


class FakeMongo:
    def __init__(self, docs=None):
        self.email_outbox = FakeCollection(docs)

    def is_connected(self):
        return True


class CampaignControlsIntegrationTests(unittest.TestCase):
    def test_campaign_values_are_clamped_before_save(self):
        with patch.object(email_campaign.mongo_client, "is_connected", return_value=False):
            saved = email_campaign.save_email_campaign({"max_emails": 99999, "interval_minutes": 0, "daily_limit": 99999})
        self.assertEqual(saved["max_emails"], 10000)
        self.assertEqual(saved["interval_minutes"], 1)
        self.assertEqual(saved["daily_limit"], 500)

    def test_campaign_lifecycle_actions_transition_safely(self):
        with patch.object(email_campaign.mongo_client, "is_connected", return_value=False):
            self.assertEqual(email_campaign.campaign_action("start")["status"], "running")
            self.assertEqual(email_campaign.campaign_action("pause")["status"], "paused")
            self.assertEqual(email_campaign.campaign_action("resume")["status"], "running")
            self.assertEqual(email_campaign.campaign_action("stop")["status"], "stopped")
            with self.assertRaises(ValueError):
                email_campaign.campaign_action("delete")

    def test_worker_waits_when_campaign_is_not_running_or_window_is_closed(self):
        fake = FakeMongo()
        with patch.object(email_campaign, "mongo_client", fake), \
             patch.object(email_campaign, "get_email_campaign", return_value={"status": "paused", "max_emails": 20, "interval_minutes": 60, "daily_limit": 450}), \
             patch.object(email_campaign, "get_email_policy", return_value={"approval_required": True}), \
             patch.object(email_campaign, "send_window_open", return_value=False):
            result = email_campaign.process_email_outbox()
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["sent"], 0)

    def test_daily_limit_blocks_processing(self):
        sent = [{"status": "sent", "sent_at": datetime.utcnow()} for _ in range(3)]
        fake = FakeMongo(sent)
        campaign = {"status": "running", "max_emails": 20, "interval_minutes": 60, "daily_limit": 3}
        with patch.object(email_campaign, "mongo_client", fake), \
             patch.object(email_campaign, "get_email_campaign", return_value=campaign), \
             patch.object(email_campaign, "get_email_policy", return_value={"approval_required": True}), \
             patch.object(email_campaign, "send_window_open", return_value=True), \
             patch.object(email_campaign, "_daily_window_start", return_value=datetime.utcnow() - timedelta(days=1)):
            result = email_campaign.process_email_outbox()
        self.assertEqual(result["status"], "daily_limit_reached")
        self.assertEqual(result["sent"], 0)

    def test_pending_approval_is_not_sent_inside_approval_window(self):
        draft = {"_id": "draft-1", "status": "pending_approval", "created_at": datetime.utcnow()}
        fake = FakeMongo([draft])
        campaign = {"status": "running", "max_emails": 20, "interval_minutes": 60, "daily_limit": 450}
        with patch.object(email_campaign, "mongo_client", fake), \
             patch.object(email_campaign, "get_email_campaign", return_value=campaign), \
             patch.object(email_campaign, "get_email_policy", return_value={"approval_required": True}), \
             patch.object(email_campaign, "send_window_open", return_value=True), \
             patch.object(email_campaign, "approval_window_open", return_value=True), \
             patch.object(email_campaign, "_daily_window_start", return_value=datetime.utcnow() - timedelta(days=1)), \
             patch.object(email_campaign, "_send_draft") as send:
            result = email_campaign.process_email_outbox()
        send.assert_not_called()
        self.assertEqual(result["skipped_for_approval"], 1)
        self.assertEqual(result["sent"], 0)

    def test_timezone_reset_is_utc_naive_and_uses_policy_timezone(self):
        policy = {"timezone": "America/New_York"}
        reset = email_campaign._next_daily_reset(policy)
        self.assertIsNone(reset.tzinfo)
        self.assertGreater(reset, datetime.utcnow())


if __name__ == "__main__":
    unittest.main()
