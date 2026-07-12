"""Fetch recent Millennium BCP transactions through the Enable Banking API.

Setup:
1. Create a free account at https://enablebanking.com and register an
   application (restricted production mode lets you access your own accounts
   for free). Save the application's private key as bcp_key.pem.
2. Put the application ID and key file name in config.json (see
   config.example.json).

The first run opens a bank consent flow; later runs reuse the session.
"""

import json
import logging
import re
import sys
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import jwt
import requests

API = "https://api.enablebanking.com"
HERE = Path(__file__).parent
SESSION_FILE = HERE / "session.json"
SOURCE = "Millennium BCP"
DAYS = 1

log = logging.getLogger("perfin")


def auth_headers(config: dict) -> dict:
    now = int(datetime.now(timezone.utc).timestamp())
    token = jwt.encode(
        {
            "iss": "enablebanking.com",
            "aud": "api.enablebanking.com",
            "iat": now,
            "exp": now + 3600,
        },
        (HERE / config["private_key_file"]).read_text(),
        algorithm="RS256",
        headers={"kid": config["application_id"]},
    )
    return {"Authorization": f"Bearer {token}"}


def psu_headers() -> dict:
    """Mark data fetches as user-attended; without these headers BCP limits
    background fetches to ~4 per account per day."""
    try:
        ip = requests.get("https://api.ipify.org", timeout=5).text.strip()
    except requests.RequestException:
        log.warning("Could not determine public IP; fetching without PSU headers")
        return {}
    return {
        "Psu-Ip-Address": ip,
        "Psu-User-Agent": "Mozilla/5.0 (X11; Linux x86_64) perfin/1.0",
    }


def api(method: str, path: str, headers: dict, fatal: bool = True, **kwargs) -> dict:
    r = requests.request(method, f"{API}{path}", headers=headers, **kwargs)
    if not r.ok:
        message = f"API error {r.status_code} on {path}: {r.text}"
        if fatal:
            sys.exit(message)
        log.warning(message)
        return {}
    return r.json()


def authorize(headers: dict) -> dict:
    app = api("GET", "/application", headers)
    body = {
        "access": {
            "valid_until": (datetime.now(timezone.utc) + timedelta(days=90)).isoformat()
        },
        "aspsp": {"name": "Millennium BCP", "country": "PT"},
        "state": str(uuid.uuid4()),
        "redirect_url": app["redirect_urls"][0],
        "psu_type": "personal",
    }
    auth = api("POST", "/auth", headers, json=body)
    print(
        f"Open this URL and authenticate with the bank:\n{auth['url']}\n",
        file=sys.stderr,
    )
    redirected = input("Paste the URL you were redirected to: ")
    code = parse_qs(urlparse(redirected).query)["code"][0]
    session = api("POST", "/sessions", headers, json={"code": code})
    state = {
        "valid_until": session["access"]["valid_until"],
        "accounts": [a["uid"] for a in session["accounts"]],
    }
    SESSION_FILE.write_text(json.dumps(state))
    return state


def get_session(headers: dict) -> dict:
    if SESSION_FILE.exists():
        state = json.loads(SESSION_FILE.read_text())
        if datetime.fromisoformat(state["valid_until"]) > datetime.now(timezone.utc):
            return state
    return authorize(headers)


def transactions(
    headers: dict, account_uid: str, date_from: str, date_to: str, status: str
):
    params = {"date_from": date_from, "date_to": date_to, "transaction_status": status}
    while True:
        page = api(
            "GET",
            f"/accounts/{account_uid}/transactions",
            headers,
            fatal=status == "BOOK",
            params=params,
        )
        yield from page.get("transactions", [])
        if not page.get("continuation_key"):
            return
        params["continuation_key"] = page["continuation_key"]


def description(tx: dict) -> str:
    remittance = " ".join(tx.get("remittance_information") or [])
    counterparty = (tx.get("creditor") or tx.get("debtor") or {}).get("name")
    return remittance or counterparty or ""


def tx_date(tx: dict) -> str:
    # BCP leaves transaction_date empty but embeds the actual purchase
    # timestamp in entry_reference (e.g. "2026070602026-07-04-10.20.15.809596",
    # booking date followed by purchase datetime). Prefer it over booking_date,
    # which for card purchases can be days in the future.
    match = re.search(r"\d{4}-\d{2}-\d{2}", tx.get("entry_reference") or "")
    if match:
        return match.group()
    return (
        tx.get("booking_date")
        or tx.get("transaction_date")
        or tx.get("value_date")
        or ""
    )


def tx_time(tx: dict) -> str:
    """Purchase time from entry_reference (see tx_date), empty if absent."""
    match = re.search(
        r"\d{4}-\d{2}-\d{2}-(\d{2})\.(\d{2})\.(\d{2})", tx.get("entry_reference") or ""
    )
    return ":".join(match.groups()) if match else ""


def drop_batch_timestamps(txs: list[dict]) -> None:
    """BCP processes online purchases, direct debits and fees in overnight
    batches; those rows share one second-level timestamp in entry_reference,
    which is the batch time, not the purchase time. Drop the reference on such
    rows so the date falls back to booking_date and the time stays empty."""
    stamp = re.compile(r"\d{4}-\d{2}-\d{2}-\d{2}\.\d{2}\.\d{2}")
    counts = Counter(
        m.group() for tx in txs if (m := stamp.search(tx.get("entry_reference") or ""))
    )
    for tx in txs:
        m = stamp.search(tx.get("entry_reference") or "")
        if m and counts[m.group()] > 1:
            tx["entry_reference"] = None


def relevant(txs: list[dict], since: str) -> list[dict]:
    """Drop credits, ATM withdrawals and transactions booked before the fetch
    window. The window check uses the booking date, not the purchase date:
    card authorizations are invisible to the API while pending, so a purchase
    made days ago may book (and become fetchable) only now and must still be
    offered."""
    kept = []
    for tx in txs:
        amount = tx["transaction_amount"]
        date = tx_date(tx)
        desc = description(tx)
        booked = tx.get("booking_date") or date
        if booked and booked < since:
            reason = f"booked before fetch window ({booked})"
        elif tx["credit_debit_indicator"] == "CRDT":
            reason = "credit"
        elif "LEV ATM" in desc:
            reason = "ATM withdrawal"
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


def normalize(tx: dict) -> dict:
    amount = tx["transaction_amount"]
    date = tx_date(tx)
    booking = tx.get("booking_date")
    notes = []
    if booking and booking != date:
        notes.append(f"booked {booking}")
    if tx.get("status") == "PDNG":
        notes.append("(pending)")
    return {
        "date": date,
        "time": tx_time(tx),
        "amount": amount["amount"],
        "currency": amount["currency"],
        "desc": description(tx),
        "source": SOURCE,
        "notes": notes,
    }


def fetch(config: dict, days: int = DAYS) -> list[dict]:
    headers = auth_headers(config)
    session = get_session(headers)
    headers |= psu_headers()
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).date().isoformat()
    # BCP books card transactions under a future date; without a future
    # date_to the bank omits them entirely, so today's purchases would never
    # show up.
    until = (now + timedelta(days=7)).date().isoformat()
    log.info("Fetching BCP transactions since %s", since)
    result = []
    for uid in session["accounts"]:
        log.info("Processing BCP account %s", uid)
        txs = [
            tx
            for status in ("BOOK", "PDNG")
            for tx in transactions(headers, uid, since, until, status)
        ]
        drop_batch_timestamps(txs)
        result += [normalize(tx) for tx in relevant(txs, since)]
    return result
