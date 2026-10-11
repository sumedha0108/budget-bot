import hmac
import time
from datetime import timedelta

import pandas as pd
import plotly.express as px
import psycopg
import streamlit as st

import config
import db
import engine
import nlp
from config import BUDGET_KEYS, CATEGORIES, inr, today

st.set_page_config(page_title="Money", page_icon="💸", layout="centered",
                   initial_sidebar_state="collapsed")
st.markdown("<style>.block-container{padding-top:1.2rem;padding-bottom:6rem;}</style>",
            unsafe_allow_html=True)


#login
def require_login():
    pw = config.get_secret("APP_PASSWORD")
    if not pw:
        st.error("APP_PASSWORD is not set. Add it to your Streamlit secrets (see README).")
        st.stop()
    if st.session_state.get("authed"):
        return
    ok = lambda s: hmac.compare_digest(str(s).encode(), str(pw).encode())
    key = st.query_params.get("k")          # open the app once with ?k=YOUR_PASSWORD
    if key and ok(key):                      # to skip the prompt on your phone
        st.session_state.authed = True
        return
    st.title("💸 Money")
    entered = st.text_input("Password", type="password")
    if entered:
        if ok(entered):
            st.session_state.authed = True
            st.rerun()
        time.sleep(1)
        st.error("Wrong password.")
    st.stop()


require_login()


@st.cache_resource
def _init_db():
    db.init_schema()
    return True


try:
    _init_db()
except Exception as e:
    st.error(f"Can't reach the database yet ({type(e).__name__}). Refresh in a few seconds.")
    st.stop()


def flash_here():
    if msg := st.session_state.pop("flash", None):
        st.success(msg)


#cached reads
@st.cache_data(ttl=60, show_spinner=False)
def header_totals(day):
    with db.connect() as conn:
        ms, me = db.month_bounds(day)
        return (float(db.spend_total(conn, day, day)), float(db.spend_total(conn, ms, me)))


@st.cache_data(ttl=120, show_spinner=False)
def months_available():
    with db.connect() as conn:
        return db.month_list(conn)


@st.cache_data(ttl=120, show_spinner=False)
def load_dashboard(month_start):
    ms, me = db.month_bounds(month_start)
    ps, pe = db.month_bounds(ms - timedelta(days=1))
    with db.connect() as conn:
        return {
            "total": float(db.spend_total(conn, ms, me)),
            "prev": float(db.spend_total(conn, ps, pe)),
            "cats": db.category_totals(conn, ms, me),
            "daily": db.daily_totals(conn, ms, me),
            "monthly": db.monthly_totals(conn),
            "top": db.top_expenses(conn, ms, me),
            "notes": db.top_notes(conn, ms, me),
            "budgets": {k: float(v) for k, v in db.get_budgets(conn).items()},
        }


#chat page
WELCOME = ("Hi! Tell me what you spent, like `200 food` or `coffee 150 yesterday`. "
           "You can also say things like *paid 3000 for dinner with Rahul and Priya*. "
           "Type `help` to see everything I understand.")


def _undo_cb():
    reply = engine.execute([{"type": "undo"}])
    st.cache_data.clear()
    st.session_state.messages += [{"role": "user", "content": "undo"},
                                  {"role": "assistant", "content": reply}]


def handle(prompt: str) -> str:
    history = [m for m in st.session_state.messages if not m.get("welcome")]
    actions, msg = nlp.interpret(prompt, history, today())
    if msg:
        return msg
    try:
        reply = engine.execute(actions)
    except psycopg.Error as e:
        return f"Couldn't save that, nothing was recorded ({type(e).__name__}). Please try again."
    st.cache_data.clear()
    return reply


def page_chat():
    if "messages" not in st.session_state:
        st.session_state.messages = [{"role": "assistant", "content": WELCOME, "welcome": True}]
    try:
        day, month = header_totals(today())
        caption = f"Today **{inr(day)}** · This month **{inr(month)}**"
    except psycopg.Error:
        caption = "Totals unavailable (database is slow to respond)"
    c1, c2 = st.columns([3, 1], vertical_alignment="center")
    c1.caption(caption)
    c2.button("↩ Undo", on_click=_undo_cb, use_container_width=True)

    for m in st.session_state.messages:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])

    if prompt := st.chat_input("200 food · uber 340 · split 3000 dinner with Rahul"):
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            with st.spinner("Thinking…"):
                reply = handle(prompt)
        st.session_state.messages += [{"role": "user", "content": prompt},
                                      {"role": "assistant", "content": reply}]
        st.rerun()


