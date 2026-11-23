from __future__ import annotations

import calendar
import csv
import io
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from db import Database

class ExpenseTrackError(Exception):
    pass


class InvalidRequestError(ExpenseTrackError):
    pass


class ResourceNotFoundError(ExpenseTrackError):
    pass



INCOME = "INCOME"
EXPENSE = "EXPENSE"
TYPES = (INCOME, EXPENSE)

INCOME_CATEGORIES = ["Salary", "Scholarship", "Freelance", "Other Income"]
EXPENSE_CATEGORIES = ["Food", "Travel", "Shopping", "Education",
                      "Entertainment", "Bills", "Healthcare", "Other"]

CURRENCIES = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}

WARNING_THRESHOLD = Decimal("80")
MAX_AMOUNT = Decimal("9999999999.99")
TREND_MONTHS = 6

NOTE_MAX_LENGTH = 1000
NOTE_USER = "USER"
NOTE_SYSTEM = "SYSTEM"


def categories_for(tx_type: str) -> list[str]:
    return INCOME_CATEGORIES if tx_type == INCOME else EXPENSE_CATEGORIES


def is_valid_category(tx_type: str, category: str | None) -> bool:
    return bool(category) and category in categories_for(tx_type)


TWO_PLACES = Decimal("0.01")


def money(value) -> Decimal:
    if value is None:
        return Decimal("0.00")
    return Decimal(str(value)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def parse_amount(value, label: str = "Amount") -> Decimal:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise InvalidRequestError(f"{label} is required")
    try:
        d = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        raise InvalidRequestError(f"{label} must be a number")
    if not d.is_finite():
        raise InvalidRequestError(f"{label} must be a number")
    if d <= 0:
        raise InvalidRequestError(f"{label} must be greater than zero")
    if d > MAX_AMOUNT:
        raise InvalidRequestError(f"{label} is too large")
    if d != d.quantize(TWO_PLACES):
        raise InvalidRequestError(f"{label} can have at most 2 decimal places")
    return d.quantize(TWO_PLACES)


def to_cents(amount: Decimal) -> int:
    return int((amount * 100).to_integral_value(rounding=ROUND_HALF_UP))


def from_cents(cents: int | None) -> Decimal:
    return (Decimal(cents or 0) / 100).quantize(TWO_PLACES)


def month_bounds(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def add_months(year: int, month: int, n: int) -> tuple[int, int]:
    idx = year * 12 + (month - 1) + n
    return idx // 12, idx % 12 + 1


def clamp_day(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def month_label(year: int, month: int) -> str:
    return f"{calendar.month_abbr[month]} {year}"


def blank_to_none(value: str | None) -> str | None:
    return None if value is None or not value.strip() else value.strip()


def _indian_group(digits: str) -> str:
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts + [tail])


def format_money(amount, currency: str = "INR") -> str:
    d = money(amount)
    sign = "-" if d < 0 else ""
    whole, frac = f"{abs(d):.2f}".split(".")
    whole = _indian_group(whole) if currency == "INR" else f"{int(whole):,}"
    return f"{sign}{CURRENCIES.get(currency, '')}{whole}.{frac}"


@dataclass
class Note:
    id: int
    transaction_id: int
    text: str
    kind: str
    created_at: datetime
    updated_at: datetime | None = None
    transaction_title: str | None = None

    @property
    def is_system(self) -> bool:
        return self.kind == NOTE_SYSTEM

    @property
    def edited(self) -> bool:
        return self.updated_at is not None


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def insert_note(conn, transaction_id: int, text: str, kind: str = NOTE_USER) -> int:
    cur = conn.execute(
        "INSERT INTO transaction_notes (transaction_id, note, kind, created_at) VALUES (?,?,?,?)",
        (transaction_id, text, kind, now_iso()))
    return cur.lastrowid


def clean_note(text: str | None) -> str:
    text = (text or "").strip()
    if not text:
        raise InvalidRequestError("Note cannot be empty")
    if len(text) > NOTE_MAX_LENGTH:
        raise InvalidRequestError(f"Note must be at most {NOTE_MAX_LENGTH} characters")
    return text


def _note_from_row(r, with_title: bool = False) -> Note:
    return Note(
        id=r["note_id"], transaction_id=r["transaction_id"], text=r["note"], kind=r["kind"],
        created_at=datetime.fromisoformat(r["created_at"]),
        updated_at=datetime.fromisoformat(r["updated_at"]) if r["updated_at"] else None,
        transaction_title=r["title"] if with_title else None)


class NoteService:
    def __init__(self, db: Database):
        self.db = db

    def add(self, transaction_id: int, text: str) -> Note:
        text = clean_note(text)
        with self.db.connect() as conn:
            if not conn.execute("SELECT 1 FROM transactions WHERE transaction_id=?",
                                (transaction_id,)).fetchone():
                raise ResourceNotFoundError(f"Transaction not found with id {transaction_id}")
            note_id = insert_note(conn, transaction_id, text, NOTE_USER)
        return self.get(note_id)

    def get(self, note_id: int) -> Note:
        with self.db.connect() as conn:
            r = conn.execute("SELECT * FROM transaction_notes WHERE note_id=?", (note_id,)).fetchone()
        if not r:
            raise ResourceNotFoundError(f"Note not found with id {note_id}")
        return _note_from_row(r)

    def list_for_transaction(self, transaction_id: int, newest_first: bool = True) -> list[Note]:
        order = "DESC" if newest_first else "ASC"
        with self.db.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM transaction_notes WHERE transaction_id=? "
                f"ORDER BY created_at {order}, note_id {order}", (transaction_id,)).fetchall()
        return [_note_from_row(r) for r in rows]

    def update(self, note_id: int, text: str) -> Note:
        text = clean_note(text)
        note = self.get(note_id)
        if note.is_system:
            raise InvalidRequestError("Automatic history entries cannot be edited")
        with self.db.connect() as conn:
            conn.execute("UPDATE transaction_notes SET note=?, updated_at=? WHERE note_id=?",
                         (text, now_iso(), note_id))
        return self.get(note_id)

    def delete(self, note_id: int) -> None:
        note = self.get(note_id)
        if note.is_system:
            raise InvalidRequestError("Automatic history entries cannot be deleted")
        with self.db.connect() as conn:
            conn.execute("DELETE FROM transaction_notes WHERE note_id=?", (note_id,))

    def recent(self, limit: int = 100, kind: str | None = None, search: str | None = None) -> list[Note]:
        sql = ("SELECT n.*, t.title FROM transaction_notes n "
               "JOIN transactions t ON t.transaction_id = n.transaction_id WHERE 1=1")
        params: list = []
        if kind:
            sql += " AND n.kind = ?"
            params.append(kind)
        if search and search.strip():
            sql += " AND (LOWER(n.note) LIKE ? ESCAPE '\\' OR LOWER(t.title) LIKE ? ESCAPE '\\')"
            like = like_pattern(search)
            params += [like, like]
        sql += " ORDER BY n.created_at DESC, n.note_id DESC LIMIT ?"
        params.append(limit)
        with self.db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_note_from_row(r, with_title=True) for r in rows]


