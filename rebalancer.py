# -*- coding: utf-8 -*-
"""
rebalancer.py — Monthly Portfolio Rebalancer
=============================================
Runs on the 1st working day of each month, BEFORE the screener.

What it does:
  1. Loads current portfolio from API (what you bought, at what price)
  2. Fetches live prices from Yahoo Finance
  3. Calculates P&L for each position
  4. Decides: EXIT / TRIM / HOLD for each stock
  5. Computes freed cash available for new screener picks
  6. Sends Telegram message with action list
  7. Saves rebalance report to API

Decision rules:
  TRIM  — Stage 1 (+20%): sell 30% | Stage 2 (+35%): sell 30%
  EXIT  — Stage 3 (+50%): sell all  |  3+ months, no stage hit  |  stop-loss breach
  HOLD  — Everything else

Railway schedule: 0 2 1 * *  (2:00 AM UTC on 1st of every month)

Environment variables required:
  API_URL              — Railway API base URL
  TELEGRAM_BOT_TOKEN   — Telegram bot token
  TELEGRAM_CHAT_ID     — Telegram chat ID

Usage:
  python rebalancer.py              # normal monthly run
  python rebalancer.py --dry-run    # print report, no Telegram
  python rebalancer.py --force      # skip date check (for testing)
"""

import os
import json
import argparse
import time
import math
import urllib.request as _ur
import urllib.error   as _ure
from datetime  import datetime, date, timedelta
from zoneinfo  import ZoneInfo
from typing    import Optional

try:
    import yfinance as yf
except ImportError:
    yf = None

# ─────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────

API_URL      = os.getenv("API_URL", "https://web-production-2d832.up.railway.app")
IST          = ZoneInfo("Asia/Kolkata")

# Profit booking stages: (threshold_pct, label, sell_fraction)
PROFIT_STAGES = [
    (0.50, "Stage 3 — Sell ALL (+50%)",   1.00),   # Full exit
    (0.35, "Stage 2 — Sell 30% (+35%)",   0.30),
    (0.20, "Stage 1 — Sell 30% (+20%)",   0.30),
]

# Max months to hold a stock with no profit stage hit
MAX_HOLD_MONTHS = 3

# Min share count to bother selling (avoid fractional / tiny positions)
MIN_SHARES_TO_SELL = 1

# ─────────────────────────────────────────────
# DATA FETCHING
# ─────────────────────────────────────────────

def _api_get(path: str) -> Optional[dict]:
    """GET from Railway API. Returns parsed JSON or None."""
    try:
        url = f"{API_URL}{path}"
        req = _ur.Request(url, headers={"Accept": "application/json"})
        with _ur.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"  ⚠️  API GET {path} failed: {e}")
        return None


def _api_post(path: str, payload: dict) -> Optional[dict]:
    """POST JSON to Railway API. Returns parsed response or None."""
    try:
        url  = f"{API_URL}{path}"
        data = json.dumps(payload, default=str).encode("utf-8")
        req  = _ur.Request(url, data=data,
                           headers={"Content-Type": "application/json"},
                           method="POST")
        with _ur.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"  ⚠️  API POST {path} failed: {e}")
        return None


def fetch_portfolio() -> Optional[dict]:
    """Load latest portfolio from API."""
    print("  📡 Fetching portfolio from API...")
    data = _api_get("/portfolio/latest")
    if not data:
        print("  ❌ Could not load portfolio from API.")
        return None
    total_stocks = sum(len(b.get("stocks", [])) for b in data.values())
    print(f"  ✅ Portfolio loaded — {len(data)} buckets, {total_stocks} positions")
    return data


def fetch_live_price(ticker: str) -> Optional[float]:
    """Fetch current price from Yahoo Finance with 3-source fallback."""
    if yf is None:
        print(f"  ⚠️  yfinance not installed — cannot fetch {ticker}")
        return None
    try:
        stock = yf.Ticker(ticker)
        # Source 1: fast_info
        price = getattr(stock.fast_info, "last_price", None)
        if price and not math.isnan(price):
            return round(price, 2)
        # Source 2: info regularMarketPrice
        info  = stock.info
        price = info.get("regularMarketPrice") or info.get("currentPrice")
        if price and not math.isnan(float(price)):
            return round(float(price), 2)
        # Source 3: last close from history
        hist = stock.history(period="2d")
        if not hist.empty:
            return round(hist["Close"].iloc[-1], 2)
    except Exception as e:
        print(f"  ⚠️  Price fetch failed for {ticker}: {e}")
    return None


