"""
FII/DII Daily Data Collector
==============================
Fetches today's FII and DII net buy/sell figures from NSE
and appends them to fiidii_history.json.

Scheduled on Railway: every weekday at 4:00 PM IST
(after NSE publishes the day's data — usually by 3:45 PM)

The history file is kept to last 90 trading days maximum.
The screener reads from this file for its 10-day rolling signal.

Usage:
    python collector.py              # fetch today and append
    python collector.py --date 2026-04-15  # backfill a specific date
    python collector.py --status     # show current history summary
"""

import json

# Sent with every API POST; uploads are rejected when the server has
# UPLOAD_TOKEN set and this env var is missing or wrong.
import os as _os_tok
_UPLOAD_AUTH = {"X-Upload-Token": _os_tok.environ["UPLOAD_TOKEN"]} if _os_tok.getenv("UPLOAD_TOKEN") else {}

import os
import argparse
import time
import requests
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

IST              = ZoneInfo("Asia/Kolkata")
HISTORY_FILE     = os.path.join(os.getenv("DATA_DIR", os.path.dirname(__file__)), "fiidii_history.json")
MAX_HISTORY_DAYS = 90      # keep last 90 trading days
NSE_FIIDII_URL   = "https://www.nseindia.com/api/fiidiiTradeReact"

NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.nseindia.com/",
}


# ─────────────────────────────────────────────
# HISTORY FILE HELPERS
# ─────────────────────────────────────────────

def load_history() -> list:
    """
    Load FII/DII history with two-source strategy:
      1. GET /fiidii from the API  ← always has the full merged history
      2. Fall back to local disk   ← only has recent runs (volume resets on deploy)

    This ensures the collector always starts with the FULL history regardless
    of whether its own volume was wiped on a Railway redeploy. Without this,
    the collector would only know about the last 3-4 days and would overwrite
    the API's full history with a truncated version.
    """
    import urllib.request as _ur
    import urllib.error   as _ure

    api_url = os.getenv("API_URL", "https://web-production-50eee.up.railway.app").rstrip("/")
    get_url = f"{api_url}/fiidii"
    print(f"  🌐 Fetching history from API: {get_url}")

    # ── Source 1: API (always authoritative) ──────────────────────
    try:
        req = _ur.Request(get_url, headers={"Accept": "application/json"})
        with _ur.urlopen(req, timeout=10) as resp:
            api_data = json.loads(resp.read().decode())
            if api_data and isinstance(api_data, list) and len(api_data) > 0:
                print(f"  📡 History loaded from API: {len(api_data)} records")
                # Also sync to local disk so we have a backup
                try:
                    with open(HISTORY_FILE, "w") as f:
                        json.dump(api_data, f, indent=2)
                except Exception:
                    pass
                return api_data
    except _ure.HTTPError as e:
        hint = " — check API_URL env var in Railway collector service" if e.code == 404 else ""
        print(f"  ⚠️  Could not fetch history from API (HTTP {e.code}{hint}) — falling back to local disk")
    except Exception as e:
        print(f"  ⚠️  Could not fetch history from API ({e}) — falling back to local disk")

    # ── Source 2: Local disk (fallback) ───────────────────────────
    if not os.path.exists(HISTORY_FILE):
        print(f"  ⚠️  No local history file either — starting fresh")
        return []
    try:
        with open(HISTORY_FILE) as f:
            local_data = json.load(f)
            print(f"  💾 History loaded from local disk: {len(local_data)} records")
            return local_data
    except Exception:
        return []


def save_history(records: list):
    """Save history, keeping only last MAX_HISTORY_DAYS records."""
    # Sort by date descending, keep most recent
    records = sorted(records, key=lambda r: r["date"], reverse=True)
    records = records[:MAX_HISTORY_DAYS]
    with open(HISTORY_FILE, "w") as f:
        json.dump(records, f, indent=2)


def date_already_exists(history: list, target_date: str) -> bool:
    """Check if a date is already in history."""
    return any(r["date"] == target_date for r in history)


# ─────────────────────────────────────────────
# NSE FETCHER
# ─────────────────────────────────────────────

