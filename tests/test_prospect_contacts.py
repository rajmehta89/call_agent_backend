from unittest.mock import Mock, PropertyMock, patch

from email_campaign import _discovery_locations
from prospect_discovery import _website_contact, discover_prospects
import email_campaign
from pymongo.errors import DuplicateKeyError
from datetime import datetime, timedelta
import smtplib


def page(html, url="https://acme.co.uk/contact"):
    return Mock(status_code=200, text=html, url=url)


def test_locations_keep_city_and_country_together():
    assert _discovery_locations("Dubai, UAE; Canada; canada; United Kingdom") == ["Dubai, UAE", "Canada", "United Kingdom"]


def test_contact_ignores_script_addresses_and_unrelated_domains():
    html = '<script>"admin@acme.co.uk"</script><p>privacy@acme.co.uk; info@yourcompany.com; vendor@analytics.com</p><a href="mailto:hello%40acme.co.uk?subject=Hi">Contact</a>'
    with patch("prospect_discovery.requests.get", return_value=page(html)):
        assert _website_contact("https://acme.co.uk") == ("hello@acme.co.uk", "https://acme.co.uk/contact")


def test_contact_does_not_use_redirected_unrelated_site():
    with patch("prospect_discovery.requests.get", return_value=page("info@acme.co.uk", "https://parked.com")):
        assert _website_contact("https://acme.co.uk") == ("", "")


def test_contact_prioritises_business_role_over_personal_address():
    with patch("prospect_discovery.requests.get", return_value=page("alice@acme.co.uk info@acme.co.uk")):
        assert _website_contact("https://acme.co.uk")[0] == "info@acme.co.uk"


def test_international_outreach_uses_business_mailbox():
    mongo = Mock()
    mongo.db.email_suppressions.find_one.return_value = None
    mongo.email_outbox.find_one_and_update.return_value = {"status": "approved"}
    draft = {"_id": "draft", "recipient_email": "info@acme.co.uk", "status": "approved", "sender_provider": "namecheap_private_email", "subject": "An enquiry idea", "body": "Hi team", "company_context": ""}
    with patch.object(email_campaign, "mongo_client", mongo), \
         patch("private_email_service.private_email_service.send", return_value={"status": "sent"}) as private_send, \
         patch("private_email_service.PrivateEmailService.configured", new_callable=PropertyMock, return_value=True), \
         patch("gmail_service.gmail_service.send") as gmail_send:
        assert email_campaign._send_draft(draft)["status"] == "sent"
    private_send.assert_called_once_with(["info@acme.co.uk"], "An enquiry idea", "Hi team")
    gmail_send.assert_not_called()


def test_declined_business_is_excluded_before_smtp():
    mongo = Mock()
    mongo.db.email_suppressions.find_one.return_value = {"reason": "Recipient declined"}
    with patch.object(email_campaign, "mongo_client", mongo), \
         patch("private_email_service.private_email_service.send") as send:
        assert email_campaign._send_draft({"_id": "draft", "recipient_email": "hello@declined.com"})["status"] == "excluded"
    send.assert_not_called()


def test_research_without_drafts_saves_every_business_on_page():
    mongo = Mock()
    mongo.is_connected.return_value = True
    response = Mock(status_code=200)
    response.json.return_value = {"places": [{"id": str(i), "displayName": {"text": f"Business {i}"}} for i in range(3)]}
    with patch("prospect_discovery.mongo_client", mongo), \
         patch("prospect_discovery._google_places_key", return_value="test"), \
         patch("prospect_discovery._website_contact", return_value=("", "")), \
         patch("prospect_discovery.requests.post", return_value=response):
        result = discover_prospects("cleaning", "Canada", create_drafts=False, context_mode="campaign", shared_context="BuildWithRaj")
    assert result["found"] == 3
    assert mongo.campaign_prospects.update_one.call_count == 3


def test_second_delivery_worker_cannot_take_an_active_lease():
    mongo = Mock()
    mongo.is_connected.return_value = True
    mongo.db.campaign_delivery_locks.find_one_and_update.side_effect = DuplicateKeyError("lease held")
    with patch.object(email_campaign, "mongo_client", mongo), patch.object(email_campaign, "_process_email_outbox_locked") as process:
        assert email_campaign.process_email_outbox()["status"] == "delivery_in_progress"
    process.assert_not_called()


def test_delivery_lease_is_released_when_delivery_fails():
    mongo = Mock()
    mongo.is_connected.return_value = True
    with patch.object(email_campaign, "mongo_client", mongo), patch.object(email_campaign, "_process_email_outbox_locked", side_effect=RuntimeError("test")):
        try:
            email_campaign.process_email_outbox()
        except RuntimeError:
            pass
        else:
            raise AssertionError("Expected delivery failure")
    mongo.db.campaign_delivery_locks.delete_one.assert_called_once()


def test_provider_hourly_rejection_preserves_approved_draft_for_retry():
    mongo = Mock()
    mongo.db.email_suppressions.find_one.return_value = None
    mongo.email_outbox.find_one_and_update.return_value = {"status": "approved"}
    draft = {"_id": "draft", "recipient_email": "info@acme.co.uk", "status": "approved", "sender_provider": "namecheap_private_email", "subject": "An enquiry idea", "body": "Hi team", "company_context": ""}
    rejection = smtplib.SMTPDataError(554, b"Reject: too many messages from sender in last 60 minutes")
    with patch.object(email_campaign, "mongo_client", mongo), \
         patch("private_email_service.private_email_service.send", side_effect=rejection), \
         patch("private_email_service.PrivateEmailService.configured", new_callable=PropertyMock, return_value=True):
        assert email_campaign._send_draft(draft)["status"] == "provider_rate_limited"
    saved = mongo.email_outbox.update_one.call_args.args[1]["$set"]
    assert saved["status"] == "approved"
    assert saved["delivery_retry_after"] > datetime.utcnow()
    mongo.db.email_provider_backoff.update_one.assert_called_once()


def test_provider_backoff_prevents_further_delivery_attempts():
    mongo = Mock()
    mongo.db.email_provider_backoff.find_one.return_value = {"retry_after": datetime.utcnow() + timedelta(hours=1)}
    with patch.object(email_campaign, "mongo_client", mongo), \
         patch.object(email_campaign, "get_email_campaign", return_value={"status": "running", "sender_provider": "namecheap_private_email"}), \
         patch.object(email_campaign, "get_email_policy", return_value={}), \
         patch.object(email_campaign, "send_window_open", return_value=True), \
         patch.object(email_campaign, "_send_draft") as send:
        assert email_campaign._process_email_outbox_locked(None, 100, "owner")["status"] == "provider_rate_limited"
    send.assert_not_called()
