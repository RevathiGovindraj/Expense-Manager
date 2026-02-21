from flask import Flask, render_template, request, redirect, session, send_file
from modules.ai_engine import detect_category
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
import sqlite3
import os
import pytesseract
from PIL import Image
import re
import os

# Tell pytesseract where Tesseract is installed
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

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
    # Expenses + Filtering
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
    # Monthly Trend Data
    # ===============================

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

    # ===============================
    # AI Insights
    # ===============================

    insights = []
    if category_totals:
        top_category = max(category_totals, key=category_totals.get)
        insights.append(f"You spend most on {top_category}.")

    if percent_change > 0:
        insights.append("Spending increased compared to last month.")
    elif percent_change < 0:
        insights.append("Spending decreased compared to last month.")

    if budget_percent >= 100:
        insights.append("You have exceeded your budget!")
    elif budget_percent >= 80:
        insights.append("Warning: You have used more than 80% of your budget.")

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
        insights=insights,
        months=months,
        month_totals=month_totals
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
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase import pdfmetrics

@app.route("/download_pdf")
def download_pdf():
    if "user_id" not in session:
        return redirect("/login")

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT description, category, amount, expense_date
        FROM expenses
        WHERE user_id = ?
        ORDER BY expense_date DESC
    """, (session["user_id"],))

    expenses = cursor.fetchall()
    conn.close()

    filename = "expense_report.pdf"
    doc = SimpleDocTemplate(filename)
    pdfmetrics.registerFont(TTFont("DejaVuSans", "DejaVuSans.ttf"))
    elements = []

    styles = getSampleStyleSheet()
    elements.append(Paragraph("<b>Expense Report</b>", styles["Title"]))
    elements.append(Spacer(1, 0.5 * inch))

    # Table Data
    data = [["Date", "Description", "Category", "Amount"]]

    total_amount = 0

    for e in expenses:
        data.append([
            str(e["expense_date"]),
            e["description"],
            e["category"],
            f"₹{e['amount']}"
        ])
        total_amount += e["amount"]

    # Add total row
    data.append(["", "", "Total", f"₹{total_amount}"])

    table = Table(data, colWidths=[1.2*inch, 1.8*inch, 1.2*inch, 1*inch])

    table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.grey),
        ("TEXTCOLOR",(0,0),(-1,0),colors.whitesmoke),
        ("GRID", (0,0), (-1,-1), 1, colors.black),
        ("FONTNAME", (0,0), (-1,-1), "DejaVuSans"),
        ("FONTSIZE", (0,0), (-1,-1), 10),
        ("ALIGN", (3,1), (3,-1), "RIGHT"),
        ("BACKGROUND", (0,-1), (-1,-1), colors.lightgrey)
    ]))

    elements.append(table)

    doc.build(elements)

    return send_file(filename, as_attachment=True)

@app.route("/upload_receipt", methods=["POST"])
def upload_receipt():
    if "receipt" not in request.files:
        return redirect("/dashboard")

    file = request.files["receipt"]

    if file.filename == "":
        return redirect("/dashboard")

    filepath = os.path.join("uploads", file.filename)
    file.save(filepath)

    # Extract text from image
    text = pytesseract.image_to_string(Image.open(filepath))

    # Find amount using regex
    amounts = re.findall(r"\d+\.\d+|\d+", text)

    amount = 0
    if amounts:
        amount = max([float(a) for a in amounts])

    # Guess category
    text_lower = text.lower()

    if any(word in text_lower for word in ["restaurant", "food", "cafe", "dine"]):
        category = "Food"
    elif any(word in text_lower for word in ["mall", "shopping", "store"]):
        category = "Shopping"
    elif any(word in text_lower for word in ["uber", "ola", "taxi", "bus"]):
        category = "Transport"
    else:
        category = "Other"

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        "INSERT INTO expenses (description, amount, category, user_id) VALUES (?, ?, ?, ?)",
        ("Receipt Expense", amount, category, session["user_id"])
    )

    conn.commit()
    conn.close()

    return redirect("/dashboard")

# ---------------------------
# RUN
# ---------------------------
if __name__ == "__main__":
    init_db()
    app.run(debug=True)
