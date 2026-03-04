from flask import Flask, render_template, request, redirect, session, flash, send_from_directory, make_response
from modules.ai_engine import detect_category
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta
import calendar
import sqlite3
import os
import re
import secrets
import csv
import io
import smtplib
import ssl
from email.message import EmailMessage
import pytesseract
from PIL import Image
from expense_predictor import predict_next_month_expense
from modules.ai_engine import train_model

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "0") == "1"
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB upload cap

DATABASE = "database.db"
PROFILE_UPLOAD_DIR = os.path.join("uploads", "profile")
SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASS = os.getenv("SMTP_PASS", "").strip()
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER).strip()
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "1").strip() != "0"


@app.after_request
def apply_security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Cache-Control"] = "no-store"
    return resp


def parse_expense_message(message):
    message = message.strip().lower()
    if not message:
        return None, None

    patterns = [
        r"^add\s+(\d+(?:\.\d+)?)\s+(.+)$",
        r"^spent\s+(\d+(?:\.\d+)?)\s+on\s+(.+)$",
        r"^i\s+spent\s+(\d+(?:\.\d+)?)\s+on\s+(.+)$",
        r"^pay(?:ed)?\s+(\d+(?:\.\d+)?)\s+for\s+(.+)$",
    ]

    for pattern in patterns:
        match = re.match(pattern, message)
        if match:
            amount = float(match.group(1))
            description = match.group(2).strip()
            description = re.sub(r"^(on|for)\s+", "", description).strip()
            if description:
                return amount, description

    # Fallback: first number is amount, remaining words become description.
    amount_match = re.search(r"\d+(?:\.\d+)?", message)
    if not amount_match:
        return None, None

    amount = float(amount_match.group())
    before = message[:amount_match.start()].strip()
    after = message[amount_match.end():].strip()
    description = f"{before} {after}".strip()

    # Trim filler words common in voice commands.
    description = re.sub(r"^(add|spent|i spent|pay|paid|on|for)\s+", "", description).strip()
    if not description:
        return None, None

    return amount, description


