from flask import Flask, render_template, request, redirect, session, send_file
from modules.ai_engine import detect_category
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
import sqlite3
import os

app = Flask(__name__)
app.secret_key = "secret123"

DATABASE = "database.db"


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

    # Users table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL
    )
    """)

    # Expenses table
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

    # Budgets table  ✅ NEW
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS budgets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        month TEXT,
        amount REAL,
        FOREIGN KEY(user_id) REFERENCES users(id)
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

    conn = get_db()
    cursor = conn.cursor()

    # ===============================
    # Monthly Summary
    # ===============================
    current_month = datetime.now().strftime("%Y-%m")

    cursor.execute("""
        SELECT SUM(amount) as total
        FROM expenses
        WHERE user_id = ?
        AND strftime('%Y-%m', expense_date) = ?
    """, (session["user_id"], current_month))

    row = cursor.fetchone()
    month_total = row["total"] if row["total"] else 0

    last_month_date = datetime.now().replace(day=1) - timedelta(days=1)
    last_month = last_month_date.strftime("%Y-%m")

    cursor.execute("""
        SELECT SUM(amount) as total
        FROM expenses
        WHERE user_id = ?
        AND strftime('%Y-%m', expense_date) = ?
    """, (session["user_id"], last_month))

    last_row = cursor.fetchone()
    last_month_total = last_row["total"] if last_row["total"] else 0

    if last_month_total > 0:
        percent_change = ((month_total - last_month_total) / last_month_total) * 100
    else:
        percent_change = 0

    # ===============================
    # Budget
    # ===============================
    cursor.execute("""
        SELECT amount FROM budgets
        WHERE user_id = ? AND month = ?
    """, (session["user_id"], current_month))

    budget_row = cursor.fetchone()
    budget = budget_row["amount"] if budget_row else 0

    if budget > 0:
        budget_percent = (month_total / budget) * 100
    else:
        budget_percent = 0

    # ===============================
    # Filtering
    # ===============================
    selected_month = request.args.get("month")
    search = request.args.get("search")
    selected_category = request.args.get("category")
    sort = request.args.get("sort")

    query = """
        SELECT id, description, category, amount, expense_date
        FROM expenses
        WHERE user_id = ?
    """

    params = [session["user_id"]]

    if selected_month:
        query += " AND strftime('%Y-%m', expense_date) = ?"
        params.append(selected_month)

    if search:
        query += " AND description LIKE ?"
        params.append(f"%{search}%")

    if selected_category:
        query += " AND category = ?"
        params.append(selected_category)

    if sort == "high":
        query += " ORDER BY amount DESC"
    elif sort == "low":
        query += " ORDER BY amount ASC"
    else:
        query += " ORDER BY created_at DESC"

    cursor.execute(query, params)
    expenses = cursor.fetchall()

    total = sum([row["amount"] for row in expenses])

    category_totals = {}
    for row in expenses:
        cat = row["category"]
        category_totals[cat] = category_totals.get(cat, 0) + row["amount"]

    # ===============================
    # AI Insights
    # ===============================
    insights = []

    if category_totals:
        top_category = max(category_totals, key=category_totals.get)
        insights.append(f"You spend most on {top_category}.")

    if percent_change > 0:
        insights.append("Your spending increased compared to last month.")
    elif percent_change < 0:
        insights.append("Good job! Spending decreased from last month.")

    if budget_percent >= 100:
        insights.append("⚠ Budget exceeded! Control your expenses.")
    elif budget_percent >= 80:
        insights.append("⚠ You are close to exceeding your budget.")

    conn.close()

    return render_template(
        "dashboard.html",
        expenses=expenses,
        total=total,
        category_totals=category_totals,
        month_total=month_total,
        last_month_total=last_month_total,
        percent_change=round(percent_change, 2),
        budget=budget,
        budget_percent=round(budget_percent, 2),
        insights=insights
    )

# ---------------------------
# ADD EXPENSE
# ---------------------------
@app.route("/add", methods=["POST"])
def add():
    if "user_id" not in session:
        return redirect("/login")

    description = request.form.get("name")
    category = detect_category(description)
    amount = request.form.get("amount")

    if not description or not amount:
        return redirect("/dashboard")

    try:
        amount = float(amount)
    except:
        return redirect("/dashboard")

    conn = get_db()
    cursor = conn.cursor()

    # Duplicate detection
    cursor.execute("""
        SELECT * FROM expenses
        WHERE user_id = ?
        AND description = ?
        AND amount = ?
        AND DATE(created_at) = DATE('now')
    """, (session["user_id"], description, amount))

    duplicate = cursor.fetchone()

    if duplicate:
        conn.close()
        return "⚠️ Duplicate expense detected!"

    cursor.execute("""
        INSERT INTO expenses (user_id, description, category, amount)
        VALUES (?, ?, ?, ?)
    """, (session["user_id"], description, category, amount))

    conn.commit()
    conn.close()

    return redirect("/dashboard")


# ---------------------------
# DELETE EXPENSE
# ---------------------------
@app.route("/delete/<int:id>")
def delete(id):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("DELETE FROM expenses WHERE id = ?", (id,))
    conn.commit()
    conn.close()

    return redirect("/dashboard")

# ---------------------------
# SET BUDGET
# ---------------------------
@app.route("/set_budget", methods=["POST"])
def set_budget():
    if "user_id" not in session:
        return redirect("/login")

    amount = request.form.get("budget")

    try:
        amount = float(amount)
    except:
        return redirect("/dashboard")

    current_month = datetime.now().strftime("%Y-%m")

    conn = get_db()
    cursor = conn.cursor()

    # Check if budget already exists
    cursor.execute("""
        SELECT * FROM budgets
        WHERE user_id = ? AND month = ?
    """, (session["user_id"], current_month))

    existing = cursor.fetchone()

    if existing:
        cursor.execute("""
            UPDATE budgets
            SET amount = ?
            WHERE user_id = ? AND month = ?
        """, (amount, session["user_id"], current_month))
    else:
        cursor.execute("""
            INSERT INTO budgets (user_id, month, amount)
            VALUES (?, ?, ?)
        """, (session["user_id"], current_month, amount))

    conn.commit()
    conn.close()

    return redirect("/dashboard")
# ---------------------------
# LOGOUT
# ---------------------------
@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

@app.route("/chat_add", methods=["POST"])
def chat_add():
    if "user_id" not in session:
        return "Login required"

    message = request.form.get("message", "").strip().lower()

    if not message:
        return "Please enter a message"

    parts = message.split()

    if len(parts) < 3 or parts[0] != "add":
        return "Format: Add <amount> <description>"

    try:
        amount = float(parts[1])
    except:
        return "Amount must be a number"

    description = " ".join(parts[2:])
    category = detect_category(description)

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO expenses (user_id, description, category, amount)
        VALUES (?, ?, ?, ?)
    """, (session["user_id"], description, category, amount))

    conn.commit()
    conn.close()

    return f"✅ Added ₹{amount} under {category}"


# ---------------------------
# RUN
# ---------------------------
if __name__ == "__main__":
    init_db()
    app.run(debug=True)
