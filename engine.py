"""Executes validated actions. Everything from one message runs in ONE transaction,
so a failure halfway leaves nothing behind."""
from decimal import Decimal

import config
import db
from config import inr, today

HELP = """**Quick entries**
`200 food` · `coffee 150 yesterday` · `uber 340`

**Natural language** (uses the AI parser)
`paid 3000 for dinner with Rahul and Priya`
`Rahul gave me back 500` · `lent Priya 2000`
`haircut 300 and auto 80`

**Commands**
`today` · `week` · `month` · `owed` · `budget` · `budget food 5000` · `undo`

You can also ask things like *how much did I spend on food last month?*"""


def _when(d):
    t = today()
    if d == t:
        return ""
    if (t - d).days == 1:
        return " (yesterday)"
    return f" ({d.strftime('%d %b')})"


def _budget_warnings(conn, cats):
    t = today()
    start, end = db.month_bounds(t)
    budgets = db.get_budgets(conn)
    if not budgets:
        return []
    out = []
    checks = [(c, db.spend_total(conn, start, end, c), budgets[c]) for c in cats if c in budgets]
    if "total" in budgets:
        checks.append(("total", db.spend_total(conn, start, end), budgets["total"]))
    for cat, spent, limit in checks:
        label = "overall" if cat == "total" else cat
        if spent >= limit:
            out.append(f"🚨 Over your {label} budget: {inr(spent)} of {inr(limit)} this month.")
        elif spent >= limit * Decimal(str(config.ALERT_AT)):
            out.append(f"⚠️ {label.title()} budget {int(spent / limit * 100)}% used "
                       f"({inr(spent)} of {inr(limit)}).")
    return out


def _spend_report(conn, a):
    start, end = db.period_bounds(a["period"], today())
    names = {"today": "today", "week": "in the last 7 days", "month": "this month",
             "last_month": "last month", "year": "this year", "all": "in total"}
    when = names[a["period"]]
    if a["category"]:
        total = db.spend_total(conn, start, end, a["category"])
        return f"You spent **{inr(total)}** on {a['category']} {when}."
    total = db.spend_total(conn, start, end)
    if not total:
        return f"No spending recorded {when}."
    lines = [f"You spent **{inr(total)}** {when}."]
    for _, r in db.category_totals(conn, start, end).head(6).iterrows():
        lines.append(f"- {r['category']}: {inr(r['total'])}")
    if a["period"] == "today":
        lines.append("")
        for _, r in db.list_expenses(conn, start, end).head(15).iterrows():
            lines.append(f"- {r['note'] or r['category']} · {inr(r['amount'])} · {r['category']}")
    return "\n".join(lines)


def _owed_report(conn):
    rows = db.balances(conn)
    if not rows:
        return "Nobody owes anything. All settled. 🎉"
    lines, owed_to_me, i_owe = [], Decimal(0), Decimal(0)
    for person, bal in rows:
        if bal > 0:
            owed_to_me += bal
            lines.append(f"- {person} owes you **{inr(bal)}**")
        else:
            i_owe += -bal
            lines.append(f"- You owe {person} **{inr(-bal)}**")
    head = []
    if owed_to_me:
        head.append(f"Owed to you: **{inr(owed_to_me)}**")
    if i_owe:
        head.append(f"You owe: **{inr(i_owe)}**")
    return " · ".join(head) + "\n\n" + "\n".join(lines)


def _budget_report(conn):
    budgets = db.get_budgets(conn)
    if not budgets:
        return "No budgets set. Try `budget food 5000` or `budget total 40000`."
    start, end = db.month_bounds(today())
    lines = ["**Budgets this month**"]
    for cat in config.BUDGET_KEYS:
        if cat in budgets:
            spent = db.spend_total(conn, start, end, None if cat == "total" else cat)
            pct = int(spent / budgets[cat] * 100)
            lines.append(f"- {cat}: {inr(spent)} / {inr(budgets[cat])} ({pct}%)")
    return "\n".join(lines)