def fetch_from_nse() -> list:
    """
    Fetch FII/DII data from NSE API.
    Returns list of {date, fii_net_cr, dii_net_cr} records.

    NSE returns multiple days — we take all available
    to catch up if the collector missed any days.
    """
    session = requests.Session()

    # Hit main page first to get session cookies
    try:
        session.get("https://www.nseindia.com", headers=NSE_HEADERS, timeout=10)
        time.sleep(1.5)
    except Exception:
        pass  # proceed anyway — sometimes works without

    resp = session.get(NSE_FIIDII_URL, headers=NSE_HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    # Debug: print first row keys so we can see the actual field names
    if data and isinstance(data, list) and len(data) > 0:
        print(f"  🔍 NSE API sample keys: {list(data[0].keys())}")
        print(f"  🔍 NSE API first row: {data[0]}")
    
    if not data or not isinstance(data, list):
        raise ValueError(f"Unexpected NSE response format: {type(data)}")

    # NSE returns rows like:
    # {"category": "FII/FPI", "date": "17-Apr-2026", "buyValue": "...", "sellValue": "...", "netValue": "..."}
    # {"category": "DII",     "date": "17-Apr-2026", "buyValue": "...", "sellValue": "...", "netValue": "..."}
    # Group by date, collect FII and DII netValue per date

    from collections import defaultdict
    date_data = defaultdict(lambda: {"fii": None, "dii": None, "date_str": None})

    for row in data:
        category = str(row.get("category", "")).upper().strip()
        raw_date  = row.get("date") or row.get("Date") or row.get("tradeDate") or row.get("TRADE_DATE")
        net_raw   = row.get("netValue") or row.get("net") or row.get("NET") or row.get("netBuy") or 0

        if not raw_date:
            continue

        parsed_date = _parse_nse_date(str(raw_date))
        if not parsed_date:
            continue

        try:
            net_val = float(str(net_raw).replace(",", "").replace("+", "").strip())
        except (ValueError, AttributeError):
            continue

        date_data[parsed_date]["date_str"] = parsed_date

        if "FII" in category or "FPI" in category:
            date_data[parsed_date]["fii"] = net_val
        elif "DII" in category:
            date_data[parsed_date]["dii"] = net_val

    records = []
    for parsed_date, vals in date_data.items():
        fii_net = vals["fii"]
        dii_net = vals["dii"]

        # Skip if both missing
        if fii_net is None and dii_net is None:
            continue

        fii_net = fii_net if fii_net is not None else 0.0
        dii_net = dii_net if dii_net is not None else 0.0

        # Skip rows where both are exactly 0 (data pending)
        if fii_net == 0.0 and dii_net == 0.0:
            print(f"  ⚠️  Skipping {parsed_date} — both FII and DII are 0 (data pending)")
            continue

        print(f"  📊 {parsed_date}: FII ₹{fii_net:+,.2f}Cr | DII ₹{dii_net:+,.2f}Cr")
        records.append({
            "date":       parsed_date,
            "fii_net_cr": round(fii_net, 2),
            "dii_net_cr": round(dii_net, 2),
            "source":     "nse_api",
        })

    return records


def _parse_nse_date(raw: str) -> str | None:
    """
    Try to parse various date formats NSE uses.
    Returns YYYY-MM-DD string or None if unparseable.
    """
    formats = [
        "%d-%b-%Y",   # 15-Apr-2026
        "%Y-%m-%d",   # 2026-04-15
        "%d/%m/%Y",   # 15/04/2026
        "%d-%m-%Y",   # 15-04-2026
        "%b %d, %Y",  # Apr 15, 2026
    ]
    for fmt in formats:
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


# ─────────────────────────────────────────────
# MANUAL ENTRY (for days NSE API misses)
# ─────────────────────────────────────────────

def add_manual_entry(
    target_date: str,
    fii_net: float,
    dii_net: float,
):
    """Add a manual FII/DII entry for a specific date."""
    history = load_history()

    if date_already_exists(history, target_date):
        # Update existing entry
        for r in history:
            if r["date"] == target_date:
                r["fii_net_cr"] = fii_net
                r["dii_net_cr"] = dii_net
                r["source"]     = "manual_override"
        print(f"  ✅ Updated existing entry for {target_date}")
    else:
        history.append({
            "date":       target_date,
            "fii_net_cr": fii_net,
            "dii_net_cr": dii_net,
            "source":     "manual_entry",
        })
        print(f"  ✅ Added manual entry for {target_date}: "
              f"FII ₹{fii_net:+,.2f}Cr | DII ₹{dii_net:+,.2f}Cr")

    save_history(history)


# ─────────────────────────────────────────────
# MAIN COLLECTION RUN
# ─────────────────────────────────────────────

def _post_to_api(history: list):
    """POST FII/DII history to the web API so it can serve fresh data."""
    import urllib.request as _urllib
    import urllib.error as _urlerr
    api_url = os.getenv("API_URL", "https://web-production-50eee.up.railway.app").rstrip("/")
    url = f"{api_url}/fiidii/upload"
    print(f"  📤 POSTing {len(history)} records to: {url}")
    try:
        payload = json.dumps(history).encode("utf-8")
        req = _urllib.Request(url, data=payload,
                              headers={"Content-Type": "application/json", **_UPLOAD_AUTH},
                              method="POST")
        with _urllib.urlopen(req, timeout=10) as resp:
            print(f"  ✅ FII/DII POSTed to API: {resp.read().decode()}")
    except _urlerr.HTTPError as e:
        hint = " — check API_URL env var in Railway collector service" if e.code == 404 else ""
        print(f"  ⚠️  Could not POST to API (HTTP {e.code}{hint}): {e}")
    except Exception as e:
        print(f"  ⚠️  Could not POST to API (non-fatal): {e}")


def collect(target_date: str = None, force: bool = False):
    """
    Fetch today's (or a specific date's) FII/DII data
    and append to history file.

    Args:
      target_date : YYYY-MM-DD string, defaults to today
      force       : overwrite if date already exists
    """
    today_str = target_date or date.today().strftime("%Y-%m-%d")

    print(f"\n{'='*55}")
    print(f"  📊 FII/DII COLLECTOR")
    print(f"  {datetime.now(IST).strftime('%d %B %Y, %I:%M %p IST')}")
    print(f"{'='*55}")

    history = load_history()

    # Skip if already collected today (unless forced)
    if not force and date_already_exists(history, today_str):
        print(f"  ℹ️  Data for {today_str} already in history — skipping.")
        print(f"  Use --force to overwrite.")
        _print_summary(history)
        return

    print(f"  🌐 Fetching from NSE API...")

    try:
        new_records = fetch_from_nse()
    except Exception as e:
        print(f"  ❌ NSE fetch failed: {e}")
        print(f"  ℹ️  Add manually: python collector.py --manual {today_str} <fii> <dii>")
        return

    if not new_records:
        print(f"  ⚠️  NSE returned no parseable records.")
        return

    # Merge new records into history (avoid duplicates)
    existing_dates = {r["date"] for r in history}
    added = 0

    for record in new_records:
        if record["date"] not in existing_dates or force:
            # Remove existing entry for this date if force
            history = [r for r in history if r["date"] != record["date"]]
            history.append(record)
            existing_dates.add(record["date"])
            added += 1
            print(f"  ✅ {record['date']}: "
                  f"FII ₹{record['fii_net_cr']:+,.2f}Cr | "
                  f"DII ₹{record['dii_net_cr']:+,.2f}Cr")

    if added == 0:
        print(f"  ℹ️  All fetched dates already in history.")
        # Still POST to API — this re-seeds it if the API volume was wiped on redeploy
        print(f"  📡 Re-seeding API with full history ({len(history)} records)...")
        _post_to_api(history)
    else:
        save_history(history)
        print(f"\n  ✅ Added {added} record(s). History now has {len(history)} days.")
        # POST updated history to API so web service can serve it
        _post_to_api(history)

    _print_summary(load_history())


def _print_summary(history: list):
    """Print a rolling summary of the last 10 days."""
    if not history:
        print("  No history available.")
        return

    recent = sorted(history, key=lambda r: r["date"], reverse=True)[:10]

    fii_10d = sum(r["fii_net_cr"] for r in recent)
    dii_10d = sum(r["dii_net_cr"] for r in recent)

    print(f"\n  📋 Last {len(recent)} trading days:")
    print(f"  {'Date':<14} {'FII Net (₹Cr)':>15} {'DII Net (₹Cr)':>15}")
    print(f"  {'-'*44}")
    for r in recent:
        fii_str = f"{r['fii_net_cr']:+,.2f}"
        dii_str = f"{r['dii_net_cr']:+,.2f}"
        print(f"  {r['date']:<14} {fii_str:>15} {dii_str:>15}")
    print(f"  {'-'*44}")
    print(f"  {'10d Total':<14} {fii_10d:>+15,.2f} {dii_10d:>+15,.2f}")

    # Quick signal
    if fii_10d <= -5000:
        signal = "🔴 Strong FII Selling"
    elif fii_10d <= -1000:
        signal = "🟠 Mild FII Selling"
    elif fii_10d >= 5000:
        signal = "🟢 Strong FII Buying"
    elif fii_10d >= 1000:
        signal = "🟢 Mild FII Buying"
    else:
        signal = "🟡 Neutral"

    print(f"\n  Current Signal: {signal}")
    print(f"{'='*55}")


# ─────────────────────────────────────────────
# MAIN ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FII/DII daily data collector")
    parser.add_argument(
        "--date",
        help="Specific date to fetch (YYYY-MM-DD). Defaults to today.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing entry for the date if it already exists.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show current history summary without fetching new data.",
    )
    parser.add_argument(
        "--manual",
        nargs=3,
        metavar=("DATE", "FII", "DII"),
        help=(
            "Add a manual entry. "
            "Example: --manual 2026-04-16 666.20 -569.00"
        ),
    )
    args = parser.parse_args()

    if args.status:
        history = load_history()
        print(f"\n  History file: {HISTORY_FILE}")
        print(f"  Total records: {len(history)}")
        _print_summary(history)

    elif args.manual:
        target_date, fii_str, dii_str = args.manual
        add_manual_entry(
            target_date,
            float(fii_str.replace(",", "")),
            float(dii_str.replace(",", "")),
        )

    else:
        collect(
            target_date = args.date,
            force       = args.force,
        )
