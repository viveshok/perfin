"""Sync the last 24 hours of Millennium BCP expenses into a Google Sheet.

Pulls recent transactions from the bank, checks which ones are already in the
sheet (matched on date + currency + amount, counting occurrences; a same
currency + amount row within 7 days is offered as a likely duplicate), and
asks for confirmation before inserting each missing one at its date position.

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
import sys
import uuid
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


def tx_key(date: str, currency: str, amount: str) -> tuple:
    try:
        normalized = str(Decimal(amount))
    except InvalidOperation:
        normalized = amount
    return (date, currency.strip().upper(), normalized)


def fuzzy_match(seen: dict, key: tuple) -> tuple | None:
    """Find the closest unmatched sheet key with the same currency and amount
    within 7 days of the transaction date."""
    date, currency, amount = key
    try:
        target = datetime.fromisoformat(date)
    except ValueError:
        return None
    best = None
    for candidate, items in seen.items():
        if not items or candidate[1:] != (currency, amount):
            continue
        try:
            diff = abs((datetime.fromisoformat(candidate[0]) - target).days)
        except ValueError:
            continue
        if diff <= 7 and (best is None or diff < best[0]):
            best = (diff, candidate)
    return best[1] if best else None


def sheet_id(config: dict, token: str) -> int:
    url = f"{SHEETS_API}/{config['spreadsheet_id']}?fields=sheets.properties"
    for sheet in sheets_api("GET", url, token)["sheets"]:
        if sheet["properties"]["title"] == config["sheet_tab"]:
            return sheet["properties"]["sheetId"]
    sys.exit(f"Sheet tab {config['sheet_tab']!r} not found")


def sheet_state(config: dict, token: str) -> tuple[dict, list, list]:
    """Return dedup keys (mapping key -> item names of matching rows), the date
    column and (item, category) pairs of data rows."""
    url = f"{SHEETS_API}/{config['spreadsheet_id']}/values/{quote(config['sheet_tab'])}"
    log.info("Fetching existing rows from sheet tab %r", config["sheet_tab"])
    rows = sheets_api("GET", url, token).get("values", [])
    log.info("Sheet has %d rows (including header)", len(rows))
    keys: dict[tuple, list[str]] = {}
    for row in rows[1:]:
        if len(row) >= 5:
            keys.setdefault(tx_key(row[0], row[3], row[4]), []).append(row[1])
    dates = [row[0] if row else "" for row in rows[1:]]
    history = [(row[1], row[2]) for row in rows[1:] if len(row) >= 3]
    return keys, dates, history


def suggest(config: dict, history: list, desc: str, amount: dict) -> tuple[str, str]:
    """Ask OpenAI for a readable item name and a category from those in use."""
    categories = sorted({category for _, category in history if category})
    examples = "\n".join(f"{item} -> {category}" for item, category in history[-30:])
    prompt = (
        "You clean up bank transaction descriptions for a personal expense sheet.\n"
        f"Raw description: {desc}\n"
        f"Amount: {amount['amount']} {amount['currency']}\n\n"
        "Rewrite the description as a short, human-readable item name (e.g. the "
        "merchant or purpose, no codes or dates), and pick the best-fitting "
        f"category from this list: {', '.join(categories)}\n\n"
        "Recent entries from the sheet, as 'item -> category':\n"
        f"{examples}\n\n"
        'Reply with JSON: {"item": ..., "category": ...}'
    )
    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {config['openai_api_key']}"},
        json={
            "model": config.get("openai_model", "gpt-4o-mini"),
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
        },
        timeout=30,
    )
    if not r.ok:
        log.warning("OpenAI API error %d: %s", r.status_code, r.text)
        return desc, ""
    reply = json.loads(r.json()["choices"][0]["message"]["content"])
    return reply.get("item") or desc, reply.get("category") or ""


def insert_row(config: dict, token: str, grid_id: int, dates: list, row: list) -> None:
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
    log.info("Inserted row at position %d: %s", index + 1, row)


def confirm_and_insert(
    config: dict, token: str, grid_id: int, dates: list, history: list, tx: dict
) -> bool:
    date = tx_date(tx)
    amount = tx["transaction_amount"]
    desc = description(tx)
    pending = " (pending)" if tx.get("status") == "PDNG" else ""
    print(
        f"\nNew transaction{pending}: "
        f"{date}  {amount['amount']} {amount['currency']}  {desc}"
    )
    if input("Add to Google Sheets? [Y/n] ").strip().lower() in ("n", "no"):
        log.info("Skipped by user: %s %s %s", date, amount["amount"], desc)
        return False
    default_item, default_category = suggest(config, history, desc, amount)
    item = input(f"Item [{default_item}]: ").strip() or default_item
    prompt = f"Category [{default_category}]: " if default_category else "Category: "
    category = input(prompt).strip() or default_category
    insert_row(
        config,
        token,
        grid_id,
        dates,
        [date, item, category, amount["currency"], amount["amount"]],
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
        for tx in reversed(txs):
            amount = tx["transaction_amount"]
            date = tx_date(tx)
            desc = description(tx)
            if tx["credit_debit_indicator"] == "CRDT":
                log.info(
                    "Skipping credit: %s %s %s %s",
                    date,
                    amount["amount"],
                    amount["currency"],
                    desc,
                )
                continue
            key = tx_key(date, amount["currency"], amount["amount"])
            if seen.get(key):
                seen[key].pop()
                log.info(
                    "Already in sheet: %s %s %s %s",
                    date,
                    amount["amount"],
                    amount["currency"],
                    desc,
                )
                skipped += 1
                continue
            near = fuzzy_match(seen, key)
            if near is not None:
                item = seen[near][-1]
                print(
                    f"\nPossible duplicate: {date}  {amount['amount']} "
                    f"{amount['currency']}  {desc}\n"
                    f"matches sheet row {near[0]}  {item!r} with the same amount."
                )
                if input("Treat as duplicate and skip? [Y/n] ").strip().lower() not in (
                    "n",
                    "no",
                ):
                    seen[near].pop()
                    log.info(
                        "Deduplicated against sheet row %s %r: %s %s %s %s",
                        near[0],
                        item,
                        date,
                        amount["amount"],
                        amount["currency"],
                        desc,
                    )
                    skipped += 1
                    continue
            if confirm_and_insert(config, token, grid_id, dates, history, tx):
                added += 1
            else:
                declined += 1
    log.info(
        "Done: %d already in sheet, %d added, %d declined", skipped, added, declined
    )


if __name__ == "__main__":
    main()
