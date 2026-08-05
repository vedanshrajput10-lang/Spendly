# Ledger — Daily Expense Tracker

A simple, local, offline-friendly web app for tracking daily expenses,
organized into your own custom sections (categories).

## Features

- **Accounts** — each person registers with a username/password; everyone's
  data is kept separate.
- **Custom sections** — create as many expense sections as you want
  (e.g. Food, Transport, Rent, Subscriptions), each with its own color tag.
  Five starter sections are created automatically when you sign up, and you
  can rename the idea by adding new ones or removing ones you don't need.
- **Log expenses** — amount, date, section, and an optional note.
- **Edit / delete** any expense.
- **Monthly view** — switch months with the date picker; see a running
  total for the month and a per-section breakdown with bars.
- **Local storage** — everything is saved in a single SQLite file
  (`expenses.db`) next to the app. No internet connection or external
  service is required.

## Setup

1. Make sure you have Python 3.9+ installed.
2. Install the one dependency:

   ```bash
   pip install -r requirements.txt
   ```

3. Run the app:

   ```bash
   python app.py
   ```

4. Open your browser to **http://127.0.0.1:5000**

The first time you run it, `expenses.db` is created automatically in the
same folder — no extra setup needed.

## Project structure

```
expense_tracker/
├── app.py              # Flask app: routes, database logic
├── requirements.txt
├── expenses.db          # created automatically on first run
├── static/
│   └── style.css
└── templates/
    ├── base.html
    ├── login.html
    ├── register.html
    ├── dashboard.html
    └── edit_expense.html
```

## Notes / next steps you might want

- The `app.secret_key` in `app.py` is set to a placeholder. If you plan to
  deploy this somewhere other people can reach, change it to a long random
  string.
- Currently it runs in Flask's debug/dev server, which is fine for personal,
  local use. For anything beyond that, run it behind a production server
  (e.g. `waitress` or `gunicorn`).
- Ideas for extensions: CSV export, budgets/spending limits per section,
  recurring expenses, multi-currency support.