def like_pattern(text: str) -> str:
    escaped = text.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


TITLE_MAX = 100
DESCRIPTION_MAX = 500


@dataclass
class Transaction:
    id: int
    title: str
    amount: Decimal
    type: str
    category: str
    date: date
    description: str | None = None
    note_count: int = 0
    user_note_count: int = 0
    savings_goal_id: int | None = None
    savings_contribution: Decimal = Decimal("0.00")
    recurring_id: int | None = None


def _transaction_from_row(r) -> Transaction:
    return Transaction(
        id=r["transaction_id"], title=r["title"], amount=from_cents(r["amount_cents"]),
        type=r["type"], category=r["category"], date=date.fromisoformat(r["transaction_date"]),
        description=r["description"], note_count=r["note_count"], user_note_count=r["user_note_count"],
        savings_goal_id=r["savings_goal_id"],
        savings_contribution=from_cents(r["savings_contribution_cents"]), recurring_id=r["recurring_id"])

_SELECT = ("SELECT t.*, "
           "(SELECT COUNT(*) FROM transaction_notes n WHERE n.transaction_id = t.transaction_id) AS note_count, "
           "(SELECT COUNT(*) FROM transaction_notes n WHERE n.transaction_id = t.transaction_id "
           " AND n.kind = 'USER') AS user_note_count FROM transactions t")