def extract_receipt_amount(text):
    if not text:
        return 0.0

    cleaned = text.lower()
    # Remove common date/time patterns that pollute numeric extraction.
    cleaned = re.sub(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", " ", cleaned)
    cleaned = re.sub(r"\b\d{4}[/-]\d{1,2}[/-]\d{1,2}\b", " ", cleaned)
    cleaned = re.sub(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", " ", cleaned)

    amount_pattern = re.compile(r"(?<!\d)(\d+(?:,\d{3})*(?:\.\d{1,2})?)(?!\d)")
    priority_keywords = [
        "grand total", "total amount", "net amount", "amount due", "payable", "total"
    ]
    low_priority_keywords = ["qty", "quantity", "item", "invoice no", "bill no", "gstin", "phone"]

    candidates = []

    for line in cleaned.splitlines():
        line = line.strip()
        if not line:
            continue

        nums = amount_pattern.findall(line)
        if not nums:
            continue

        line_score = 0
        if any(k in line for k in priority_keywords):
            line_score += 3
        if any(k in line for k in low_priority_keywords):
            line_score -= 2

        for raw in nums:
            try:
                value = float(raw.replace(",", ""))
            except ValueError:
                continue

            if value <= 0 or value > 100000:
                continue

            score = line_score
            if "." in raw:
                score += 1
            candidates.append((score, value))

    if not candidates:
        return 0.0

    # Pick best-scored candidate; if tie, choose larger value.
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return candidates[0][1]


def extract_payment_amount(text):
    if not text:
        return 0.0

    def parse_amount_token(raw):
        token = raw.strip().replace(",", "")
        token = re.sub(r"(?<=\d)[oO](?=\d|\b)", "0", token)
        if not re.fullmatch(r"\d+(?:\.\d{1,2})?", token):
            return None
        try:
            value = float(token)
        except ValueError:
            return None
        if 0 < value <= 200000:
            return value
        return None

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    compact = " ".join(lines)
    candidates = []

    marker_pattern = re.compile(
        r"(?:\u20B9|rs\.?|inr)\s*([0-9O]{1,7}(?:,[0-9O]{2,3})*(?:\.[0-9O]{1,2})?)",
        flags=re.IGNORECASE,
    )
    for raw in marker_pattern.findall(compact):
        parsed = parse_amount_token(raw)
        if parsed is not None:
            candidates.append((12, parsed))

    # Generic number extraction with scoring to avoid transaction IDs.
    amount_pattern = re.compile(r"(?<!\d)(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d{1,6}(?:\.\d{1,2})?)(?!\d)")
    low_priority = ("upi", "transaction id", "google transaction id", "utr", "ref", "account", "bank", "@")

    for line in lines:
        line_l = line.lower()
        line_score = 0
        if "\u20B9" in line or " rs" in f" {line_l}" or "inr" in line_l:
            line_score += 5
        if any(word in line_l for word in ("paid", "sent", "received", "from", "to", "completed")):
            line_score += 1
        if any(k in line_l for k in low_priority):
            line_score -= 5
        if len(line) <= 18:
            line_score += 2

        for raw in amount_pattern.findall(line):
            parsed = parse_amount_token(raw)
            if parsed is None:
                continue
            score = line_score
            if "," in raw:
                score += 4
            if "." in raw:
                score += 1
            if raw.isdigit() and len(raw) >= 7:
                score -= 6
            candidates.append((score, parsed))

    if candidates:
        # Highest score first; for ties choose larger amount.
        candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return candidates[0][1]

    return 0.0


def clean_party_name(raw_name):
    if not raw_name:
        return ""

    name = raw_name.strip()
    name = re.split(r"\b(upi|utr|ref|txn|transaction|id)\b", name, flags=re.IGNORECASE)[0]
    name = re.sub(r"[^A-Za-z0-9 .&_-]", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" -:")
    return name[:60]


def extract_personal_payment_details(text):
    if not text:
        return "Unknown", "Payment Screenshot", 0.0, "Send"

    lines = [line.strip() for line in text.splitlines() if line.strip()]

    person_name = ""
    status = "Send"

    # Required behavior: from -> Received, to -> Send.
    for line in lines:
        from_match = re.match(r"^(received\s+from|from)\s*[:\-]?\s*(.+)$", line, flags=re.IGNORECASE)
        if from_match:
            person_name = clean_party_name(from_match.group(2))
            status = "Received"
            break

        to_match = re.match(r"^(paid\s+to|to)\s*[:\-]?\s*(.+)$", line, flags=re.IGNORECASE)
        if to_match:
            person_name = clean_party_name(to_match.group(2))
            status = "Send"
            break

    if not person_name:
        joined = " ".join(lines)
        from_any = re.search(r"\bfrom\s*[:\-]?\s*([A-Za-z0-9 .&_-]{2,60})", joined, flags=re.IGNORECASE)
        to_any = re.search(r"\bto\s*[:\-]?\s*([A-Za-z0-9 .&_-]{2,60})", joined, flags=re.IGNORECASE)
        if from_any:
            person_name = clean_party_name(from_any.group(1))
            status = "Received"
        elif to_any:
            person_name = clean_party_name(to_any.group(1))
            status = "Send"

    if not person_name:
        person_name = "Unknown"

    amount = extract_payment_amount(text)
    description = "Payment Screenshot"
    return person_name, description, amount, status


def parse_iso_date(value):
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except Exception:
        return None


def add_months(base_date, months):
    month_index = (base_date.month - 1) + months
    year = base_date.year + month_index // 12
    month = (month_index % 12) + 1
    last_day = calendar.monthrange(year, month)[1]
    day = min(base_date.day, last_day)
    return base_date.replace(year=year, month=month, day=day)


def advance_due_date(current_due, frequency):
    freq = str(frequency or "monthly").lower()
    if freq == "weekly":
        return current_due + timedelta(days=7)
    if freq == "yearly":
        return add_months(current_due, 12)
    return add_months(current_due, 1)


def reminder_meta(next_due_date, reminder_days):
    today = datetime.now().date()
    days_left = (next_due_date - today).days
    if days_left < 0:
        return "overdue", days_left
    if days_left == 0:
        return "due_today", days_left
    if days_left <= reminder_days:
        return "upcoming", days_left
    return "normal", days_left


def mask_email(email):
    if not email or "@" not in email:
        return "your email"
    name, domain = email.split("@", 1)
    if len(name) <= 2:
        masked_name = name[0] + "*"
    else:
        masked_name = name[0] + ("*" * (len(name) - 2)) + name[-1]
    return f"{masked_name}@{domain}"


def clear_password_otp_session():
    session.pop("pwd_reset_otp_hash", None)
    session.pop("pwd_reset_new_hash", None)
    session.pop("pwd_reset_expires_at", None)


def clear_signup_otp_session():
    session.pop("signup_name", None)
    session.pop("signup_email", None)
    session.pop("signup_otp_hash", None)
    session.pop("signup_otp_expires_at", None)


def is_alpha_space_text(value):
    if not value:
        return False
    normalized = re.sub(r"\s+", " ", value).strip()
    return bool(re.fullmatch(r"[A-Za-z]+(?: [A-Za-z]+)*", normalized))


def send_email_message(recipient_email, subject, body):
    if not SMTP_HOST or not SMTP_PORT or not SMTP_USER or not SMTP_PASS or not SMTP_FROM:
        return False, "SMTP not configured"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = recipient_email
    msg.set_content(body)

    try:
        if SMTP_USE_TLS:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
                server.ehlo()
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
                server.login(SMTP_USER, SMTP_PASS)
                server.send_message(msg)
        else:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15, context=ssl.create_default_context()) as server:
                server.login(SMTP_USER, SMTP_PASS)
                server.send_message(msg)
    except Exception as exc:
        return False, str(exc)

    return True, ""


def send_otp_email(recipient_email, otp_code):
    body = (
        f"Your SplitPilot OTP is: {otp_code}\n\n"
        "This OTP expires in 10 minutes.\n"
        "If you did not request this, ignore this email."
    )
    return send_email_message(recipient_email, "SplitPilot Password Reset OTP", body)


def send_recurring_reminder_email(recipient_email, recurring_item, status_key, days_left):
    title = recurring_item["title"]
    amount = float(recurring_item["amount"])
    next_due = recurring_item["next_due_date"]
    frequency = str(recurring_item["frequency"]).title()

    if status_key == "overdue":
        status_line = f"This payment is overdue by {abs(days_left)} day(s)."
    elif status_key == "due_today":
        status_line = "This payment is due today."
    else:
        status_line = f"This payment is due in {days_left} day(s)."

    notes = (recurring_item["notes"] or "").strip()
    notes_line = f"\nNotes: {notes}" if notes else ""

    subject = f"SplitPilot Reminder: {title} due on {next_due}"
    body = (
        f"Hello,\n\n"
        f"Recurring expense reminder from SplitPilot:\n"
        f"Title: {title}\n"
        f"Category: {recurring_item['category']}\n"
        f"Amount: INR {amount:.2f}\n"
        f"Frequency: {frequency}\n"
        f"Next Due Date: {next_due}\n"
        f"{status_line}{notes_line}\n\n"
        f"Please open SplitPilot and mark it paid when completed."
    )

    return send_email_message(recipient_email, subject, body)


def ensure_recurring_last_paid_column(cursor):
    cursor.execute("PRAGMA table_info(recurring_expenses)")
    recurring_columns = [row[1] for row in cursor.fetchall()]
    if "last_paid_date" not in recurring_columns:
        cursor.execute("ALTER TABLE recurring_expenses ADD COLUMN last_paid_date DATE")
    if "reminder_last_due_date" not in recurring_columns:
        cursor.execute("ALTER TABLE recurring_expenses ADD COLUMN reminder_last_due_date DATE")


# ---------------------------
# DATABASE CONNECTION
# ---------------------------
def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------
# INITIALIZE DATABASE
# ---------------------------
def init_db():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        profile_photo TEXT DEFAULT ''
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        description TEXT NOT NULL,
        category TEXT NOT NULL,
        amount REAL NOT NULL,
        status TEXT DEFAULT 'Send',
        expense_date DATE DEFAULT CURRENT_DATE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS personal_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        person_name TEXT NOT NULL,
        description TEXT NOT NULL,
        amount REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'Send',
        transaction_date DATE DEFAULT CURRENT_DATE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS recurring_expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'Bills',
        amount REAL NOT NULL,
        frequency TEXT NOT NULL DEFAULT 'monthly',
        start_date DATE NOT NULL,
        next_due_date DATE NOT NULL,
        last_paid_date DATE,
        reminder_last_due_date DATE,
        reminder_days INTEGER NOT NULL DEFAULT 3,
        is_active INTEGER NOT NULL DEFAULT 1,
        notes TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS budgets (
        user_id INTEGER PRIMARY KEY,
        monthly_budget REAL NOT NULL DEFAULT 0,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )
    """)

    # Backward-compatible migration for older budgets schemas.
    cursor.execute("PRAGMA table_info(budgets)")
    budget_cols = [row[1] for row in cursor.fetchall()]
    if "monthly_budget" not in budget_cols:
        cursor.execute("ALTER TABLE budgets ADD COLUMN monthly_budget REAL NOT NULL DEFAULT 0")
        if "budget" in budget_cols:
            cursor.execute("""
                UPDATE budgets
                SET monthly_budget = COALESCE(monthly_budget, budget, 0)
            """)
    if "updated_at" not in budget_cols:
        cursor.execute("ALTER TABLE budgets ADD COLUMN updated_at TIMESTAMP")

    # Ensure old databases have the status column.
    cursor.execute("PRAGMA table_info(users)")
    user_columns = [row[1] for row in cursor.fetchall()]
    if "profile_photo" not in user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN profile_photo TEXT DEFAULT ''")

    # Ensure old databases have the status column.
    cursor.execute("PRAGMA table_info(expenses)")
    expense_columns = [row[1] for row in cursor.fetchall()]
    if "status" not in expense_columns:
        cursor.execute("ALTER TABLE expenses ADD COLUMN status TEXT DEFAULT 'Send'")

    # Ensure old recurring schema has last_paid_date.
    ensure_recurring_last_paid_column(cursor)

    conn.commit()
    conn.close()


# ---------------------------
# HOME
# ---------------------------
@app.route("/")
def home():
    return render_template("index.html")


@app.route("/profile_photo/<path:filename>")
def profile_photo(filename):
    return send_from_directory(PROFILE_UPLOAD_DIR, filename)


# ---------------------------
# LOGIN
# ---------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None

    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE email = ?", (email,))
        user = cursor.fetchone()
        conn.close()

        if user and check_password_hash(user["password"], password):
            session["user_id"] = user["id"]
            return redirect("/dashboard")
        else:
            error = "Invalid credentials"

    return render_template("login.html", error=error)


# ---------------------------
# SIGNUP
# ---------------------------
@app.route("/signup", methods=["GET", "POST"])
def signup():
    otp_pending = False
    pending_email = session.get("signup_email", "")
    expiry_raw = session.get("signup_otp_expires_at")
    if session.get("signup_otp_hash") and pending_email and expiry_raw:
        try:
            otp_pending = datetime.fromisoformat(expiry_raw) > datetime.now()
        except Exception:
            clear_signup_otp_session()
            otp_pending = False

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        if not name or not email:
            flash("Name and email are required.", "error")
            return redirect("/signup")

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE email = ?", (email,))
        exists = cursor.fetchone()
        conn.close()
        if exists:
            flash("Email already exists. Please login.", "error")
            return redirect("/login")

        otp = f"{secrets.randbelow(900000) + 100000}"
        session["signup_name"] = name
        session["signup_email"] = email
        session["signup_otp_hash"] = generate_password_hash(otp)
        session["signup_otp_expires_at"] = (datetime.now() + timedelta(minutes=10)).isoformat()

        sent, reason = send_otp_email(email, otp)
        if sent:
            flash(f"OTP sent to {mask_email(email)}. Verify to set password.", "success")
        else:
            flash(f"Email OTP could not be sent ({reason}). Demo OTP: {otp}", "error")
        return redirect("/signup")

    return render_template("signup.html", otp_pending=otp_pending, pending_email=pending_email)


@app.route("/verify_signup_otp", methods=["POST"])
def verify_signup_otp():
    otp = request.form.get("otp", "").strip()
    password = request.form.get("password", "")
    confirm_password = request.form.get("confirm_password", "")

    name = session.get("signup_name", "")
    email = session.get("signup_email", "")
    otp_hash = session.get("signup_otp_hash")
    expiry_raw = session.get("signup_otp_expires_at")

    if not name or not email or not otp_hash or not expiry_raw:
        flash("No active signup OTP. Please start signup again.", "error")
        return redirect("/signup")

    if not otp or not password or not confirm_password:
        flash("OTP and password fields are required.", "error")
        return redirect("/signup")

    if password != confirm_password:
        flash("Password and confirm password do not match.", "error")
        return redirect("/signup")

    if len(password) < 6:
        flash("Password must be at least 6 characters.", "error")
        return redirect("/signup")

    try:
        expiry = datetime.fromisoformat(expiry_raw)
    except Exception:
        clear_signup_otp_session()
        flash("OTP session invalid. Please signup again.", "error")
        return redirect("/signup")

    if datetime.now() > expiry:
        clear_signup_otp_session()
        flash("OTP expired. Please signup again.", "error")
        return redirect("/signup")

    if not check_password_hash(otp_hash, otp):
        flash("Invalid OTP. Please try again.", "error")
        return redirect("/signup")

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO users (name, email, password)
            VALUES (?, ?, ?)
        """, (name, email, generate_password_hash(password)))
        conn.commit()
    except Exception:
        conn.close()
        clear_signup_otp_session()
        flash("Could not create account. Email may already be in use.", "error")
        return redirect("/signup")

    conn.close()
    clear_signup_otp_session()
    flash("Account created successfully. Please login.", "success")
    return redirect("/login")


