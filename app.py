import os
from datetime import datetime, date
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, flash, g
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(BASE_DIR, "schema.sql")
def init_db():
    db = sqlite3.connect("expenses.db")
    with open("schema.sql") as f:
        db.executescript(f.read())
    db.close()

# Run this once when starting the app
init_db()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "expenses.db")

app = Flask(__name__)
app.secret_key = "change-this-secret-key-before-real-use"

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
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            color TEXT NOT NULL DEFAULT '#22304A',
            created_at TEXT NOT NULL,
            UNIQUE(user_id, name),
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            category_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            note TEXT,
            spent_on TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE CASCADE
        )
    """)
    db.commit()
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

        db = get_db()
        existing = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if existing:
            flash("That username is already taken.", "error")
            return render_template("register.html")

        password_hash = generate_password_hash(password)
        cur = db.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, password_hash, datetime.utcnow().isoformat()),
        )
        user_id = cur.lastrowid

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

    categories = db.execute(
        "SELECT * FROM categories WHERE user_id = ? ORDER BY name", (user_id,)
    ).fetchall()

    query = """
        SELECT expenses.*, categories.name AS category_name, categories.color AS category_color
        FROM expenses
        JOIN categories ON categories.id = expenses.category_id
        WHERE expenses.user_id = ? AND strftime('%Y-%m', expenses.spent_on) = ?
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
            AND strftime('%Y-%m', expenses.spent_on) = ?
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


if __name__ == "__main__":
    init_db()
    app.run(debug=True, host="127.0.0.1", port=5000)
