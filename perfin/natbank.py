"""Import Natbank USD Mastercard transactions from the card portal's QFX
files.

The card is issued by Card Assets (a division of First Arkansas Bank & Trust)
and is not reachable through any aggregator, but its servicing portal at
https://www.24-7cardaccess.com offers transaction file downloads. With
natbank_user and natbank_password in config.json, a Playwright-driven
Chromium logs in and downloads the file into the statements/ folder next to
this script automatically.
The first run opens a visible browser window to pass the portal's device
check (log in happens automatically; answer the security challenge if one
appears); the browser profile in natbank_profile/ then keeps the device
trusted for headless runs. Whenever headless login fails, a window opens
again for you to finish, and if the script cannot find the download link it
asks you to click it yourself and still captures the file.

Without portal credentials in config.json, download the QFX/OFX (Quicken)
file manually into statements/ before each sync.

Every .qfx/.ofx file in the folder is parsed on each run and duplicates
(same FITID) are merged, so overlapping downloads and stale files are
harmless; transactions posted before the lookback window are skipped.
"""

import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SOURCE = "Natbank Mastercard"
# Statements are monthly, so look back far enough to cover a full cycle plus
# some slack; the dedup pass skips already synced rows.
DAYS = 40

PORTAL = "https://www.24-7cardaccess.com"
HERE = Path(__file__).parent
FOLDER = HERE / "statements"
PROFILE_DIR = HERE / "natbank_profile"
ACTIVITY_LINK = re.compile(r"transaction|activity", re.I)
DOWNLOAD_LINK = re.compile(r"quicken|qfx|download|export", re.I)

log = logging.getLogger("perfin")


def submit_login(page, config: dict) -> None:
    """Fill and submit the portal's login form if it is showing."""
    user_field = page.locator("#UserId")
    if user_field.count() and user_field.is_visible():
        # The server validates the fingerprintjs device hash, which a page
        # script computes asynchronously after load.
        page.wait_for_function("document.getElementById('DeviceHash').value !== ''")
        user_field.fill(config["natbank_user"])
        page.fill("#Password", config["natbank_password"])
        page.locator("#RememberMe").check()
        page.click("#loginSubmitButton")
        page.wait_for_load_state()
        page.wait_for_timeout(2000)


def attempt_download_clicks(page) -> None:
    """Best-effort path to the QFX download. The markup behind the login is
    not publicly documented, so on any failure the caller falls back to the
    user clicking through the open browser window."""
    try:
        link = page.get_by_role("link", name=ACTIVITY_LINK).first
        if link.count():
            link.click()
            page.wait_for_load_state()
        control = page.get_by_text(DOWNLOAD_LINK).first
        if control.count():
            control.click()
    except Exception as error:
        log.info("Automatic navigation to the download stopped: %s", error)


def download(config: dict, folder: Path) -> None:
    """Log in to the portal with a persistent Chromium profile and save the
    transaction file into the folder. Tries headless first; falls back to a
    visible window for device checks, challenges and unexpected pages."""
    from playwright.sync_api import sync_playwright

    attempts = [True, False] if PROFILE_DIR.exists() else [False]
    with sync_playwright() as p:
        for headless in attempts:
            saved: list[Path] = []

            def save(dl) -> None:
                target = folder / dl.suggested_filename
                dl.save_as(target)
                saved.append(target)
                log.info("Downloaded %s", target)

            log.info("Opening portal (%s)", "headless" if headless else "window")
            context = p.chromium.launch_persistent_context(
                PROFILE_DIR, headless=headless, accept_downloads=True
            )
            try:
                context.on("page", lambda pg: pg.on("download", save))
                page = context.pages[0]
                page.on("download", save)
                page.set_default_timeout(15000)
                page.goto(f"{PORTAL}/Login")
                submit_login(page, config)
                if "/Login" in page.url:
                    if headless:
                        log.info("Headless login did not get through")
                        continue
                    print(
                        "Finish the login in the browser window (security "
                        "challenge, captcha, ...).",
                        file=sys.stderr,
                    )
                    page.wait_for_url(lambda url: "/Login" not in url, timeout=300000)
                attempt_download_clicks(page)
                if not saved and not headless:
                    print(
                        "If no file downloaded by itself, navigate to the "
                        "transaction download in the browser window and fetch "
                        "the Quicken (QFX) file; it is saved automatically.",
                        file=sys.stderr,
                    )
                deadline = time.time() + (30 if headless else 300)
                while not saved and time.time() < deadline:
                    page.wait_for_timeout(500)
                if saved:
                    return
                log.info("No download started%s", " headlessly" if headless else "")
            except Exception as error:
                log.warning("Portal automation attempt failed: %s", error)
            finally:
                context.close()
    sys.exit("Could not download a transaction file from the portal.")