@app.route("/cancel_signup_otp", methods=["POST"])
def cancel_signup_otp():
    clear_signup_otp_session()
    flash("Signup OTP request cancelled.", "success")
    return redirect("/signup")


# ---------------------------
# DASHBOARD
# ---------------------------
@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect("/login")

    predicted_expense = predict_next_month_expense(session["user_id"])

    otp_pending = False
    otp_expiry = session.get("pwd_reset_expires_at")
    if session.get("pwd_reset_otp_hash") and otp_expiry:
        expiry_date = parse_iso_date(otp_expiry.split("T")[0])
        if expiry_date and expiry_date < datetime.now().date():
            clear_password_otp_session()
        else:
            try:
                otp_pending = datetime.fromisoformat(otp_expiry) > datetime.now()
            except Exception:
                clear_password_otp_session()

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, email, profile_photo FROM users WHERE id = ?", (session["user_id"],))
    user_profile = cursor.fetchone()

    cursor.execute("""
        SELECT id, description, category, amount, status, expense_date
        FROM expenses
        WHERE user_id = ?
        ORDER BY created_at DESC
    """, (session["user_id"],))

    expenses = cursor.fetchall()

    cursor.execute("""
        SELECT id, person_name, description, amount, status, transaction_date
        FROM personal_transactions
        WHERE user_id = ?
        ORDER BY created_at DESC
    """, (session["user_id"],))

    personal_transactions = cursor.fetchall()

    cursor.execute("""
        SELECT id, title, category, amount, frequency, start_date, next_due_date, last_paid_date,
               reminder_days, is_active, notes, reminder_last_due_date
        FROM recurring_expenses
        WHERE user_id = ?
        ORDER BY next_due_date ASC
    """, (session["user_id"],))

    recurring_expenses = cursor.fetchall()

    category_totals = {}
    for row in expenses:
        cat = row["category"]
        category_totals[cat] = category_totals.get(cat, 0) + row["amount"]

    total = sum([row["amount"] for row in expenses])

    cursor.execute("""
        SELECT strftime('%Y-%m', expense_date) as month,
               SUM(amount) as total
        FROM expenses
        WHERE user_id = ?
        GROUP BY month
        ORDER BY month
    """, (session["user_id"],))

    trend_data = cursor.fetchall()
    months = [row["month"] for row in trend_data]
    month_totals = [row["total"] for row in trend_data]

    cursor.execute("PRAGMA table_info(budgets)")
    budget_cols = [row[1] for row in cursor.fetchall()]
    budget_column = "monthly_budget" if "monthly_budget" in budget_cols else ("budget" if "budget" in budget_cols else None)
    monthly_budget = 0.0
    if budget_column:
        cursor.execute(f"SELECT {budget_column} AS budget_value FROM budgets WHERE user_id = ?", (session["user_id"],))
        budget_row = cursor.fetchone()
        monthly_budget = float(budget_row["budget_value"]) if budget_row and budget_row["budget_value"] is not None else 0.0

    conn.close()

    today = datetime.now().date()
    start_this_month = today.replace(day=1)
    start_next_month = (start_this_month + timedelta(days=32)).replace(day=1)
    start_last_month = (start_this_month - timedelta(days=1)).replace(day=1)

    def parse_date_safe(value):
        try:
            return datetime.strptime(str(value), "%Y-%m-%d").date()
        except Exception:
            return None

    this_month_total = 0.0
    last_month_total = 0.0
    for row in expenses:
        if str(row["status"]).lower() == "received":
            continue
        d = parse_date_safe(row["expense_date"])
        if not d:
            continue
        if start_this_month <= d < start_next_month:
            this_month_total += float(row["amount"])
        elif start_last_month <= d < start_this_month:
            last_month_total += float(row["amount"])

    if last_month_total > 0:
        percent_change = round(((this_month_total - last_month_total) / last_month_total) * 100, 2)
    else:
        percent_change = 100.0 if this_month_total > 0 else 0.0

    total_sent = 0.0
    total_received = 0.0
    for row in expenses:
        status = str(row["status"]).lower()
        amount = float(row["amount"])
        if status == "received":
            total_received += amount
        else:
            total_sent += amount

    budget_percent = 0.0
    if monthly_budget > 0:
        budget_percent = round((this_month_total / monthly_budget) * 100, 2)
    for row in personal_transactions:
        status = str(row["status"]).lower()
        amount = float(row["amount"])
        if status == "received":
            total_received += amount
        else:
            total_sent += amount

    lifetime_spending = round(total_sent, 2)
    total_transactions = len(expenses) + len(personal_transactions)
    active_recurring_count = sum(1 for rec in recurring_expenses if int(rec["is_active"]) == 1)

    monthly_send_totals = {}
    for row in expenses:
        if str(row["status"]).lower() != "send":
            continue
        d = parse_date_safe(row["expense_date"])
        if not d:
            continue
        key = d.strftime("%Y-%m")
        monthly_send_totals[key] = monthly_send_totals.get(key, 0.0) + float(row["amount"])

    for row in personal_transactions:
        if str(row["status"]).lower() != "send":
            continue
        d = parse_date_safe(row["transaction_date"])
        if not d:
            continue
        key = d.strftime("%Y-%m")
        monthly_send_totals[key] = monthly_send_totals.get(key, 0.0) + float(row["amount"])

    avg_monthly_spend = round(
        (sum(monthly_send_totals.values()) / len(monthly_send_totals)) if monthly_send_totals else 0.0,
        2,
    )

    top_category_name = "-"
    top_category_value = 0.0
    if category_totals:
        top_category_name, top_category_value = max(category_totals.items(), key=lambda kv: kv[1])

    expenses_json = [
        {
            "amount": float(row["amount"]),
            "status": str(row["status"]),
            "date": str(row["expense_date"]),
            "category": str(row["category"]),
            "source": "expense",
        }
        for row in expenses
    ]

    personal_json = [
        {
            "amount": float(row["amount"]),
            "status": str(row["status"]),
            "date": str(row["transaction_date"]),
            "category": "Personal",
            "source": "personal",
        }
        for row in personal_transactions
    ]

    recurring_alerts = []
    reminder_updates = []
    user_email = user_profile["email"] if user_profile and user_profile["email"] else ""
    for rec in recurring_expenses:
        if int(rec["is_active"]) != 1:
            continue
        next_due = parse_iso_date(rec["next_due_date"])
        if not next_due:
            continue
        status_key, days_left = reminder_meta(next_due, int(rec["reminder_days"]))
        if status_key in {"overdue", "due_today", "upcoming"}:
            reminder_due_key = str(rec["next_due_date"])
            already_sent_for_due = (rec["reminder_last_due_date"] == reminder_due_key)
            if user_email and not already_sent_for_due:
                sent, _ = send_recurring_reminder_email(user_email, rec, status_key, days_left)
                if sent:
                    reminder_updates.append((reminder_due_key, rec["id"], session["user_id"]))
            recurring_alerts.append({
                "id": rec["id"],
                "title": rec["title"],
                "category": rec["category"],
                "frequency": rec["frequency"],
                "notes": rec["notes"] or "",
                "reminder_days": int(rec["reminder_days"]),
                "amount": float(rec["amount"]),
                "next_due_date": rec["next_due_date"],
                "status_key": status_key,
                "days_left": days_left,
            })

    if reminder_updates:
        conn2 = get_db()
        cursor2 = conn2.cursor()
        cursor2.executemany("""
            UPDATE recurring_expenses
            SET reminder_last_due_date = ?
            WHERE id = ? AND user_id = ?
        """, reminder_updates)
        conn2.commit()
        conn2.close()

    insights = []
    if monthly_budget > 0:
        if this_month_total > monthly_budget:
            over_by = round(this_month_total - monthly_budget, 2)
            insights.append(f"Budget exceeded by ₹{over_by} this month.")
        elif budget_percent >= 80:
            insights.append(f"You have used {budget_percent}% of this month's budget.")
        else:
            insights.append(f"Budget usage is {budget_percent}% this month.")

    if percent_change > 0:
        insights.append(f"Spending is up {percent_change}% compared to last month.")
    elif percent_change < 0:
        insights.append(f"Spending is down {abs(percent_change)}% compared to last month.")

    if category_totals:
        top_cat = max(category_totals.items(), key=lambda kv: kv[1])
        insights.append(f"Top spending category: {top_cat[0]} (₹{round(top_cat[1], 2)}).")

    if recurring_alerts:
        insights.append(f"You have {len(recurring_alerts)} recurring payment reminder(s).")

    try:
        predicted_value = float(predicted_expense)
    except Exception:
        predicted_value = 0.0

    if monthly_budget > 0 and predicted_value > monthly_budget:
        diff = round(predicted_value - monthly_budget, 2)
        insights.append(f"Next month prediction is ₹{diff} above your budget.")

    if not insights:
        insights.append("Add more transactions to generate richer insights.")

    return render_template(
        "dashboard.html",
        user_profile=user_profile,
        otp_pending=otp_pending,
        expenses=expenses,
        personal_transactions=personal_transactions,
        recurring_expenses=recurring_expenses,
        recurring_alerts=recurring_alerts,
        total=total,
        months=months,
        month_totals=month_totals,
        predicted_expense=predicted_expense,
        category_totals=category_totals,
        total_expense=round(total, 2),
        total_sent=round(total_sent, 2),
        total_received=round(total_received, 2),
        month_total=round(this_month_total, 2),
        last_month_total=round(last_month_total, 2),
        percent_change=percent_change,
        budget=monthly_budget,
        budget_percent=budget_percent,
        insights=insights,
        expenses_json=expenses_json,
        personal_json=personal_json,
        lifetime_spending=lifetime_spending,
        total_transactions=total_transactions,
        active_recurring_count=active_recurring_count,
        avg_monthly_spend=avg_monthly_spend,
        top_category_name=top_category_name,
        top_category_value=round(top_category_value, 2),
        today_iso=today.isoformat(),
    )