class TransactionService:
    def __init__(self, db: Database):
        self.db = db

    def _validate(self, title, amount, tx_type, category, tx_date, description):
        title = (title or "").strip()
        if not title:
            raise InvalidRequestError("Title is required")
        if len(title) > TITLE_MAX:
            raise InvalidRequestError(f"Title must be at most {TITLE_MAX} characters")
        amount = parse_amount(amount)
        if tx_type not in TYPES:
            raise InvalidRequestError("Type is required (INCOME or EXPENSE)")
        if not is_valid_category(tx_type, category):
            raise InvalidRequestError(
                f"Category '{category}' is not valid for type {tx_type}. "
                f"Allowed: {categories_for(tx_type)}")
        if not isinstance(tx_date, date):
            raise InvalidRequestError("Date is required")
        description = blank_to_none(description)
        if description and len(description) > DESCRIPTION_MAX:
            raise InvalidRequestError(f"Description must be at most {DESCRIPTION_MAX} characters")
        return title, amount, tx_type, category, tx_date, description

    def add(self, title, amount, tx_type, category, tx_date, description=None,
            note: str | None = None, history_text: str = "Transaction created",
            savings_goal_id: int | None = None, savings_contribution=None) -> Transaction:
        title, amount, tx_type, category, tx_date, description = self._validate(
            title, amount, tx_type, category, tx_date, description)
        first_note = clean_note(note) if note and note.strip() else None
        with self.db.connect() as conn:
            contribution = self._goal_contribution(conn, savings_goal_id, savings_contribution, amount)
            cur = conn.execute(
                "INSERT INTO transactions (title, amount_cents, type, category, transaction_date, description, "
                "savings_goal_id, savings_contribution_cents) VALUES (?,?,?,?,?,?,?,?)",
                (title, to_cents(amount), tx_type, category, tx_date.isoformat(), description, savings_goal_id,
                 contribution))
            tx_id = cur.lastrowid
            if savings_goal_id:
                conn.execute("UPDATE savings_goals SET saved_cents=saved_cents+? WHERE goal_id=?",
                             (contribution, savings_goal_id))
            insert_note(conn, tx_id, history_text, NOTE_SYSTEM)
            if first_note:
                insert_note(conn, tx_id, first_note)
        return self.get(tx_id)

    def update(self, tx_id, title, amount, tx_type, category, tx_date, description=None,
               savings_goal_id: int | None = None, savings_contribution=None) -> Transaction:
        title, amount, tx_type, category, tx_date, description = self._validate(
            title, amount, tx_type, category, tx_date, description)
        old = self.get(tx_id)
        changes = []
        if old.title != title:
            changes.append(f"Title: “{old.title}” → “{title}”")
        if old.amount != amount:
            changes.append(f"Amount: {old.amount} → {amount}")
        if old.type != tx_type:
            changes.append(f"Type: {old.type} → {tx_type}")
        if old.category != category:
            changes.append(f"Category: {old.category} → {category}")
        if old.date != tx_date:
            changes.append(f"Date: {old.date.isoformat()} → {tx_date.isoformat()}")
        if (old.description or "") != (description or ""):
            changes.append("Description changed")
        if old.savings_goal_id != savings_goal_id:
            changes.append("Savings goal changed")
        with self.db.connect() as conn:
            contribution = self._goal_contribution(conn, savings_goal_id, savings_contribution, amount)
        if old.savings_contribution != from_cents(contribution):
            changes.append(f"Savings contribution: {old.savings_contribution} → {from_cents(contribution)}")
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE transactions SET title=?, amount_cents=?, type=?, category=?, "
                "transaction_date=?, description=?, savings_goal_id=?, savings_contribution_cents=? "
                "WHERE transaction_id=?",
                (title, to_cents(amount), tx_type, category, tx_date.isoformat(), description,
                 savings_goal_id, contribution, tx_id))
            goal_changes = {}
            if old.savings_goal_id:
                goal_changes[old.savings_goal_id] = goal_changes.get(old.savings_goal_id, 0) - to_cents(old.savings_contribution)
            if savings_goal_id:
                goal_changes[savings_goal_id] = goal_changes.get(savings_goal_id, 0) + contribution
            for goal_id, change in goal_changes.items():
                if change:
                    conn.execute("UPDATE savings_goals SET saved_cents=saved_cents+? WHERE goal_id=?",
                                 (change, goal_id))
            if changes:
                insert_note(conn, tx_id, "Edited — " + "; ".join(changes), NOTE_SYSTEM)
        return self.get(tx_id)

    def delete(self, tx_id: int) -> None:
        tx = self.get(tx_id)
        with self.db.connect() as conn:
            if tx.savings_goal_id:
                conn.execute("UPDATE savings_goals SET saved_cents=saved_cents-? WHERE goal_id=?",
                             (to_cents(tx.savings_contribution), tx.savings_goal_id))
            conn.execute("DELETE FROM transactions WHERE transaction_id=?", (tx_id,))

    @staticmethod
    def _goal_contribution(conn, goal_id, contribution, amount):
        if goal_id is None:
            if contribution not in (None, 0, "0", "0.00"):
                raise InvalidRequestError("Choose a savings goal for this contribution")
            return 0
        if not conn.execute("SELECT 1 FROM savings_goals WHERE goal_id=?", (goal_id,)).fetchone():
            raise ResourceNotFoundError(f"Savings goal not found: {goal_id}")
        contribution = amount if contribution is None else parse_amount(contribution, "Savings contribution")
        if contribution > amount:
            raise InvalidRequestError("Savings contribution cannot exceed the transaction amount")
        return to_cents(contribution)

    def get(self, tx_id: int) -> Transaction:
        with self.db.connect() as conn:
            r = conn.execute(_SELECT + " WHERE t.transaction_id=?", (tx_id,)).fetchone()
        if not r:
            raise ResourceNotFoundError(f"Transaction not found with id {tx_id}")
        return _transaction_from_row(r)

    def search(self, tx_type=None, category=None, date_from=None, date_to=None,
               text=None) -> list[Transaction]:
        if date_from and date_to and date_from > date_to:
            raise InvalidRequestError("'from' date must not be after 'to' date")
        sql, params = _SELECT + " WHERE 1=1", []
        if tx_type:
            sql += " AND t.type = ?"; params.append(tx_type)
        if category:
            sql += " AND t.category = ?"; params.append(category)
        if date_from:
            sql += " AND t.transaction_date >= ?"; params.append(date_from.isoformat())
        if date_to:
            sql += " AND t.transaction_date <= ?"; params.append(date_to.isoformat())
        if text and text.strip():
            like = like_pattern(text)
            sql += (" AND (LOWER(t.title) LIKE ? ESCAPE '\\'"
                    " OR LOWER(COALESCE(t.description,'')) LIKE ? ESCAPE '\\'"
                    " OR EXISTS (SELECT 1 FROM transaction_notes n WHERE n.transaction_id = t.transaction_id"
                    "            AND n.kind = 'USER' AND LOWER(n.note) LIKE ? ESCAPE '\\'))")
            params += [like, like, like]
        sql += " ORDER BY t.transaction_date DESC, t.transaction_id DESC"
        with self.db.connect() as conn:
            return [_transaction_from_row(r) for r in conn.execute(sql, params).fetchall()]

    def recent(self, limit: int = 5) -> list[Transaction]:
        with self.db.connect() as conn:
            rows = conn.execute(_SELECT + " ORDER BY t.transaction_date DESC, t.transaction_id DESC LIMIT ?",
                                (limit,)).fetchall()
        return [_transaction_from_row(r) for r in rows]

    def sum_by_type(self, tx_type: str, start: date | None = None, end: date | None = None,
                    category: str | None = None) -> Decimal:
        sql, params = "SELECT SUM(amount_cents) FROM transactions WHERE type=?", [tx_type]
        if start:
            sql += " AND transaction_date >= ?"; params.append(start.isoformat())
        if end:
            sql += " AND transaction_date <= ?"; params.append(end.isoformat())
        if category:
            sql += " AND category = ?"; params.append(category)
        with self.db.connect() as conn:
            return from_cents(conn.execute(sql, params).fetchone()[0])

    def sum_by_category(self, tx_type: str, start: date, end: date) -> list[tuple[str, Decimal]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT category, SUM(amount_cents) s FROM transactions "
                "WHERE type=? AND transaction_date BETWEEN ? AND ? GROUP BY category ORDER BY s DESC",
                (tx_type, start.isoformat(), end.isoformat())).fetchall()
        return [(r["category"], from_cents(r["s"])) for r in rows]

    def total_income(self) -> Decimal:
        return self.sum_by_type(INCOME)

    def total_expenses(self) -> Decimal:
        return self.sum_by_type(EXPENSE)

    def balance(self) -> Decimal:
        return self.total_income() - self.total_expenses()


