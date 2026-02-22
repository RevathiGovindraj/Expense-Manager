import os
import re
import sqlite3

import joblib


# ---------------------------
# TEXT CLEANING
# ---------------------------
def clean_text(text):
    text = text.lower()
    text = re.sub(r'[^a-zA-Z\s]', '', text)
    return text


# ---------------------------
# TRAIN MODEL
# ---------------------------
DATABASE = "database.db"
MODEL_PATH = "expense_model.pkl"
VECTORIZER_PATH = "vectorizer.pkl"

CATEGORY_KEYWORDS = {
    "Travel": [
        "fuel", "petrol", "diesel", "uber", "ola", "taxi", "auto", "bus",
        "train", "metro", "flight", "cab", "parking", "toll",
    ],
    "Bills": [
        "emi", "insurance", "electricity", "recharge", "bill", "internet",
        "wifi", "broadband", "water", "gas", "rent", "phone",
    ],
    "Food": [
        "biryani", "dosa", "pizza", "burger", "curry", "sandwich", "ramen",
        "restaurant", "lunch", "dinner", "breakfast", "snack", "chips",
        "coffee", "tea", "swiggy", "zomato", "meal", "grocery", "groceries",
    ],
    "Shopping": [
        "shopping", "shop", "shirt", "tshirt", "shoe", "slipper", "dress",
        "jeans", "kurti", "bag", "watch", "myntra", "amazon", "flipkart",
        "clothes", "fashion",
    ],
}


def keyword_category(description):
    normalized = clean_text(description)
    for category, words in CATEGORY_KEYWORDS.items():
        for word in words:
            if re.search(rf"\b{re.escape(word)}\b", normalized):
                return category
    return None

def train_model():
    # Import heavy ML dependencies only when training.
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression

    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    cursor.execute("SELECT description, category FROM expenses")
    data = cursor.fetchall()
    conn.close()

    if len(data) < 5:
        return

    texts = [row[0].lower() for row in data]
    labels = [row[1] for row in data]

    vectorizer = TfidfVectorizer(
        stop_words="english",
        ngram_range=(1,2)
    )

    X = vectorizer.fit_transform(texts)

    model = LogisticRegression(max_iter=1000)
    model.fit(X, labels)

    joblib.dump(model, MODEL_PATH)
    joblib.dump(vectorizer, VECTORIZER_PATH)


# ---------------------------
# LOAD MODEL
# ---------------------------
def load_model():
    if os.path.exists(MODEL_PATH):
        return joblib.load(MODEL_PATH)
    return train_model()


# ---------------------------
# DETECT CATEGORY
# ---------------------------
def detect_category(description):
    description = description.lower().strip()

    # Rule-based detection first for predictable command-like text.
    rule_category = keyword_category(description)
    if rule_category:
        return rule_category

    if not os.path.exists(MODEL_PATH) or not os.path.exists(VECTORIZER_PATH):
        return "Others"

    model = joblib.load(MODEL_PATH)
    vectorizer = joblib.load(VECTORIZER_PATH)

    X = vectorizer.transform([description])
    prediction = model.predict(X)[0]

    if prediction in {"Food", "Travel", "Bills", "Shopping", "Others"}:
        return prediction
    return "Others"
