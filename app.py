from __future__ import annotations

import calendar
from datetime import date
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from services import (
    CURRENCIES, EXPENSE, EXPENSE_CATEGORIES, INCOME, INCOME_CATEGORIES,
    NOTE_MAX_LENGTH, NOTE_SYSTEM, NOTE_USER,
    ExpenseTrackError, Services, categories_for, csv_export, csv_preview, import_rows,
    month_label,
)

st.set_page_config(page_title="ExpenseTrack", layout="wide")


def flash(message: str) -> None:
    st.session_state.setdefault("_flash", []).append(message)


def show_flashes() -> None:
    for message in st.session_state.pop("_flash", []):
        st.toast(message)


def attempt(fn, success: str | None = None) -> bool:
    try:
        fn()
    except ExpenseTrackError as e:
        st.error(str(e))
        return False
    if success:
        flash(success)
    return True


def fmt_dt(dt) -> str:
    return dt.strftime("%d %b %Y, %I:%M %p")


def donut(rows):
    df = pd.DataFrame([(c, float(a)) for c, a in rows], columns=["Category", "Amount"])
    return (alt.Chart(df).mark_arc(innerRadius=55)
            .encode(theta="Amount:Q", color=alt.Color("Category:N", legend=alt.Legend(orient="right")),
                    tooltip=["Category", alt.Tooltip("Amount:Q", format=",.2f")]).properties(height=280))


def render_dashboard(svc, year: int, month: int):
    st.header("Dashboard")
    s = svc.dashboard.summary(year, month)
    fmt = svc.fmt

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Balance", fmt(s.balance))
    c2.metric("Total income", fmt(s.total_income))
    c3.metric("Total expenses", fmt(s.total_expenses))
    c4.metric(f"Spent in {month_label(year, month)}", fmt(s.monthly_spending),
              delta=f"Income {fmt(s.monthly_income)}", delta_color="off")

    with st.container(border=True):
        st.subheader(f"Monthly budget · {month_label(year, month)}")
        b = s.budget
        if not b.has_budget:
            st.info("No budget set for this month. Set one on the **Budgets** page.")
        else:
            st.progress(min(float(b.utilization) / 100, 1.0),
                        text=f"{fmt(b.spent)} of {fmt(b.limit)} ({b.utilization}%)")
            if b.exceeded:
                st.error(f"Budget exceeded by {fmt(-b.remaining)}.")
            elif b.warning:
                st.warning(f"{b.utilization}% of the budget used — {fmt(b.remaining)} left.")
            else:
                st.caption(f"{fmt(b.remaining)} remaining.")

    left, right = st.columns(2)
    with left:
        with st.container(border=True):
            st.subheader("Expense distribution")
            if s.expense_by_category:
                st.altair_chart(donut(s.expense_by_category), width="stretch")
            else:
                st.caption("No expenses recorded this month.")
    with right:
        with st.container(border=True):
            st.subheader("Recent transactions")
            if s.recent:
                df = pd.DataFrame([{
                    "Date": t.date, "Title": t.title,
                    "Amount": ("+" if t.type == "INCOME" else "−") + fmt(t.amount),
                    "Notes": t.user_note_count} for t in s.recent])
                st.dataframe(df, hide_index=True, width="stretch")
                st.caption("Notes = number of notes you attached to the transaction.")
            else:
                st.caption("Nothing yet — add your first transaction.")

    with st.container(border=True):
        st.subheader("Latest notes")
        latest = svc.notes.recent(5, kind="USER")
        if not latest:
            st.caption("Notes you attach to transactions will show up here.")
        for n in latest:
            st.markdown(f"**{n.transaction_title}** — {n.text}")
            st.caption(n.created_at.strftime("%d %b %Y, %I:%M %p"))

    with st.container(border=True):
        f = svc.recurring.forecast(date.today())
        st.subheader("Recurring forecast · next 30 days")
        a, b_, c = st.columns(3)
        a.metric("Expected income", fmt(f.income))
        b_.metric("Expected expenses", fmt(f.expense))
        c.metric("Net", fmt(f.net))
        if f.overdue_count:
            st.warning(f"{f.overdue_count} past-due occurrence(s) are excluded. "
                       "Process them on Transactions → Recurring.")
        if f.upcoming:
            st.dataframe(pd.DataFrame([{"Date": d, "Title": r.title, "Type": r.type, "Amount": fmt(r.amount)}
                                       for d, r in f.upcoming[:10]]), hide_index=True, width="stretch")
            if len(f.upcoming) > 10:
                st.caption(f"Showing 10 of {len(f.upcoming)} scheduled occurrences.")
        else:
            st.caption("No recurring items scheduled in the next 30 days.")


