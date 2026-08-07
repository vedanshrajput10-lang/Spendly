import os
from datetime import datetime, date
from functools import wraps

import psycopg2
import psycopg2.extras
from flask import Flask, render_template, request, redirect, url_for, session, flash, g
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)

_secret = os.environ.get("SESSION_SECRET")
if not _secret:
    raise RuntimeError(
        "SESSION_SECRET environment variable is not set. "
        "Add it as a Replit secret before starting the app."
    )
app.secret_key = _secret

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is not set.")

DEFAULT_CATEGORIES = [
    ("Food", "#B5533C"),
    ("Transport", "#7C9473"),
    ("Bills", "#22304A"),
    ("Shopping", "#C08A2E"),
    ("Other", "#6B6355"),
]


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = psycopg2.connect(DATABASE_URL)
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query(sql, params=(), one=False, commit=False):
    """Run a SQL statement and return results as dicts."""
    db = get_db()
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        if commit:
            db.commit()
            return cur.lastrowid if cur.description else None
        if one:
            return cur.fetchone()
        return cur.fetchall()


def execute(sql, params=(), returning=False):
    """Run a mutating statement; returns the first column of the first row if RETURNING."""
    db = get_db()
    with db.cursor() as cur:
        cur.execute(sql, params)
        db.commit()
        if returning:
            row = cur.fetchone()
            return row[0] if row else None


