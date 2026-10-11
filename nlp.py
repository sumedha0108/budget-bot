"""Turns a chat message into a list of validated actions.

Order of attack (cheapest first):
  1. exact commands            ("undo", "today", "budget food 5000")  -> no LLM
  2. simple expenses           ("200 food", "coffee 150 yesterday")   -> no LLM
  3. everything else           -> one Groq call that returns JSON
Python validates whatever the model returns; the model never touches the DB.
"""
import json
import math
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

import config
from config import CATEGORIES, BUDGET_KEYS

KEYWORDS = {
    "food": "food lunch dinner breakfast brunch snack snacks coffee tea chai restaurant cafe "
            "swiggy zomato biryani pizza burger meal eating juice icecream dessert bakery",
    "groceries": "groceries grocery vegetables veggies fruits milk bigbasket blinkit zepto "
                 "instamart dmart supermarket eggs",
    "transport": "transport uber ola rapido auto cab taxi bus metro train petrol diesel fuel "
                 "flight parking toll bike",
    "shopping": "shopping clothes shoes amazon flipkart myntra shirt jeans gadget",
    "entertainment": "entertainment movie movies netflix spotify prime hotstar game games "
                     "concert party pub bar drinks",
    "utilities": "utilities electricity wifi internet broadband recharge bill bills water gas "
                 "mobile postpaid",
    "rent": "rent maintenance emi",
    "health": "health medicine medicines doctor pharmacy hospital gym dentist checkup",
}
_KW = {w: cat for cat, words in KEYWORDS.items() for w in words.split()}


def guess_category(text: str):
    """Return a category if a known keyword appears in text, else None."""
    for tok in re.findall(r"[a-z]+", text.lower()):
        if tok in _KW:
            return _KW[tok]
        if tok.endswith("s") and tok[:-1] in _KW:
            return _KW[tok[:-1]]
    return None


# numbers & dates
def to_amount(v):
    """Positive finite Decimal (2 dp) or None."""
    try:
        s = str(v).strip().lower().replace(",", "").replace("₹", "")
        mult = 1
        if s.endswith("k"):
            mult, s = 1000, s[:-1]
        d = Decimal(s) * mult
        if not d.is_finite() or d <= 0:
            return None
        return d.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def to_date(v, today: date) -> date:
    if not v:
        return today
    try:
        d = date.fromisoformat(str(v)[:10])
    except ValueError:
        return today
    return today if d > today else d


#1. commands
def parse_command(text: str):
    t = text.strip().lower().lstrip("/").strip()
    if t == "undo":
        return [{"type": "undo"}]
    if t in ("today", "week", "month"):
        return [{"type": "query", "what": "spend", "category": None, "period": t}]
    if t in ("owed", "debts"):
        return [{"type": "query", "what": "owed"}]
    if t in ("budget", "budgets"):
        return [{"type": "query", "what": "budget"}]
    if t in ("help", "start", "?"):
        return [{"type": "help"}]
    m = re.fullmatch(r"budget\s+([a-z]+)\s+(\S+)", t)
    if m and m.group(1) in BUDGET_KEYS:
        amt = to_amount(m.group(2))
        if amt:
            return [{"type": "set_budget", "category": m.group(1), "amount": amt}]
    return None



# dates written in words
_MONTHS = ("jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
           "sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?")
_MONTH_NUM = {m: i for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), start=1)}
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_DAY_FIRST = re.compile(rf"\b(?:on\s+)?(?:the\s+)?(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTHS})\b\.?")
_MONTH_FIRST = re.compile(rf"\b(?:on\s+)?({_MONTHS})\b\.?\s+(?:the\s+)?(\d{{1,2}})(?:st|nd|rd|th)?\b")
_NUMERIC = re.compile(r"\b(\d{1,2})([/.-])(\d{1,2})(?:\2(\d{2,4}))?\b")
_ORDINAL = re.compile(r"\b(?:on\s+)?(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)\b")
_WEEKDAY = re.compile(r"\b(last\s+|on\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b")
_LEFTOVER_DATE = re.compile(rf"\b({_MONTHS}|last|next|week|weekend|month|ago)\b|\d(?:st|nd|rd|th)\b")