@dataclass
class RecurringRule:
    id: int
    title: str
    amount: Decimal
    type: str
    category: str
    description: str | None
    schedule_day: int
    next_run_date: date
    end_date: date | None
    savings_goal_id: int | None = None
    savings_contribution: Decimal = Decimal("0.00")


@dataclass
class Forecast:
    income: Decimal
    expense: Decimal
    net: Decimal
    overdue_count: int
    upcoming: list[tuple[date, RecurringRule]]


def _recurring_from_row(r) -> RecurringRule:
    return RecurringRule(
        r["recurring_id"], r["title"], from_cents(r["amount_cents"]), r["type"], r["category"],
        r["description"], r["schedule_day"], date.fromisoformat(r["next_run_date"]),
        date.fromisoformat(r["end_date"]) if r["end_date"] else None, r["savings_goal_id"],
        from_cents(r["savings_contribution_cents"]))


def _next_after(current: date, schedule_day: int) -> date:
    y, m = add_months(current.year, current.month, 1)
    return clamp_day(y, m, schedule_day)


class RecurringService:
    def __init__(self, db: Database):
        self.db = db

    def list(self) -> list[RecurringRule]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM recurring_transactions "
                                "ORDER BY next_run_date, recurring_id").fetchall()
        return [_recurring_from_row(r) for r in rows]

    def create(self, title, amount, tx_type, category, start_date: date, end_date: date | None = None,
               description=None, savings_goal_id: int | None = None, savings_contribution=None) -> RecurringRule:
        title = (title or "").strip()
        if not title or len(title) > 100:
            raise InvalidRequestError("Title is required (max 100 characters)")
        amount = parse_amount(amount)
        if tx_type not in TYPES:
            raise InvalidRequestError("Type is required (INCOME or EXPENSE)")
        if not is_valid_category(tx_type, category):
            raise InvalidRequestError(f"Category '{category}' is not valid for type {tx_type}")
        if not isinstance(start_date, date):
            raise InvalidRequestError("Start date is required")
        if end_date and end_date < start_date:
            raise InvalidRequestError("End date must be on or after the start date")
        description = blank_to_none(description)
        if description and len(description) > 500:
            raise InvalidRequestError("Description must be at most 500 characters")
        with self.db.connect() as conn:
            contribution = TransactionService._goal_contribution(
                conn, savings_goal_id, savings_contribution, amount)
            cur = conn.execute(
                "INSERT INTO recurring_transactions (title, amount_cents, type, category, description, "
                "schedule_day, next_run_date, end_date, savings_goal_id, savings_contribution_cents) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (title, to_cents(amount), tx_type, category, description, start_date.day,
                 start_date.isoformat(), end_date.isoformat() if end_date else None, savings_goal_id, contribution))
            rid = cur.lastrowid
            r = conn.execute("SELECT * FROM recurring_transactions WHERE recurring_id=?", (rid,)).fetchone()
        return _recurring_from_row(r)

    def delete(self, rule_id: int) -> None:
        with self.db.connect() as conn:
            if not conn.execute("DELETE FROM recurring_transactions WHERE recurring_id=?",
                                (rule_id,)).rowcount:
                raise ResourceNotFoundError(f"Recurring transaction not found with id {rule_id}")

    def generate_due(self, today: date) -> int:
        created = 0
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM recurring_transactions WHERE next_run_date <= ? "
                                "ORDER BY next_run_date, recurring_id", (today.isoformat(),)).fetchall()
            for row in rows:
                rule = _recurring_from_row(row)
                run = rule.next_run_date
                while run <= today and (rule.end_date is None or run <= rule.end_date):
                    cur = conn.execute(
                        "INSERT INTO transactions (title, amount_cents, type, category, transaction_date, description, "
                        "recurring_id, savings_goal_id, savings_contribution_cents) VALUES (?,?,?,?,?,?,?,?,?)",
                        (rule.title, to_cents(rule.amount), rule.type, rule.category, run.isoformat(),
                         rule.description, rule.id, rule.savings_goal_id, to_cents(rule.savings_contribution)))
                    if rule.savings_goal_id:
                        conn.execute("UPDATE savings_goals SET saved_cents=saved_cents+? WHERE goal_id=?",
                                     (to_cents(rule.savings_contribution), rule.savings_goal_id))
                    insert_note(conn, cur.lastrowid,
                                f"Auto-created from monthly recurring rule “{rule.title}” (#{rule.id})",
                                NOTE_SYSTEM)
                    run = _next_after(run, rule.schedule_day)
                    created += 1
                conn.execute("UPDATE recurring_transactions SET next_run_date=? WHERE recurring_id=?",
                             (run.isoformat(), rule.id))
        return created

    def forecast(self, today: date, days: int = 30) -> Forecast:
        last = today + timedelta(days=days)
        income = expense = Decimal("0.00")
        overdue, upcoming = 0, []
        for rule in self.list():
            run = rule.next_run_date
            while run <= last and (rule.end_date is None or run <= rule.end_date):
                if run < today:
                    overdue += 1
                else:
                    upcoming.append((run, rule))
                    if rule.type == "INCOME":
                        income += rule.amount
                    else:
                        expense += rule.amount
                run = _next_after(run, rule.schedule_day)
        upcoming.sort(key=lambda x: (x[0], x[1].id))
        return Forecast(income, expense, income - expense, overdue, upcoming)


