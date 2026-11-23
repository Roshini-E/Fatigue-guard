"""Emergency-contact email delivery (SMTP over STARTTLS, port 587)."""
import logging
import os
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

IST = timezone(timedelta(hours=5, minutes=30))
logger = logging.getLogger(__name__)


def _cfg():
    try:
        port = int(os.environ.get("SMTP_PORT") or 587)
    except ValueError:
        port = 0
    return {
        "host": os.environ.get("SMTP_HOST"),
        "port": port,
        "username": os.environ.get("SMTP_USERNAME"),
        "password": os.environ.get("SMTP_PASSWORD"),
        "from": os.environ.get("SMTP_FROM_EMAIL"),
    }


def smtp_configured(c) -> bool:
    sender = parseaddr(c["from"] or "")[1]
    return bool(c["host"] and c["port"] in (465, 587) and c["username"]
                and c["password"] and sender and "@" in sender
                and not any(x in (c["host"] + c["username"] + sender).lower()
                            for x in ("your-", "example.com", "placeholder")))


def configuration_status() -> dict:
    c = _cfg()
    missing = []
    for key, value in (("SMTP_HOST", c["host"]), ("SMTP_USERNAME", c["username"]),
                       ("SMTP_PASSWORD", c["password"]), ("SMTP_FROM_EMAIL", c["from"])):
        if not value or any(x in value.lower() for x in ("your-", "example.com", "placeholder")):
            missing.append(key)
    if c["port"] not in (465, 587):
        missing.append("SMTP_PORT (use 587 for STARTTLS or 465 for SSL)")
    sender = parseaddr(c["from"] or "")[1]
    if (not sender or "@" not in sender) and "SMTP_FROM_EMAIL" not in missing:
        missing.append("SMTP_FROM_EMAIL (must contain a valid email address)")
    return {"configured": smtp_configured(c), "missing": missing}


def ist_now_text() -> str:
    d = datetime.now(IST)
    h12 = d.hour % 12 or 12
    return f"{d.day}/{d.month}/{d.year}, {h12}:{d.minute:02d}:{d.second:02d} {'am' if d.hour < 12 else 'pm'}"


def send_fatigue_alert(to: dict, driver_name: str, vehicle, trigger: str, risk_score: float, session_id: str) -> dict:
    c = _cfg()
    subject = f"FatigueGuard ALERT - {driver_name}: {trigger} (risk {round(risk_score)}%)"
    text = "\n".join([
        "FATIGUE ALERT - immediate attention required",
        "",
        f"Driver:    {driver_name}",
        f"Vehicle:   {vehicle or 'N/A'}",
        f"Trigger:   {trigger}",
        f"Risk score: {round(risk_score)}/100",
        f"Session:   {session_id}",
        f"Time:      {ist_now_text()} IST",
        "",
        "FatigueGuard detected signs of drowsiness while the driver was on a monitoring session.",
        "The in-cabin siren has been triggered. Please reach out to the driver immediately.",
    ])

    if not smtp_configured(c):
        missing = configuration_status()["missing"]
        return {"status": "simulated", "detail": (
            f"SMTP is not configured ({', '.join(missing)}). Set valid values in the project-root .env.")}

    try:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = c["from"], to["email"], subject
        msg.set_content(text)
        smtp_class = smtplib.SMTP_SSL if c["port"] == 465 else smtplib.SMTP
        with smtp_class(c["host"], c["port"], timeout=20) as s:
            if c["port"] == 587:
                s.ehlo()
                s.starttls()
                s.ehlo()
            s.login(c["username"], c["password"])
            refused = s.send_message(msg)
        if to["email"] in refused:
            return {"status": "failed", "detail": f"SMTP refused recipient {to['email']}: {refused[to['email']][1].decode(errors='replace')}"}
        return {"status": "delivered", "detail": f"Email sent to {to['email']}"}
    except Exception as exc:
        # SMTP exceptions can include the configured username; never log credentials.
        detail = f"{type(exc).__name__}: {exc}"
        for secret in (c["password"], c["username"]):
            if secret:
                detail = detail.replace(secret, "[redacted]")
        logger.exception("SMTP delivery failed for recipient %s", to.get("email"))
        return {"status": "failed", "detail": f"Send failed for {to['email']}: {detail[:500]}"}


def send_test_email(recipient: str) -> dict:
    c = _cfg()
    if not smtp_configured(c):
        return {"status": "simulated", "detail": f"SMTP is not configured ({', '.join(configuration_status()['missing'])})."}
    try:
        msg = EmailMessage()
        msg["From"], msg["To"] = c["from"], recipient
        msg["Subject"] = "FatigueGuard SMTP configuration test"
        msg.set_content("This test confirms FatigueGuard can authenticate with SMTP and deliver email.")
        smtp_class = smtplib.SMTP_SSL if c["port"] == 465 else smtplib.SMTP
        with smtp_class(c["host"], c["port"], timeout=20) as s:
            if c["port"] == 587:
                s.ehlo(); s.starttls(); s.ehlo()
            s.login(c["username"], c["password"])
            refused = s.send_message(msg)
        if recipient in refused:
            return {"status": "failed", "detail": "SMTP refused the test recipient."}
        return {"status": "delivered", "detail": f"Test email delivered to {recipient}."}
    except Exception as exc:
        logger.exception("SMTP test delivery failed for recipient %s", recipient)
        detail = f"{type(exc).__name__}: {exc}"
        for secret in (c["password"], c["username"]):
            if secret: detail = detail.replace(secret, "[redacted]")
        return {"status": "failed", "detail": detail[:500]}
