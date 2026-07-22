"""Expenses submission portal: Flask + SQLite + Google Sign-In."""

import json
import os
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from flask import (
    Flask,
    Response,
    abort,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

APP_DIR = Path(__file__).parent
DB_PATH = APP_DIR / "expenses.db"
CURRENCIES = ("MXN", "USD", "EUR")
MAX_RECEIPT_BYTES = 10 * 1024 * 1024

CONFIG = json.loads((APP_DIR / "config.json").read_text())
DEV_LOGIN = os.environ.get("EXPENSES_DEV_LOGIN") == "1"

app = Flask(__name__)
app.secret_key = CONFIG["secret_key"]
app.config["MAX_CONTENT_LENGTH"] = MAX_RECEIPT_BYTES + 64 * 1024
if not DEV_LOGIN:
    # Behind the exe.dev TLS-terminating proxy: trust X-Forwarded-Proto/Host
    # so generated URLs (e.g. the Google login_uri) are https.
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)  # ty: ignore


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


with db() as _conn:
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY,
            user_email TEXT NOT NULL,
            date TEXT NOT NULL,
            amount TEXT NOT NULL,
            currency TEXT NOT NULL,
            description TEXT NOT NULL,
            receipt_name TEXT,
            receipt_blob BLOB,
            created_at TEXT NOT NULL,
            reimbursed INTEGER NOT NULL DEFAULT 0
        )
    """)
    # Migration for databases created before the reimbursed column existed.
    # ALTER TABLE is an ordinary WAL transaction, so Litestream replicates it
    # to S3 like any other write; no backup-side action is needed.
    _cols = {r[1] for r in _conn.execute("PRAGMA table_info(expenses)")}
    if "reimbursed" not in _cols:
        _conn.execute(
            "ALTER TABLE expenses ADD COLUMN reimbursed INTEGER NOT NULL DEFAULT 0"
        )


def current_user() -> str | None:
    return session.get("email")


def require_user() -> str:
    email = current_user()
    if not email:
        abort(401)
    return email


def is_admin(email: str) -> bool:
    return email in CONFIG["admin_emails"]


@app.get("/login")
def login():
    if current_user():
        return redirect(url_for("index"))
    return render_template(
        "login.html",
        google_client_id=CONFIG["google_client_id"],
        dev_login=DEV_LOGIN,
    )


@app.post("/auth/google")
def auth_google():
    credential = request.form.get("credential", "")
    try:
        info = id_token.verify_oauth2_token(
            credential, google_requests.Request(), CONFIG["google_client_id"]
        )
    except ValueError:
        abort(401, "Invalid Google token")
    email = info.get("email")
    if not email or not info.get("email_verified"):
        abort(401, "Google account has no verified email")
    if email not in CONFIG["allowed_emails"] and not is_admin(email):
        abort(403, f"{email} is not authorized to use this portal")
    session["email"] = email
    return redirect(url_for("index"))


if DEV_LOGIN:

    @app.get("/dev-login")
    def dev_login():
        session["email"] = request.args.get("email", "dev@example.com")
        return redirect(url_for("index"))


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
def index():
    email = current_user()
    if not email:
        return redirect(url_for("login"))
    admin = is_admin(email)
    user_filter = request.args.get("user", "") if admin else email

    query = "SELECT id, user_email, date, amount, currency, description, reimbursed, receipt_name IS NOT NULL AS has_receipt FROM expenses"
    params: tuple = ()
    if user_filter:
        query += " WHERE user_email = ?"
        params = (user_filter,)
    query += " ORDER BY date DESC, id DESC"

    with db() as conn:
        expenses = conn.execute(query, params).fetchall()
        users = [
            r["user_email"]
            for r in conn.execute(
                "SELECT DISTINCT user_email FROM expenses ORDER BY user_email"
            )
        ]

    totals: dict[str, Decimal] = {}
    for e in expenses:
        if e["reimbursed"]:
            continue
        totals[e["currency"]] = totals.get(e["currency"], Decimal(0)) + Decimal(
            e["amount"]
        )

    return render_template(
        "index.html",
        email=email,
        admin=admin,
        user_filter=user_filter,
        users=users,
        expenses=expenses,
        totals=sorted(totals.items()),
        currencies=CURRENCIES,
        today=date.today().isoformat(),
        last_currency=session.get("last_currency", "MXN"),
    )


@app.post("/submit")
def submit():
    email = require_user()

    expense_date = request.form.get("date", "")
    amount_raw = request.form.get("amount", "").strip()
    currency = request.form.get("currency", "")
    description = request.form.get("description", "").strip()

    try:
        date.fromisoformat(expense_date)
    except ValueError:
        abort(400, "Invalid date")
    try:
        amount = Decimal(amount_raw)
        if amount <= 0:
            raise InvalidOperation
    except InvalidOperation:
        abort(400, "Invalid amount")
    if currency not in CURRENCIES:
        abort(400, "Invalid currency")
    if not description:
        abort(400, "Description is required")

    receipt_name = None
    receipt_blob = None
    receipt = request.files.get("receipt")
    if receipt and receipt.filename:
        receipt_blob = receipt.read()
        if len(receipt_blob) > MAX_RECEIPT_BYTES:
            abort(400, "Receipt file too large (max 10 MB)")
        receipt_name = Path(receipt.filename).name

    with db() as conn:
        conn.execute(
            "INSERT INTO expenses (user_email, date, amount, currency, description,"
            " receipt_name, receipt_blob, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                email,
                expense_date,
                str(amount),
                currency,
                description,
                receipt_name,
                receipt_blob,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    session["last_currency"] = currency
    return redirect(url_for("index"))


@app.post("/reimburse/<int:expense_id>")
def reimburse(expense_id: int):
    email = require_user()
    if not is_admin(email):
        abort(403)
    with db() as conn:
        updated = conn.execute(
            "UPDATE expenses SET reimbursed = NOT reimbursed WHERE id = ?",
            (expense_id,),
        ).rowcount
    if not updated:
        abort(404)
    return redirect(request.referrer or url_for("index"))


@app.post("/delete/<int:expense_id>")
def delete(expense_id: int):
    email = require_user()
    with db() as conn:
        row = conn.execute(
            "SELECT user_email FROM expenses WHERE id = ?", (expense_id,)
        ).fetchone()
        if row is None:
            abort(404)
        if row["user_email"] != email and not is_admin(email):
            abort(403)
        conn.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
    return redirect(request.referrer or url_for("index"))


@app.get("/receipt/<int:expense_id>")
def receipt(expense_id: int):
    email = require_user()
    with db() as conn:
        row = conn.execute(
            "SELECT user_email, receipt_name, receipt_blob FROM expenses WHERE id = ?",
            (expense_id,),
        ).fetchone()
    if row is None or row["receipt_blob"] is None:
        abort(404)
    if row["user_email"] != email and not is_admin(email):
        abort(403)
    return Response(
        row["receipt_blob"],
        headers={
            "Content-Disposition": f'inline; filename="{row["receipt_name"]}"',
            "Content-Type": "application/octet-stream",
        },
    )


if __name__ == "__main__":
    # 0.0.0.0 so the exe.dev HTTPS proxy can reach it; the VM itself is
    # not directly exposed to the internet.
    host = "127.0.0.1" if DEV_LOGIN else "0.0.0.0"
    app.run(host=host, port=8000, debug=DEV_LOGIN)