def pct_change(cur, prev):
    return "—" if not prev else f"{(cur - prev) / prev * 100:+.1f}%"


def render_analytics(svc, year: int, month: int):
    st.header("Analytics")
    a = svc.dashboard.analytics(year, month)
    order = [m.label for m in a.monthly]
    df = pd.DataFrame([{"Month": m.label, "Income": float(m.income), "Expenses": float(m.expense)}
                       for m in a.monthly])

    c1, c2 = st.columns(2)
    with c1:
        with st.container(border=True):
            st.subheader("Income vs expenses · last 6 months")
            long = df.melt("Month", var_name="Type", value_name="Amount")
            st.altair_chart(alt.Chart(long).mark_bar().encode(
                x=alt.X("Month:N", sort=order, title=None), xOffset="Type:N", y=alt.Y("Amount:Q", title=None),
                color="Type:N", tooltip=["Month", "Type", alt.Tooltip("Amount:Q", format=",.2f")]),
                width="stretch")
    with c2:
        with st.container(border=True):
            st.subheader(f"Category-wise expenses · {month_label(year, month)}")
            if a.expense_by_category:
                st.altair_chart(donut(a.expense_by_category), width="stretch")
            else:
                st.caption("No expenses this month.")

    with st.container(border=True):
        st.subheader("Monthly spending trend")
        st.altair_chart(alt.Chart(df).mark_line(point=True).encode(
            x=alt.X("Month:N", sort=order, title=None), y=alt.Y("Expenses:Q", title=None),
            tooltip=["Month", alt.Tooltip("Expenses:Q", format=",.2f")]), width="stretch")

    with st.container(border=True):
        st.subheader("Month-to-month comparison")
        cur, prev = a.monthly[-1], a.monthly[-2]
        rows = [("Income", prev.income, cur.income), ("Expenses", prev.expense, cur.expense),
                ("Net", prev.income - prev.expense, cur.income - cur.expense)]
        st.dataframe(pd.DataFrame([{
            "": name, prev.label: svc.fmt(p), cur.label: svc.fmt(c),
            "Change": svc.fmt(c - p), "Change %": pct_change(c, p)} for name, p, c in rows]),
            hide_index=True, width="stretch")


def _bar(svc, spent, limit, util, exceeded, warning):
    st.progress(min(float(util) / 100, 1.0), text=f"{svc.fmt(spent)} of {svc.fmt(limit)} ({util}%)")
    if exceeded:
        st.error(f"Over by {svc.fmt(spent - limit)}")
    elif warning:
        st.warning("Close to the limit")


def render_budgets(svc, year: int, month: int):
    st.header("Budgets")
    label = month_label(year, month)
    left, right = st.columns(2)

    with left:
        with st.container(border=True):
            st.subheader("Monthly budget")
            with st.form("budget_form"):
                limit = st.number_input(f"Limit for {label}", min_value=0.0, step=500.0, format="%.2f")
                if st.form_submit_button("Save budget", type="primary"):
                    if attempt(lambda: svc.budgets.set_budget(f"{limit:.2f}", month, year), "Budget saved"):
                        st.rerun()
            b = svc.budgets.get_status(month, year)
            st.markdown(f"**Status · {label}**")
            if b.has_budget:
                _bar(svc, b.spent, b.limit, b.utilization, b.exceeded, b.warning)
                st.caption(f"Remaining: {svc.fmt(b.remaining)}")
            else:
                st.info("No budget set for this month.")

    with right:
        with st.container(border=True):
            st.subheader("Category budgets")
            with st.form("cat_budget_form"):
                cat = st.selectbox("Expense category", EXPENSE_CATEGORIES)
                limit = st.number_input(f"Limit for {label}", min_value=0.0, step=100.0, format="%.2f",
                                        key="cb_limit")
                if st.form_submit_button("Save category budget", type="primary"):
                    if attempt(lambda: svc.category_budgets.save(cat, f"{limit:.2f}", month, year),
                               "Category budget saved"):
                        st.rerun()
            st.markdown(f"**Status · {label}**")
            items = svc.category_budgets.list(month, year)
            if not items:
                st.info("No category budgets for this month.")
            for c in items:
                st.write(f"**{c.category}**")
                _bar(svc, c.spent, c.limit, c.utilization, c.exceeded, c.warning)


