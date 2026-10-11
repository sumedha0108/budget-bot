"""Settings, constants and small helpers shared by every module."""
import os
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Kolkata")

# Edit this list to change categories. Keep "other" as the fallback.
CATEGORIES = [
    "food", "groceries", "transport", "shopping", "entertainment",
    "utilities", "rent", "health", "other",
]
BUDGET_KEYS = CATEGORIES + ["total"]   # "total" = overall monthly budget
ALERT_AT = 0.8                         # warn when a budget is 80% used


def today() -> date:
    """Today's date in India (the server itself runs in UTC)."""
    return datetime.now(TZ).date()


def get_secret(name: str, default=None):
    """Read from Streamlit secrets first, then environment variables."""
    try:
        import streamlit as st
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name, default)


def inr(x) -> str:
    """Format a number as rupees with Indian digit grouping: 1234567 -> ₹12,34,567."""
    d = Decimal(str(x)).quantize(Decimal("0.01"))
    neg = d < 0
    d = abs(d)
    whole = str(int(d))
    cents = int((d - int(d)) * 100)
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    out = "₹" + whole + (f".{cents:02d}" if cents else "")
    return "-" + out if neg else out