@dataclass
class SavingsGoal:
    id: int
    title: str
    target: Decimal
    saved: Decimal
    target_date: date | None

    @property
    def progress(self) -> float:
        return min(1.0, float(self.saved / self.target)) if self.target else 0.0

    @property
    def remaining(self) -> Decimal:
        return max(Decimal("0.00"), self.target - self.saved)


def _goal_from_row(r) -> SavingsGoal:
    return SavingsGoal(r["goal_id"], r["title"], from_cents(r["target_cents"]), from_cents(r["saved_cents"]),
                       date.fromisoformat(r["target_date"]) if r["target_date"] else None)


class SavingsGoalService:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _title(title):
        title = (title or "").strip()
        if not title:
            raise InvalidRequestError("Goal name is required")
        if len(title) > 100:
            raise InvalidRequestError("Goal name must be at most 100 characters")
        return title

    def list(self) -> list[SavingsGoal]:
        with self.db.connect() as conn:
            return [_goal_from_row(r) for r in conn.execute("SELECT * FROM savings_goals ORDER BY goal_id DESC")]

    def get(self, goal_id: int) -> SavingsGoal:
        with self.db.connect() as conn:
            r = conn.execute("SELECT * FROM savings_goals WHERE goal_id=?", (goal_id,)).fetchone()
        if not r:
            raise ResourceNotFoundError(f"Savings goal not found: {goal_id}")
        return _goal_from_row(r)

    def create(self, title, target, target_date: date | None = None) -> SavingsGoal:
        title, target = self._title(title), parse_amount(target, "Target amount")
        with self.db.connect() as conn:
            cur = conn.execute("INSERT INTO savings_goals (title, target_cents, target_date) VALUES (?,?,?)",
                               (title, to_cents(target), target_date.isoformat() if target_date else None))
        return self.get(cur.lastrowid)

    def update(self, goal_id, title, target, target_date: date | None = None) -> SavingsGoal:
        title, target = self._title(title), parse_amount(target, "Target amount")
        self.get(goal_id)
        with self.db.connect() as conn:
            conn.execute("UPDATE savings_goals SET title=?, target_cents=?, target_date=? WHERE goal_id=?",
                         (title, to_cents(target), target_date.isoformat() if target_date else None, goal_id))
        return self.get(goal_id)

    def contribute(self, goal_id, amount) -> SavingsGoal:
        amount = parse_amount(amount, "Contribution")
        self.get(goal_id)
        with self.db.connect() as conn:
            conn.execute("UPDATE savings_goals SET saved_cents = saved_cents + ? WHERE goal_id=?",
                         (to_cents(amount), goal_id))
        return self.get(goal_id)

    def delete(self, goal_id: int) -> None:
        self.get(goal_id)
        with self.db.connect() as conn:
            conn.execute("DELETE FROM savings_goals WHERE goal_id=?", (goal_id,))