def render_goals(svc, year: int, month: int):
    st.header("Savings goals")
    with st.container(border=True):
        st.subheader("Create a savings goal")
        with st.form("goal_form", clear_on_submit=True):
            c1, c2, c3 = st.columns([2, 1, 1])
            title = c1.text_input("Goal name", max_chars=100)
            target = c2.number_input("Target amount", min_value=0.0, step=500.0, format="%.2f")
            target_date = c3.date_input("Target date (optional)", value=None)
            if st.form_submit_button("Create goal", type="primary"):
                if attempt(lambda: svc.goals.create(title, f"{target:.2f}", target_date), "Goal created"):
                    st.rerun()

    goals = svc.goals.list()
    if not goals:
        st.info("No savings goals yet.")
    for g in goals:
        with st.container(border=True):
            h1, h2 = st.columns([3, 2])
            h1.subheader(g.title)
            h1.caption(f"Target date: {g.target_date:%d %b %Y}" if g.target_date else "No target date")
            h2.metric("Saved", svc.fmt(g.saved), delta=f"of {svc.fmt(g.target)}", delta_color="off")
            st.progress(g.progress, text=f"{g.progress * 100:.0f}% · {svc.fmt(g.remaining)} to go")
            with st.form(f"contrib_{g.id}", clear_on_submit=True):
                c1, c2 = st.columns([3, 1])
                amt = c1.number_input("Add contribution", min_value=0.0, step=100.0, format="%.2f",
                                      key=f"contrib_amt_{g.id}")
                c2.write("")
                if c2.form_submit_button("Add"):
                    if attempt(lambda: svc.goals.contribute(g.id, f"{amt:.2f}"), "Contribution added"):
                        st.rerun()
            with st.expander("Edit / delete goal"):
                with st.form(f"goal_edit_{g.id}"):
                    t = st.text_input("Goal name", value=g.title, max_chars=100)
                    tg = st.number_input("Target amount", min_value=0.0, value=float(g.target), step=500.0,
                                         format="%.2f")
                    td = st.date_input("Target date", value=g.target_date)
                    if st.form_submit_button("Save changes"):
                        if attempt(lambda: svc.goals.update(g.id, t, f"{tg:.2f}", td), "Goal updated"):
                            st.rerun()
                if st.button("Delete goal", key=f"goal_del_{g.id}"):
                    if attempt(lambda: svc.goals.delete(g.id), "Goal deleted"):
                        st.rerun()


KINDS = {"All": None, "My notes": NOTE_USER, "Automatic history": NOTE_SYSTEM}


def open_transaction(tx_id: int):
    reset_filters()
    st.session_state.sel_tx = tx_id
    st.session_state.nav = "Transactions"


def render_history(svc, year: int, month: int):
    st.header("Notes & history")
    st.caption("Everything written about — or automatically recorded for — your transactions, newest first.")
    c1, c2 = st.columns([2, 2])
    search = c1.text_input("Search notes or transaction titles")
    kind = c2.radio("Show", list(KINDS), horizontal=True)
    feed = svc.notes.recent(limit=200, kind=KINDS[kind], search=search)
    if not feed:
        st.info("Nothing to show yet. Open a transaction and add a note to start its history.")
    for n in feed:
        with st.container(border=True):
            a, b = st.columns([6, 1])
            kind = "Automatic history" if n.is_system else "Note"
            a.markdown(f"**{kind}: {n.transaction_title}** — {n.text}")
            a.caption(fmt_dt(n.created_at) + (" · edited" if n.edited else ""))
            b.button("Open", key=f"open_{n.id}", on_click=open_transaction, args=(n.transaction_id,))


