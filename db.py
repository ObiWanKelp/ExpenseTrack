from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("EXPENSETRACK_DB", "expensetrack.db")
SCHEMA = r"""
CREATE TABLE IF NOT EXISTS users (
    user_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL,
    email    TEXT NOT NULL UNIQUE,
    password TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    transaction_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER REFERENCES users(user_id),
    title            TEXT    NOT NULL,
    amount_cents     INTEGER NOT NULL CHECK (amount_cents > 0),
    type             TEXT    NOT NULL CHECK (type IN ('INCOME','EXPENSE')),
    category         TEXT    NOT NULL,
    transaction_date TEXT    NOT NULL,
    description      TEXT,
    savings_goal_id  INTEGER REFERENCES savings_goals(goal_id) ON DELETE SET NULL,
    savings_contribution_cents INTEGER NOT NULL DEFAULT 0 CHECK (savings_contribution_cents >= 0),
    recurring_id     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_transactions_date      ON transactions(transaction_date);
CREATE INDEX IF NOT EXISTS idx_transactions_type_date ON transactions(type, transaction_date);


CREATE TABLE IF NOT EXISTS transaction_notes (
    note_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id INTEGER NOT NULL REFERENCES transactions(transaction_id) ON DELETE CASCADE,
    note           TEXT    NOT NULL,
    kind           TEXT    NOT NULL DEFAULT 'USER' CHECK (kind IN ('USER','SYSTEM')),
    created_at     TEXT    NOT NULL,
    updated_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_notes_transaction ON transaction_notes(transaction_id, created_at);

CREATE TABLE IF NOT EXISTS budgets (
    budget_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER REFERENCES users(user_id),
    monthly_limit_cents INTEGER NOT NULL CHECK (monthly_limit_cents > 0),
    budget_month  INTEGER NOT NULL CHECK (budget_month BETWEEN 1 AND 12),
    budget_year   INTEGER NOT NULL,
    UNIQUE (budget_month, budget_year)
);

CREATE TABLE IF NOT EXISTS category_budgets (
    category_budget_id INTEGER PRIMARY KEY AUTOINCREMENT,
    category      TEXT    NOT NULL,
    monthly_limit_cents INTEGER NOT NULL CHECK (monthly_limit_cents > 0),
    budget_month  INTEGER NOT NULL CHECK (budget_month BETWEEN 1 AND 12),
    budget_year   INTEGER NOT NULL,
    UNIQUE (category, budget_month, budget_year)
);

CREATE TABLE IF NOT EXISTS recurring_transactions (
    recurring_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT    NOT NULL,
    amount_cents  INTEGER NOT NULL CHECK (amount_cents > 0),
    type          TEXT    NOT NULL CHECK (type IN ('INCOME','EXPENSE')),
    category      TEXT    NOT NULL,
    description   TEXT,
    schedule_day  INTEGER NOT NULL CHECK (schedule_day BETWEEN 1 AND 31),
    next_run_date TEXT    NOT NULL,
    end_date      TEXT,
    savings_goal_id INTEGER REFERENCES savings_goals(goal_id) ON DELETE SET NULL,
    savings_contribution_cents INTEGER NOT NULL DEFAULT 0 CHECK (savings_contribution_cents >= 0)
);
CREATE INDEX IF NOT EXISTS idx_recurring_next_run ON recurring_transactions(next_run_date);

CREATE TABLE IF NOT EXISTS savings_goals (
    goal_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT    NOT NULL,
    target_cents  INTEGER NOT NULL CHECK (target_cents > 0),
    saved_cents   INTEGER NOT NULL DEFAULT 0 CHECK (saved_cents >= 0),
    target_date   TEXT
);


CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class Database:
    def __init__(self, path: str | None = None):
        self.path = path or DB_PATH
        self.init()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init(self):
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._add_column(conn, "transactions", "savings_goal_id",
                             "INTEGER REFERENCES savings_goals(goal_id) ON DELETE SET NULL")
            self._add_column(conn, "transactions", "recurring_id", "INTEGER")
            self._add_column(conn, "recurring_transactions", "savings_goal_id",
                             "INTEGER REFERENCES savings_goals(goal_id) ON DELETE SET NULL")
            if self._add_column(conn, "transactions", "savings_contribution_cents",
                                "INTEGER NOT NULL DEFAULT 0"):
                conn.execute("UPDATE transactions SET savings_contribution_cents=amount_cents "
                             "WHERE savings_goal_id IS NOT NULL")
            if self._add_column(conn, "recurring_transactions", "savings_contribution_cents",
                                "INTEGER NOT NULL DEFAULT 0"):
                conn.execute("UPDATE recurring_transactions SET savings_contribution_cents=amount_cents "
                             "WHERE savings_goal_id IS NOT NULL")

    @staticmethod
    def _add_column(conn, table, name, definition):
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if name not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            return True
        return False