HUNDRED = Decimal("100")


@dataclass
class BudgetStatus:
    has_budget: bool
    month: int
    year: int
    limit: Decimal
    spent: Decimal
    remaining: Decimal
    utilization: Decimal
    exceeded: bool
    warning: bool


@dataclass
class CategoryBudgetStatus:
    category: str
    limit: Decimal
    spent: Decimal
    remaining: Decimal
    utilization: Decimal
    exceeded: bool
    warning: bool


def _check_month(month: int):
    if not 1 <= month <= 12:
        raise InvalidRequestError("Month must be between 1 and 12")


def calculate_utilization(spent: Decimal, limit: Decimal) -> Decimal:
    if limit is None or limit <= 0:
        raise InvalidRequestError("Monthly limit must be greater than zero")
    return (spent * HUNDRED / limit).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class BudgetService:
    def __init__(self, db: Database, transactions: TransactionService):
        self.db, self.tx = db, transactions

    def set_budget(self, limit, month: int, year: int) -> BudgetStatus:
        limit = parse_amount(limit, "Monthly limit")
        _check_month(month)
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO budgets (monthly_limit_cents, budget_month, budget_year) VALUES (?,?,?) "
                "ON CONFLICT(budget_month, budget_year) DO UPDATE SET monthly_limit_cents=excluded.monthly_limit_cents",
                (to_cents(limit), month, year))
        return self.get_status(month, year)

    def get_status(self, month: int, year: int) -> BudgetStatus:
        start, end = month_bounds(year, month)
        spent = self.tx.sum_by_type(EXPENSE, start, end)
        with self.db.connect() as conn:
            r = conn.execute("SELECT monthly_limit_cents FROM budgets WHERE budget_month=? AND budget_year=?",
                             (month, year)).fetchone()
        return self.build_status(month, year, from_cents(r[0]) if r else None, spent)

    @staticmethod
    def build_status(month: int, year: int, limit: Decimal | None, spent_raw) -> BudgetStatus:
        spent = money(spent_raw)
        zero = Decimal("0.00")
        if limit is None:
            return BudgetStatus(False, month, year, zero, spent, zero, zero, False, False)
        limit = money(limit)
        utilization = calculate_utilization(spent, limit)
        exceeded = spent > limit
        warning = (not exceeded) and utilization >= WARNING_THRESHOLD
        return BudgetStatus(True, month, year, limit, spent, limit - spent, utilization, exceeded, warning)


