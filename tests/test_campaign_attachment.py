"""Outreach emails carry the campaign brochure, say so in a P.S., and the outbox records what was sent."""

from email import message_from_bytes
from unittest.mock import Mock, PropertyMock, patch

import email_campaign
from private_email_service import private_email_service


def test_brochure_is_attached_and_noted(tmp_path):
    brochure = tmp_path / "Overview.pdf"
    brochure.write_bytes(b"%PDF-1.4 test")
    mongo = Mock()
    mongo.db.email_suppressions.find_one.return_value = None
    mongo.email_outbox.find_one_and_update.return_value = {"status": "approved"}
    draft = {"_id": "draft", "recipient_email": "info@acme.co.uk", "status": "approved", "sender_provider": "namecheap_private_email",
             "subject": "Missed calls?", "body": "Hi team\n\nCheers,\nRaj", "company_context": ""}
    with patch.object(email_campaign, "mongo_client", mongo), \
         patch.object(email_campaign, "get_email_campaign", return_value={"attachment_paths": [str(brochure)]}), \
         patch("private_email_service.private_email_service.send", return_value={"status": "sent"}) as send, \
         patch("private_email_service.PrivateEmailService.configured", new_callable=PropertyMock, return_value=True):
        assert email_campaign._send_draft(draft)["status"] == "sent"
    body = send.call_args[0][2]
    assert body.endswith(email_campaign.ATTACHMENT_NOTE) and send.call_args[1]["attachments"] == [str(brochure)]
    saved = mongo.email_outbox.update_one.call_args[0][1]["$set"]
    assert saved["body"] == body and saved["attachments"] == ["Overview.pdf"]


def test_a_missing_brochure_never_blocks_the_email(tmp_path):
    with patch.object(email_campaign, "get_email_campaign", return_value={"attachment_paths": [str(tmp_path / "gone.pdf")]}):
        assert email_campaign.campaign_attachments() == []


def test_message_carries_the_pdf(tmp_path, monkeypatch):
    brochure = tmp_path / "Overview.pdf"
    brochure.write_bytes(b"%PDF-1.4 test")
    monkeypatch.setenv("PRIVATE_EMAIL_PASSWORD", "x")
    with patch("private_email_service.smtplib.SMTP_SSL") as smtp, patch("private_email_service.imaplib.IMAP4_SSL"):
        private_email_service.send(["owner@example.com"], "Test", "Hello https://buildwithraj.com", attachments=[str(brochure)])
    message = message_from_bytes(smtp.return_value.__enter__.return_value.send_message.call_args[0][0].as_bytes())
    parts = [(part.get_content_type(), part.get_filename()) for part in message.walk()]
    assert ("application/pdf", "Overview.pdf") in parts and ("text/html", None) in parts and ("text/plain", None) in parts
