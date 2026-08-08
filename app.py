import os
from datetime import datetime, date
from functools import wraps

import psycopg2
import psycopg2.extras
from flask import Flask, render_template, request, redirect, url_for, session, flash, g
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", "change-this-secret-key-before-real-use")

DEFAULT_CATEGORIES = [
    ("Food", "#B5533C"),
    ("Transport", "#7C9473"),
    ("Bills", "#22304A"),
    ("Shopping", "#C08A2E"),
    ("Other", "#6B6355"),
]

# Replit's built-in database (and Render's Postgres add-on) both provide this
# environment variable automatically once the database is attached to the app.
DATABASE_URL = os.environ.get("DATABASE_URL")


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

class Db:
    """Thin wrapper so the rest of the app can keep using the same
    db.execute(...).fetchone() / .fetchall() style as before, while actually
    talking to PostgreSQL under the hood (dict-like rows, ? -> %s, etc.)."""

    def __init__(self, conn):
        self.conn = conn

    def execute(self, query, params=()):
        query = query.replace("?", "%s")
        cur = self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(query, params)
        return cur

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()


def get_db():
    if "db" not in g:
        if not DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL is not set. Attach a PostgreSQL database to this "
                "app (Replit: sidebar -> Database. Render: add a PostgreSQL "
                "instance and link its connection string as DATABASE_URL)."
            )
        conn = psycopg2.connect(DATABASE_URL)
        g.db = Db(conn)
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    if not DATABASE_URL:
        # No database attached yet - skip silently so the app can still start
        # and show a clear error on first use instead of crashing at boot.
        return
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            color TEXT NOT NULL DEFAULT '#22304A',
            created_at TEXT NOT NULL,
            UNIQUE(user_id, name)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
            amount REAL NOT NULL,
            note TEXT,
            spent_on DATE NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            username TEXT,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