def render_settings(svc, year: int, month: int):
    st.header("Settings")
    with st.container(border=True):
        st.subheader("Preferences")
        with st.form("prefs"):
            name = st.text_input("Display name", value=svc.settings.name, max_chars=40)
            codes = list(CURRENCIES)
            cur = st.selectbox("Currency", codes, index=codes.index(svc.settings.currency),
                               format_func=lambda c: f"{c} ({CURRENCIES[c]})")
            if st.form_submit_button("Save preferences", type="primary"):
                if attempt(lambda: svc.settings.save_preferences(name, cur), "Preferences saved"):
                    st.rerun()
    with st.container(border=True):
        st.subheader("Backup")
        st.caption(f"All data (including notes) lives in one SQLite file: `{svc.db.path}`")
        path = Path(svc.db.path)
        if path.exists():
            st.download_button("Download database backup", data=path.read_bytes(),
                               file_name="expensetrack-backup.db", mime="application/octet-stream")


ALL = "All"
FILTER_DEFAULTS = {"f_text": "", "f_type": ALL, "f_cat": ALL, "f_from": None, "f_to": None}


def reset_filters():
    for k, v in FILTER_DEFAULTS.items():
        st.session_state[k] = v


def render_transactions(svc, year: int, month: int):
    st.header("Transactions")
    for k, v in FILTER_DEFAULTS.items():
        st.session_state.setdefault(k, v)
    st.session_state.setdefault("tx_table_ver", 0)

    tab_all, tab_rec, tab_io = st.tabs(["All transactions", "Recurring", "Import / Export"])
    txs = []

    with tab_all:
        _add_form(svc)
        _filters()
        s = st.session_state
        try:
            txs = svc.transactions.search(
                None if s.f_type == ALL else s.f_type, None if s.f_cat == ALL else s.f_cat,
                s.f_from, s.f_to, s.f_text)
        except Exception as e:
            st.error(str(e))
        _table_and_detail(svc, txs)
        _upcoming_recurring(svc)

    with tab_rec:
        _recurring(svc)

    with tab_io:
        _import_export(svc, txs)


def _add_form(svc):
    with st.expander("Add transaction"):
        tx_type = st.radio("Type", [EXPENSE, INCOME], horizontal=True, key="add_type")
        with st.form("add_tx", clear_on_submit=True):
            c1, c2 = st.columns(2)
            title = c1.text_input("Title", max_chars=100)
            amount = c2.number_input("Amount", min_value=0.0, step=10.0, format="%.2f")
            c3, c4 = st.columns(2)
            category = c3.selectbox("Category", categories_for(tx_type))
            tx_date = c4.date_input("Date", value=date.today())
            description = st.text_area("Description (optional)", max_chars=500, height=70)
            note = st.text_area("First note (optional)", max_chars=NOTE_MAX_LENGTH, height=70,
                                help="Anything worth remembering — you can add more notes later.")
            goals = svc.goals.list()
            goal = st.selectbox("Savings goal", [None] + goals,
                                format_func=lambda g: "No savings goal" if g is None else g.title)
            contribution = (st.number_input("Contribution to goal", min_value=0.0, max_value=amount,
                                            value=amount, step=10.0, format="%.2f")
                           if goal else None)
            if st.form_submit_button("Save transaction", type="primary"):
                if attempt(lambda: svc.transactions.add(title, f"{amount:.2f}", tx_type, category,
                                                        tx_date, description, note=note,
                                                        savings_goal_id=goal.id if goal else None,
                                                        savings_contribution=f"{contribution:.2f}" if goal else None),
                           "Transaction added"):
                    st.rerun()


def _filters():
    c1, c2, c3 = st.columns([2, 1, 1])
    c1.text_input("Search title, description or notes", key="f_text")
    c2.selectbox("Type", [ALL, INCOME, EXPENSE], key="f_type")
    c3.selectbox("Category", [ALL] + INCOME_CATEGORIES + EXPENSE_CATEGORIES, key="f_cat")
    d1, d2, d3 = st.columns([1, 1, 1])
    d1.date_input("From", value=None, key="f_from")
    d2.date_input("To", value=None, key="f_to")
    d3.write("")
    d3.button("Reset filters", on_click=reset_filters)