def init_db():
    db = psycopg2.connect(DATABASE_URL)
    cur = db.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            color TEXT NOT NULL DEFAULT '#22304A',
            created_at TIMESTAMP NOT NULL,
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
            created_at TIMESTAMP NOT NULL
        )
    """)
    db.commit()
    cur.close()
    db.close()


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

        existing = query("SELECT id FROM users WHERE username = %s", (username,), one=True)
        if existing:
            flash("That username is already taken.", "error")
            return render_template("register.html")

        password_hash = generate_password_hash(password)
        user_id = execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (%s, %s, %s) RETURNING id",
            (username, password_hash, datetime.utcnow()),
            returning=True,
        )

        for name, color in DEFAULT_CATEGORIES:
            execute(
                "INSERT INTO categories (user_id, name, color, created_at) VALUES (%s, %s, %s, %s)",
                (user_id, name, color, datetime.utcnow()),
            )

        flash("Account created. You can log in now.", "success")
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = query("SELECT * FROM users WHERE username = %s", (username,), one=True)
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
    user_id = session["user_id"]

    category_filter = request.args.get("category", type=int)
    month = request.args.get("month", date.today().strftime("%Y-%m"))

    categories = query(
        "SELECT * FROM categories WHERE user_id = %s ORDER BY name", (user_id,)
    )

    sql = """
        SELECT expenses.*, categories.name AS category_name, categories.color AS category_color
        FROM expenses
        JOIN categories ON categories.id = expenses.category_id
        WHERE expenses.user_id = %s AND to_char(expenses.spent_on, 'YYYY-MM') = %s
    """
    params = [user_id, month]
    if category_filter:
        sql += " AND expenses.category_id = %s"
        params.append(category_filter)
    sql += " ORDER BY expenses.spent_on DESC, expenses.id DESC"

    expenses = query(sql, params)

    totals_rows = query("""
        SELECT categories.id, categories.name, categories.color,
               COALESCE(SUM(expenses.amount), 0) AS total
        FROM categories
        LEFT JOIN expenses ON expenses.category_id = categories.id
            AND to_char(expenses.spent_on, 'YYYY-MM') = %s
        WHERE categories.user_id = %s
        GROUP BY categories.id, categories.name, categories.color
        ORDER BY total DESC
    """, (month, user_id))

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
        today=date.today().isoformat(),
    )


@app.route("/expenses/add", methods=["POST"])
@login_required
def add_expense():
    user_id = session["user_id"]

    category_id = request.form.get("category_id", type=int)
    amount = request.form.get("amount", type=float)
    note = request.form.get("note", "").strip()
    spent_on = request.form.get("spent_on") or date.today().isoformat()

    if not category_id or not amount or amount <= 0:
        flash("Please choose a section and enter a valid amount.", "error")
        return redirect(url_for("dashboard", month=spent_on[:7]))

    owns_category = query(
        "SELECT id FROM categories WHERE id = %s AND user_id = %s", (category_id, user_id), one=True
    )
    if not owns_category:
        flash("That section does not exist.", "error")
        return redirect(url_for("dashboard"))

    execute(
        "INSERT INTO expenses (user_id, category_id, amount, note, spent_on, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (user_id, category_id, amount, note, spent_on, datetime.utcnow()),
    )
    flash("Expense added.", "success")
    return redirect(url_for("dashboard", month=spent_on[:7]))


@app.route("/expenses/<int:expense_id>/delete", methods=["POST"])
@login_required
def delete_expense(expense_id):
    user_id = session["user_id"]
    expense = query(
        "SELECT * FROM expenses WHERE id = %s AND user_id = %s", (expense_id, user_id), one=True
    )
    if expense:
        execute("DELETE FROM expenses WHERE id = %s", (expense_id,))
        flash("Expense deleted.", "success")
    return redirect(url_for("dashboard", month=request.form.get("month", date.today().strftime("%Y-%m"))))


@app.route("/expenses/<int:expense_id>/edit", methods=["GET", "POST"])
@login_required
def edit_expense(expense_id):
    user_id = session["user_id"]
    expense = query(
        "SELECT * FROM expenses WHERE id = %s AND user_id = %s", (expense_id, user_id), one=True
    )
    if not expense:
        flash("Expense not found.", "error")
        return redirect(url_for("dashboard"))

    categories = query(
        "SELECT * FROM categories WHERE user_id = %s ORDER BY name", (user_id,)
    )

    if request.method == "POST":
        category_id = request.form.get("category_id", type=int)
        amount = request.form.get("amount", type=float)
        note = request.form.get("note", "").strip()
        spent_on = request.form.get("spent_on")

        if not category_id or not amount or amount <= 0 or not spent_on:
            flash("Please fill in all fields with a valid amount.", "error")
            return render_template("edit_expense.html", expense=expense, categories=categories)

        execute(
            "UPDATE expenses SET category_id = %s, amount = %s, note = %s, spent_on = %s WHERE id = %s",
            (category_id, amount, note, spent_on, expense_id),
        )
        flash("Expense updated.", "success")
        return redirect(url_for("dashboard", month=spent_on[:7]))

    return render_template("edit_expense.html", expense=expense, categories=categories)


# ---------------------------------------------------------------------------
# Categories (sections)
# ---------------------------------------------------------------------------

@app.route("/categories/add", methods=["POST"])
@login_required
def add_category():
    user_id = session["user_id"]
    name = request.form.get("name", "").strip()
    color = request.form.get("color", "#22304A")

    if not name:
        flash("Section name cannot be empty.", "error")
        return redirect(url_for("dashboard"))

    existing = query(
        "SELECT id FROM categories WHERE user_id = %s AND name = %s", (user_id, name), one=True
    )
    if existing:
        flash("You already have a section with that name.", "error")
        return redirect(url_for("dashboard"))

    execute(
        "INSERT INTO categories (user_id, name, color, created_at) VALUES (%s, %s, %s, %s)",
        (user_id, name, color, datetime.utcnow()),
    )
    flash(f'Section "{name}" created.', "success")
    return redirect(url_for("dashboard"))


@app.route("/categories/<int:category_id>/delete", methods=["POST"])
@login_required
def delete_category(category_id):
    user_id = session["user_id"]
    category = query(
        "SELECT * FROM categories WHERE id = %s AND user_id = %s", (category_id, user_id), one=True
    )
    if category:
        execute("DELETE FROM categories WHERE id = %s", (category_id,))
        flash(f'Section "{category["name"]}" and its expenses were deleted.', "success")
    return redirect(url_for("dashboard"))


# Initialise DB on startup regardless of how the app is launched
init_db()

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=5000)
