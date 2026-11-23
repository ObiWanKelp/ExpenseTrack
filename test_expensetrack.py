import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services import (
    BudgetService, Database, InvalidRequestError, ResourceNotFoundError, Services,
    calculate_utilization, csv_export, csv_preview, format_money, import_rows,
)


@pytest.fixture
def svc(tmp_path):
    return Services(Database(tmp_path / "test.db"))


APP = str(Path(__file__).resolve().parent / "app.py")
PAGES = ["Dashboard", "Transactions", "Notes & history", "Budgets", "Analytics",
         "Savings goals", "Settings"]


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    path = tmp_path / "ui.db"
    monkeypatch.setattr("db.DB_PATH", str(path))
    s = Services(Database(path))
    today = date.today()
    s.transactions.add("Salary", "50000", "INCOME", "Salary", today)
    tx = s.transactions.add("Groceries", "1250.50", "EXPENSE", "Food", today, note="weekly shop")
    s.budgets.set_budget("2000", today.month, today.year)
    s.goals.create("Laptop", "60000", None)
    s.recurring.create("Rent", "5000", "EXPENSE", "Bills", today)
    return s, tx


def fresh_app():
    st.cache_resource.clear()
    return AppTest.from_file(APP, default_timeout=30).run()


def test_all_pages_render(seeded):
    at = fresh_app()
    for page in PAGES:
        at.sidebar.radio(key="nav").set_value(page).run()
        assert not at.exception, f"{page}: {[e.value for e in at.exception]}"


def test_transaction_form_and_notes_history(seeded):
    svc, tx = seeded
    at = fresh_app()
    at.sidebar.radio(key="nav").set_value("Transactions").run()
    next(t for t in at.text_input if t.label == "Title").set_value("Coffee")
    at.number_input[0].set_value(120.0)
    next(t for t in at.text_area if t.label == "First note (optional)").set_value("Met a friend")
    next(b for b in at.button if b.label == "Save transaction").click().run()
    assert [t.title for t in svc.transactions.search(text="Met a friend")] == ["Coffee"]

    at.session_state["sel_tx"] = tx.id
    at.run()
    next(t for t in at.text_area if t.label == "Add a note").set_value("Reimbursable")
    next(b for b in at.button if b.label == "Add note").click().run()
    assert "Reimbursable" in [n.text for n in svc.notes.list_for_transaction(tx.id)]


def test_budget_status_and_monthly_summary(svc):
    t = svc.transactions
    t.add("Pay", "15000", "INCOME", "Scholarship", date(2026, 11, 1))
    t.add("Food", "1000", "EXPENSE", "Food", date(2026, 11, 3))
    t.add("Bus", "500", "EXPENSE", "Travel", date(2026, 10, 3))
    svc.budgets.set_budget("800", 11, 2026)
    status = svc.budgets.get_status(11, 2026)
    summary = svc.dashboard.summary(2026, 11)
    analytics = svc.dashboard.analytics(2026, 11)
    assert status.exceeded and status.spent == Decimal("1000.00")
    assert summary.balance == Decimal("13500.00") and summary.monthly_spending == Decimal("1000.00")
    assert len(analytics.monthly) == 6 and analytics.monthly[-1].label == "Nov 2026"
    assert BudgetService.build_status(11, 2026, None, 100).has_budget is False
    with pytest.raises(InvalidRequestError):
        calculate_utilization(Decimal("1"), Decimal("0"))


def test_transaction_validation_search_update_and_delete(svc):
    t = svc.transactions
    tx = t.add("Lunch", "150.00", "EXPENSE", "Food", date(2026, 11, 3), description="with team")
    assert t.get(tx.id).description == "with team"
    t.add("Pay", "1000", "INCOME", "Salary", date(2026, 11, 4))
    assert [x.title for x in t.search(tx_type="INCOME")] == ["Pay"]
    updated = t.update(tx.id, "Dinner", "200", "EXPENSE", "Food", date(2026, 11, 5))
    assert updated.title == "Dinner" and t.balance() == Decimal("800.00")
    for amount in ("0", "-5", "abc", "1.234"):
        with pytest.raises(InvalidRequestError):
            t.add("Bad", amount, "EXPENSE", "Food", date(2026, 11, 3))
    with pytest.raises(InvalidRequestError):
        t.add("Bad", "5", "INCOME", "Food", date(2026, 11, 3))
    t.delete(tx.id)
    with pytest.raises(ResourceNotFoundError):
        t.get(tx.id)


