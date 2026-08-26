"""SMTP delivery for workspace invitations and account recovery messages."""

import os
import smtplib
from email.message import EmailMessage


class EmailService:
    @property
    def configured(self) -> bool:
        return bool(os.getenv("SMTP_HOST") and os.getenv("SMTP_FROM"))

    def send_invitation(self, recipient: str, name: str, workspace_name: str, invite_url: str, role: str) -> bool:
        subject = f"You have been invited to {workspace_name}"
        body = f"Hi {name},\n\nYou have been invited to join {workspace_name} as {role}.\n\nCreate your account using this secure link (expires in 7 days):\n{invite_url}\n\nIf you were not expecting this invitation, you can ignore this email."
        return self.send(recipient, subject, body)

    def send(self, recipient: str, subject: str, body: str) -> bool:
        if not self.configured:
            return False
        message = EmailMessage()
        message["From"] = os.getenv("SMTP_FROM")
        message["To"] = recipient
        message["Subject"] = subject
        message.set_content(body)
        host = os.getenv("SMTP_HOST")
        port = int(os.getenv("SMTP_PORT", "587"))
        username = os.getenv("SMTP_USERNAME", "")
        password = os.getenv("SMTP_PASSWORD", "")
        use_tls = os.getenv("SMTP_USE_TLS", "true").lower() in {"1", "true", "yes", "on"}
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
