from flask import Flask, render_template, request, redirect, session
import sqlite3
import os

app = Flask(__name__)
app.secret_key = "secret123"

DATABASE = "database.db"

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


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
    CREATE TABLE IF NOT EXISTS groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        created_by INTEGER,
        FOREIGN KEY (created_by) REFERENCES users(id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS group_members (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER,
        user_id INTEGER,
        FOREIGN KEY (group_id) REFERENCES groups(id),
        FOREIGN KEY (user_id) REFERENCES users(id)
    )
    """)

    cursor.execute("""
CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    description TEXT,
    category TEXT,
    amount REAL
)
""")


    cursor.execute("""
    CREATE TABLE IF NOT EXISTS expense_splits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        expense_id INTEGER,
        user_id INTEGER,
        share_amount REAL,
        FOREIGN KEY (expense_id) REFERENCES expenses(id),
        FOREIGN KEY (user_id) REFERENCES users(id)
    )
    """)

    conn.commit()
    conn.close()


CATEGORY_KEYWORDS = {
    "Food": ["food", "pizza", "burger", "biryani", "dinner", "lunch", "snacks", "tea", "coffee"],
    "Travel": ["travel", "uber", "ola", "bus", "train", "taxi", "petrol", "fuel", "cab"],
    "Shopping": ["shopping", "dress", "clothes", "amazon", "flipkart", "mall"],
    "Bills": ["bill", "electricity", "current", "water", "recharge", "wifi", "rent"],
}

# ---------------------------
# INTRO PAGE (NEW)
# ---------------------------
@app.route("/")
def home():
    return render_template("index.html")


# ---------------------------
# LOGIN PAGE
# ---------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None

    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]

        if username == "admin" and password == "admin":
            session["user"] = username
            return redirect("/dashboard")
        else:
            error = "Invalid credentials"

    return render_template("login.html", error=error)


# ---------------------------
# DASHBOARD PAGE
# ---------------------------
@app.route("/dashboard")
def dashboard():
    if "user" not in session:
        return redirect("/login")

    import sqlite3
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()

    # Get all expenses
    cursor.execute("SELECT description, category, amount FROM expenses")
    expenses = cursor.fetchall()

    # Calculate total
    total = sum([row[2] for row in expenses])

    # Calculate category totals for chart
    category_totals = {}
    for row in expenses:
        cat = row[1]
        category_totals[cat] = category_totals.get(cat, 0) + row[2]

    conn.close()

    return render_template(
        "dashboard.html",
        expenses=expenses,
        total=total,
        category_totals=category_totals
    )


    # ✅ Chatbot message
    chat_reply = session.pop("chat_reply", None)

    # ✅ Chart data (category totals)
    category_totals = {}

    if os.path.exists(FILE_NAME):
        with open(FILE_NAME, mode="r") as file:
            reader = csv.reader(file)
            for row in reader:
                if len(row) >= 3:
                    expenses.append(row)
                    total += int(row[2])

                    cat = row[1]
                    amt = int(row[2])

                    if cat in category_totals:
                        category_totals[cat] += amt
                    else:
                        category_totals[cat] = amt

    return render_template(
        "dashboard.html",
        expenses=expenses,
        total=total,
        chat_reply=chat_reply,
        category_totals=category_totals
    )


# ---------------------------
# ADD EXPENSE
# ---------------------------

@app.route("/add", methods=["POST"])
def add():
    if "user" not in session:
        return redirect("/login")

    description = request.form.get("name")
    category = request.form.get("category")
    amount = request.form.get("amount")

    if not description or not amount:
        return redirect("/dashboard")

    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO expenses (group_id, paid_by, description, category, amount)
    VALUES (?, ?, ?, ?, ?)
    """, (1, 1, description, amount))

    conn.commit()
    conn.close()

    return redirect("/dashboard")


    try:
        amount = int(amount)
        if amount <= 0:
            return redirect("/dashboard")
    except:
        return redirect("/dashboard")

    with open(FILE_NAME, mode="a", newline="") as file:
        writer = csv.writer(file)
        writer.writerow([name, category, amount])

    return redirect("/dashboard")


# ---------------------------
# CHATBOT ADD EXPENSE
# ---------------------------
@app.route("/chat_add", methods=["POST"])
def chat_add():
    if "user" not in session:
        return redirect("/login")

    message = request.form["message"].lower().strip()
    parts = message.split()

    # Expected: add 200 pizza
    if len(parts) < 3 or parts[0] != "add":
        session["chat_reply"] = "❌ Format: Add <amount> <expense name>"
        return redirect("/dashboard")

    try:
        amount = int(parts[1])
    except:
        session["chat_reply"] = "❌ Amount must be a number"
        return redirect("/dashboard")

    # Expense name text (pizza / uber cab / electricity bill)
    name_text = " ".join(parts[2:]).strip()
    name = name_text.capitalize() if name_text else "Chat Expense"

    # Auto detect category
    category = "Other"
    for cat, keywords in CATEGORY_KEYWORDS.items():
        for word in keywords:
            if word in message:
                category = cat
                break
        if category != "Other":
            break

    # Save to CSV
    with open(FILE_NAME, mode="a", newline="") as file:
        writer = csv.writer(file)
        writer.writerow([name, category, amount])

    # Success message
    session["chat_reply"] = f"✅ Added: {name} - {category} - ₹{amount}"
    return redirect("/dashboard")


# ---------------------------
# DELETE EXPENSE
# ---------------------------
@app.route("/delete/<int:index>")
def delete(index):
    if "user" not in session:
        return redirect("/login")

    rows = []

    if os.path.exists(FILE_NAME):
        with open(FILE_NAME, mode="r") as file:
            reader = csv.reader(file)
            rows = list(reader)

    if 0 <= index < len(rows):
        rows.pop(index)

    with open(FILE_NAME, mode="w", newline="") as file:
        writer = csv.writer(file)
        writer.writerows(rows)

    return redirect("/dashboard")
@app.route("/edit/<int:index>", methods=["GET","POST"])
def edit(index):
    if "user" not in session:
        return redirect("/")

    rows = []
    with open(FILE_NAME, "r") as f:
        rows = list(csv.reader(f))

    if request.method == "POST":
        rows[index] = [
            request.form["name"],
            request.form["category"],
            request.form["amount"]
        ]
        with open(FILE_NAME, "w", newline="") as f:
            csv.writer(f).writerows(rows)
        return redirect("/dashboard")

    return render_template("edit.html", item=rows[index], index=index)


# ---------------------------
# LOGOUT
# ---------------------------
@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")
from flask import send_file

@app.route("/export")
def export():
    if "user" not in session:
        return redirect("/")

    return send_file(
        FILE_NAME,
        as_attachment=True,
        download_name="expenses.csv"
    )


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
import webbrowser

if __name__ == "__main__":
    webbrowser.open("http://127.0.0.1:5000/")
    app.run(debug=True)
