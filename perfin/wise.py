"""Fetch recent Wise transactions (all active currency balances) through the
Enable Banking API.

Reuses the Enable Banking application credentials set up for bcp.py; no extra
configuration beyond `"wise": true` in config.json. The first run opens a
consent flow (approved in the Wise app); later runs reuse the session stored
in wise_session.json. Each active currency balance appears as its own account
in the session, so all currencies come through one consent.
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bcp

HERE = Path(__file__).parent
SESSION_FILE = HERE / "wise_session.json"
SOURCE = "Wise"
ASPSP = {"name": "Wise", "country": "FR"}
DAYS = 7

# Wise reports balance movements that are not spending: conversions between
# own balances and top-ups. Matched against the transaction description.
INTERNAL = re.compile(r"\b(converted|topped up|added money|balance cashback)\b", re.I)

log = logging.getLogger("perfin")


def relevant(txs: list[dict], since: str) -> list[dict]:
    """Drop credits, conversions between own balances, top-ups and
    transactions booked before the fetch window."""
    kept = []
    for tx in txs:
        amount = tx["transaction_amount"]
        desc = bcp.description(tx)
        date = tx_date(tx)
        if date and date < since:
            reason = f"booked before fetch window ({date})"
        elif tx["credit_debit_indicator"] == "CRDT":
            reason = "credit"
        elif INTERNAL.search(desc):
            reason = "internal balance movement"
        else:
            kept.append(tx)
            continue
        log.info(
            "Skipping %s: %s %s %s %s",
            reason,
            date,
            amount["amount"],
            amount["currency"],
            desc,
        )
    return kept


def tx_date(tx: dict) -> str:
    return (
        tx.get("transaction_date")
        or tx.get("booking_date")
        or tx.get("value_date")
        or ""
    )


def normalize(tx: dict) -> dict:
    amount = tx["transaction_amount"]
    return {
        "date": tx_date(tx),
        "time": "",
        "amount": amount["amount"],
        "currency": amount["currency"],
        "desc": bcp.description(tx),
        "source": SOURCE,
        "notes": ["(pending)"] if tx.get("status") == "PDNG" else [],
    }


def fetch(config: dict, days: int = DAYS) -> list[dict]:
    headers = bcp.auth_headers(config)
    session = bcp.get_session(headers, ASPSP, SESSION_FILE)
    headers |= bcp.psu_headers()
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).date().isoformat()
    until = now.date().isoformat()
    log.info("Fetching Wise transactions since %s", since)
    result = []
    for uid in session["accounts"]:
        log.info("Processing Wise balance %s", uid)
        txs = [
            tx
            for status in ("BOOK", "PDNG")
            for tx in bcp.transactions(headers, uid, since, until, status)
        ]
        result += [normalize(tx) for tx in relevant(txs, since)]
    return result