def _make_date(day: int, month: int, today: date, year=None) -> date:
    """date(...) that raises ValueError when invalid; no year means 'the most recent such date'."""
    if year is not None:
        return date(year + 2000 if year < 100 else year, month, day)
    d = date(today.year, month, day)
    return d if d <= today else date(today.year - 1, month, day)


def extract_date(low: str, today: date):
    """Find ONE date phrase in lowercase text. Returns (date, text_without_it).
    Returns (today, text) when there is none. Raises ValueError for impossible dates."""
    m = re.search(r"\b(\d+)\s+days?\s+ago\b", low)
    if m:
        return today - timedelta(days=int(m.group(1))), low.replace(m.group(0), " ")
    for phrase, back in (("day before yesterday", 2), ("yesterday", 1), ("today", 0)):
        if re.search(rf"\b{phrase}\b", low):
            return today - timedelta(days=back), re.sub(rf"\b{phrase}\b", " ", low)

    m = _MONTH_FIRST.search(low)
    if m:
        return _make_date(int(m.group(2)), _MONTH_NUM[m.group(1)[:3]], today), low.replace(m.group(0), " ")
    m = _DAY_FIRST.search(low)
    if m:
        return _make_date(int(m.group(1)), _MONTH_NUM[m.group(2)[:3]], today), low.replace(m.group(0), " ")
    for m in _NUMERIC.finditer(low):            # 9/10 or 09/10/2026 (day first, India style)
        sep, year = m.group(2), m.group(4)
        if sep != "/" and not year:             # "1.5" or "10-12" are more likely amounts
            continue
        return (_make_date(int(m.group(1)), int(m.group(3)), today, int(year) if year else None),
                low.replace(m.group(0), " "))
    m = _ORDINAL.search(low)                    # "on the 9th" = that day of this (or last) month
    if m:
        day = int(m.group(1))
        month, year = (today.month, today.year) if day <= today.day else (
            (today.month - 1 or 12), today.year if today.month > 1 else today.year - 1)
        return date(year, month, day), low.replace(m.group(0), " ")
    m = _WEEKDAY.search(low)
    if m:
        delta = (today.weekday() - _WEEKDAYS.index(m.group(2))) % 7
        if delta == 0 and m.group(1) and m.group(1).strip() == "last":
            delta = 7
        return today - timedelta(days=delta), low.replace(m.group(0), " ")
    return today, low


# 2. fast path
_NEEDS_LLM = re.compile(
    r"\b(split|owe|owes|owed|paid|pay|lent|lend|borrow|borrowed|settle|settled|back|with|"
    r"between|share|budget|how|what|who|show|total|much|my|me|them|him|her)\b|\?",
    re.I,
)
_FILLER = {"spent", "spend", "on", "for", "i", "rs", "inr", "rupees", "bought", "at", "the",
           "a", "an", "of", "to", "got"}


def fast_parse(text: str, today: date):
    """Return (actions, confident) for plain single expenses, or None to use the LLM."""
    t = text.strip()
    if not t or _NEEDS_LLM.search(t):
        return None
    low = t.lower()

    try:
        when, low = extract_date(low, today)
    except ValueError:      # looks like a date but isn't a real one -> let the LLM sort it out
        return None
    if _LEFTOVER_DATE.search(low):
        return None         # something date-like we couldn't parse ("next week", "oct" ...)

    low = low.replace("₹", " ")
    low = re.sub(r"(?<=\d),(?=\d)", "", low)
    low = re.sub(r"\brs\.?\s*(?=\d)", " ", low)
    nums = re.findall(r"(?<![\w.])\d+(?:\.\d+)?k?(?![\w.])", low)
    if len(nums) != 1:
        return None
    amount = to_amount(nums[0])
    if not amount:
        return None

    words = [w for w in re.sub(r"(?<![\w.])\d+(?:\.\d+)?k?(?![\w.])", " ", low).split()
             if w.strip(".,!") not in _FILLER]
    note = " ".join(w.strip(".,!") for w in words).strip()
    if not note:
        return None  # a bare number tells us nothing
    category = guess_category(note)
    action = {"type": "expense", "amount": amount, "category": category or "other",
              "note": note, "date": when}
    return [action], category is not None


#3. LLM
class LLMError(Exception):
    pass


