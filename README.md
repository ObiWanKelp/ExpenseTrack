# ExpenseTrack

A simple expense tracker built with Python, Streamlit, and SQLite.

## Run

```bash
pip install -r requirements.txt
streamlit run app.py
```

The database file is created automatically. Transactions and recurring rules are stored in `expensetrack.db`.

The app is kept in three main files: `app.py` contains the pages, `db.py` handles SQLite, and `services.py` contains the app logic.

Run the checks with `python -m pytest`.
