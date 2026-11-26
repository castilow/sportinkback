"""Envío de emails transaccionales por SMTP (Resend, Brevo, Google Workspace…).

Variables: SMTP_HOST, SMTP_PORT (587 STARTTLS / 465 SSL), SMTP_USER,
SMTP_PASSWORD, EMAIL_FROM («Sportink <hola@tudominio.com>»), EMAIL_REPLY_TO.
Sin SMTP_HOST no se envía nada (solo se registra en el log).
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr

logger = logging.getLogger("sportink.mailer")


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def configured() -> bool:
    return bool(_env("SMTP_HOST") and _env("EMAIL_FROM"))


def send_email(to: list[str] | str, subject: str, html: str, text: str) -> bool:
    recipients = [to] if isinstance(to, str) else [t for t in to if t]
    if not recipients:
        return False
    if not configured():
        logger.info("Email NO enviado (falta SMTP): %s → %s", subject, ", ".join(recipients))
        return False
    msg = EmailMessage()
    name, addr = parseaddr(_env("EMAIL_FROM"))
    msg["From"] = formataddr((name or "Sportink", addr))
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=addr.split("@")[-1] if "@" in addr else None)
    if _env("EMAIL_REPLY_TO"):
        msg["Reply-To"] = _env("EMAIL_REPLY_TO")
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    host, port = _env("SMTP_HOST"), int(_env("SMTP_PORT", "587") or 587)
    user, password = _env("SMTP_USER"), _env("SMTP_PASSWORD")
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=20) as s:
                if user:
                    s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.ehlo()
                if s.has_extn("starttls"):
                    s.starttls(context=ssl.create_default_context())
                    s.ehlo()
                if user:
                    s.login(user, password)
                s.send_message(msg)
        logger.info("Email enviado: %s → %s", subject, ", ".join(recipients))
        return True
    except Exception as exc:
        logger.error("No se pudo enviar el email «%s»: %s", subject, exc)
        return False
