"""All SQL lives here. Functions take an open connection so callers can group
several operations into one transaction."""
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pandas as pd
import psycopg

import config

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS expenses (
        id         BIGSERIAL PRIMARY KEY,
        spent_on   DATE NOT NULL,
        amount     NUMERIC(12,2) NOT NULL CHECK (amount > 0),
        category   TEXT NOT NULL DEFAULT 'other',
        note       TEXT NOT NULL DEFAULT '',
        source     TEXT NOT NULL DEFAULT 'chat',
        batch_id   TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX IF NOT EXISTS expenses_spent_on_idx ON expenses (spent_on)",
    # Ledger: amount > 0 means THEY owe YOU, amount < 0 means YOU owe THEM.
    """CREATE TABLE IF NOT EXISTS debts (
        id         BIGSERIAL PRIMARY KEY,
        person     TEXT NOT NULL,
        amount     NUMERIC(12,2) NOT NULL,
        kind       TEXT NOT NULL,
        note       TEXT NOT NULL DEFAULT '',
        batch_id   TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX IF NOT EXISTS debts_person_idx ON debts (person)",
    """CREATE TABLE IF NOT EXISTS budgets (
        category      TEXT PRIMARY KEY,
        monthly_limit NUMERIC(12,2) NOT NULL CHECK (monthly_limit > 0)
    )""",
]


def connect():
    # prepare_threshold=None keeps this safe behind Neon's pgbouncer pooler.
    return psycopg.connect(
        config.get_secret("DATABASE_URL"),
        prepare_threshold=None,
        connect_timeout=30,
    )


def init_schema():
    with connect() as conn:
        for stmt in SCHEMA:
            conn.execute(stmt)


def new_batch() -> str:
    return uuid.uuid4().hex


def _df(cur) -> pd.DataFrame:
    return pd.DataFrame(cur.fetchall(), columns=[c.name for c in cur.description])


# ---------- date helpers ----------
def month_bounds(d: date):
    start = d.replace(day=1)
    nxt = (start + timedelta(days=32)).replace(day=1)
    return start, nxt - timedelta(days=1)


def period_bounds(period: str, today: date):
    if period == "today":
        return today, today
    if period == "week":
        return today - timedelta(days=6), today
    if period == "month":
        return month_bounds(today)
    if period == "last_month":
        return month_bounds(month_bounds(today)[0] - timedelta(days=1))
    if period == "year":
        return today.replace(month=1, day=1), today
    return date(2000, 1, 1), today  # "all"


#expenses
def add_expense(conn, spent_on, amount, category, note, batch, source="chat"):
    conn.execute(
        "INSERT INTO expenses (spent_on, amount, category, note, source, batch_id) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (spent_on, amount, category, note, source, batch),
    )


def spend_total(conn, start, end, category=None) -> Decimal:
    sql = "SELECT COALESCE(SUM(amount),0) FROM expenses WHERE spent_on BETWEEN %s AND %s"
    params = [start, end]
    if category:
        sql += " AND category = %s"
        params.append(category)
    return conn.execute(sql, params).fetchone()[0]


def category_totals(conn, start, end) -> pd.DataFrame:
    cur = conn.execute(
        "SELECT category, SUM(amount)::float AS total FROM expenses "
        "WHERE spent_on BETWEEN %s AND %s GROUP BY category ORDER BY total DESC",
        (start, end),
    )
    return _df(cur)


def daily_totals(conn, start, end) -> pd.DataFrame:
    cur = conn.execute(
        "SELECT spent_on AS day, SUM(amount)::float AS total FROM expenses "
        "WHERE spent_on BETWEEN %s AND %s GROUP BY spent_on ORDER BY spent_on",
        (start, end),
    )
    return _df(cur)


def monthly_totals(conn, months: int = 12) -> pd.DataFrame:
    first = config.today().replace(day=1)
    for _ in range(months - 1):
        first = (first - timedelta(days=1)).replace(day=1)
    cur = conn.execute(
        "SELECT date_trunc('month', spent_on)::date AS month, SUM(amount)::float AS total "
        "FROM expenses WHERE spent_on >= %s GROUP BY 1 ORDER BY 1",
        (first,),
    )
    return _df(cur)


def top_expenses(conn, start, end, n=5) -> pd.DataFrame:
    cur = conn.execute(
        "SELECT spent_on AS date, amount::float AS amount, category, note FROM expenses "
        "WHERE spent_on BETWEEN %s AND %s ORDER BY amount DESC LIMIT %s",
        (start, end, n),
    )
    return _df(cur)


def top_notes(conn, start, end, n=8) -> pd.DataFrame:
    cur = conn.execute(
        "SELECT lower(note) AS what, SUM(amount)::float AS total, COUNT(*) AS times "
        "FROM expenses WHERE spent_on BETWEEN %s AND %s AND note <> '' "
        "GROUP BY lower(note) ORDER BY total DESC LIMIT %s",
        (start, end, n),
    )
    return _df(cur)


def list_expenses(conn, start, end, categories=None, search=None, limit=1000) -> pd.DataFrame:
    sql = ("SELECT id, spent_on, amount::float AS amount, category, note FROM expenses "
           "WHERE spent_on BETWEEN %s AND %s")
    params = [start, end]
    if categories:
        sql += " AND category = ANY(%s)"
        params.append(list(categories))
    if search:
        sql += " AND note ILIKE %s"
        params.append(f"%{search}%")
    sql += " ORDER BY spent_on DESC, id DESC LIMIT %s"
    params.append(limit)
    return _df(conn.execute(sql, params))


def month_list(conn) -> list:
    rows = conn.execute(
        "SELECT DISTINCT date_trunc('month', spent_on)::date FROM expenses ORDER BY 1 DESC"
    ).fetchall()
    months = [r[0] for r in rows]
    cur_month = config.today().replace(day=1)
    if cur_month not in months:
        months.insert(0, cur_month)
    return months


def update_expense(conn, id_, spent_on, amount, category, note):
    conn.execute(
        "UPDATE expenses SET spent_on=%s, amount=%s, category=%s, note=%s WHERE id=%s",
        (spent_on, amount, category, note, id_),
    )


def delete_expenses(conn, ids):
    if ids:
        conn.execute("DELETE FROM expenses WHERE id = ANY(%s)", (list(ids),))


# budgets 
def get_budgets(conn) -> dict:
    return {c: v for c, v in conn.execute("SELECT category, monthly_limit FROM budgets")}


def set_budget(conn, category, limit):
    if limit and limit > 0:
        conn.execute(
            "INSERT INTO budgets (category, monthly_limit) VALUES (%s,%s) "
            "ON CONFLICT (category) DO UPDATE SET monthly_limit = EXCLUDED.monthly_limit",
            (category, limit),
        )
    else:
        conn.execute("DELETE FROM budgets WHERE category = %s", (category,))


#debts
def canonical_person(conn, name: str) -> str:
    """Reuse the spelling already stored for this person (case-insensitive)."""
    row = conn.execute(
        "SELECT person FROM debts WHERE lower(person) = lower(%s) LIMIT 1", (name,)
    ).fetchone()
    if row:
        return row[0]
    return " ".join(w[:1].upper() + w[1:].lower() for w in name.split())


def add_debt(conn, person, amount, kind, note, batch):
    conn.execute(
        "INSERT INTO debts (person, amount, kind, note, batch_id) VALUES (%s,%s,%s,%s,%s)",
        (person, amount, kind, note, batch),
    )


def balance(conn, person) -> Decimal:
    return conn.execute(
        "SELECT COALESCE(SUM(amount),0) FROM debts WHERE person = %s", (person,)
    ).fetchone()[0]


def balances(conn) -> list:
    return conn.execute(
        "SELECT person, SUM(amount) AS bal FROM debts GROUP BY person "
        "HAVING SUM(amount) <> 0 ORDER BY ABS(SUM(amount)) DESC"
    ).fetchall()


def ledger(conn, limit=100) -> pd.DataFrame:
    cur = conn.execute(
        "SELECT (created_at AT TIME ZONE 'Asia/Kolkata')::date AS date, person, "
        "amount::float AS amount, kind, note FROM debts ORDER BY id DESC LIMIT %s",
        (limit,),
    )
    return _df(cur)


#undo
def undo_last(conn):
    """Delete everything written by the most recent chat message. Returns counts or None."""
    row = conn.execute(
        "SELECT batch_id FROM ("
        "  SELECT batch_id, created_at FROM expenses WHERE batch_id IS NOT NULL"
        "  UNION ALL"
        "  SELECT batch_id, created_at FROM debts WHERE batch_id IS NOT NULL"
        ") t ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    b = row[0]
    e = conn.execute("DELETE FROM expenses WHERE batch_id = %s", (b,)).rowcount
    d = conn.execute("DELETE FROM debts WHERE batch_id = %s", (b,)).rowcount
    return e, d


def apply_edits(conn, original: pd.DataFrame, edited: pd.DataFrame):
    """Diff the table editor's result against what was loaded. Returns (updated, deleted)."""
    orig = original.set_index("id")
    gone = edited.loc[edited["delete"], "id"].astype(int).tolist()
    delete_expenses(conn, gone)
    updated = 0
    for _, r in edited[~edited["delete"]].iterrows():
        if pd.isna(r["spent_on"]) or pd.isna(r["amount"]) or r["amount"] <= 0:
            continue
        o = orig.loc[int(r["id"])]
        new = (pd.Timestamp(r["spent_on"]).date(), round(float(r["amount"]), 2),
               r["category"], "" if pd.isna(r["note"]) else str(r["note"]).strip())
        old = (pd.Timestamp(o["spent_on"]).date(), round(float(o["amount"]), 2),
               o["category"], o["note"])
        if new != old:
            update_expense(conn, int(r["id"]), *new)
            updated += 1
    return updated, len(gone)