def _prompt(today: date) -> str:
    return f"""You convert a message about personal money into JSON for a budget tracker.
Today is {today.strftime('%A')}, {today.isoformat()}. Currency is INR.
Reply with ONLY a JSON object: {{"actions": [ ... ]}}. One message may contain several actions.

Action types:
{{"type":"expense","amount":N,"category":C,"note":"short label","date":"YYYY-MM-DD" or null}}
  The user's own spending. null date = today. Resolve "yesterday", "last friday", "on the 3rd" from today's date.
{{"type":"split","total":N,"paid_by":"me" or "Name","people":["Name",...],"category":C,"note":"label","date":...}}
  A bill shared with others. total = the FULL bill. people = everyone EXCEPT the user (include the payer if it is someone else). Do NOT calculate shares.
{{"type":"debt","person":"Name","direction":"they_owe_me" or "i_owe_them","amount":N,"note":"label"}}
  Money lent or borrowed that is not a shared bill.
{{"type":"settle","person":"Name","direction":"they_paid_me" or "i_paid_them","amount":N or null}}
  A repayment. amount null = everything outstanding.
{{"type":"set_budget","category":C or "total","amount":N}}
{{"type":"query","what":"spend","category":C or null,"period":"today"|"week"|"month"|"last_month"|"year"|"all"}}
{{"type":"query","what":"owed"}}
{{"type":"query","what":"budget"}}
{{"type":"undo"}}
{{"type":"reply","text":"..."}}
  Use when the message is unclear, an amount is missing or not a number, or it is not about money. Ask ONE short question.

C must be one of: {', '.join(CATEGORIES)}.
Amounts are plain numbers (no symbols). "2k" = 2000.
A date without a year means the most recent such date that is not in the future.

Examples:
"paid 3000 for dinner with Rahul and Priya" -> {{"actions":[{{"type":"split","total":3000,"paid_by":"me","people":["Rahul","Priya"],"category":"food","note":"dinner","date":null}}]}}
"Rahul paid 1200 for the cab, me and Priya shared it" -> {{"actions":[{{"type":"split","total":1200,"paid_by":"Rahul","people":["Rahul","Priya"],"category":"transport","note":"cab","date":null}}]}}
"rahul gave me back 500" -> {{"actions":[{{"type":"settle","person":"Rahul","direction":"they_paid_me","amount":500}}]}}
"lent priya 2000" -> {{"actions":[{{"type":"debt","person":"Priya","direction":"they_owe_me","amount":2000,"note":"lent"}}]}}
"haircut 300 and auto 80" -> {{"actions":[{{"type":"expense","amount":300,"category":"other","note":"haircut","date":null}},{{"type":"expense","amount":80,"category":"transport","note":"auto","date":null}}]}}
"spent some on food" -> {{"actions":[{{"type":"reply","text":"How much did you spend on food?"}}]}}"""


DEFAULT_MODELS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]  # Llama 3.x was retired by Groq


def llm_parse(text: str, history: list, today: date) -> list:
    key = config.get_secret("GROQ_API_KEY")
    if not key:
        raise LLMError("GROQ_API_KEY is not set")
    from groq import Groq
    client = Groq(api_key=key, timeout=25)
    primary = config.get_secret("GROQ_MODEL")
    fallback = config.get_secret("GROQ_FALLBACK_MODEL")
    models = [m for m in (primary, fallback) if m] or DEFAULT_MODELS
    convo = "\n".join(f"{m['role'].title()}: {m['content'][:200]}" for m in history[-4:])
    user = (f"Recent conversation:\n{convo}\n\n" if convo else "") + f"New message: {text}"
    errors = []
    for model in models:
        extra = {"reasoning_effort": "low"} if "gpt-oss" in model else {}  # only these accept it
        try:
            r = client.chat.completions.create(
                model=model, temperature=0, max_completion_tokens=1500,
                response_format={"type": "json_object"},
                messages=[{"role": "system", "content": _prompt(today)},
                          {"role": "user", "content": user}],
                **extra,
            )
            data = json.loads(r.choices[0].message.content)
            acts = data.get("actions")
            if isinstance(acts, list):
                return acts
            errors.append(f"{model}: no actions in reply")
        except Exception as e:  # retired model, rate limit, bad JSON, network...
            errors.append(f"{model}: {e}")
    first = errors[0] if errors else "unknown error"
    low = first.lower()
    if "404" in low or "model_not_found" in low or "decommission" in low or "does not exist" in low:
        raise LLMError("the AI model is no longer available on Groq. Set GROQ_MODEL in your "
                       "secrets to a current model (see README, Troubleshooting).")
    raise LLMError(first[:150])