# ─────────────────────────────────────────────
# CORE DECISION ENGINE
# ─────────────────────────────────────────────

def months_held(buy_date_str: str) -> float:
    """Return number of months since buy_date."""
    try:
        buy = datetime.strptime(buy_date_str, "%Y-%m-%d").date()
        delta = date.today() - buy
        return delta.days / 30.44
    except Exception:
        return 0.0


def get_profit_stage(gain_pct: float) -> Optional[tuple]:
    """
    Return the highest profit stage triggered by gain_pct.
    Returns (threshold_pct, label, sell_fraction) or None.
    """
    for threshold, label, fraction in PROFIT_STAGES:
        if gain_pct >= threshold * 100:
            return (threshold, label, fraction)
    return None


def decide_action(stock: dict, current_price: float) -> dict:
    """
    Core decision logic for one stock position.

    Returns a dict with:
      action      : "EXIT" | "TRIM" | "HOLD" | "WATCH"
      reason      : plain English explanation
      gain_pct    : float
      shares_to_sell : int
      proceeds_inr   : float
      stage       : stage label or None
      urgency     : "HIGH" | "MEDIUM" | "LOW"
    """
    ticker         = stock["ticker"]
    buy_price      = stock["price"]            # price at time of screener pick
    shares         = stock.get("approx_shares", 0)
    stop_loss      = stock.get("stop_loss_price", 0)
    buy_date_str   = stock.get("buy_date", str(date.today()))
    held_months    = months_held(buy_date_str)

    # ── P&L ─────────────────────────────────────
    gain_pct   = ((current_price - buy_price) / buy_price) * 100 if buy_price > 0 else 0
    gain_inr   = (current_price - buy_price) * shares

    result = {
        "ticker":          ticker,
        "name":            stock.get("name", ticker),
        "bucket":          None,            # filled by caller
        "buy_price":       buy_price,
        "current_price":   current_price,
        "gain_pct":        round(gain_pct, 2),
        "gain_inr":        round(gain_inr, 2),
        "shares_held":     shares,
        "shares_to_sell":  0,
        "proceeds_inr":    0.0,
        "action":          "HOLD",
        "reason":          "",
        "stage":           None,
        "urgency":         "LOW",
        "held_months":     round(held_months, 1),
        "stop_loss_price": stop_loss,
    }

    # ── Priority 1: Stop-loss breach ─────────────
    if stop_loss and current_price <= stop_loss:
        result.update({
            "action":        "EXIT",
            "reason":        (f"Stop-loss breached — price ₹{current_price} "
                              f"at or below GTT ₹{stop_loss:.2f}. "
                              f"Exit immediately on Kite if GTT hasn't fired."),
            "shares_to_sell": shares,
            "proceeds_inr":   round(current_price * shares, 2),
            "urgency":        "HIGH",
        })
        return result

    # ── Priority 2: Profit stage hit ─────────────
    stage = get_profit_stage(gain_pct)
    if stage:
        threshold_pct, label, fraction = stage
        if threshold_pct == 1.00 or fraction == 1.00:
            # Stage 3 — full exit
            shares_to_sell = shares
            action = "EXIT"
        else:
            # Stage 1 or 2 — partial trim
            shares_to_sell = max(MIN_SHARES_TO_SELL, round(shares * fraction))
            action = "TRIM"

        result.update({
            "action":         action,
            "reason":         (f"{label} triggered at +{gain_pct:.1f}%. "
                               f"Sell {shares_to_sell} of {shares} shares @ ₹{current_price}."),
            "shares_to_sell": shares_to_sell,
            "proceeds_inr":   round(current_price * shares_to_sell, 2),
            "stage":          label,
            "urgency":        "HIGH" if action == "EXIT" else "MEDIUM",
        })
        return result

    # ── Priority 3: Dead money — held too long, no progress ──
    if held_months >= MAX_HOLD_MONTHS and gain_pct < 5.0:
        result.update({
            "action":         "EXIT",
            "reason":         (f"Dead money — held {held_months:.1f} months with "
                               f"only {gain_pct:+.1f}% gain. Redeploy into "
                               f"next screener picks."),
            "shares_to_sell": shares,
            "proceeds_inr":   round(current_price * shares, 2),
            "urgency":        "MEDIUM",
        })
        return result

    # ── Priority 4: Approaching stop-loss (within 5%) ────────
    if stop_loss and current_price <= stop_loss * 1.05:
        result.update({
            "action":   "WATCH",
            "reason":   (f"Price ₹{current_price} is within 5% of stop-loss "
                         f"₹{stop_loss:.2f}. Monitor closely. "
                         f"Current: {gain_pct:+.1f}%."),
            "urgency":  "MEDIUM",
        })
        return result

    # ── Default: HOLD ────────────────────────────
    stage_pct  = 20  # next stage threshold
    to_stage_1 = ((buy_price * 1.20) - current_price) / buy_price * 100

    result.update({
        "action":  "HOLD",
        "reason":  (f"No action needed. P&L: {gain_pct:+.1f}% "
                    f"(₹{gain_inr:+,.0f}). "
                    f"Need +{to_stage_1:.1f}% more to hit Stage 1."),
        "urgency": "LOW",
    })
    return result