# ---------------------------
# ADD EXPENSE
# ---------------------------
@app.route("/add", methods=["POST"])
def add():
    if "user_id" not in session:
        return redirect("/login")

    description = (request.form.get("name") or "").strip()
    manual_category = request.form.get("manual_category")
    amount = request.form.get("amount")

    if not description or not amount:
        flash("Please enter expense name and amount.", "error")
        return redirect("/dashboard")

    if not is_alpha_space_text(description):
        flash("Expense name must contain only alphabets and spaces.", "error")
        return redirect("/dashboard")

    try:
        amount = float(amount)
    except:
        flash("Please enter a valid amount.", "error")
        return redirect("/dashboard")

    if manual_category:
        category = manual_category
    else:
        category = detect_category(description)

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO expenses (user_id, description, category, amount, status)
        VALUES (?, ?, ?, ?, ?)
    """, (session["user_id"], description, category, amount, "Send"))

    conn.commit()
    conn.close()

    train_model()
    flash("Expense added successfully.", "success")
    return redirect("/dashboard")


# ---------------------------
# DELETE EXPENSE
# ---------------------------
@app.route("/delete/<int:id>")
def delete_expense(id):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM expenses WHERE id = ?", (id,))
    conn.commit()
    conn.close()

    train_model()
    return redirect("/dashboard")


# ---------------------------
# EDIT EXPENSE
# ---------------------------
@app.route("/edit/<int:id>", methods=["POST"])
def edit_expense(id):
    if "user_id" not in session:
        return redirect("/login")

    description = (request.form.get("edit_name") or "").strip()
    amount = request.form.get("edit_amount")

    if not is_alpha_space_text(description):
        flash("Expense name must contain only alphabets and spaces.", "error")
        return redirect("/dashboard")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE expenses
        SET description = ?, amount = ?
        WHERE id = ?
    """, (description, amount, id))
    conn.commit()
    conn.close()

    train_model()
    return redirect("/dashboard")


