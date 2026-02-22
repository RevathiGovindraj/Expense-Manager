from flask import Flask, render_template, request, redirect, session
from modules.ai_engine import detect_category
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
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
        expense_date DATE DEFAULT CURRENT_DATE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )
    """)

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
        SELECT id, description, category, amount, expense_date
        FROM expenses
        WHERE user_id = ?
        ORDER BY created_at DESC
    """, (session["user_id"],))

    expenses = cursor.fetchall()

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

    return render_template(
        "dashboard.html",
        expenses=expenses,
        total=total,
        months=months,
        month_totals=month_totals,
        predicted_expense=predicted_expense,
        category_totals=category_totals,
        month_total=0,
        last_month_total=0,
        percent_change=0,
        budget=0,
        budget_percent=0,
        insights=[]
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
        INSERT INTO expenses (user_id, description, category, amount)
        VALUES (?, ?, ?, ?)
    """, (session["user_id"], description, category, amount))

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
        INSERT INTO expenses (user_id, description, category, amount)
        VALUES (?, ?, ?, ?)
    """, (session["user_id"], "Scanned Receipt", category, amount))
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
        INSERT INTO expenses (user_id, description, category, amount)
        VALUES (?, ?, ?, ?)
    """, (session["user_id"], description, category, amount))

    conn.commit()
    conn.close()

    # retrain model
    from modules.ai_engine import train_model
    train_model()

    return redirect("/dashboard")

if __name__ == "__main__":
    init_db()
    app.run(debug=True, use_reloader=False)
