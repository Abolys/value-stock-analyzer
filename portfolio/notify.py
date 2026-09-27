"""Optional email for new alerts, over the standard library's smtplib (STARTTLS).

Off unless SMTP_HOST and ALERT_EMAIL_TO are set in .env (config.smtp_settings). One
message per alert check lists every new alert. The outcome ("sent", "skipped - …",
"failed - …") is returned and stored on each alert; a failure never stops a check.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

import config
from portfolio.models import Alert

SKIPPED_NOT_CONFIGURED = "skipped - SMTP not configured"
SKIPPED_NOTHING = "skipped - no new alerts"
SENT = "sent"


def configured() -> bool:
    s = config.smtp_settings()
    return bool(s["SMTP_HOST"] and s["ALERT_EMAIL_TO"])


def build_message(alerts: list[Alert], settings: dict[str, str]) -> EmailMessage:
    msg = EmailMessage()
    tickers = sorted({a.ticker for a in alerts})
    msg["Subject"] = f"Value Stock Analyzer: {len(alerts)} new alert(s) — {', '.join(tickers)}"
    msg["From"] = settings["ALERT_EMAIL_FROM"] or settings["SMTP_USER"] or settings["ALERT_EMAIL_TO"]
    msg["To"] = settings["ALERT_EMAIL_TO"]
    lines = [f"- {a.ticker}: {config.ALERT_KINDS.get(a.kind, a.kind)} — {a.message}" for a in alerts]
    msg.set_content("New alerts:\n\n" + "\n".join(lines) + "\n\nOpen the Portfolio page for details.\n\n"
                    "Personal research output, not investment advice.\n")
    return msg


def send_alert_email(alerts: list[Alert], smtp_factory=smtplib.SMTP) -> str:
    if not alerts:
        return SKIPPED_NOTHING
    if not configured():
        return SKIPPED_NOT_CONFIGURED
    s = config.smtp_settings()
    try:
        port = int(s["SMTP_PORT"] or config.SMTP_DEFAULT_PORT)
        with smtp_factory(s["SMTP_HOST"], port, timeout=config.SMTP_TIMEOUT_SECONDS) as smtp:
            smtp.starttls()
            if s["SMTP_USER"]:
                smtp.login(s["SMTP_USER"], s["SMTP_PASSWORD"])
            smtp.send_message(build_message(alerts, s))
    except (OSError, smtplib.SMTPException, ValueError) as exc:
        return f"failed - {type(exc).__name__}: {exc}"
    return SENT
