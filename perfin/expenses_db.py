"""Import reimbursed expenses from the expenses app's SQLite database.

The expenses portal (../expenses) stores employee expenses in expenses.db;
when an admin marks one as reimbursed, the payout becomes a personal expense
worth tracking in the sheet.

Set expenses_db in config.json to the database location: a local path, or an
ssh location like host:/path (fetched with scp into expenses.db next to this
script on each run and deleted afterwards).

Expenses have no purchase time and the sheet row carries the expense date, so
the dedup pass in perfin.py matches them by date, amount and currency alone.
"""

import logging
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DAYS = 7

HERE = Path(__file__).parent
CACHE = HERE / "expenses.db"

log = logging.getLogger("perfin")


def database(config: dict) -> Path:
    location = config["expenses_db"]
    if ":" in location:
        log.info("Fetching expenses database from %s", location)
        if subprocess.run(["scp", "-q", location, str(CACHE)]).returncode != 0:
            sys.exit(f"Could not fetch the expenses database from {location}")
        return CACHE
    path = Path(location).expanduser()
    if not path.exists():
        sys.exit(f"Expenses database not found at {path}")
    return path


def fetch(config: dict, days: int = DAYS) -> list[dict]:
    path = database(config)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    # The scp'd cache is a private snapshot, so it can be opened as immutable,
    # which keeps sqlite from creating -shm/-wal sidecar files. A local path
    # may be written concurrently by the expenses app, so only read-only there.
    query = "immutable=1" if path == CACHE else "mode=ro"
    conn = sqlite3.connect(f"file:{path}?{query}", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT date, amount, currency, description, user_email FROM expenses"
            " WHERE reimbursed = 1 AND date >= ? ORDER BY date",
            (since,),
        ).fetchall()
    finally:
        conn.close()
        CACHE.unlink(missing_ok=True)
    log.info("Found %d reimbursed expense(s) since %s", len(rows), since)
    return [
        {
            "date": row["date"],
            "time": "",
            "amount": row["amount"],
            "currency": row["currency"],
            "desc": row["description"],
            "source": row["user_email"],
            "notes": [],
        }
        for row in rows
    ]
