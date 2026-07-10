"""Sync the last 24 hours of Millennium BCP expenses into a Google Sheet.

Pulls recent transactions from the bank and matches them against existing
sheet rows by currency, amount, date and purchase time. Exact re-fetches of
already synced rows are skipped silently; near matches (same currency and
amount within 7 days, times not contradicting or within a few minutes) are
offered as likely duplicates to confirm. Missing transactions are inserted at their date
position after confirmation.

Setup:
1. Create a free account at https://enablebanking.com and register an application
   (restricted production mode lets you access your own accounts for free).
   Save the application's private key as bcp_key.pem.
2. In Google Cloud, enable the Sheets API and create a service account with a
   JSON key. Save the key next to this script and share the spreadsheet with
   the service account's email (editor access).
3. Copy config.example.json to config.json and fill in the application ID,
   service account key file, spreadsheet ID, sheet tab name and OpenAI API key
   (used to suggest item names and categories for new transactions).
4. Run: uv run bcp_transactions.py
   The first run opens a bank consent flow; later runs reuse the session.
"""

import json
import logging
import re
import readline
import sys
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import jwt
import requests

API = "https://api.enablebanking.com"
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
HERE = Path(__file__).parent
CONFIG_FILE = HERE / "config.json"
SESSION_FILE = HERE / "session.json"
SOURCE = "Millennium BCP"

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


def tx_summary(tx: dict) -> str:
    """One line with everything useful to identify the transaction."""
    amount = tx["transaction_amount"]
    date = tx_date(tx)
    parts = [
        f"{date} {tx_time(tx)}".strip(),
        f"{amount['amount']} {amount['currency']}",
    ]
    remittance = " ".join(tx.get("remittance_information") or [])
    counterparty = (tx.get("creditor") or tx.get("debtor") or {}).get("name") or ""
    if remittance:
        parts.append(remittance)
    if counterparty and counterparty not in remittance:
        parts.append(counterparty)
    booking = tx.get("booking_date")
    if booking and booking != date:
        parts.append(f"booked {booking}")
    if tx.get("status") == "PDNG":
        parts.append("(pending)")
    return "  ".join(parts)


def sheets_token(config: dict) -> str:
    sa = json.loads((HERE / config["service_account_key_file"]).read_text())
    now = int(datetime.now(timezone.utc).timestamp())
    assertion = jwt.encode(
        {
            "iss": sa["client_email"],
            "scope": "https://www.googleapis.com/auth/spreadsheets",
            "aud": sa["token_uri"],
            "iat": now,
            "exp": now + 3600,
        },
        sa["private_key"],
        algorithm="RS256",
    )
    log.info("Requesting Google Sheets access token for %s", sa["client_email"])
    r = requests.post(
        sa["token_uri"],
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        },
    )
    if not r.ok:
        sys.exit(f"Google token error {r.status_code}: {r.text}")
    return r.json()["access_token"]


def sheets_api(method: str, url: str, token: str, **kwargs) -> dict:
    r = requests.request(
        method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs
    )
    if not r.ok:
        sys.exit(f"Sheets API error {r.status_code}: {r.text}")
    return r.json()


def tx_key(currency: str, amount: str) -> tuple[str, str]:
    try:
        # normalize() drops trailing zeros so "19.90" matches "19.9"; the "f"
        # format keeps integers like 20 out of scientific notation ("2E+1")
        normalized = format(Decimal(amount).normalize(), "f")
    except InvalidOperation:
        normalized = amount
    return (currency.strip().upper(), normalized)