class CategoryBudgetService:
    def __init__(self, db: Database, transactions: TransactionService):
        self.db, self.tx = db, transactions

    def list(self, month: int, year: int) -> list[CategoryBudgetStatus]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT category, monthly_limit_cents FROM category_budgets "
                "WHERE budget_month=? AND budget_year=? ORDER BY category", (month, year)).fetchall()
        return [self._status(r["category"], from_cents(r["monthly_limit_cents"]), month, year) for r in rows]

    def save(self, category: str, limit, month: int, year: int) -> CategoryBudgetStatus:
        limit = parse_amount(limit, "Category budget limit")
        _check_month(month)
        if not 2000 <= year <= 2100:
            raise InvalidRequestError("Year must be between 2000 and 2100")
        if not is_valid_category(EXPENSE, category):
            raise InvalidRequestError(f"Unknown expense category: {category}")
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO category_budgets (category, monthly_limit_cents, budget_month, budget_year) "
                "VALUES (?,?,?,?) ON CONFLICT(category, budget_month, budget_year) "
                "DO UPDATE SET monthly_limit_cents=excluded.monthly_limit_cents",
                (category, to_cents(limit), month, year))
        return self._status(category, limit, month, year)

    def _status(self, category, limit, month, year) -> CategoryBudgetStatus:
        start, end = month_bounds(year, month)
        spent = self.tx.sum_by_type(EXPENSE, start, end, category)
        utilization = calculate_utilization(spent, limit)
        exceeded = spent > limit
        return CategoryBudgetStatus(category, limit, spent, limit - spent, utilization, exceeded,
                                    (not exceeded) and utilization >= WARNING_THRESHOLD)


@dataclass
class DashboardSummary:
    total_income: Decimal
    total_expenses: Decimal
    balance: Decimal
    monthly_income: Decimal
    monthly_spending: Decimal
    budget: BudgetStatus
    recent: list[Transaction]
    expense_by_category: list[tuple[str, Decimal]]


@dataclass
class MonthlyTotal:
    year: int
    month: int
    label: str
    income: Decimal
    expense: Decimal


@dataclass
class Analytics:
    monthly: list[MonthlyTotal]
    expense_by_category: list[tuple[str, Decimal]]
    month: int
    year: int


