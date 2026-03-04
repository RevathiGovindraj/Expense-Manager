import os
import smtplib
import ssl
from email.message import EmailMessage


SENDER_EMAIL = "revathig2709@gmail.com"
SMTP_HOST_DEFAULT = "smtp.gmail.com"
SMTP_PORT_DEFAULT = 587


def _load_env_fallback():
    """Load .env manually if python-dotenv is unavailable."""
    if os.getenv("GMAIL_APP_PASSWORD") or os.getenv("SMTP_PASS"):
        return
    env_path = os.path.join(os.getcwd(), ".env")
    if not os.path.exists(env_path):
        return
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception:
        pass


def send_email(receiver_email: str, subject: str, body: str):
    _load_env_fallback()

    sender_email = os.getenv("SMTP_USER", SENDER_EMAIL).strip() or SENDER_EMAIL
    receiver_email = (receiver_email or "").strip().lower()
    smtp_host = os.getenv("SMTP_HOST", SMTP_HOST_DEFAULT).strip() or SMTP_HOST_DEFAULT
    smtp_port = int(os.getenv("SMTP_PORT", str(SMTP_PORT_DEFAULT)))
    use_tls = os.getenv("SMTP_USE_TLS", "1").strip() != "0"
    smtp_pass = os.getenv("SMTP_PASS", "").strip()
    gmail_pass = os.getenv("GMAIL_APP_PASSWORD", "").strip()

    # Ignore placeholder values so they don't shadow the real password.
    if smtp_pass.upper().startswith("YOUR_"):
        smtp_pass = ""
    if gmail_pass.upper().startswith("YOUR_"):
        gmail_pass = ""

    password = smtp_pass or gmail_pass
    password = password.replace(" ", "")

    if not receiver_email:
        return False, "Receiver email is required."
    if not password:
        return False, "SMTP password is not configured."

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.getenv("SMTP_FROM", sender_email).strip() or sender_email
    msg["To"] = receiver_email
    msg.set_content(body)

    try:
        if use_tls:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
                server.ehlo()
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
                server.login(sender_email, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=15, context=ssl.create_default_context()) as server:
                server.login(sender_email, password)
                server.send_message(msg)
    except Exception as exc:
        return False, str(exc)

    return True, ""


def send_login_otp(receiver_email: str, otp_code: str):
    subject = "SplitPilot Login OTP"
    body = (
        f"Your SplitPilot login OTP is: {otp_code}\\n\\n"
        "This OTP is valid for 10 minutes.\\n"
        "If this wasn't you, ignore this email."
    )
    return send_email(receiver_email, subject, body)
