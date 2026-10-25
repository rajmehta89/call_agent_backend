"""SMTP delivery for workspace invitations and account recovery messages."""

import os
import smtplib
from email.message import EmailMessage


class EmailService:
    def _setting(self, name: str) -> str:
        return os.getenv(f"RELAYDESK_SMTP_{name}", "").strip()

    @property
    def configured(self) -> bool:
        return bool(self._setting("HOST") and self._setting("FROM"))

    def send_invitation(self, recipient: str, name: str, workspace_name: str, invite_url: str, role: str) -> bool:
        subject = f"You have been invited to {workspace_name}"
        body = f"Hi {name},\n\nYou have been invited to join {workspace_name} as {role}.\n\nCreate your account using this secure link (expires in 7 days):\n{invite_url}\n\nIf you were not expecting this invitation, you can ignore this email."
        return self.send(recipient, subject, body)

    def send_verification(self, recipient: str, name: str, verification_url: str, workspace_name: str) -> bool:
        subject = f"Verify your email for {workspace_name}"
        body = f"Hi {name},\n\nFinish creating your {workspace_name} account by opening this secure link:\n\n{verification_url}\n\nThis link expires in 15 minutes and works only once. If you did not create this account, you can ignore this email."
        return self.send(recipient, subject, body)

    def send(self, recipient: str, subject: str, body: str) -> bool:
        if not self.configured:
            return False
        message = EmailMessage()
        message["From"] = self._setting("FROM")
        message["To"] = recipient
        message["Subject"] = subject
        message.set_content(body)
        host = self._setting("HOST")
        port = int(self._setting("PORT") or "587")
        username = self._setting("USERNAME")
        password = self._setting("PASSWORD")
        use_tls = (self._setting("USE_TLS") or "true").lower() in {"1", "true", "yes", "on"}
        with smtplib.SMTP(host, port, timeout=12) as server:
            server.ehlo()
            if use_tls:
                server.starttls()
                server.ehlo()
            if username:
                server.login(username, password)
            server.send_message(message)
        return True


email_service = EmailService()