# ---------------------------
# UPLOAD RECEIPT
# ---------------------------
@app.route("/upload_receipt", methods=["POST"])
def upload_receipt():
    if "user_id" not in session:
        return redirect("/login")

    file = request.files["receipt"]
    os.makedirs("uploads", exist_ok=True)
    filepath = os.path.join("uploads", file.filename)
    file.save(filepath)

    text = pytesseract.image_to_string(Image.open(filepath))
    amount = extract_receipt_amount(text)

    category = detect_category("receipt")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO expenses (user_id, description, category, amount, status)
        VALUES (?, ?, ?, ?, ?)
    """, (session["user_id"], "Scanned Receipt", category, amount, "Send"))
    conn.commit()
    conn.close()

    train_model()
    flash("Receipt processed and expense added.", "success")
    return redirect("/dashboard")


# ---------------------------
# LOGOUT
# ---------------------------
@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")
# ---------------------------
# SMART CHAT ADD
# ---------------------------
@app.route("/chat_add", methods=["POST"])
def chat_add():
    if "user_id" not in session:
        return redirect("/login")

    message = request.form.get("message", "").strip().lower()

    if not message:
        flash("Please type or speak a command.", "error")
        return redirect("/dashboard")

    amount, description = parse_expense_message(message)
    if amount is None or not description:
        flash("Could not parse the expense command.", "error")
        return redirect("/dashboard")
    category = detect_category(description)

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO expenses (user_id, description, category, amount, status)
        VALUES (?, ?, ?, ?, ?)
    """, (session["user_id"], description, category, amount, "Send"))

    conn.commit()
    conn.close()

    # retrain model
    from modules.ai_engine import train_model
    train_model()

    flash("Expense added from smart assistant.", "success")
    return redirect("/dashboard")