def execute(actions: list) -> str:
    batch = db.new_batch()
    lines, touched = [], set()
    t = today()
    with db.connect() as conn:
        for a in actions:
            kind = a["type"]

            if kind == "expense":
                db.add_expense(conn, a["date"], a["amount"], a["category"], a["note"], batch)
                label = f" · {a['note']}" if a["note"] and a["note"] != a["category"] else ""
                lines.append(f"✅ Logged **{inr(a['amount'])}** · {a['category']}{label}{_when(a['date'])}")
                if a["date"].replace(day=1) == t.replace(day=1):
                    touched.add(a["category"])

            elif kind == "split":
                n = len(a["people"])
                share = (a["total"] / (n + 1)).quantize(Decimal("0.01"))
                mine = a["total"] - share * n      # I absorb any rounding paisa
                note = a["note"] or "split"
                people = [db.canonical_person(conn, p) for p in a["people"]]
                payer = db.canonical_person(conn, a["paid_by"]) if a["paid_by"] else None
                if payer is None:
                    for p in people:
                        db.add_debt(conn, p, share, "split", note, batch)
                    owes = ", ".join(people)
                    lines.append(f"✅ Split **{inr(a['total'])}** ({note}) {n + 1} ways, "
                                 f"{inr(share)} each. {owes} owe{'s' if n == 1 else ''} you.")
                else:
                    db.add_debt(conn, payer, -share, "split", note, batch)
                    lines.append(f"✅ Split **{inr(a['total'])}** ({note}) {n + 1} ways. "
                                 f"You owe {payer} {inr(share)}.")
                db.add_expense(conn, a["date"], mine, a["category"], note, batch, "split")
                lines.append(f"Your share of {inr(mine)} was logged under {a['category']}.")
                if a["date"].replace(day=1) == t.replace(day=1):
                    touched.add(a["category"])

            elif kind == "debt":
                person = db.canonical_person(conn, a["person"])
                signed = a["amount"] if a["direction"] == "they_owe_me" else -a["amount"]
                db.add_debt(conn, person, signed,
                            "lent" if signed > 0 else "borrowed", a["note"], batch)
                lines.append(f"✅ {person} owes you {inr(a['amount'])}." if signed > 0
                             else f"✅ You owe {person} {inr(a['amount'])}.")

            elif kind == "settle":
                person = db.canonical_person(conn, a["person"])
                bal = db.balance(conn, person)
                if bal == 0 and a["amount"] is None:
                    lines.append(f"Nothing outstanding with {person}.")
                    continue
                if a["amount"] is None:
                    amt, signed = abs(bal), (-bal)      # cancel whatever is outstanding
                else:
                    amt = a["amount"]
                    d = a["direction"] or ("they_paid_me" if bal > 0 else "i_paid_them")
                    signed = -amt if d == "they_paid_me" else amt
                db.add_debt(conn, person, signed, "settlement", "", batch)
                new = db.balance(conn, person)
                who = f"{person} paid you" if signed < 0 else f"You paid {person}"
                if new == 0:
                    lines.append(f"✅ {who} {inr(amt)}. All settled with {person}.")
                elif new > 0:
                    lines.append(f"✅ {who} {inr(amt)}. {person} still owes you {inr(new)}.")
                else:
                    lines.append(f"✅ {who} {inr(amt)}. You now owe {person} {inr(-new)}.")

            elif kind == "set_budget":
                db.set_budget(conn, a["category"], a["amount"])
                lines.append(f"✅ {a['category'].title()} budget set to {inr(a['amount'])}/month.")

            elif kind == "query":
                lines.append({"spend": lambda: _spend_report(conn, a),
                              "owed": lambda: _owed_report(conn),
                              "budget": lambda: _budget_report(conn)}[a["what"]]())

            elif kind == "undo":
                res = db.undo_last(conn)
                if not res:
                    lines.append("Nothing to undo.")
                else:
                    e, d = res
                    parts = [f"{e} expense{'s' if e != 1 else ''}" if e else "",
                             f"{d} debt entr{'ies' if d != 1 else 'y'}" if d else ""]
                    lines.append("↩️ Undone: " + " and ".join(p for p in parts if p) + ".")

            elif kind == "help":
                lines.append(HELP)

            elif kind == "reply":
                lines.append(a["text"])

        if touched:
            lines.extend(_budget_warnings(conn, touched))
    return "\n\n".join(lines)