# ─────────────────────────────────────────────
# FULL REBALANCE RUN
# ─────────────────────────────────────────────

def run_rebalance(portfolio: dict) -> dict:
    """
    Run full rebalance logic across all buckets.
    Returns structured report dict.
    """
    now_ist = datetime.now(IST)
    report  = {
        "date":        now_ist.strftime("%d %B %Y"),
        "timestamp":   now_ist.isoformat(),
        "actions":     [],          # all decisions
        "exits":       [],          # full exits
        "trims":       [],          # partial sells
        "watches":     [],          # approaching stop-loss
        "holds":       [],          # no action
        "total_freed_inr":   0.0,   # cash from exits + trims
        "original_buffer_inr": 0.0, # leftover from original budget
        "new_budget_inr":    0.0,   # freed + buffer = next month budget
        "price_errors":      [],    # tickers we couldn't price
    }

    print(f"\n  Fetching live prices for all positions...")

    for bucket_key, bucket in portfolio.items():
        bucket_label = bucket.get("label", bucket_key)
        stocks       = bucket.get("stocks", [])

        for stock in stocks:
            ticker = stock["ticker"]
            print(f"    {ticker}... ", end="", flush=True)

            current_price = fetch_live_price(ticker)
            time.sleep(0.3)   # rate limit

            if current_price is None:
                print("❌ price unavailable")
                report["price_errors"].append(ticker)
                continue

            print(f"₹{current_price:,.2f}")

            decision           = decide_action(stock, current_price)
            decision["bucket"] = bucket_label

            report["actions"].append(decision)

            if decision["action"] == "EXIT":
                report["exits"].append(decision)
                report["total_freed_inr"] += decision["proceeds_inr"]
            elif decision["action"] == "TRIM":
                report["trims"].append(decision)
                report["total_freed_inr"] += decision["proceeds_inr"]
            elif decision["action"] == "WATCH":
                report["watches"].append(decision)
            else:
                report["holds"].append(decision)

    # ── Cash buffer from original deployment ─────
    # Original budget ₹1,00,000 minus deployed amount
    total_deployed = sum(
        s.get("allocation_inr", 0)
        for b in portfolio.values()
        for s in b.get("stocks", [])
    )
    original_buffer = max(0, 100_000 - total_deployed)
    report["original_buffer_inr"] = round(original_buffer, 2)
    report["total_freed_inr"]     = round(report["total_freed_inr"], 2)
    report["new_budget_inr"]      = round(
        report["original_buffer_inr"] + report["total_freed_inr"], 2
    )

    return report


# ─────────────────────────────────────────────
# REPORTING
# ─────────────────────────────────────────────

def _emoji(action: str) -> str:
    return {"EXIT": "🔴", "TRIM": "🟡", "WATCH": "👁", "HOLD": "🟢"}.get(action, "⚪")


