"""Fetch recent Desjardins transactions (checking + credit card) through the
Plaid API.

Setup:
1. Create a free Plaid account at https://dashboard.plaid.com/signup (choose
   personal use; the Trial plan gives free production access for up to 10
   linked accounts).
2. Put the client ID and production secret in config.json as plaid_client_id
   and plaid_secret (see config.example.json).

The first run prints a Plaid Hosted Link URL where you log in to AccèsD once;
the resulting access token is saved in plaid_session.json and does not expire.
Both the checking account and the credit card come through the same login.
"""

import json
import logging
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

API = "https://production.plaid.com"
HERE = Path(__file__).parent
SESSION_FILE = HERE / "plaid_session.json"
# Card purchases can take days to post and Plaid may report no pending
# transactions for Desjardins, so default to a wide window; already synced
# rows are recognized and skipped by the dedup pass.
DAYS = 5

SKIP_CATEGORIES = {
    "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT": "credit card payment",
    "TRANSFER_OUT_ACCOUNT_TRANSFER": "transfer between own accounts",
    "TRANSFER_OUT_WITHDRAWALS_AND_ATM": "ATM withdrawal",
}

log = logging.getLogger("perfin")


def api(config: dict, path: str, **body) -> dict:
    body |= {"client_id": config["plaid_client_id"], "secret": config["plaid_secret"]}
    r = requests.post(f"{API}{path}", json=body, timeout=30)
    if not r.ok:
        error = r.json() if "json" in r.headers.get("content-type", "") else {}
        if error.get("error_code") in ("PRODUCT_NOT_READY", "ITEM_LOGIN_REQUIRED"):
            return error
        sys.exit(f"Plaid error {r.status_code} on {path}: {r.text}")
    return r.json()


def link(config: dict, access_token: str | None = None) -> str:
    """Run the Plaid Hosted Link flow and return an access token. With an
    access token given, run in update mode to repair an expired login and
    return the same token (a fresh link would burn one of the Trial plan's
    10 item slots)."""
    body = {
        "client_name": "perfin",
        "language": "en",
        "country_codes": ["CA"],
        "user": {"client_user_id": str(uuid.uuid4())},
        "hosted_link": {},
    }
    if access_token:
        body["access_token"] = access_token
    else:
        body["products"] = ["transactions"]
    created = api(config, "/link/token/create", **body)
    print(
        "Open this URL and log in to Desjardins (AccèsD):\n"
        f"{created['hosted_link_url']}\n",
        file=sys.stderr,
    )
    while True:
        input("Press Enter after completing the login flow... ")
        got = api(config, "/link/token/get", link_token=created["link_token"])
        results = [
            item
            for session in got.get("link_sessions", [])
            for item in (session.get("results") or {}).get("item_add_results", [])
        ]
        if access_token and any(
            session.get("finished_at") for session in got.get("link_sessions", [])
        ):
            return access_token
        if results:
            break
        print("Link not completed yet.", file=sys.stderr)
    exchanged = api(
        config, "/item/public_token/exchange", public_token=results[0]["public_token"]
    )
    SESSION_FILE.write_text(json.dumps({"access_token": exchanged["access_token"]}))
    return exchanged["access_token"]


def get_access_token(config: dict) -> str:
    if SESSION_FILE.exists():
        return json.loads(SESSION_FILE.read_text())["access_token"]
    return link(config)


def sources(config: dict, token: str) -> dict:
    """Map each account ID to the sheet source name for that account."""
    accounts = api(config, "/accounts/get", access_token=token)["accounts"]
    return {
        a["account_id"]: (
            "Desjardins Mastercard" if a["type"] == "credit" else "Desjardins"
        )
        for a in accounts
    }


def transactions(config: dict, token: str, start: str, end: str) -> list[dict]:
    """All transactions in the window, transparently retrying while Plaid
    prepares the data and re-linking when the bank login has expired."""
    txs: list[dict] = []
    while True:
        page = api(
            config,
            "/transactions/get",
            access_token=token,
            start_date=start,
            end_date=end,
            options={"count": 500, "offset": len(txs)},
        )
        if page.get("error_code") == "PRODUCT_NOT_READY":
            log.info("Plaid is still preparing transactions; retrying in 15s")
            time.sleep(15)
            continue
        if page.get("error_code") == "ITEM_LOGIN_REQUIRED":
            log.info("Desjardins login expired; starting re-link")
            link(config, token)
            continue
        txs += page["transactions"]
        if len(txs) >= page["total_transactions"]:
            return txs


def relevant(txs: list[dict]) -> list[dict]:
    """Drop credits (refunds, card payments received) and the categories in
    SKIP_CATEGORIES. Plaid reports outflows as positive amounts."""
    kept = []
    for tx in txs:
        category = (tx.get("personal_finance_category") or {}).get("detailed", "")
        if tx["amount"] <= 0:
            reason = "credit"
        elif category in SKIP_CATEGORIES:
            reason = SKIP_CATEGORIES[category]
        else:
            kept.append(tx)
            continue
        log.info(
            "Skipping %s: %s %s %s %s",
            reason,
            tx["date"],
            tx["amount"],
            tx.get("iso_currency_code") or "",
            tx.get("merchant_name") or tx["name"],
        )
    return kept


def normalize(tx: dict, source: str) -> dict:
    stamp = tx.get("datetime") or tx.get("authorized_datetime")
    return {
        "date": tx["date"],
        "time": stamp[11:16] if stamp else "",
        "amount": f"{tx['amount']:.2f}".rstrip("0").rstrip("."),
        "currency": tx.get("iso_currency_code") or "CAD",
        "desc": tx.get("merchant_name") or tx["name"],
        "source": source,
        "notes": ["(pending)"] if tx["pending"] else [],
    }


def fetch(config: dict, days: int = DAYS) -> list[dict]:
    token = get_access_token(config)
    by_account = sources(config, token)
    now = datetime.now(timezone.utc).date()
    start = (now - timedelta(days=days)).isoformat()
    log.info("Fetching Desjardins transactions since %s", start)
    txs = transactions(config, token, start, now.isoformat())
    return [normalize(tx, by_account[tx["account_id"]]) for tx in relevant(txs)]
