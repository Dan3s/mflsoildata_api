"""Minimal SMTP email helper for the dataset-approval flow.

Configured via env: SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASSWORD,
SMTP_FROM, SMTP_TLS (default true). When SMTP_HOST is unset the message is logged
instead of sent, so dev/CI never breaks.
"""

import logging
import os
import smtplib
from email.message import EmailMessage

logger = logging.getLogger("mfl.email")


def send_email(to: str, subject: str, body: str) -> bool:
    if not to:
        return False

    host = os.getenv("SMTP_HOST")
    if not host:
        logger.info("SMTP not configured — would email %s | %s\n%s", to, subject, body)
        return False

    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASSWORD")
    sender = os.getenv("SMTP_FROM") or user or "no-reply@localhost"
    use_tls = os.getenv("SMTP_TLS", "true").lower() in {"1", "true", "yes"}

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        with smtplib.SMTP(host, port, timeout=15) as server:
            if use_tls:
                server.starttls()
            if user and password:
                server.login(user, password)
            server.send_message(msg)
        return True
    except Exception as exc:  # never let email failure break the request
        logger.warning("Failed to send email to %s: %s", to, exc)
        return False
