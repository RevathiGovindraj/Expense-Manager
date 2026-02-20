import re

CATEGORY_PATTERNS = {
    "Food": r"food|pizza|burger|biryani|dinner|lunch|snacks|tea|coffee",
    "Travel": r"travel|uber|ola|bus|train|taxi|petrol|fuel|cab",
    "Shopping": r"shopping|dress|clothes|amazon|flipkart|mall",
    "Bills": r"bill|electricity|current|water|recharge|wifi|rent",
    "Entertainment": r"movie|netflix|game|cinema",
    "Health": r"medicine|hospital|doctor|clinic"
}


def detect_category(description):
    description = description.lower()

    for category, pattern in CATEGORY_PATTERNS.items():
        if re.search(pattern, description):
            return category

    return "Others"