def parse_date(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def clock(value: str) -> str:
    """Time normalized to HH:MM for comparison, "" if there is no time.
    Tolerates formatting differences between what the script writes and what
    the sheet displays (dropped seconds, missing leading zero)."""
    match = re.search(r"(\d{1,2}):(\d{2})", value)
    return f"{int(match.group(1)):02d}:{match.group(2)}" if match else ""


def times_compatible(a: str, b: str) -> bool:
    """Whether two HH:MM times could belong to the same purchase: equal,
    either missing, or within 10 minutes of each other."""
    if not a or not b or a == b:
        return True
    minutes_a = int(a[:2]) * 60 + int(a[3:])
    minutes_b = int(b[:2]) * 60 + int(b[3:])
    return abs(minutes_a - minutes_b) <= 10


def match_rows(txs: list[dict], seen: dict) -> list[tuple[dict, dict | None]]:
    """Pair each bank transaction with the best available sheet row of the
    same currency and amount, or None if there is no plausible match.

    Transactions are matched in chronological order and each row is used at
    most once, so several identical purchases (e.g. daily coffees) pair up
    with their own rows instead of all competing for the nearest one. Rows
    more than 7 days away never match. When both sides carry a purchase time,
    clearly differing times mean distinct purchases and never match; small
    differences are tolerated because the timestamp BCP reports can shift by
    a few minutes between the pending and the booked version of a purchase."""
    pairs = []
    taken: set[int] = set()
    for tx in sorted(txs, key=lambda t: (tx_date(t), tx_time(t))):
        amount = tx["transaction_amount"]
        date, time = tx_date(tx), clock(tx_time(tx))
        target = parse_date(date)
        best = None
        for row in seen.get(tx_key(amount["currency"], amount["amount"]), []):
            row_date = parse_date(row["date"])
            row_time = clock(row["time"])
            if id(row) in taken or target is None or row_date is None:
                continue
            diff = abs((row_date - target).days)
            if diff > 7 or not times_compatible(time, row_time):
                continue
            score = (diff, row_time != time, row["pos"])
            if best is None or score < best[0]:
                best = (score, row)
        if best:
            taken.add(id(best[1]))
        pairs.append((tx, best[1] if best else None))
    return pairs


def sheet_id(config: dict, token: str) -> int:
    url = f"{SHEETS_API}/{config['spreadsheet_id']}?fields=sheets.properties"
    for sheet in sheets_api("GET", url, token)["sheets"]:
        if sheet["properties"]["title"] == config["sheet_tab"]:
            return sheet["properties"]["sheetId"]
    sys.exit(f"Sheet tab {config['sheet_tab']!r} not found")


def sheet_state(config: dict, token: str) -> tuple[dict, list, list]:
    """Return sheet rows grouped by (currency, amount), the date column and
    (item, category) pairs of data rows."""
    url = f"{SHEETS_API}/{config['spreadsheet_id']}/values/{quote(config['sheet_tab'])}"
    log.info("Fetching existing rows from sheet tab %r", config["sheet_tab"])
    rows = sheets_api("GET", url, token).get("values", [])
    log.info("Sheet has %d rows (including header)", len(rows))
    seen: dict[tuple[str, str], list[dict]] = {}
    for pos, row in enumerate(rows[1:]):
        if len(row) >= 7:
            seen.setdefault(tx_key(row[5], row[6]), []).append(
                {
                    "date": row[0],
                    "time": row[1].strip(),
                    "source": row[2].strip(),
                    "item": row[3],
                    "category": row[4],
                    "currency": row[5].strip().upper(),
                    "amount": row[6],
                    "pos": pos,
                }
            )
    dates = [row[0] if row else "" for row in rows[1:]]
    history = [(row[3], row[4]) for row in rows[1:] if len(row) >= 5]
    return seen, dates, history


def suggest(config: dict, history: list, desc: str, amount: dict) -> tuple[str, str]:
    """Ask OpenAI for a readable item name and a category from those in use."""
    categories = sorted({category for _, category in history if category})
    examples = "\n".join(f"{item} -> {category}" for item, category in history[-30:])
    prompt = (
        "You clean up bank transaction descriptions for a personal expense sheet.\n\n"
        "Recent entries from the sheet, as 'item -> category', shown only to "
        "illustrate naming style and category usage:\n"
        f"{examples}\n\n"
        "Now the transaction to process:\n"
        f"Raw description: {desc}\n"
        f"Amount: {amount['amount']} {amount['currency']}\n\n"
        "Rewrite the raw description as a short, human-readable item name (e.g. "
        "the merchant or purpose, no codes or dates). The item name must be "
        "derived from the raw description above, never copied from the examples. "
        "Pick the best-fitting category from this list: "
        f"{', '.join(categories)}\n\n"
        'Reply with JSON: {"item": ..., "category": ...}'
    )
    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {config['openai_api_key']}"},
        json={
            "model": config.get("openai_model", "gpt-4o-mini"),
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            "temperature": 0,
        },
        timeout=30,
    )
    if not r.ok:
        log.warning("OpenAI API error %d: %s", r.status_code, r.text)
        return desc, ""
    reply = json.loads(r.json()["choices"][0]["message"]["content"])
    return reply.get("item") or desc, reply.get("category") or ""