#dashboard page
def _style(fig, h=280):
    fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=h, xaxis_title=None,
                      yaxis_title=None, legend=dict(orientation="h", y=-0.15))
    fig.update_xaxes(fixedrange=True)
    fig.update_yaxes(fixedrange=True)
    return fig


def page_dashboard():
    months = months_available()
    sel = st.selectbox("Month", months, format_func=lambda m: m.strftime("%B %Y"),
                       label_visibility="collapsed")
    d = load_dashboard(sel)
    ms, me = db.month_bounds(sel)
    t = today()
    elapsed = max((min(t, me) - ms).days + 1, 1)

    c1, c2 = st.columns(2)
    delta = None
    if d["prev"] > 0:
        diff = d["total"] - d["prev"]
        delta = f"{'+' if diff >= 0 else ''}{inr(diff)} vs last month"
    c1.metric("Spent", inr(d["total"]), delta=delta, delta_color="inverse")
    c2.metric("Per day", inr(d["total"] / elapsed))

    if d["total"] == 0:
        st.info("No expenses in this month yet. Add some in the Chat tab.")
    else:
        if d["budgets"]:
            st.subheader("Budgets")
            spent_by = dict(zip(d["cats"]["category"], d["cats"]["total"]))
            for key in BUDGET_KEYS:
                if key in d["budgets"]:
                    spent = d["total"] if key == "total" else spent_by.get(key, 0.0)
                    limit = d["budgets"][key]
                    icon = "🚨" if spent >= limit else "⚠️" if spent >= limit * config.ALERT_AT else ""
                    st.progress(min(spent / limit, 1.0),
                                text=f"{icon} {key}: {inr(spent)} / {inr(limit)}".strip())

        st.subheader("By category")
        fig = px.pie(d["cats"], names="category", values="total", hole=0.55)
        fig.update_traces(textinfo="percent", hovertemplate="%{label}: ₹%{value:,.0f}<extra></extra>")
        st.plotly_chart(_style(fig, 320), width="stretch", config={"displayModeBar": False})

        st.subheader("Day by day")
        idx = pd.date_range(ms, me)
        s = pd.Series(d["daily"]["total"].values,
                      index=pd.to_datetime(d["daily"]["day"])).reindex(idx, fill_value=0)
        fig = px.bar(pd.DataFrame({"day": idx, "total": s.values}), x="day", y="total")
        fig.update_traces(hovertemplate="%{x|%d %b}: ₹%{y:,.0f}<extra></extra>")
        st.plotly_chart(_style(fig), width="stretch", config={"displayModeBar": False})

        st.subheader("Biggest expenses")
        top = d["top"].copy()
        top["date"] = pd.to_datetime(top["date"]).dt.strftime("%d %b")
        st.dataframe(top, hide_index=True, width="stretch",
                     column_config={"amount": st.column_config.NumberColumn(format="₹%.0f")})

        if not d["notes"].empty:
            st.subheader("Where it goes")
            notes = d["notes"].rename(columns={"what": "what", "total": "total", "times": "times"})
            st.dataframe(notes, hide_index=True, width="stretch",
                         column_config={"total": st.column_config.NumberColumn(format="₹%.0f")})

    if not d["monthly"].empty:
        st.subheader("Last 12 months")
        mo = d["monthly"].copy()
        mo["month"] = pd.to_datetime(mo["month"]).dt.strftime("%b %y")
        fig = px.bar(mo, x="month", y="total")
        fig.update_traces(hovertemplate="%{x}: ₹%{y:,.0f}<extra></extra>")
        st.plotly_chart(_style(fig, 240), width="stretch", config={"displayModeBar": False})

    with st.expander("Edit monthly budgets"):
        st.caption("Set 0 to remove a budget. “total” is your overall monthly limit.")
        new = {k: st.number_input(k.title(), min_value=0, step=500, key=f"bud_{k}",
                                  value=int(d["budgets"].get(k, 0))) for k in BUDGET_KEYS}
        if st.button("Save budgets"):
            with db.connect() as conn:
                for k, v in new.items():
                    db.set_budget(conn, k, v)
            st.cache_data.clear()
            st.session_state.flash = "Budgets saved."
            st.rerun()


