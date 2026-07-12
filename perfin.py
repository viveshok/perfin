"""Sync recent bank expenses into a Google Sheet.

Supported banks, each enabled by its keys being present in config.json:
- Millennium BCP via Enable Banking (see bcp.py for setup)
- Desjardins checking + credit card via Plaid (see desjardins.py for setup)

At launch you pick which configured bank to sync (skipped when only one is
configured) and the lookback period in days (each bank has a sensible
default). Pulls recent transactions and matches them against existing
sheet rows by currency, amount, date and purchase time. Exact re-fetches of
already synced rows are skipped silently; near matches (same currency and
amount within 7 days, times not contradicting or within a few minutes) are
offered as likely duplicates to confirm. Missing transactions are inserted at
their date position after confirmation.

Setup:
1. Set up bank access following the docstrings of bcp.py and desjardins.py.
2. In Google Cloud, enable the Sheets API and create a service account with a
   JSON key. Save the key next to this script and share the spreadsheet with
   the service account's email (editor access).
3. Copy config.example.json to config.json and fill in the bank credentials,
   service account key file, spreadsheet ID, sheet tab name and OpenAI API key
   (used to suggest item names and categories for new transactions).
4. Run: uv run perfin.py
   The first run for a bank opens its consent flow; later runs reuse the
   session.
"""

import json
import logging
import re
import readline
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

import jwt
import requests

import bcp
import desjardins

SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
HERE = Path(__file__).parent
CONFIG_FILE = HERE / "config.json"

log = logging.getLogger("perfin")


def tx_summary(tx: dict) -> str:
    """One line with everything useful to identify the transaction."""
    parts = [
        f"{tx['date']} {tx['time']}".strip(),
        f"{tx['amount']} {tx['currency']}",
        tx["desc"],
        *tx["notes"],
    ]
    return "  ".join(p for p in parts if p)


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
    more than 7 days away or attributed to a different bank never match. When
    both sides carry a purchase time, clearly differing times mean distinct
    purchases and never match; small differences are tolerated because the
    timestamp a bank reports can shift by a few minutes between the pending
    and the booked version of a purchase."""
    pairs = []
    taken: set[int] = set()
    for tx in sorted(txs, key=lambda t: (t["date"], t["time"])):
        time = clock(tx["time"])
        target = parse_date(tx["date"])
        best = None
        for row in seen.get(tx_key(tx["currency"], tx["amount"]), []):
            row_date = parse_date(row["date"])
            row_time = clock(row["time"])
            if id(row) in taken or target is None or row_date is None:
                continue
            if row["source"] and row["source"] != tx["source"]:
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


def suggest(config: dict, history: list, tx: dict) -> tuple[str, str]:
    """Ask OpenAI for a readable item name and a category from those in use."""
    categories = sorted({category for _, category in history if category})
    examples = "\n".join(f"{item} -> {category}" for item, category in history[-30:])
    prompt = (
        "You clean up bank transaction descriptions for a personal expense sheet.\n\n"
        "Recent entries from the sheet, as 'item -> category', shown only to "
        "illustrate naming style and category usage:\n"
        f"{examples}\n\n"
        "Now the transaction to process:\n"
        f"Raw description: {tx['desc']}\n"
        f"Amount: {tx['amount']} {tx['currency']}\n\n"
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
        return tx["desc"], ""
    reply = json.loads(r.json()["choices"][0]["message"]["content"])
    return reply.get("item") or tx["desc"], reply.get("category") or ""


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


def confirm_and_insert(
    config: dict,
    token: str,
    grid_id: int,
    dates: list,
    seen: dict,
    history: list,
    tx: dict,
) -> bool:
    print(f"\nNew transaction: {tx_summary(tx)}")
    if input("Add to Google Sheets? [Y/n] ").strip().lower() in ("n", "no"):
        log.info("Skipped by user: %s %s %s", tx["date"], tx["amount"], tx["desc"])
        return False
    default_item, default_category = suggest(config, history, tx)
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
            tx["date"],
            tx["time"],
            tx["source"],
            item,
            category,
            tx["currency"],
            tx["amount"],
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

    banks = []
    if "application_id" in config:
        banks.append(("Millennium BCP", bcp))
    if "plaid_client_id" in config:
        banks.append(("Desjardins", desjardins))
    if not banks:
        sys.exit("No bank credentials in config.json (see config.example.json).")
    if len(banks) == 1:
        name, bank = banks[0]
    else:
        for i, (name, _) in enumerate(banks, 1):
            print(f"{i}) {name}")
        choice = input("Bank to sync [1]: ").strip() or "1"
        if choice not in [str(i) for i in range(1, len(banks) + 1)]:
            sys.exit(f"Invalid choice {choice!r}")
        name, bank = banks[int(choice) - 1]
    raw = input(f"Lookback days [{bank.DAYS}]: ").strip()
    if raw and not raw.isdigit():
        sys.exit(f"Invalid number of days {raw!r}")
    days = int(raw) if raw else bank.DAYS
    log.info("Syncing %s over the last %d day(s)", name, days)
    txs = bank.fetch(config, days)

    token = sheets_token(config)
    grid_id = sheet_id(config, token)
    seen, dates, history = sheet_state(config, token)

    added = skipped = declined = 0
    for tx, row in match_rows(txs, seen):
        time = clock(tx["time"])
        if row is not None:
            if (
                row["date"] == tx["date"]
                and clock(row["time"]) == time
                and row["source"] == tx["source"]
            ):
                log.info(
                    "Already synced (same date, time and amount): %s", tx_summary(tx)
                )
                seen[tx_key(tx["currency"], tx["amount"])].remove(row)
                skipped += 1
                continue
            bank_desc = "  ".join(p for p in [tx["desc"], *tx["notes"]] if p)
            sheet_desc = row["item"] + (
                f"  [{row['category']}]" if row["category"] else ""
            )
            width = max(len(tx["amount"]), len(row["amount"]))
            print(
                "\nPossible duplicate with the same amount:\n"
                f"  bank:  {tx['date']:10}  {tx['time']:8}  "
                f"{tx['amount']:>{width}} {tx['currency']}  {bank_desc}\n"
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
                if tx["date"] and row["date"] != tx["date"]:
                    set_cell(config, token, row["pos"], "A", tx["date"])
                    dates[row["pos"]] = tx["date"]
                if tx["time"] and clock(row["time"]) != time:
                    set_cell(config, token, row["pos"], "B", tx["time"])
                if not row["source"]:
                    set_cell(config, token, row["pos"], "C", tx["source"])
                seen[tx_key(tx["currency"], tx["amount"])].remove(row)
                log.info(
                    "Deduplicated against sheet row %s %r: %s",
                    row["date"],
                    row["item"],
                    tx_summary(tx),
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