@app.route("/upload_voice_command", methods=["POST"])
def upload_voice_command():
    if "user_id" not in session:
        return redirect("/login")

    file = request.files.get("voice_audio")
    if not file or not file.filename:
        flash("No voice recording received.", "error")
        return redirect("/dashboard")

    os.makedirs("uploads", exist_ok=True)
    filename = secure_filename(file.filename) or f"voice_{int(datetime.now().timestamp())}.wav"
    filepath = os.path.join("uploads", filename)
    file.save(filepath)

    try:
        import speech_recognition as sr
    except Exception:
        flash("Voice transcription dependency missing. Install: pip install SpeechRecognition", "error")
        return redirect("/dashboard")

    recognizer = sr.Recognizer()
    try:
        with sr.AudioFile(filepath) as source:
            audio = recognizer.record(source)
        message = recognizer.recognize_google(audio).strip().lower()
    except sr.UnknownValueError:
        flash("Could not understand the recorded voice.", "error")
        return redirect("/dashboard")
    except Exception:
        flash("Voice transcription failed. Please try again.", "error")
        return redirect("/dashboard")

    amount, description = parse_expense_message(message)
    if amount is None or not description:
        flash(f"Voice command not recognized as expense: \"{message}\"", "error")
        return redirect("/dashboard")

    category = detect_category(description)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO expenses (user_id, description, category, amount, status)
        VALUES (?, ?, ?, ?, ?)
    """, (session["user_id"], description, category, amount, "Send"))
    conn.commit()
    conn.close()

    train_model()
    flash(f"Added from recording: {description} - ₹{amount}", "success")
    return redirect("/dashboard")


@app.route("/add_personal_transaction", methods=["POST"])
def add_personal_transaction():
    if "user_id" not in session:
        return redirect("/login")

    person_name = request.form.get("person_name", "").strip()
    description = request.form.get("description", "").strip()
    amount = request.form.get("amount")
    status = request.form.get("status", "Send").strip().title()

    if not person_name or not description or not amount:
        return redirect("/dashboard")

    if not is_alpha_space_text(person_name):
        flash("Person name must contain only alphabets and spaces.", "error")
        return redirect("/dashboard")

    if status not in {"Send", "Received"}:
        status = "Send"

    try:
        amount = float(amount)
    except:
        return redirect("/dashboard")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, email, profile_photo FROM users WHERE id = ?", (session["user_id"],))
    user_profile = cursor.fetchone()
    ensure_recurring_last_paid_column(cursor)
    cursor.execute("""
        INSERT INTO personal_transactions (user_id, person_name, description, amount, status)
        VALUES (?, ?, ?, ?, ?)
    """, (session["user_id"], person_name, description, amount, status))
    conn.commit()
    conn.close()

    return redirect("/dashboard")


@app.route("/upload_personal_transaction", methods=["POST"])
def upload_personal_transaction():
    if "user_id" not in session:
        return redirect("/login")

    file = request.files.get("payment_screenshot")
    if not file or not file.filename:
        flash("Please choose a payment screenshot.", "error")
        return redirect("/dashboard")

    os.makedirs("uploads", exist_ok=True)
    safe_name = secure_filename(file.filename) or f"payment_{int(datetime.now().timestamp())}.png"
    filepath = os.path.join("uploads", safe_name)
    file.save(filepath)

    image = Image.open(filepath)
    text_primary = pytesseract.image_to_string(image, config="--oem 3 --psm 6")
    text_secondary = pytesseract.image_to_string(image, config="--oem 3 --psm 11")
    text = f"{text_primary}\n{text_secondary}"
    person_name, description, amount, status = extract_personal_payment_details(text)

    if amount <= 0:
        flash("Could not detect payment amount from screenshot. Try a clearer image.", "error")
        return redirect("/dashboard")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO personal_transactions (user_id, person_name, description, amount, status)
        VALUES (?, ?, ?, ?, ?)
    """, (session["user_id"], person_name, description, amount, status))
    conn.commit()
    conn.close()

    flash(f"Personal transaction added: {person_name} - ₹{amount}", "success")
    return redirect("/dashboard")