def format_terminal_report(report: dict) -> str:
    """Format full report for Railway logs."""
    lines = []
    lines.append("=" * 60)
    lines.append(f"  📊 MONTHLY REBALANCE REPORT — {report['date']}")
    lines.append("=" * 60)

    lines.append(f"\n  Summary:")
    lines.append(f"    🔴 Exits  : {len(report['exits'])}")
    lines.append(f"    🟡 Trims  : {len(report['trims'])}")
    lines.append(f"    👁  Watches: {len(report['watches'])}")
    lines.append(f"    🟢 Holds  : {len(report['holds'])}")

    lines.append(f"\n  💰 Cash Position:")
    lines.append(f"    Cash freed (exits + trims) : ₹{report['total_freed_inr']:>10,.2f}")
    lines.append(f"    Original cash buffer       : ₹{report['original_buffer_inr']:>10,.2f}")
    lines.append(f"    ─────────────────────────────────────────")
    lines.append(f"    Budget for next screener   : ₹{report['new_budget_inr']:>10,.2f}")

    if report["exits"] or report["trims"]:
        lines.append(f"\n  ⚡ Actions Required on Kite:")
        lines.append(f"  {'─' * 56}")
        for d in sorted(report["actions"],
                        key=lambda x: {"EXIT": 0, "TRIM": 1, "WATCH": 2, "HOLD": 3}[x["action"]]):
            if d["action"] in ("EXIT", "TRIM"):
                emoji = _emoji(d["action"])
                lines.append(f"\n  {emoji} {d['ticker']} — {d['name']}")
                lines.append(f"     Action     : {d['action']} — sell {d['shares_to_sell']} shares")
                lines.append(f"     Buy price  : ₹{d['buy_price']:,.2f}")
                lines.append(f"     Now        : ₹{d['current_price']:,.2f}  ({d['gain_pct']:+.1f}%)")
                lines.append(f"     Proceeds   : ₹{d['proceeds_inr']:,.2f}")
                lines.append(f"     Reason     : {d['reason']}")

    if report["watches"]:
        lines.append(f"\n  👁  Positions to Watch Closely:")
        for d in report["watches"]:
            lines.append(f"    {d['ticker']}  {d['gain_pct']:+.1f}%  — {d['reason']}")

    lines.append(f"\n  ✅ Holds (no action needed):")
    for d in report["holds"]:
        lines.append(
            f"    🟢 {d['ticker']:<18} {d['gain_pct']:>+7.1f}%   ₹{d['current_price']:>8,.2f}   "
            f"({d['held_months']:.1f}mo)"
        )

    if report["price_errors"]:
        lines.append(f"\n  ⚠️  Price fetch errors: {', '.join(report['price_errors'])}")

    lines.append(f"\n  {'=' * 58}")
    return "\n".join(lines)


def format_telegram_message(report: dict) -> str:
    """Format concise Telegram message."""
    lines = []
    lines.append(f"📊 *Monthly Rebalance — {report['date']}*")
    lines.append("")

    # Summary counts
    total_actions = len(report["exits"]) + len(report["trims"])
    if total_actions == 0:
        lines.append("✅ No actions needed this month — all positions healthy.")
    else:
        lines.append(f"*{total_actions} action(s) required on Kite:*")
        lines.append("")

    # Exits
    if report["exits"]:
        lines.append("🔴 *SELL ALL (Exit)*")
        for d in report["exits"]:
            lines.append(
                f"  {d['ticker'].replace('.NS','')} — sell {d['shares_to_sell']} shares "
                f"@ ₹{d['current_price']:,.0f}  ({d['gain_pct']:+.1f}%)"
            )
            lines.append(f"  _Reason: {d['reason'][:80]}..._" if len(d['reason']) > 80
                         else f"  _{d['reason']}_")
            lines.append(f"  Proceeds: ₹{d['proceeds_inr']:,.0f}")
            lines.append("")

    # Trims
    if report["trims"]:
        lines.append("🟡 *PARTIAL SELL (Trim)*")
        for d in report["trims"]:
            lines.append(
                f"  {d['ticker'].replace('.NS','')} — sell {d['shares_to_sell']} of "
                f"{d['shares_held']} shares @ ₹{d['current_price']:,.0f}  ({d['gain_pct']:+.1f}%)"
            )
            lines.append(f"  _Reason: {d['stage']}_")
            lines.append(f"  Proceeds: ₹{d['proceeds_inr']:,.0f}")
            lines.append("")

    # Watches
    if report["watches"]:
        lines.append("👁 *WATCH CLOSELY*")
        for d in report["watches"]:
            lines.append(
                f"  {d['ticker'].replace('.NS','')}  {d['gain_pct']:+.1f}%  "
                f"— near stop-loss ₹{d['stop_loss_price']:,.2f}"
            )
        lines.append("")

    # Holds
    lines.append("🟢 *HOLD (no action)*")
    for d in report["holds"]:
        lines.append(
            f"  {d['ticker'].replace('.NS',''):<14} {d['gain_pct']:>+6.1f}%   "
            f"₹{d['current_price']:>8,.0f}"
        )
    lines.append("")

    # Cash
    lines.append("─" * 30)
    lines.append(f"💰 *Cash for next screener*")
    lines.append(f"  Freed this month  : ₹{report['total_freed_inr']:>10,.0f}")
    lines.append(f"  Existing buffer   : ₹{report['original_buffer_inr']:>10,.0f}")
    lines.append(f"  *Total budget     : ₹{report['new_budget_inr']:>10,.0f}*")

    return "\n".join(lines)


# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────

def send_telegram(message: str) -> bool:
    """Send message via Telegram Bot API."""
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        print("  ⚠️  Telegram not configured — skipping notification.")
        return False

    try:
        payload = json.dumps({
            "chat_id":    chat_id,
            "text":       message,
            "parse_mode": "Markdown",
        }).encode("utf-8")

        req = _ur.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _ur.urlopen(req, timeout=15) as resp:
            resp.read()
            print(f"  ✅ Telegram notification sent.")
            return True

    except _ure.HTTPError as e:
        err = e.read().decode() if e.fp else ""
        print(f"  ❌ Telegram error {e.code}: {err}")
        return False
    except Exception as e:
        print(f"  ❌ Telegram failed: {e}")
        return False


# ─────────────────────────────────────────────
# SAVE REPORT TO API
# ─────────────────────────────────────────────

def save_report_to_api(report: dict):
    """POST rebalance report to /signals/upload endpoint."""
    payload = {
        "type":    "rebalance_report",
        "payload": report,
    }
    result = _api_post("/signals/upload", payload)
    if result:
        print(f"  ✅ Rebalance report saved to API.")
    else:
        print(f"  ⚠️  Could not save report to API (non-fatal).")


# ─────────────────────────────────────────────
# DATE CHECK — only run on 1st working day
# ─────────────────────────────────────────────

def is_first_working_day_of_month() -> bool:
    """
    Returns True if today is the 1st, 2nd, or 3rd of the month AND a weekday.
    (Handles cases where 1st falls on a weekend.)
    """
    today = date.today()
    if today.day > 3:
        return False
    return today.weekday() < 5   # Mon=0 ... Fri=4


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Monthly portfolio rebalancer")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print report only — no Telegram, no API save")
    parser.add_argument("--force",   action="store_true",
                        help="Skip date check (for testing)")
    args = parser.parse_args()

    now_ist = datetime.now(IST)

    print()
    print("=" * 60)
    print(f"  📊 PORTFOLIO REBALANCER")
    print(f"  {now_ist.strftime('%d %B %Y, %I:%M %p IST')}")
    print("=" * 60)

    # ── Date guard ───────────────────────────────
    if not args.force and not is_first_working_day_of_month():
        today = date.today()
        print(f"\n  ⏭️  Skipping — today is {today.strftime('%d %b %Y')} "
              f"(not the 1st working day of the month).")
        print(f"      Use --force to override for testing.\n")
        return

    # ── Load portfolio ───────────────────────────
    portfolio = fetch_portfolio()
    if not portfolio:
        print("\n  ❌ Cannot rebalance — no portfolio data.\n")
        return

    # ── Run rebalance ────────────────────────────
    print(f"\n  Step 1: Analysing {sum(len(b.get('stocks',[])) for b in portfolio.values())} positions...")
    report = run_rebalance(portfolio)

    # ── Print terminal report ────────────────────
    print(format_terminal_report(report))

    if args.dry_run:
        print("\n  [DRY RUN] — No Telegram sent, no API save.\n")
        return

    # ── Send Telegram ────────────────────────────
    print("\n  Step 2: Sending Telegram notification...")
    tg_message = format_telegram_message(report)
    send_telegram(tg_message)

    # ── Save to API ──────────────────────────────
    print("\n  Step 3: Saving report to API...")
    save_report_to_api(report)

    print("\n  ✅ Rebalance complete.\n")


if __name__ == "__main__":
    main()
