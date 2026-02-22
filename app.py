from flask import Flask, render_template, request, redirect, session
from modules.ai_engine import detect_category
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
import sqlite3
import os
import re
import pytesseract
from PIL import Image
from expense_predictor import predict_next_month_expense
from modules.ai_engine import train_model

app = Flask(__name__)
app.secret_key = "secret123"

DATABASE = "database.db"


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
        # OCR can read zero as letter O in amounts, e.g., "1O".
        token = re.sub(r"(?<=\d)[oO](?=\d|\b)", "0", token)
        token = re.sub(r"(?<=\d)\.(?=\D*$)", ".", token)
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
    values = []

    # Strict rule: amount must appear next to rupee marker (₹ / Rs / INR).
    marker_pattern = re.compile(
        r"(?:₹|rs\.?|inr)\s*([0-9O]{1,7}(?:,[0-9O]{2,3})*(?:\.[0-9O]{1,2})?)",
        flags=re.IGNORECASE,
    )
    for raw in marker_pattern.findall(text):
        parsed = parse_amount_token(raw)
        if parsed is not None:
            values.append((10, parsed))

    # OCR may split marker and amount into separate tokens/lines.
    compact = " ".join([line.strip() for line in text.splitlines() if line.strip()])
    tokens = compact.split()
    rupee_tokens = {"₹", "rs", "rs.", "inr"}
    for idx, token in enumerate(tokens[:-1]):
        if token.lower() in rupee_tokens:
            parsed = parse_amount_token(tokens[idx + 1])
            if parsed is not None:
                values.append((9, parsed))

    # OCR variant: symbol read as non-alphanumeric character, e.g., "°50".
    symbol_line_pattern = re.compile(r"^[^\w\s]\s*([0-9O]{1,7}(?:\.[0-9O]{1,2})?)$")
    for line in lines:
        match = symbol_line_pattern.match(line)
        if not match:
            continue
        parsed = parse_amount_token(match.group(1))
        if parsed is not None:
            values.append((8, parsed))

    # OCR variant in GPay-like screenshots: rupee symbol becomes leading "2", e.g. "210" for "₹10".
    # Accept only standalone numeric lines to avoid phone/UPI IDs.
    standalone_number = re.compile(r"^\d{2,7}$")
    for line in lines:
        if not standalone_number.match(line):
            continue
        # Skip obvious long identifiers.
        if len(line) >= 5:
            continue
        if line.startswith("2") and len(line) >= 3:
            corrected = parse_amount_token(line[1:])
            if corrected is not None:
                values.append((7, corrected))

    if values:
        values.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return values[0][1]

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
        password TEXT NOT NULL
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

    # Ensure old databases have the status column.
    cursor.execute("PRAGMA table_info(expenses)")
    expense_columns = [row[1] for row in cursor.fetchall()]
    if "status" not in expense_columns:
        cursor.execute("ALTER TABLE expenses ADD COLUMN status TEXT DEFAULT 'Send'")

    conn.commit()
    conn.close()


# ---------------------------
# HOME
# ---------------------------
@app.route("/")
def home():
    return render_template("index.html")


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
    if request.method == "POST":
        name = request.form["name"]
        email = request.form["email"]
        password = generate_password_hash(request.form["password"])

        conn = get_db()
        cursor = conn.cursor()

        try:
            cursor.execute("""
                INSERT INTO users (name, email, password)
                VALUES (?, ?, ?)
            """, (name, email, password))
            conn.commit()
        except:
            return "Email already exists"

        conn.close()
        return redirect("/login")

    return render_template("signup.html")


# ---------------------------
# DASHBOARD
# ---------------------------
@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect("/login")

    predicted_expense = predict_next_month_expense(session["user_id"])

    conn = get_db()
    cursor = conn.cursor()

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
    for row in personal_transactions:
        status = str(row["status"]).lower()
        amount = float(row["amount"])
        if status == "received":
            total_received += amount
        else:
            total_sent += amount

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

    return render_template(
        "dashboard.html",
        expenses=expenses,
        personal_transactions=personal_transactions,
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
        budget=0,
        budget_percent=0,
        insights=[],
        expenses_json=expenses_json,
        personal_json=personal_json,
    )


# ---------------------------
# ADD EXPENSE
# ---------------------------
@app.route("/add", methods=["POST"])
def add():
    if "user_id" not in session:
        return redirect("/login")

    description = request.form.get("name")
    manual_category = request.form.get("manual_category")
    amount = request.form.get("amount")

    if not description or not amount:
        return redirect("/dashboard")

    try:
        amount = float(amount)
    except:
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

    description = request.form.get("edit_name")
    amount = request.form.get("edit_amount")

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
        return redirect("/dashboard")

    amount, description = parse_expense_message(message)
    if amount is None or not description:
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

    if status not in {"Send", "Received"}:
        status = "Send"

    try:
        amount = float(amount)
    except:
        return redirect("/dashboard")

    conn = get_db()
    cursor = conn.cursor()
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
        return redirect("/dashboard")

    os.makedirs("uploads", exist_ok=True)
    filepath = os.path.join("uploads", file.filename)
    file.save(filepath)

    text = pytesseract.image_to_string(Image.open(filepath))
    person_name, description, amount, status = extract_personal_payment_details(text)

    if amount <= 0:
        return redirect("/dashboard")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO personal_transactions (user_id, person_name, description, amount, status)
        VALUES (?, ?, ?, ?, ?)
    """, (session["user_id"], person_name, description, amount, status))
    conn.commit()
    conn.close()

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

if __name__ == "__main__":
    init_db()
    app.run(debug=True, use_reloader=False)