@app.route("/delete_personal_transaction/<int:id>")
def delete_personal_transaction(id):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        DELETE FROM personal_transactions
        WHERE id = ? AND user_id = ?
    """, (id, session["user_id"]))
    conn.commit()
    conn.close()

    return redirect("/dashboard")


@app.route("/export_expenses_csv")
def export_expenses_csv():
    if "user_id" not in session:
        return redirect("/login")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT description, category, amount, status, expense_date
        FROM expenses
        WHERE user_id = ?
        ORDER BY created_at DESC
    """, (session["user_id"],))
    rows = cursor.fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Description", "Category", "Amount", "Status", "Date"])
    for row in rows:
        writer.writerow([
            row["description"],
            row["category"],
            float(row["amount"]),
            row["status"],
            row["expense_date"],
        ])

    response = make_response(output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = "attachment; filename=expenses_history.csv"
    return response


@app.route("/export_personal_csv")
def export_personal_csv():
    if "user_id" not in session:
        return redirect("/login")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT person_name, description, amount, status, transaction_date
        FROM personal_transactions
        WHERE user_id = ?
        ORDER BY created_at DESC
    """, (session["user_id"],))
    rows = cursor.fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Person", "Description", "Amount", "Status", "Date"])
    for row in rows:
        writer.writerow([
            row["person_name"],
            row["description"],
            float(row["amount"]),
            row["status"],
            row["transaction_date"],
        ])

    response = make_response(output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = "attachment; filename=personal_transactions.csv"
    return response


@app.route("/add_recurring_expense", methods=["POST"])
def add_recurring_expense():
    if "user_id" not in session:
        return redirect("/login")

    title = request.form.get("title", "").strip()
    category = request.form.get("category", "Bills").strip() or "Bills"
    amount = request.form.get("amount")
    frequency = request.form.get("frequency", "monthly").strip().lower()
    start_date_raw = request.form.get("start_date", "").strip()
    reminder_days_raw = request.form.get("reminder_days", "3").strip()
    notes = request.form.get("notes", "").strip()

    if not title or not amount or not start_date_raw:
        return redirect("/dashboard")

    if not is_alpha_space_text(title):
        flash("Recurring expense title must contain only alphabets and spaces.", "error")
        return redirect("/dashboard")

    if frequency not in {"weekly", "monthly", "yearly"}:
        frequency = "monthly"

    try:
        amount = float(amount)
    except Exception:
        return redirect("/dashboard")

    start_date = parse_iso_date(start_date_raw)
    if not start_date:
        return redirect("/dashboard")

    try:
        reminder_days = int(reminder_days_raw)
    except Exception:
        reminder_days = 3
    reminder_days = max(0, min(reminder_days, 30))

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO recurring_expenses
        (user_id, title, category, amount, frequency, start_date, next_due_date, reminder_days, is_active, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
    """, (session["user_id"], title, category, amount, frequency, start_date.isoformat(), start_date.isoformat(), reminder_days, notes))
    conn.commit()
    conn.close()

    return redirect("/dashboard")