def _table_and_detail(svc, txs):
    fmt = svc.fmt
    if not txs:
        st.info("No transactions match the current filters.")
        return
    income = sum(t.amount for t in txs if t.type == INCOME)
    expense = sum(t.amount for t in txs if t.type == EXPENSE)
    st.caption(f"{len(txs)} transaction(s) · income {fmt(income)} · expenses {fmt(expense)} · "
               "select a row to see its notes & history")
    goals = {g.id: g.title for g in svc.goals.list()}
    df = pd.DataFrame([{
        "Date": t.date, "Title": t.title, "Type": t.type.title(), "Category": t.category,
        "Amount": ("+" if t.type == INCOME else "−") + fmt(t.amount),
        "Source": "Recurring" if getattr(t, "recurring_id", None) else "Manual",
        "Savings goal": goals.get(getattr(t, "savings_goal_id", None), ""),
        "Goal contribution": fmt(t.savings_contribution) if t.savings_goal_id else "",
        "Notes": str(t.user_note_count) if t.user_note_count else ""} for t in txs])
    event = st.dataframe(df, hide_index=True, width="stretch", on_select="rerun",
                         selection_mode="single-row", key=f"tx_table_{st.session_state.tx_table_ver}")
    if event.selection.rows:
        st.session_state.sel_tx = txs[event.selection.rows[0]].id
    sel = st.session_state.get("sel_tx")
    if sel is not None and not any(t.id == sel for t in txs):
        st.session_state.sel_tx = sel = None
    if sel is not None:
        _detail(svc, sel)


def _upcoming_recurring(svc):
    rules = svc.recurring.list()
    if rules:
        goals = {g.id: g.title for g in svc.goals.list()}
        st.subheader("Upcoming recurring transactions")
        st.dataframe(pd.DataFrame([{
            "Date": r.next_run_date, "Title": r.title, "Type": r.type.title(),
            "Category": r.category, "Amount": svc.fmt(r.amount),
            "Savings goal": goals.get(getattr(r, "savings_goal_id", None), ""),
            "Goal contribution": svc.fmt(r.savings_contribution) if r.savings_goal_id else "",
        } for r in rules]), hide_index=True, width="stretch")


def _close():
    st.session_state.sel_tx = None
    st.session_state.tx_table_ver += 1


def _detail(svc, tx_id: int):
    tx = svc.transactions.get(tx_id)
    fmt = svc.fmt
    with st.container(border=True):
        h1, h2 = st.columns([6, 1])
        sign = "+" if tx.type == INCOME else "−"
        h1.subheader(f"{tx.title} · {sign}{fmt(tx.amount)}")
        source = "Recurring" if getattr(tx, "recurring_id", None) else "Manual"
        goal_id = getattr(tx, "savings_goal_id", None)
        goal = next((g.title for g in svc.goals.list() if g.id == goal_id), None)
        details = f"#{tx.id} · {tx.type.title()} · {tx.category} · {tx.date:%d %b %Y} · {source}"
        allocation = f" · Savings goal: {goal} (+{fmt(tx.savings_contribution)})" if goal else ""
        h1.caption(details + allocation)
        h2.button("✕ Close", on_click=_close, key=f"close_{tx.id}")
        if tx.description:
            st.write(tx.description)

        t_notes, t_edit, t_del = st.tabs([f"Notes & history ({tx.note_count})", "Edit", "Delete"])
        with t_notes:
            _notes_panel(svc, tx)
        with t_edit:
            _edit_form(svc, tx)
        with t_del:
            _delete_panel(svc, tx)


def _notes_panel(svc, tx):
    with st.form(f"new_note_{tx.id}", clear_on_submit=True):
        text = st.text_area("Add a note", max_chars=NOTE_MAX_LENGTH, height=80,
                            placeholder="e.g. Paid by cash · reimbursed by company · split with Rahul")
        if st.form_submit_button("Add note", type="primary"):
            if attempt(lambda: svc.notes.add(tx.id, text), "Note added"):
                st.rerun()

    notes = svc.notes.list_for_transaction(tx.id)
    st.caption("Newest first. Automatic entries record what happened to this transaction and cannot be changed.")
    for n in notes:
        with st.container(border=True):
            if n.is_system:
                st.markdown(f"Automatic history: {n.text}")
                st.caption(f"Automatic · {fmt_dt(n.created_at)}")
            else:
                st.markdown(n.text)
                suffix = f" · edited {fmt_dt(n.updated_at)}" if n.edited else ""
                st.caption(f"Note · {fmt_dt(n.created_at)}{suffix}")
                with st.expander("Edit / delete"):
                    new_text = st.text_area("Note", value=n.text, key=f"note_text_{n.id}",
                                            max_chars=NOTE_MAX_LENGTH, label_visibility="collapsed")
                    b1, b2, _ = st.columns([1, 1, 4])
                    if b1.button("Save", key=f"note_save_{n.id}"):
                        if attempt(lambda: svc.notes.update(n.id, new_text), "Note updated"):
                            st.rerun()
                    if b2.button("Delete", key=f"note_del_{n.id}"):
                        if attempt(lambda: svc.notes.delete(n.id), "Note deleted"):
                            st.rerun()