def input_prefilled(prompt: str, default: str, completions: list[str] = []) -> str:
    """Prompt with the default pre-typed and editable, optionally with tab
    completion over the given candidates."""

    def complete(text: str, state: int) -> str | None:
        matches = [c for c in completions if c.lower().startswith(text.lower())]
        return matches[state] if state < len(matches) else None

    readline.set_completer(complete)
    readline.set_completer_delims("")
    readline.parse_and_bind("tab: complete")
    readline.set_startup_hook(lambda: readline.insert_text(default))
    try:
        return input(prompt).strip()
    finally:
        readline.set_startup_hook(None)
        readline.set_completer(None)


def set_cell(config: dict, token: str, pos: int, column: str, value: str) -> None:
    """Write a value into the given column of the data row at the position."""
    cell = f"{config['sheet_tab']}!{column}{pos + 2}"
    url = (
        f"{SHEETS_API}/{config['spreadsheet_id']}/values/{quote(cell)}"
        "?valueInputOption=USER_ENTERED"
    )
    sheets_api("PUT", url, token, json={"values": [[value]]})
    log.info("Filled in %s at %s", value, cell)


def insert_row(
    config: dict, token: str, grid_id: int, dates: list, seen: dict, row: list
) -> None:
    """Insert the row after the last existing row whose date is <= the new one."""
    date = row[0]
    pos = next((i for i, d in enumerate(dates) if d > date), len(dates))
    index = pos + 1  # skip the header row
    url = f"{SHEETS_API}/{config['spreadsheet_id']}:batchUpdate"
    body = {
        "requests": [
            {
                "insertDimension": {
                    "range": {
                        "sheetId": grid_id,
                        "dimension": "ROWS",
                        "startIndex": index,
                        "endIndex": index + 1,
                    },
                    "inheritFromBefore": True,
                }
            }
        ]
    }
    sheets_api("POST", url, token, json=body)
    cell = f"{config['sheet_tab']}!A{index + 1}"
    url = (
        f"{SHEETS_API}/{config['spreadsheet_id']}/values/{quote(cell)}"
        "?valueInputOption=USER_ENTERED"
    )
    sheets_api("PUT", url, token, json={"values": [row]})
    dates.insert(pos, date)
    for rows in seen.values():
        for entry in rows:
            if entry["pos"] >= pos:
                entry["pos"] += 1
    log.info("Inserted row at position %d: %s", index + 1, row)


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


