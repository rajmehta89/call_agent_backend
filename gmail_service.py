"""Gmail delivery for opt-in lead and customer automations.

This uses Gmail SMTP with an app password. It never sends anything merely by
connecting the integration; an enabled automation must explicitly invoke it.
"""

import os
import smtplib
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Iterable


class GmailService:
    @property
    def address(self) -> str:
        return (os.getenv("GMAIL_ADDRESS") or os.getenv("SMTP_USERNAME") or os.getenv("SMTP_FROM") or "").strip()

    @property
    def configured(self) -> bool:
        host = (os.getenv("GMAIL_SMTP_HOST") or os.getenv("SMTP_HOST") or "").strip().lower()
        password = (os.getenv("GMAIL_APP_PASSWORD") or os.getenv("SMTP_PASSWORD") or "").replace(" ", "").replace("\t", "").replace("\r", "").replace("\n", "")
        return bool(self.address and password and host in {"smtp.gmail.com", "smtp.googlemail.com"})

    def status(self) -> dict:
        return {
            "connected": self.configured,
            "provider": "gmail",
            "address": self.address,
            "smtp_host": (os.getenv("GMAIL_SMTP_HOST") or os.getenv("SMTP_HOST") or "smtp.gmail.com"),
            "auth_method": "app_password",
            "setup_required": not self.configured,
        }

    @staticmethod
    def _clean_recipients(recipients: Iterable[str]) -> list[str]:
        values = []
        for value in recipients:
            address = parseaddr(str(value or ""))[1].strip()
            if address and "@" in address and address not in values:
                values.append(address)
        return values

    def send(self, recipients: Iterable[str], subject: str, body: str) -> dict:
        to = self._clean_recipients(recipients)
        if not to:
            return {"status": "skipped", "reason": "No valid recipient email"}
        if not self.configured:
            return {"status": "skipped", "reason": "Gmail is not configured"}

        message = EmailMessage()
        message["From"] = self.address
        message["To"] = ", ".join(to)
        message["Subject"] = (str(subject or "").strip() or "Follow-up from Raj's team")[:180]
        message.set_content(str(body or "").strip())

        host = (os.getenv("GMAIL_SMTP_HOST") or os.getenv("SMTP_HOST") or "smtp.gmail.com").strip()
        port = int(os.getenv("GMAIL_SMTP_PORT") or os.getenv("SMTP_PORT", "587"))
        password = (os.getenv("GMAIL_APP_PASSWORD") or os.getenv("SMTP_PASSWORD") or "").replace(" ", "").replace("\t", "").replace("\r", "").replace("\n", "")
        use_ssl = (os.getenv("GMAIL_SMTP_SSL") or "false").lower() in {"1", "true", "yes", "on"}
        if use_ssl or port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=15) as server:
                server.login(self.address, password)
                server.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=15) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(self.address, password)
                server.send_message(message)
        return {"status": "sent", "recipients": to, "subject": message["Subject"]}


gmail_service = GmailService()
