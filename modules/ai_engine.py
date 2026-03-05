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

    texts = []
    labels = []
    for row in data:
        cleaned = clean_text(row[0] or "").strip()
        if cleaned:
            texts.append(cleaned)
            labels.append(row[1])

    if len(texts) < 5:
        return

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
    text = clean_text(description or "").strip()
    if not text:
        return "Others"

    # Fully rule-based detection (no ML fallback). This prevents wrong bias like everything -> Food.
    keyword_map = {
        "Travel": [
            "fuel", "petrol", "diesel", "cab", "taxi", "uber", "ola", "auto",
            "bus", "metro", "train", "flight", "ticket", "toll", "parking",
            "trip", "travel", "commute", "ride"
        ],
        "Bills": [
            "emi", "insurance", "electricity", "bill", "recharge", "rent",
            "wifi", "internet", "broadband", "mobile", "water", "gas",
            "subscription", "netflix", "prime", "hotstar", "loan",
            "postpaid", "utility", "maintenance", "fees", "tuition"
        ],
        "Food": [
            "biryani", "dosa", "pizza", "burger", "curry", "sandwich",
            "grocery", "groceries", "vegetable", "vegetables", "fruit",
            "fruits", "milk", "restaurant", "cafe", "coffee", "tea", "lunch",
            "dinner", "breakfast", "snacks", "food", "meal", "zomato",
            "swiggy", "juice", "bakery", "chocolate"
        ],
        "Shopping": [
            "clothes", "cloth", "dress", "shirt", "tshirt", "pant", "jeans",
            "saree", "kurti", "shoe", "shoes", "slipper", "footwear", "bag",
            "gift", "gifts", "present", "shopping", "amazon", "flipkart",
            "mall", "cosmetics", "makeup", "accessory", "watch", "phone",
            "laptop", "headphone", "electronics", "furniture"
        ],
    }

    tokens = re.findall(r"[a-z]+", text)
    if not tokens:
        return "Others"

    # Stem-like matching to handle OCR/voice truncation (e.g., "subscript" -> "subscription")
    keyword_stems = {
        cat: {kw[:6] for kw in kws if len(kw) >= 4}
        for cat, kws in keyword_map.items()
    }

    scores = {cat: 0 for cat in keyword_map}
    for token in tokens:
        for category, keywords in keyword_map.items():
            if token in keywords:
                scores[category] += 3
            elif len(token) >= 4 and token[:6] in keyword_stems[category]:
                scores[category] += 2

    # Phrase contains checks for multi-word signals.
    if "phone bill" in text:
        scores["Bills"] += 3

    best_category, best_score = max(scores.items(), key=lambda kv: kv[1])
    if best_score <= 0:
        return "Others"
    return best_category