def test_notes_validation_history_and_search(svc):
    tx = svc.transactions.add("Groceries", "500", "EXPENSE", "Food", date(2026, 11, 9))
    assert svc.notes.list_for_transaction(tx.id)[0].is_system
    first = svc.notes.add(tx.id, "reimbursable")
    edited = svc.notes.update(first.id, "fixed reimbursable")
    assert edited.edited and edited.text == "fixed reimbursable"
    assert [t.id for t in svc.transactions.search(text="reimbursable")] == [tx.id]
    for invalid in ("", "   ", None, "x" * 1001):
        with pytest.raises(InvalidRequestError):
            svc.notes.add(tx.id, invalid)
    system = svc.notes.list_for_transaction(tx.id)[-1]
    with pytest.raises(InvalidRequestError):
        svc.notes.delete(system.id)
    svc.notes.delete(edited.id)
    assert svc.notes.recent(kind="USER") == []


def test_recurring_catch_up_forecast_and_validation(svc):
    svc.recurring.create("Rent", "5000", "EXPENSE", "Bills", date(2026, 1, 31))
    assert svc.recurring.generate_due(date(2026, 3, 31)) == 3
    assert sorted(t.date for t in svc.transactions.search()) == [
        date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)
    ]
    assert svc.recurring.list()[0].next_run_date == date(2026, 4, 30)
    assert svc.recurring.generate_due(date(2026, 3, 31)) == 0
    svc.recurring.create("Pay", "1000", "INCOME", "Salary", date(2026, 11, 25))
    f = svc.recurring.forecast(date(2026, 11, 22))
    assert f.income == Decimal("1000.00") and f.overdue_count == 7
    with pytest.raises(InvalidRequestError):
        svc.recurring.create("X", "1", "EXPENSE", "Food", date(2026, 5, 1), date(2026, 4, 1))


def test_goal_contributions_follow_transactions(svc):
    goal = svc.goals.create("Trip", "1000")
    tx = svc.transactions.add("Trip savings", "100", "EXPENSE", "Other", date(2026, 11, 3),
                              savings_goal_id=goal.id)
    assert svc.goals.get(goal.id).saved == Decimal("100.00")
    svc.transactions.update(tx.id, "Trip savings", "150", "EXPENSE", "Other", tx.date,
                            savings_goal_id=goal.id)
    assert svc.goals.get(goal.id).saved == Decimal("150.00")
    svc.transactions.delete(tx.id)
    assert svc.goals.get(goal.id).saved == Decimal("0.00")
    income = svc.transactions.add("Pay", "500", "INCOME", "Salary", date(2026, 11, 4),
                                  savings_goal_id=goal.id, savings_contribution="125")
    assert svc.goals.get(goal.id).saved == Decimal("125.00")
    assert income.savings_contribution == Decimal("125.00")
    svc.transactions.update(income.id, "Pay", "500", "INCOME", "Salary", income.date,
                            savings_goal_id=goal.id, savings_contribution="500")
    assert svc.goals.get(goal.id).saved == Decimal("500.00")
    with pytest.raises(InvalidRequestError):
        svc.transactions.add("Pay", "500", "INCOME", "Salary", date(2026, 11, 5),
                             savings_goal_id=goal.id, savings_contribution="500.01")
    rule = svc.recurring.create("Automatic savings", "1000", "INCOME", "Salary", date(2026, 11, 20),
                                savings_goal_id=goal.id, savings_contribution="250")
    assert svc.recurring.generate_due(date(2026, 11, 20)) == 1
    assert svc.transactions.search()[0].recurring_id == rule.id
    assert svc.goals.get(goal.id).saved == Decimal("750.00")
    with pytest.raises(InvalidRequestError):
        svc.goals.contribute(goal.id, "0")


def test_preferences_and_currency_formatting(svc):
    assert svc.settings.currency == "INR"
    svc.settings.save_preferences("Asha", "USD")
    assert svc.settings.currency == "USD" and svc.settings.name == "Asha"
    assert format_money(Decimal("1234567.5"), "INR") == "₹12,34,567.50"
    assert format_money(Decimal("1234567.5"), "USD") == "$1,234,567.50"


def test_csv_roundtrip_with_notes(svc):
    tx = svc.transactions.add("Books, used", "300", "EXPENSE", "Education", date(2026, 11, 6),
                              note="from library sale")
    exported = csv_export([tx], svc.notes)
    assert "from library sale" in exported
    rows = csv_preview(exported + "2026-13-45,Bad,EXPENSE,Food,5,,\n", svc.transactions)
    assert [bool(r.issue) for r in rows] == [False, True]
    assert import_rows(rows, svc.transactions) == 1
    assert len(svc.transactions.search(text="library")) == 2


def test_transaction_totals_and_search_filters(svc):
    t = svc.transactions
    t.add("A", "10", "EXPENSE", "Food", date(2026, 11, 1))
    t.add("B", "20", "INCOME", "Salary", date(2026, 11, 2))
    t.add("C", "30", "EXPENSE", "Travel", date(2026, 11, 3))
    assert [x.title for x in t.search(category="Travel")] == ["C"]
    assert t.total_income() == Decimal("20.00")
    assert t.total_expenses() == Decimal("40.00")
    assert t.balance() == Decimal("-20.00")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