def confirm_and_insert(
    config: dict,
    token: str,
    grid_id: int,
    dates: list,
    seen: dict,
    history: list,
    tx: dict,
) -> bool:
    date = tx_date(tx)
    amount = tx["transaction_amount"]
    desc = description(tx)
    print(f"\nNew transaction: {tx_summary(tx)}")
    if input("Add to Google Sheets? [Y/n] ").strip().lower() in ("n", "no"):
        log.info("Skipped by user: %s %s %s", date, amount["amount"], desc)
        return False
    default_item, default_category = suggest(config, history, desc, amount)
    categories = sorted({c for _, c in history if c})
    item = input_prefilled("Item: ", default_item)
    category = input_prefilled("Category: ", default_category, categories)
    insert_row(
        config,
        token,
        grid_id,
        dates,
        seen,
        [
            date,
            tx_time(tx),
            SOURCE,
            item,
            category,
            amount["currency"],
            amount["amount"],
        ],
    )
    history.append((item, category))
    return True


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    if not CONFIG_FILE.exists():
        sys.exit("Missing config.json (see config.example.json and the docstring).")
    config = json.loads(CONFIG_FILE.read_text())
    headers = auth_headers(config)
    session = get_session(headers)
    headers |= psu_headers()

    token = sheets_token(config)
    grid_id = sheet_id(config, token)
    seen, dates, history = sheet_state(config, token)

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=24)
    # BCP books card transactions under a future date; without a future date_to
    # the bank omits them entirely, so today's purchases would never show up.
    until = now + timedelta(days=7)
    log.info("Fetching bank transactions since %s", since.date().isoformat())
    added = skipped = declined = 0
    for uid in session["accounts"]:
        log.info("Processing account %s", uid)
        txs = [
            tx
            for status in ("BOOK", "PDNG")
            for tx in transactions(
                headers,
                uid,
                since.date().isoformat(),
                until.date().isoformat(),
                status,
            )
        ]
        drop_batch_timestamps(txs)
        for tx, row in match_rows(relevant(txs, since.date().isoformat()), seen):
            amount = tx["transaction_amount"]
            date = tx_date(tx)
            time = tx_time(tx)
            desc = description(tx)
            if row is not None:
                if (
                    row["date"] == date
                    and time
                    and clock(row["time"]) == clock(time)
                    and row["source"] == SOURCE
                ):
                    log.info(
                        "Already synced (same date, time and amount): %s %s %s %s %s",
                        date,
                        time,
                        amount["amount"],
                        amount["currency"],
                        desc,
                    )
                    seen[tx_key(amount["currency"], amount["amount"])].remove(row)
                    skipped += 1
                    continue
                booking = tx.get("booking_date")
                bank_notes = [
                    f"booked {booking}" if booking and booking != date else "",
                    "(pending)" if tx.get("status") == "PDNG" else "",
                ]
                bank_desc = "  ".join(p for p in [desc, *bank_notes] if p)
                sheet_desc = row["item"] + (
                    f"  [{row['category']}]" if row["category"] else ""
                )
                width = max(len(amount["amount"]), len(row["amount"]))
                print(
                    "\nPossible duplicate with the same amount:\n"
                    f"  bank:  {date:10}  {time:8}  "
                    f"{amount['amount']:>{width}} {amount['currency']}  {bank_desc}\n"
                    f"  sheet: {row['date']:10}  {row['time']:8}  "
                    f"{row['amount']:>{width}} {row['currency']}  {sheet_desc}"
                )
                if input("Treat as duplicate and skip? [Y/n] ").strip().lower() not in (
                    "n",
                    "no",
                ):
                    # Align the row with the bank's purchase date so the next
                    # run recognizes it as already synced instead of asking
                    # again.
                    if date and row["date"] != date:
                        set_cell(config, token, row["pos"], "A", date)
                        dates[row["pos"]] = date
                    if time and clock(row["time"]) != clock(time):
                        set_cell(config, token, row["pos"], "B", time)
                    if not row["source"]:
                        set_cell(config, token, row["pos"], "C", SOURCE)
                    seen[tx_key(amount["currency"], amount["amount"])].remove(row)
                    log.info(
                        "Deduplicated against sheet row %s %r: %s %s %s %s",
                        row["date"],
                        row["item"],
                        date,
                        amount["amount"],
                        amount["currency"],
                        desc,
                    )
                    skipped += 1
                    continue
            if confirm_and_insert(config, token, grid_id, dates, seen, history, tx):
                added += 1
            else:
                declined += 1
    log.info(
        "Done: %d already in sheet, %d added, %d declined", skipped, added, declined
    )


if __name__ == "__main__":
    main()