class DashboardService:
    def __init__(self, transactions: TransactionService, budgets: BudgetService):
        self.tx, self.budgets = transactions, budgets

    def summary(self, year: int, month: int) -> DashboardSummary:
        start, end = month_bounds(year, month)
        income, expenses = self.tx.total_income(), self.tx.total_expenses()
        return DashboardSummary(
            total_income=income, total_expenses=expenses, balance=income - expenses,
            monthly_income=self.tx.sum_by_type(INCOME, start, end),
            monthly_spending=self.tx.sum_by_type(EXPENSE, start, end),
            budget=self.budgets.get_status(month, year),
            recent=self.tx.recent(5),
            expense_by_category=self.tx.sum_by_category(EXPENSE, start, end))

    def analytics(self, year: int, month: int) -> Analytics:
        monthly = []
        for i in range(TREND_MONTHS - 1, -1, -1):
            y, m = add_months(year, month, -i)
            start, end = month_bounds(y, m)
            monthly.append(MonthlyTotal(y, m, month_label(y, m),
                                        self.tx.sum_by_type(INCOME, start, end),
                                        self.tx.sum_by_type(EXPENSE, start, end)))
        start, end = month_bounds(year, month)
        return Analytics(monthly, self.tx.sum_by_category(EXPENSE, start, end), month, year)


class SettingsService:
    def __init__(self, db: Database):
        self.db = db

    def get(self, key: str, default: str = "") -> str:
        with self.db.connect() as conn:
            r = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def set(self, key: str, value: str) -> None:
        with self.db.connect() as conn:
            conn.execute("INSERT INTO app_settings (key, value) VALUES (?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    @property
    def currency(self) -> str:
        return self.get("currency", "INR")

    @property
    def name(self) -> str:
        return self.get("name", "")

    def save_preferences(self, name: str, currency: str) -> None:
        if currency not in CURRENCIES:
            raise InvalidRequestError("Unsupported currency")
        if len(name.strip()) > 40:
            raise InvalidRequestError("Name must be at most 40 characters")
        self.set("name", name.strip())
        self.set("currency", currency)


HEADERS = ["Date", "Title", "Type", "Category", "Amount", "Description", "Notes"]


@dataclass
class ImportRow:
    line: int
    data: dict
    issue: str | None = None


def export_csv(transactions: list[Transaction], notes: NoteService) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(HEADERS)
    for t in transactions:
        user_notes = [n.text for n in notes.list_for_transaction(t.id, newest_first=False) if not n.is_system]
        w.writerow([t.date.isoformat(), t.title, t.type, t.category, f"{t.amount:.2f}",
                    t.description or "", " | ".join(user_notes)])
    return buf.getvalue()


def preview_import(text: str, svc: TransactionService) -> list[ImportRow]:
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if not reader.fieldnames or not {"Date", "Title", "Type", "Category", "Amount"} <= set(reader.fieldnames):
        raise ExpenseTrackError("CSV must have the columns: Date, Title, Type, Category, Amount "
                                "(optional: Description, Notes)")
    rows = []
    for i, raw in enumerate(reader, start=2):
        row = ImportRow(i, {k: (v or "").strip() for k, v in raw.items() if k})
        try:
            d = row.data
            d["_date"] = date.fromisoformat(d["Date"])
            d["Type"] = d["Type"].upper()
            svc._validate(d["Title"], d["Amount"], d["Type"], d["Category"], d["_date"], d.get("Description"))
        except ValueError:
            row.issue = "Date must be YYYY-MM-DD"
        except ExpenseTrackError as e:
            row.issue = str(e)
        rows.append(row)
    return rows


def import_rows(rows: list[ImportRow], svc: TransactionService) -> int:
    count = 0
    for r in rows:
        if r.issue:
            continue
        d = r.data
        svc.add(d["Title"], d["Amount"], d["Type"], d["Category"], d["_date"], d.get("Description"),
                note=d.get("Notes") or None, history_text="Transaction imported from CSV")
        count += 1
    return count


class Services:
    def __init__(self, db: Database | None = None):
        self.db = db or Database()
        self.transactions = TransactionService(self.db)
        self.notes = NoteService(self.db)
        self.budgets = BudgetService(self.db, self.transactions)
        self.category_budgets = CategoryBudgetService(self.db, self.transactions)
        self.dashboard = DashboardService(self.transactions, self.budgets)
        self.recurring = RecurringService(self.db)
        self.goals = SavingsGoalService(self.db)
        self.settings = SettingsService(self.db)

    def fmt(self, amount) -> str:
        return format_money(amount, self.settings.currency)


csv_export = export_csv
csv_preview = preview_import