# ---------- validation of model output ----------
def _cat(v, fallback_text=""):
    c = str(v or "").strip().lower()
    return c if c in CATEGORIES else (guess_category(f"{c} {fallback_text}") or "other")


def _name(v):
    n = re.sub(r"\s+", " ", str(v or "")).strip()
    return n if n and n.lower() not in ("me", "i", "user", "myself", "someone", "null") else None


def validate(raw: list, today: date):
    """Return (clean_actions, problems)."""
    out, problems = [], []
    for a in raw:
        if not isinstance(a, dict):
            continue
        t = a.get("type")
        if t == "expense":
            amt = to_amount(a.get("amount"))
            if not amt:
                problems.append("I couldn't read the amount.")
                continue
            note = str(a.get("note") or "").strip()[:80]
            out.append({"type": t, "amount": amt, "category": _cat(a.get("category"), note),
                        "note": note, "date": to_date(a.get("date"), today)})
        elif t == "split":
            total = to_amount(a.get("total"))
            people = []
            for p in a.get("people") or []:
                n = _name(p)
                if n and n.lower() not in [x.lower() for x in people]:
                    people.append(n)
            payer = _name(a.get("paid_by"))  # None means "me"
            if payer and payer.lower() not in [x.lower() for x in people]:
                people.append(payer)
            if not total or not people:
                problems.append("For a split I need the total and who it was split with.")
                continue
            note = str(a.get("note") or "").strip()[:80]
            out.append({"type": t, "total": total, "paid_by": payer, "people": people,
                        "category": _cat(a.get("category"), note), "note": note,
                        "date": to_date(a.get("date"), today)})
        elif t == "debt":
            amt, person = to_amount(a.get("amount")), _name(a.get("person"))
            if not amt or not person or a.get("direction") not in ("they_owe_me", "i_owe_them"):
                problems.append("I couldn't work out that debt.")
                continue
            out.append({"type": t, "person": person, "direction": a["direction"], "amount": amt,
                        "note": str(a.get("note") or "").strip()[:80]})
        elif t == "settle":
            person = _name(a.get("person"))
            amt = to_amount(a.get("amount")) if a.get("amount") not in (None, "", "null") else None
            if not person:
                problems.append("Who was the repayment with?")
                continue
            d = a.get("direction") if a.get("direction") in ("they_paid_me", "i_paid_them") else None
            out.append({"type": t, "person": person, "direction": d, "amount": amt})
        elif t == "set_budget":
            cat, amt = str(a.get("category", "")).lower(), to_amount(a.get("amount"))
            if cat not in BUDGET_KEYS or not amt:
                problems.append("I couldn't read that budget.")
                continue
            out.append({"type": t, "category": cat, "amount": amt})
        elif t == "query" and a.get("what") in ("spend", "owed", "budget"):
            cat = str(a.get("category") or "").lower()
            period = a.get("period") if a.get("period") in (
                "today", "week", "month", "last_month", "year", "all") else "month"
            out.append({"type": t, "what": a["what"],
                        "category": cat if cat in CATEGORIES else None, "period": period})
        elif t == "undo":
            out.append({"type": t})
        elif t == "reply" and a.get("text"):
            out.append({"type": t, "text": str(a["text"])[:400]})
    return out, problems


def interpret(text: str, history: list, today: date):
    """Main entry. Returns (actions, message_if_nothing_to_do)."""
    cmd = parse_command(text)
    if cmd:
        return cmd, None
    fast = fast_parse(text, today)
    if fast and fast[1]:
        return fast[0], None
    try:
        raw = llm_parse(text, history, today)
        actions, problems = validate(raw, today)
        if actions:
            return actions, None
        return [], (problems[0] if problems else "I didn't catch that. Try “200 food”.")
    except LLMError as e:
        if fast:  # LLM down: still log the simple expense under "other"
            return fast[0], None
        return [], (f"I couldn't use the AI parser: {e} "
                    "Simple entries like “200 food” still work.")