# Always initialize on startup, regardless of how the app is launched
# (python app.py locally, or gunicorn in production) - this was the root
# cause of the "no such table" crashes seen on both Replit and Render.
init_db()


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_user():
    return {"current_username": session.get("username")}


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")

        if not username or not password:
            flash("Username and password are required.", "error")
            return render_template("register.html")
        if password != confirm:
            flash("Passwords do not match.", "error")
            return render_template("register.html")

        db = get_db()
        existing = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if existing:
            flash("That username is already taken.", "error")
            return render_template("register.html")

        password_hash = generate_password_hash(password)
        row = db.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?) RETURNING id",
            (username, password_hash, datetime.utcnow().isoformat()),
        ).fetchone()
        user_id = row["id"]

        for name, color in DEFAULT_CATEGORIES:
            db.execute(
                "INSERT INTO categories (user_id, name, color, created_at) VALUES (?, ?, ?, ?)",
                (user_id, name, color, datetime.utcnow().isoformat()),
            )
        db.commit()

        flash("Account created. You can log in now.", "success")
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        db = get_db()
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Incorrect username or password.", "error")
            return render_template("login.html")

        session.clear()
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        return redirect(url_for("dashboard"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Dashboard / expenses
# ---------------------------------------------------------------------------

@app.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    user_id = session["user_id"]

    category_filter = request.args.get("category", type=int)
    month = request.args.get("month", date.today().strftime("%Y-%m"))
    search_query = request.args.get("q", "").strip()

    categories = db.execute(
        "SELECT * FROM categories WHERE user_id = ? ORDER BY name", (user_id,)
    ).fetchall()

    # A search searches across ALL months (not just the selected one), since
    # the point is finding something regardless of when it happened. Each
    # result still shows its own date so context isn't lost.
    if search_query:
        query = """
            SELECT expenses.*, categories.name AS category_name, categories.color AS category_color
            FROM expenses
            JOIN categories ON categories.id = expenses.category_id
            WHERE expenses.user_id = ? AND expenses.note ILIKE ?
        """
        params = [user_id, f"%{search_query}%"]
        if category_filter:
            query += " AND expenses.category_id = ?"
            params.append(category_filter)
        query += " ORDER BY expenses.spent_on DESC, expenses.id DESC"
        expenses = db.execute(query, params).fetchall()
    else:
        query = """
            SELECT expenses.*, categories.name AS category_name, categories.color AS category_color
            FROM expenses
            JOIN categories ON categories.id = expenses.category_id
            WHERE expenses.user_id = ? AND to_char(expenses.spent_on, 'YYYY-MM') = ?
        """
        params = [user_id, month]
        if category_filter:
            query += " AND expenses.category_id = ?"
            params.append(category_filter)
        query += " ORDER BY expenses.spent_on DESC, expenses.id DESC"
        expenses = db.execute(query, params).fetchall()

    totals_rows = db.execute("""
        SELECT categories.id, categories.name, categories.color,
               COALESCE(SUM(expenses.amount), 0) AS total
        FROM categories
        LEFT JOIN expenses ON expenses.category_id = categories.id
            AND to_char(expenses.spent_on, 'YYYY-MM') = ?
        WHERE categories.user_id = ?
        GROUP BY categories.id
        ORDER BY total DESC
    """, (month, user_id)).fetchall()

    month_total = sum(row["total"] for row in totals_rows)
    max_total = max((row["total"] for row in totals_rows), default=0)

    return render_template(
        "dashboard.html",
        categories=categories,
        expenses=expenses,
        totals_rows=totals_rows,
        month_total=month_total,
        max_total=max_total,
        selected_category=category_filter,
        selected_month=month,
        search_query=search_query,
        today=date.today().isoformat(),
    )


@app.route("/expenses/add", methods=["POST"])
@login_required
def add_expense():
    db = get_db()
    user_id = session["user_id"]

    category_id = request.form.get("category_id", type=int)
    amount = request.form.get("amount", type=float)
    note = request.form.get("note", "").strip()
    spent_on = request.form.get("spent_on") or date.today().isoformat()

    if not category_id or not amount or amount <= 0:
        flash("Please choose a section and enter a valid amount.", "error")
        return redirect(url_for("dashboard", month=spent_on[:7]))

    owns_category = db.execute(
        "SELECT id FROM categories WHERE id = ? AND user_id = ?", (category_id, user_id)
    ).fetchone()
    if not owns_category:
        flash("That section does not exist.", "error")
        return redirect(url_for("dashboard"))

    db.execute(
        "INSERT INTO expenses (user_id, category_id, amount, note, spent_on, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, category_id, amount, note, spent_on, datetime.utcnow().isoformat()),
    )
    db.commit()
    flash("Expense added.", "success")
    return redirect(url_for("dashboard", month=spent_on[:7]))


@app.route("/expenses/<int:expense_id>/delete", methods=["POST"])
@login_required
def delete_expense(expense_id):
    db = get_db()
    user_id = session["user_id"]
    expense = db.execute(
        "SELECT * FROM expenses WHERE id = ? AND user_id = ?", (expense_id, user_id)
    ).fetchone()
    if expense:
        db.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
        db.commit()
        flash("Expense deleted.", "success")
    return redirect(url_for("dashboard", month=request.form.get("month", date.today().strftime("%Y-%m"))))


@app.route("/expenses/<int:expense_id>/edit", methods=["GET", "POST"])
@login_required
def edit_expense(expense_id):
    db = get_db()
    user_id = session["user_id"]
    expense = db.execute(
        "SELECT * FROM expenses WHERE id = ? AND user_id = ?", (expense_id, user_id)
    ).fetchone()
    if not expense:
        flash("Expense not found.", "error")
        return redirect(url_for("dashboard"))

    categories = db.execute(
        "SELECT * FROM categories WHERE user_id = ? ORDER BY name", (user_id,)
    ).fetchall()

    if request.method == "POST":
        category_id = request.form.get("category_id", type=int)
        amount = request.form.get("amount", type=float)
        note = request.form.get("note", "").strip()
        spent_on = request.form.get("spent_on")

        if not category_id or not amount or amount <= 0 or not spent_on:
            flash("Please fill in all fields with a valid amount.", "error")
            return render_template("edit_expense.html", expense=expense, categories=categories)

        db.execute(
            "UPDATE expenses SET category_id = ?, amount = ?, note = ?, spent_on = ? WHERE id = ?",
            (category_id, amount, note, spent_on, expense_id),
        )
        db.commit()
        flash("Expense updated.", "success")
        return redirect(url_for("dashboard", month=spent_on[:7]))

    return render_template("edit_expense.html", expense=expense, categories=categories)


# ---------------------------------------------------------------------------
# Categories (sections)
# ---------------------------------------------------------------------------

@app.route("/categories/add", methods=["POST"])
@login_required
def add_category():
    db = get_db()
    user_id = session["user_id"]
    name = request.form.get("name", "").strip()
    color = request.form.get("color", "#22304A")

    if not name:
        flash("Section name cannot be empty.", "error")
        return redirect(url_for("dashboard"))

    existing = db.execute(
        "SELECT id FROM categories WHERE user_id = ? AND name = ?", (user_id, name)
    ).fetchone()
    if existing:
        flash("You already have a section with that name.", "error")
        return redirect(url_for("dashboard"))

    db.execute(
        "INSERT INTO categories (user_id, name, color, created_at) VALUES (?, ?, ?, ?)",
        (user_id, name, color, datetime.utcnow().isoformat()),
    )
    db.commit()
    flash(f'Section "{name}" created.', "success")
    return redirect(url_for("dashboard"))


@app.route("/categories/<int:category_id>/delete", methods=["POST"])
@login_required
def delete_category(category_id):
    db = get_db()
    user_id = session["user_id"]
    category = db.execute(
        "SELECT * FROM categories WHERE id = ? AND user_id = ?", (category_id, user_id)
    ).fetchone()
    if category:
        db.execute("DELETE FROM categories WHERE id = ?", (category_id,))
        db.commit()
        flash(f'Section "{category["name"]}" and its expenses were deleted.', "success")
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

@app.route("/expenses/export")
@login_required
def export_expenses():
    import csv
    import io

    db = get_db()
    user_id = session["user_id"]

    category_filter = request.args.get("category", type=int)
    month = request.args.get("month", date.today().strftime("%Y-%m"))
    search_query = request.args.get("q", "").strip()

    if search_query:
        query = """
            SELECT expenses.spent_on, categories.name AS category_name,
                   expenses.amount, expenses.note
            FROM expenses
            JOIN categories ON categories.id = expenses.category_id
            WHERE expenses.user_id = ? AND expenses.note ILIKE ?
        """
        params = [user_id, f"%{search_query}%"]
        filename_part = f"search-{search_query}"
    else:
        query = """
            SELECT expenses.spent_on, categories.name AS category_name,
                   expenses.amount, expenses.note
            FROM expenses
            JOIN categories ON categories.id = expenses.category_id
            WHERE expenses.user_id = ? AND to_char(expenses.spent_on, 'YYYY-MM') = ?
        """
        params = [user_id, month]
        filename_part = month

    if category_filter:
        query += " AND expenses.category_id = ?"
        params.append(category_filter)
    query += " ORDER BY expenses.spent_on DESC, expenses.id DESC"

    rows = db.execute(query, params).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date", "Section", "Amount", "Note"])
    for row in rows:
        writer.writerow([row["spent_on"], row["category_name"], row["amount"], row["note"] or ""])

    csv_data = output.getvalue()
    safe_name = "".join(c for c in filename_part if c.isalnum() or c in ("-", "_")) or "expenses"

    from flask import Response
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=spendly-{safe_name}.csv"},
    )


# ---------------------------------------------------------------------------
# About
# ---------------------------------------------------------------------------

@app.route("/about")
def about():
    return render_template("about.html")


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------

@app.route("/feedback", methods=["GET", "POST"])
def feedback():
    if request.method == "POST":
        message = request.form.get("message", "").strip()
        if not message:
            flash("Please write something before submitting.", "error")
            return render_template("feedback.html")

        db = get_db()
        user_id = session.get("user_id")
        username = session.get("username")
        db.execute(
            "INSERT INTO feedback (user_id, username, message, created_at) VALUES (?, ?, ?, ?)",
            (user_id, username, message, datetime.utcnow().isoformat()),
        )
        db.commit()
        flash("Thanks for the feedback! It's been sent through.", "success")
        return redirect(url_for("feedback"))

    return render_template("feedback.html")


if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000)
