"""Namecheap Private Email SMTP sender for individually approved messages."""

import html
import mimetypes
import os
import re
import smtplib
import imaplib
import logging
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Iterable

from gmail_service import GmailService

logger = logging.getLogger(__name__)


class PrivateEmailService:
    @staticmethod
    def _header(value: str) -> str:
        return str(make_header(decode_header(value or "")))

    @staticmethod
    def _html_body(text: str) -> str:
        escaped = html.escape(text, quote=True)
        linked = re.sub(r"https?://[^\s<]+[^\s<.,;:!?)]", lambda match: f'<a href="{match.group(0)}">{match.group(0)}</a>', escaped)
        return f'<div style="font-family:Arial,sans-serif;font-size:14px;line-height:1.5">{linked.replace(chr(10), "<br>")}</div>'

    @property
    def address(self) -> str:
        return (os.getenv("PRIVATE_EMAIL_ADDRESS") or "raj@buildwithraj.com").strip()

    @property
    def configured(self) -> bool:
        return bool(self.address and (os.getenv("PRIVATE_EMAIL_PASSWORD") or ""))

    def status(self) -> dict:
        return {
            "connected": self.configured,
            "provider": "namecheap_private_email",
            "address": self.address,
            "smtp_host": "mail.privateemail.com",
            "setup_required": not self.configured,
        }

    def verify_connection(self) -> bool:
        if not self.configured:
            return False
        with smtplib.SMTP_SSL("mail.privateemail.com", 465, timeout=15) as server:
            server.login(self.address, os.environ["PRIVATE_EMAIL_PASSWORD"])
        return True

    def inbox(self, limit: int = 20) -> list[dict]:
        if not self.configured:
            raise RuntimeError("Private Email is not configured")
        with imaplib.IMAP4_SSL("mail.privateemail.com", 993, timeout=15) as mailbox:
            mailbox.login(self.address, os.environ["PRIVATE_EMAIL_PASSWORD"])
            status, _ = mailbox.select("INBOX", readonly=True)
            if status != "OK":
                raise RuntimeError("Could not open Private Email inbox")
            status, data = mailbox.search(None, "ALL")
            if status != "OK":
                raise RuntimeError("Could not list Private Email messages")
            result = []
            for number in reversed(data[0].split()[-max(1, min(limit, 50)):]):
                status, parts = mailbox.fetch(number, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)] FLAGS)")
                if status != "OK":
                    continue
                raw = next((part[1] for part in parts if isinstance(part, tuple) and isinstance(part[1], bytes)), None)
                if not raw:
                    continue
                message = message_from_bytes(raw)
                flags = b" ".join(part[0] for part in parts if isinstance(part, tuple) and isinstance(part[0], bytes))
                result.append({
                    "from": self._header(message.get("From", "")),
                    "subject": self._header(message.get("Subject", "")),
                    "date": message.get("Date", ""),
                    "unread": b"\\Seen" not in flags,
                })
            return result

    def send(self, recipients: Iterable[str], subject: str, body: str, attachments: Iterable[str] = ()) -> dict:
        to = GmailService._clean_recipients(recipients)
        if not to:
            return {"status": "skipped", "reason": "No valid recipient email"}
        if not self.configured:
            return {"status": "error", "reason": "Private Email password is not configured on the server"}

        message = EmailMessage()
        message["From"] = self.address
        message["Message-ID"] = make_msgid()
        message["To"] = ", ".join(to)
        message["Subject"] = (str(subject or "").strip() or "Follow-up from Raj's team")[:180]
        message.set_content(str(body or "").strip())
        # Some mail apps leave a web address in plain text unclickable; the HTML alternative links it.
        message.add_alternative(self._html_body(str(body or "").strip()), subtype="html")
        for path in attachments:
            maintype, _, subtype = (mimetypes.guess_type(path)[0] or "application/octet-stream").partition("/")
            with open(path, "rb") as handle:
                message.add_attachment(handle.read(), maintype=maintype, subtype=subtype, filename=os.path.basename(path))

        with smtplib.SMTP_SSL("mail.privateemail.com", 465, timeout=15) as server:
            server.login(self.address, os.environ["PRIVATE_EMAIL_PASSWORD"])
            server.send_message(message)
        # SMTP submission does not add a copy to the mailbox's Sent folder.
        # This happens only after submission succeeds, and an IMAP failure must
        # never cause a retry that would send the same message twice.
        saved_to_sent = self._save_sent_copy(message)
        return {"status": "sent", "recipients": to, "subject": message["Subject"], "from": self.address, "provider": "namecheap_private_email", "saved_to_sent": saved_to_sent}

    def _save_sent_copy(self, message: EmailMessage) -> bool:
        """Retry only archiving, checking an uncertain APPEND before creating another copy."""
        for attempt in range(2):
            try:
                with imaplib.IMAP4_SSL("mail.privateemail.com", 993, timeout=15) as mailbox:
                    mailbox.login(self.address, os.environ["PRIVATE_EMAIL_PASSWORD"])
                    if attempt:
                        status, _ = mailbox.select("Sent", readonly=True)
                        if status != "OK":
                            break
                        status, found = mailbox.search(None, "HEADER", "Message-ID", message["Message-ID"])
                        if status != "OK":
                            break
                        if any(part.strip() for part in found if isinstance(part, bytes)):
                            return True
                    status, _ = mailbox.append("Sent", "\\Seen", None, message.as_bytes())
                    if status == "OK":
                        return True
                    break
            except (imaplib.IMAP4.abort, OSError) as exc:
                logger.warning("Private Email Sent archive attempt %d failed: %s", attempt + 1, type(exc).__name__)
            except Exception as exc:
                logger.warning("Private Email Sent archive failed: %s", type(exc).__name__)
                break
        logger.warning("Private Email message submitted but Sent copy could not be saved")
        return False


private_email_service = PrivateEmailService()
