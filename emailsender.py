import os
import smtplib
from email.mime.text import MIMEText


def _try_load_dotenv():
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv()
    except Exception:
        pass


def _get_smtp_config():
    _try_load_dotenv()

    host = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
    port_raw = os.getenv("SMTP_PORT", "587").strip()
    user = os.getenv("SMTP_USER", "").strip()
    smtp_pass = os.getenv("SMTP_PASS", "").replace(" ", "").strip()
    gmail_app_pass = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    sender = os.getenv("SMTP_FROM", user).strip() or user
    use_tls = os.getenv("SMTP_USE_TLS", "1").strip() == "1"

    password = smtp_pass or gmail_app_pass

    try:
        port = int(port_raw)
    except ValueError:
        port = 587

    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "sender": sender,
        "use_tls": use_tls,
    }


def send_email(receiver_email: str, subject: str, body: str):
    cfg = _get_smtp_config()

    if not receiver_email or "@" not in receiver_email:
        return False, "Invalid receiver email"

    if not cfg["host"] or not cfg["port"] or not cfg["user"] or not cfg["password"]:
        return False, "SMTP not configured"

    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = cfg["sender"]
    msg["To"] = receiver_email
    msg["Subject"] = subject

    try:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=20) as server:
            if cfg["use_tls"]:
                server.starttls()
            server.login(cfg["user"], cfg["password"])
            server.sendmail(cfg["sender"], [receiver_email], msg.as_string())
        return True, "Email sent"
    except Exception as exc:
        return False, str(exc)