def _edit_form(svc, tx):
    key = f"edit_{tx.id}"
    tx_type = st.radio("Type", [EXPENSE, INCOME], horizontal=True, key=f"{key}_type",
                       index=0 if tx.type == EXPENSE else 1)
    cats = categories_for(tx_type)
    with st.form(key):
        c1, c2 = st.columns(2)
        title = c1.text_input("Title", value=tx.title, max_chars=100)
        amount = c2.number_input("Amount", min_value=0.0, value=float(tx.amount), step=10.0, format="%.2f")
        c3, c4 = st.columns(2)
        category = c3.selectbox("Category", cats, index=cats.index(tx.category) if tx.category in cats else 0)
        tx_date = c4.date_input("Date", value=tx.date)
        description = st.text_area("Description", value=tx.description or "", max_chars=500, height=70)
        goals = svc.goals.list()
        goal_ids = [None] + [g.id for g in goals]
        current_goal = getattr(tx, "savings_goal_id", None)
        selected_goal = current_goal if current_goal in goal_ids else None
        goal_id = st.selectbox("Savings goal", goal_ids,
                               index=goal_ids.index(selected_goal),
                               format_func=lambda value: "No savings goal" if value is None else
                               next(g.title for g in goals if g.id == value))
        contribution = (st.number_input("Contribution to goal", min_value=0.0, max_value=amount,
                                        value=min(float(tx.savings_contribution), amount), step=10.0,
                                        format="%.2f") if goal_id else None)
        if st.form_submit_button("Save changes", type="primary"):
            if attempt(lambda: svc.transactions.update(tx.id, title, f"{amount:.2f}", tx_type, category,
                                                       tx_date, description, savings_goal_id=goal_id,
                                                       savings_contribution=f"{contribution:.2f}" if goal_id else None),
                       "Transaction updated — the change was logged in its history"):
                st.rerun()


def _delete_panel(svc, tx):
    st.warning(f"This permanently deletes the transaction and its {tx.note_count} history/note entries.")
    ok = st.checkbox("I understand", key=f"del_ok_{tx.id}")
    if st.button("Delete transaction", type="primary", disabled=not ok, key=f"del_{tx.id}"):
        if attempt(lambda: svc.transactions.delete(tx.id), "Transaction deleted"):
            _close()
            st.rerun()


