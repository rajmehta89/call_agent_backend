import os
import unittest
from unittest.mock import MagicMock, patch

import email_campaign
from private_email_service import private_email_service


class PrivateEmailIntegrationTests(unittest.TestCase):
    def test_status_requires_mailbox_password(self):
        with patch.dict(os.environ, {"PRIVATE_EMAIL_ADDRESS": "raj@buildwithraj.com", "PRIVATE_EMAIL_PASSWORD": ""}):
            self.assertFalse(private_email_service.status()["connected"])

    def test_smtp_uses_namecheap_and_business_sender(self):
        with patch.dict(os.environ, {"PRIVATE_EMAIL_ADDRESS": "raj@buildwithraj.com", "PRIVATE_EMAIL_PASSWORD": "test-secret"}), \
             patch("private_email_service.smtplib.SMTP_SSL") as smtp, \
             patch("private_email_service.imaplib.IMAP4_SSL") as imap:
            imap.return_value.__enter__.return_value.append.return_value = ("OK", [b"1"])
            result = private_email_service.send(["owner@example.com"], "Test", "Hello")
        smtp.assert_called_once_with("mail.privateemail.com", 465, timeout=15)
        connection = smtp.return_value.__enter__.return_value
        connection.login.assert_called_once_with("raj@buildwithraj.com", "test-secret")
        self.assertEqual(connection.send_message.call_args.args[0]["From"], "raj@buildwithraj.com")
        self.assertEqual(result["provider"], "namecheap_private_email")
        self.assertTrue(result["saved_to_sent"])
        imap.return_value.__enter__.return_value.append.assert_called_once()

    def test_sent_copy_failure_does_not_retry_smtp(self):
        with patch.dict(os.environ, {"PRIVATE_EMAIL_ADDRESS": "raj@buildwithraj.com", "PRIVATE_EMAIL_PASSWORD": "test-secret"}), \
             patch("private_email_service.smtplib.SMTP_SSL") as smtp, \
             patch("private_email_service.imaplib.IMAP4_SSL", side_effect=TimeoutError):
            result = private_email_service.send(["owner@example.com"], "Test", "Hello")
        self.assertEqual(result["status"], "sent")
        self.assertFalse(result["saved_to_sent"])
        smtp.return_value.__enter__.return_value.send_message.assert_called_once()

    def test_dubai_draft_uses_private_email_sender(self):
        draft = {
            "_id": "draft-1", "status": "approved", "campaign_name": "Dubai voice WhatsApp CRM outreach",
            "recipient_email": "owner@example.com", "subject": "Hello", "body": "Message", "company_context": "",
        }
        fake_mongo = MagicMock()
        fake_mongo.email_outbox.find_one_and_update.return_value = draft
        with patch.dict(os.environ, {"PRIVATE_EMAIL_ADDRESS": "raj@buildwithraj.com", "PRIVATE_EMAIL_PASSWORD": "test-secret"}), \
             patch.object(email_campaign, "mongo_client", fake_mongo), \
             patch.object(private_email_service, "send", return_value={"status": "sent", "from": "raj@buildwithraj.com"}) as private_send, \
             patch("gmail_service.gmail_service.send") as gmail_send:
            result = email_campaign._send_draft(draft)
        self.assertEqual(result["status"], "sent")
        private_send.assert_called_once()
        gmail_send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