#table page
def page_table():
    flash_here()
    t = today()
    c1, c2 = st.columns(2)
    rng = c1.date_input("Dates", value=(t.replace(day=1), t), format="DD/MM/YYYY")
    cats = c2.multiselect("Categories", CATEGORIES)
    q = st.text_input("Search notes", placeholder="e.g. uber")
    if not (isinstance(rng, tuple) and len(rng) == 2):
        st.info("Pick an end date.")
        return
    with db.connect() as conn:
        df = db.list_expenses(conn, rng[0], rng[1], cats, q.strip() or None)
    if df.empty:
        st.info("No expenses match.")
        return
    df["spent_on"] = pd.to_datetime(df["spent_on"])
    df["delete"] = False
    st.caption(f"{len(df)} entries · **{inr(df['amount'].sum())}** · edit cells, tick 🗑 to delete, then Save.")

    ver = st.session_state.get("editor_ver", 0)
    edited = st.data_editor(
        df, hide_index=True, num_rows="fixed", width="stretch", key=f"editor_{ver}",
        column_order=("spent_on", "amount", "category", "note", "delete"),
        column_config={
            "id": None,
            "spent_on": st.column_config.DateColumn("Date", format="DD MMM YY", required=True),
            "amount": st.column_config.NumberColumn("₹", min_value=0.01, format="%.2f", required=True),
            "category": st.column_config.SelectboxColumn("Category", options=CATEGORIES, required=True),
            "note": st.column_config.TextColumn("Note"),
            "delete": st.column_config.CheckboxColumn("🗑"),
        },
    )

    b1, b2 = st.columns(2)
    if b1.button("Save changes", type="primary", use_container_width=True):
        with db.connect() as conn:
            updated, deleted = db.apply_edits(conn, df, edited)
        st.cache_data.clear()
        st.session_state.editor_ver = ver + 1
        st.session_state.flash = f"Saved: {updated} updated, {deleted} deleted."
        st.rerun()

    csv = df[["spent_on", "amount", "category", "note"]].assign(
        spent_on=df["spent_on"].dt.strftime("%Y-%m-%d")).to_csv(index=False)
    b2.download_button("Download CSV", csv, "expenses.csv", "text/csv", use_container_width=True)


#owed page
def page_owed():
    flash_here()
    with db.connect() as conn:
        rows = db.balances(conn)
        ledger = db.ledger(conn)
    if not rows:
        st.info("Nobody owes anything. Split a bill in the Chat tab to get started.")
    else:
        to_me = sum(b for _, b in rows if b > 0)
        i_owe = sum(-b for _, b in rows if b < 0)
        c1, c2 = st.columns(2)
        c1.metric("Owed to you", inr(to_me))
        c2.metric("You owe", inr(i_owe))
        for person, bal in rows:
            a, b = st.columns([3, 1], vertical_alignment="center")
            a.markdown(f"**{person}** owes you **{inr(bal)}**" if bal > 0
                       else f"You owe **{person}** **{inr(-bal)}**")
            if b.button("Settle", key=f"settle_{person}", use_container_width=True):
                reply = engine.execute([{"type": "settle", "person": person,
                                         "direction": None, "amount": None}])
                st.cache_data.clear()
                st.session_state.flash = reply.replace("✅ ", "")
                st.rerun()
    if not ledger.empty:
        with st.expander("History"):
            st.dataframe(ledger, hide_index=True, width="stretch")


#router
PAGES = {"💬 Chat": page_chat, "📊 Dashboard": page_dashboard,
         "🧾 Table": page_table, "🤝 Owed": page_owed}
choice = st.radio("Page", list(PAGES), horizontal=True, label_visibility="collapsed", key="nav")
try:
    PAGES[choice]()
except psycopg.Error as e:
    st.error("The database didn't respond (it may be waking up). Wait a few seconds and "
             f"refresh. ({type(e).__name__})")