def _recurring(svc):
    fmt = svc.fmt
    st.caption("Monthly rules are processed automatically when the app opens (and via the button below). "
               "Each generated transaction gets an automatic history entry.")
    rules = svc.recurring.list()
    if st.button("Create due transactions now"):
        n = svc.recurring.generate_due(date.today())
        flash(f"{n} transaction(s) created" if n else "Nothing is due")
        st.rerun()
    if rules:
        goals = {g.id: g.title for g in svc.goals.list()}
        st.dataframe(pd.DataFrame([{
            "ID": r.id, "Title": r.title, "Type": r.type.title(), "Category": r.category,
            "Amount": fmt(r.amount), "Day": r.schedule_day, "Next run": r.next_run_date,
            "Ends": r.end_date, "Savings goal": goals.get(getattr(r, "savings_goal_id", None), ""),
            "Goal contribution": fmt(r.savings_contribution) if r.savings_goal_id else ""}
            for r in rules]), hide_index=True, width="stretch")
        c1, c2 = st.columns([3, 1])
        pick = c1.selectbox("Delete a rule", rules, format_func=lambda r: f"#{r.id} {r.title}", index=None,
                            placeholder="Choose a rule…")
        c2.write("")
        if c2.button("Delete rule", disabled=pick is None) and pick:
            if attempt(lambda: svc.recurring.delete(pick.id), "Rule deleted"):
                st.rerun()
    else:
        st.info("No recurring rules yet.")

    st.subheader("Add monthly rule")
    tx_type = st.radio("Type", [EXPENSE, INCOME], horizontal=True, key="rec_type")
    with st.form("rec_form", clear_on_submit=True):
        c1, c2 = st.columns(2)
        title = c1.text_input("Title", max_chars=100)
        amount = c2.number_input("Amount", min_value=0.0, step=10.0, format="%.2f")
        c3, c4, c5 = st.columns(3)
        category = c3.selectbox("Category", categories_for(tx_type))
        start = c4.date_input("First date", value=date.today())
        end = c5.date_input("End date (optional)", value=None)
        description = st.text_input("Description (optional)", max_chars=500)
        goals = svc.goals.list()
        goal = st.selectbox("Savings goal", [None] + goals,
                            format_func=lambda g: "No savings goal" if g is None else g.title)
        contribution = (st.number_input("Contribution per occurrence", min_value=0.0, max_value=amount,
                                        value=amount, step=10.0, format="%.2f") if goal else None)
        if st.form_submit_button("Add monthly rule", type="primary"):
            if attempt(lambda: svc.recurring.create(title, f"{amount:.2f}", tx_type, category, start, end,
                                                    description, goal.id if goal else None,
                                                    f"{contribution:.2f}" if goal else None),
                       "Recurring rule added"):
                st.rerun()


def _import_export(svc, txs):
    st.subheader("Export")
    st.caption("Exports the transactions currently shown (respecting filters), including your notes.")
    st.download_button("Download CSV", data="\ufeff" + csv_export(txs, svc.notes),
                       file_name=f"expensetrack-transactions-{date.today()}.csv", mime="text/csv",
                       disabled=not txs)

    st.subheader("Import")
    st.caption("Columns: Date (YYYY-MM-DD), Title, Type, Category, Amount — optional: Description, Notes.")
    st.session_state.setdefault("import_ver", 0)
    up = st.file_uploader("CSV file", type="csv", key=f"import_{st.session_state.import_ver}")
    if not up:
        return
    try:
        rows = csv_preview(up.getvalue().decode("utf-8-sig"), svc.transactions)
    except Exception as e:
        st.error(str(e))
        return
    valid = [r for r in rows if not r.issue]
    st.write(f"**{len(valid)}** valid row(s), **{len(rows) - len(valid)}** with issues (these are skipped).")
    st.dataframe(pd.DataFrame([{"Line": r.line, "Title": r.data.get("Title"), "Amount": r.data.get("Amount"),
                                "Status": r.issue or "OK"} for r in rows]), hide_index=True, width="stretch")
    if st.button("Import valid rows", type="primary", disabled=not valid):
        n = import_rows(rows, svc.transactions)
        st.session_state.import_ver += 1
        flash(f"Imported {n} transaction(s)")
        st.rerun()

PAGES = {
    "Dashboard": render_dashboard,
    "Transactions": render_transactions,
    "Notes & history": render_history,
    "Budgets": render_budgets,
    "Analytics": render_analytics,
    "Savings goals": render_goals,
    "Settings": render_settings,
}


@st.cache_resource
def get_services() -> Services:
    return Services()


svc = get_services()
show_flashes()
if "recurring_checked" not in st.session_state:
    st.session_state.recurring_checked = True
    created = svc.recurring.generate_due(date.today())
    if created:
        flash(f"{created} recurring transaction(s) created")
        st.rerun()

today = date.today()
with st.sidebar:
    st.title("ExpenseTrack")
    if svc.settings.name:
        st.caption(f"Hello, {svc.settings.name}")
    page = st.radio("Navigate", list(PAGES), key="nav", label_visibility="collapsed")
    st.divider()
    st.caption("Period for dashboard, budgets & analytics")
    month = st.selectbox("Month", range(1, 13), index=today.month - 1,
                         format_func=lambda m: calendar.month_name[m])
    year = st.number_input("Year", min_value=2000, max_value=2100, value=today.year, step=1)

PAGES[page](svc, int(year), int(month))
