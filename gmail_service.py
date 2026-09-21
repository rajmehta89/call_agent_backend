"""Gmail delivery for opt-in lead and customer automations.

This uses Gmail SMTP with an app password. It never sends anything merely by
connecting the integration; an enabled automation must explicitly invoke it.
"""

import os
import smtplib
import base64
import socket
import requests
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Iterable


class GmailService:
    ASSET_EXTENSIONS = {"webp", "png", "jpg", "jpeg", "gif", "svg", "ico", "css", "js", "woff", "woff2"}

    @property
    def address(self) -> str:
        return (os.getenv("GMAIL_ADDRESS") or os.getenv("SMTP_USERNAME") or os.getenv("SMTP_FROM") or "").strip()

    @property
    def api_configured(self) -> bool:
        return all((os.getenv(name) or "").strip() for name in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN"))

    @property
    def smtp_configured(self) -> bool:
        host = (os.getenv("GMAIL_SMTP_HOST") or os.getenv("SMTP_HOST") or "").strip().lower()
        password = (os.getenv("GMAIL_APP_PASSWORD") or os.getenv("SMTP_PASSWORD") or "").replace(" ", "").replace("\t", "").replace("\r", "").replace("\n", "")
        return bool(self.address and password and host in {"smtp.gmail.com", "smtp.googlemail.com"})

    @property
    def configured(self) -> bool:
        return self.api_configured or self.smtp_configured

    def status(self) -> dict:
        return {
            "connected": self.configured,
            "provider": "gmail",
            "address": self.address,
            "smtp_host": (os.getenv("GMAIL_SMTP_HOST") or os.getenv("SMTP_HOST") or "smtp.gmail.com"),
            "auth_method": "oauth_refresh_token" if self.api_configured else "app_password",
            "transport": "gmail_api" if self.api_configured else "smtp",
            "setup_required": not self.configured,
        }

    @staticmethod
    def _clean_recipients(recipients: Iterable[str]) -> list[str]:
        values = []
        for value in recipients:
            address = parseaddr(str(value or ""))[1].strip()
            if not address or "@" not in address:
                continue
            local, domain = address.rsplit("@", 1)
            labels = domain.lower().split(".")
            tld = labels[-1] if labels else ""
            if not local or len(labels) < 2 or not all(labels) or len(tld) < 2 or tld in GmailService.ASSET_EXTENSIONS:
                continue
            if address not in values:
                values.append(address)
        return values

    @staticmethod
    def _smtp_ipv4(host: str, port: int, timeout: int = 15) -> smtplib.SMTP:
        """Connect over IPv4 and retain the hostname for STARTTLS SNI/cert checks."""
        addresses = []
        for item in socket.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_STREAM):
            address = item[4][0]
            if address not in addresses:
                addresses.append(address)
        last_error = None
        for address in addresses:
            server = smtplib.SMTP(timeout=timeout)
            try:
                server.connect(address, port)
                server._host = host
                return server
            except OSError as exc:
                last_error = exc
                try:
                    server.close()
                except Exception:
                    pass
        if last_error:
            raise last_error
        raise OSError(f"Unable to resolve an IPv4 address for {host}")

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

        if self.api_configured:
            return self._send_api(message)

        host = (os.getenv("GMAIL_SMTP_HOST") or os.getenv("SMTP_HOST") or "smtp.gmail.com").strip()
        port = int(os.getenv("GMAIL_SMTP_PORT") or os.getenv("SMTP_PORT", "587"))
        password = (os.getenv("GMAIL_APP_PASSWORD") or os.getenv("SMTP_PASSWORD") or "").replace(" ", "").replace("\t", "").replace("\r", "").replace("\n", "")
        use_ssl = (os.getenv("GMAIL_SMTP_SSL") or "false").lower() in {"1", "true", "yes", "on"}
        if use_ssl or port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=15) as server:
                server.login(self.address, password)
                server.send_message(message)
        else:
            with self._smtp_ipv4(host, port) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(self.address, password)
                server.send_message(message)
        return {"status": "sent", "recipients": to, "subject": message["Subject"]}

    def _send_api(self, message: EmailMessage) -> dict:
        token_response = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": os.getenv("GMAIL_CLIENT_ID"),
                "client_secret": os.getenv("GMAIL_CLIENT_SECRET"),
                "refresh_token": os.getenv("GMAIL_REFRESH_TOKEN"),
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
        if token_response.status_code >= 400:
            return {"status": "error", "reason": "Gmail OAuth token refresh failed"}
        access_token = token_response.json().get("access_token")
        if not access_token:
            return {"status": "error", "reason": "Gmail OAuth token was not returned"}
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")
        response = requests.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"raw": raw},
            timeout=15,
        )
        if response.status_code >= 400:
            return {"status": "error", "reason": "Gmail API rejected the message"}
        return {"status": "sent", "recipients": [message["To"]], "subject": message["Subject"]}


gmail_service = GmailService()