def field(block: str, name: str) -> str:
    """Value of an SGML-style OFX tag, which often has no closing tag."""
    match = re.search(rf"<{name}>([^<\r\n]*)", block, re.IGNORECASE)
    return match.group(1).strip() if match else ""


def parse_stamp(value: str) -> tuple[str, str]:
    """OFX datetime (YYYYMMDD[HHMMSS[.XXX]][[gmt offset:tz]]) as (date, time).
    Placeholder times of midnight or noon mean the portal did not record a
    real purchase time, so they are dropped."""
    match = re.match(r"(\d{4})(\d{2})(\d{2})(?:(\d{2})(\d{2})\d{2})?", value)
    if not match:
        return "", ""
    year, month, day, hour, minute = match.groups()
    time = f"{hour}:{minute}" if hour else ""
    if time in ("00:00", "12:00"):
        time = ""
    return f"{year}-{month}-{day}", time


def parse_qfx(text: str) -> list[dict]:
    currency = field(text, "CURDEF") or "USD"
    txs = []
    for block in re.findall(r"<STMTTRN>(.*?)</STMTTRN>", text, re.DOTALL | re.I):
        posted_date, posted_time = parse_stamp(field(block, "DTPOSTED"))
        transaction_date, transaction_time = parse_stamp(field(block, "DTUSER"))
        memo = field(block, "MEMO")
        name = field(block, "NAME") or memo
        txs.append(
            {
                "fitid": field(block, "FITID"),
                "date": transaction_date or posted_date,
                "time": transaction_time if transaction_date else posted_time,
                "posted_date": posted_date,
                "raw_amount": field(block, "TRNAMT"),
                "currency": field(block, "ORIGCURRENCY") or currency,
                "desc": name,
                "notes": [memo] if memo and memo != name else [],
            }
        )
    return txs


def relevant(txs: list[dict], since: str) -> list[dict]:
    """Drop credits (payments received, refunds; positive TRNAMT in card QFX)
    and transactions posted before the fetch window."""
    kept = []
    for tx in txs:
        amount = float(tx["raw_amount"] or 0)
        if tx["posted_date"] < since:
            reason = f"posted before fetch window ({tx['posted_date']})"
        elif amount >= 0:
            reason = "credit"
        else:
            kept.append(tx)
            continue
        log.info(
            "Skipping %s: %s %s %s %s",
            reason,
            tx["date"],
            tx["raw_amount"],
            tx["currency"],
            tx["desc"],
        )
    return kept


def normalize(tx: dict) -> dict:
    notes = list(tx["notes"])
    if tx["posted_date"] and tx["posted_date"] != tx["date"]:
        notes.append(f"posted {tx['posted_date']}")
    return {
        "date": tx["date"],
        "time": tx["time"],
        "amount": f"{-float(tx['raw_amount']):.2f}".rstrip("0").rstrip("."),
        "currency": tx["currency"],
        "desc": tx["desc"],
        "source": SOURCE,
        "notes": notes,
    }


def fetch(config: dict, days: int = DAYS) -> list[dict]:
    FOLDER.mkdir(exist_ok=True)
    if "natbank_user" in config:
        download(config, FOLDER)
    files = sorted(p for p in FOLDER.glob("*") if p.suffix.lower() in (".qfx", ".ofx"))
    if not files:
        sys.exit(
            f"No .qfx/.ofx files in {FOLDER}. Download the recent activity "
            "from 24-7cardaccess.com first (see natbank.py)."
        )
    txs: dict[str, dict] = {}
    for path in files:
        parsed = parse_qfx(path.read_text(errors="replace"))
        log.info("Parsed %d transaction(s) from %s", len(parsed), path.name)
        for tx in parsed:
            txs.setdefault(tx["fitid"] or f"{path.name}:{len(txs)}", tx)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    log.info("Keeping Natbank transactions posted since %s", since)
    return [normalize(tx) for tx in relevant(list(txs.values()), since)]
