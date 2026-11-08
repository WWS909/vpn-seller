"""
Starting points the owner can take, never forced on them.

A fresh install sells nothing: no plans, no discounts, no documents. The
panel offers these templates behind a button, and the owner sees and edits
every number before anything is created.
"""

from __future__ import annotations

from .config import settings as env

# Suggested prices per currency. For a currency not listed the owner types
# the prices themselves — a guess in the wrong currency would be worse.
_PRICES = {
    "RUB": (199, 499, 899, 1490),
    "USD": (3, 8, 14, 24),
    "EUR": (3, 8, 14, 24),
    "GBP": (3, 7, 12, 20),
    "UAH": (99, 249, 449, 749),
    "KZT": (1200, 3000, 5400, 9000),
    "BYN": (7, 18, 32, 55),
}

_PLANS = (
    {"key": "m1", "name": "1 месяц", "name_en": "1 month", "days": 30},
    {"key": "m3", "name": "3 месяца", "name_en": "3 months", "days": 90},
    {"key": "m6", "name": "6 месяцев", "name_en": "6 months", "days": 180},
    {"key": "y1", "name": "1 год", "name_en": "1 year", "days": 365},
)


def plan_templates(currency: str | None = None) -> list[dict]:
    """The usual four terms with suggested prices (0 = the owner must set one)."""
    prices = _PRICES.get((currency or env.currency).upper(), (0, 0, 0, 0))
    return [{**p, "price": prices[i], "devices": 3, "traffic_gb": 0, "sort": i + 1}
            for i, p in enumerate(_PLANS)]


# What the panel suggests next to the settings that start empty or off.
TRIAL_HINT = {"days": 3, "devices": 1}