@app.route("/mark_recurring_paid/<int:id>", methods=["POST"])
def mark_recurring_paid(id):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_db()
    cursor = conn.cursor()
    ensure_recurring_last_paid_column(cursor)
    cursor.execute("""
        SELECT id, title, category, amount, frequency, next_due_date, is_active
        FROM recurring_expenses
        WHERE id = ? AND user_id = ?
    """, (id, session["user_id"]))
    rec = cursor.fetchone()

    if not rec or int(rec["is_active"]) != 1:
        conn.close()
        flash("Recurring expense is inactive or not found.", "error")
        return redirect("/dashboard")

    today = datetime.now().date()
    due_date = parse_iso_date(rec["next_due_date"]) or today
    if due_date > today:
        conn.close()
        flash(f"'{rec['title']}' is not due yet. Next due: {due_date.isoformat()}", "error")
        return redirect("/dashboard")

    next_due = advance_due_date(due_date, rec["frequency"])

    cursor.execute("""
        INSERT INTO expenses (user_id, description, category, amount, status, expense_date)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (session["user_id"], f"{rec['title']} (Recurring)", rec["category"], float(rec["amount"]), "Send", today.isoformat()))

    cursor.execute("""
        UPDATE recurring_expenses
        SET next_due_date = ?, last_paid_date = ?
        WHERE id = ? AND user_id = ?
    """, (next_due.isoformat(), today.isoformat(), id, session["user_id"]))

    conn.commit()
    conn.close()

    train_model()
    flash(f"Marked paid for '{rec['title']}'. Next due: {next_due.isoformat()}", "success")
    return redirect("/dashboard")


@app.route("/set_budget", methods=["POST"])
def set_budget():
    if "user_id" not in session:
        return redirect("/login")

    budget_value = request.form.get("budget")
    if not budget_value:
        return redirect("/dashboard")

    try:
        monthly_budget = float(budget_value)
    except Exception:
        return redirect("/dashboard")

    if monthly_budget < 0:
        return redirect("/dashboard")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(budgets)")
    budget_cols = [row[1] for row in cursor.fetchall()]

    if "monthly_budget" not in budget_cols:
        cursor.execute("ALTER TABLE budgets ADD COLUMN monthly_budget REAL NOT NULL DEFAULT 0")
    if "updated_at" not in budget_cols:
        cursor.execute("ALTER TABLE budgets ADD COLUMN updated_at TIMESTAMP")

    # Use update-then-insert for compatibility with older schemas
    # that may not have a UNIQUE/PRIMARY constraint on user_id.
    cursor.execute("""
        UPDATE budgets
        SET monthly_budget = ?, updated_at = CURRENT_TIMESTAMP
        WHERE user_id = ?
    """, (monthly_budget, session["user_id"]))

    if cursor.rowcount == 0:
        cursor.execute("""
            INSERT INTO budgets (user_id, monthly_budget, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
        """, (session["user_id"], monthly_budget))
    conn.commit()
    conn.close()

    return redirect("/dashboard")


@app.route("/update_profile", methods=["POST"])
def update_profile():
    if "user_id" not in session:
        return redirect("/login")

    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    if not name or not email:
        flash("Name and email are required.", "error")
        return redirect("/dashboard")

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            UPDATE users
            SET name = ?, email = ?
            WHERE id = ?
        """, (name, email, session["user_id"]))
        conn.commit()
        flash("Profile updated successfully.", "success")
    except Exception:
        flash("Could not update profile. Email may already be in use.", "error")
    finally:
        conn.close()

    return redirect("/dashboard")


@app.route("/change_password", methods=["POST"])
def change_password():
    if "user_id" not in session:
        return redirect("/login")

    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    if not current_password or not new_password or not confirm_password:
        flash("All password fields are required.", "error")
        return redirect("/dashboard")

    if new_password != confirm_password:
        flash("New password and confirm password do not match.", "error")
        return redirect("/dashboard")

    if len(new_password) < 6:
        flash("New password must be at least 6 characters.", "error")
        return redirect("/dashboard")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT password FROM users WHERE id = ?", (session["user_id"],))
    user = cursor.fetchone()
    if not user or not check_password_hash(user["password"], current_password):
        conn.close()
        flash("Current password is incorrect.", "error")
        return redirect("/dashboard")

    cursor.execute("""
        UPDATE users
        SET password = ?
        WHERE id = ?
    """, (generate_password_hash(new_password), session["user_id"]))
    conn.commit()
    conn.close()
    flash("Password changed successfully.", "success")
    return redirect("/dashboard")


@app.route("/request_password_otp", methods=["POST"])
def request_password_otp():
    if "user_id" not in session:
        return redirect("/login")

    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    if not new_password or not confirm_password:
        flash("New password and confirm password are required.", "error")
        return redirect("/dashboard")

    if new_password != confirm_password:
        flash("New password and confirm password do not match.", "error")
        return redirect("/dashboard")

    if len(new_password) < 6:
        flash("New password must be at least 6 characters.", "error")
        return redirect("/dashboard")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT email FROM users WHERE id = ?", (session["user_id"],))
    user = cursor.fetchone()
    conn.close()

    otp = f"{secrets.randbelow(900000) + 100000}"
    session["pwd_reset_otp_hash"] = generate_password_hash(otp)
    session["pwd_reset_new_hash"] = generate_password_hash(new_password)
    session["pwd_reset_expires_at"] = (datetime.now() + timedelta(minutes=10)).isoformat()

    recipient_email = user["email"] if user else ""
    destination = mask_email(recipient_email)
    sent, reason = send_otp_email(recipient_email, otp)
    if sent:
        flash(f"OTP sent to {destination}. Check your inbox.", "success")
    else:
        # Dev fallback so flow still works if SMTP is not configured.
        flash(f"Email OTP could not be sent ({reason}). Demo OTP: {otp}", "error")
    return redirect("/dashboard")


@app.route("/verify_password_otp", methods=["POST"])
def verify_password_otp():
    if "user_id" not in session:
        return redirect("/login")

    otp = request.form.get("otp", "").strip()
    otp_hash = session.get("pwd_reset_otp_hash")
    new_password_hash = session.get("pwd_reset_new_hash")
    expiry_raw = session.get("pwd_reset_expires_at")

    if not otp_hash or not new_password_hash or not expiry_raw:
        flash("No active OTP request. Generate OTP first.", "error")
        return redirect("/dashboard")

    try:
        expiry = datetime.fromisoformat(expiry_raw)
    except Exception:
        clear_password_otp_session()
        flash("OTP session invalid. Please generate a new OTP.", "error")
        return redirect("/dashboard")

    if datetime.now() > expiry:
        clear_password_otp_session()
        flash("OTP expired. Please generate a new OTP.", "error")
        return redirect("/dashboard")

    if not check_password_hash(otp_hash, otp):
        flash("Invalid OTP. Please try again.", "error")
        return redirect("/dashboard")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE users
        SET password = ?
        WHERE id = ?
    """, (new_password_hash, session["user_id"]))
    conn.commit()
    conn.close()

    clear_password_otp_session()
    flash("Password updated successfully with OTP verification.", "success")
    return redirect("/dashboard")


@app.route("/cancel_password_otp", methods=["POST"])
def cancel_password_otp():
    if "user_id" not in session:
        return redirect("/login")
    clear_password_otp_session()
    flash("OTP request cleared.", "success")
    return redirect("/dashboard")


@app.route("/upload_profile_photo", methods=["POST"])
def upload_profile_photo():
    if "user_id" not in session:
        return redirect("/login")

    file = request.files.get("profile_photo")
    if not file or not file.filename:
        flash("Please choose an image file.", "error")
        return redirect("/dashboard")

    filename = secure_filename(file.filename)
    if not filename:
        flash("Invalid filename.", "error")
        return redirect("/dashboard")

    ext = os.path.splitext(filename)[1].lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
        flash("Only JPG, PNG, or WEBP images are allowed.", "error")
        return redirect("/dashboard")

    os.makedirs(PROFILE_UPLOAD_DIR, exist_ok=True)
    stored_name = f"user_{session['user_id']}_{int(datetime.now().timestamp())}{ext}"
    filepath = os.path.join(PROFILE_UPLOAD_DIR, stored_name)
    file.save(filepath)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE users
        SET profile_photo = ?
        WHERE id = ?
    """, (stored_name, session["user_id"]))
    conn.commit()
    conn.close()

    flash("Profile photo updated.", "success")
    return redirect("/dashboard")


@app.route("/toggle_recurring_expense/<int:id>", methods=["POST"])
def toggle_recurring_expense(id):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE recurring_expenses
        SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END
        WHERE id = ? AND user_id = ?
    """, (id, session["user_id"]))
    conn.commit()
    conn.close()

    return redirect("/dashboard")


@app.route("/delete_recurring_expense/<int:id>")
def delete_recurring_expense(id):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        DELETE FROM recurring_expenses
        WHERE id = ? AND user_id = ?
    """, (id, session["user_id"]))
    conn.commit()
    conn.close()

    return redirect("/dashboard")

if __name__ == "__main__":
    init_db()
    host = os.getenv("FLASK_HOST", "127.0.0.1")
    port = int(os.getenv("FLASK_PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(host=host, port=port, debug=debug, use_reloader=False)

