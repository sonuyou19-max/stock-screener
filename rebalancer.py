# -*- coding: utf-8 -*-
"""
rebalancer.py — Monthly Portfolio Rebalancer
=============================================
Runs on the 1st working day of each month, BEFORE the screener (3rd).

What it does:
  1. Loads current portfolio from API (what you bought, at what price)
  2. Fetches live prices from Yahoo Finance
  3. Calculates P&L for each position
  4. Decides: EXIT / TRIM / HOLD for each stock using fixed rules
  5. Computes freed cash available for new screener picks
  6. Saves rebalance report to API  ← screener reads this on the 3rd

The screener reads this report to protect HOLD-rated stocks — it will NOT
suggest replacing a stock that the rebalancer has cleared as healthy.

Telegram notifications are handled by the separate alerter service.
This service is a pure analysis + API-save tool.

Decision rules:
  TRIM  — Stage 1 (+20%): sell 30% | Stage 2 (+35%): sell 30%
  EXIT  — Stage 3 (+50%): sell all  |  3+ months, <5% gain  |  stop-loss breach
  WATCH — Within 5% of stop-loss
  HOLD  — Everything else

Railway schedule: 0 2 1 * *  (2:00 AM UTC on 1st of every month)

Environment variables required:
  API_URL   — Railway API base URL

Usage:
  python rebalancer.py              # normal monthly run
  python rebalancer.py --dry-run    # print report only, no API save
  python rebalancer.py --force      # skip date check (for testing)
"""

import os

# Sent with every API POST; uploads are rejected when the server has
# UPLOAD_TOKEN set and this env var is missing or wrong.
import os as _os_tok
_UPLOAD_AUTH = {"X-Upload-Token": _os_tok.environ["UPLOAD_TOKEN"]} if _os_tok.getenv("UPLOAD_TOKEN") else {}

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

API_URL      = os.getenv("API_URL", "https://web-production-50eee.up.railway.app")
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
                           headers={"Content-Type": "application/json", **_UPLOAD_AUTH},
                           method="POST")
        with _ur.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"  ⚠️  API POST {path} failed: {e}")
        return None


def fetch_portfolio() -> Optional[dict]:
    """
    Load LIVE positions from API — what the investor actually holds on Kite.
    Falls back to /portfolio/latest for backward compat.
    """
    print("  📡 Fetching live positions from API...")
    data = _api_get("/portfolio/live")
    if not data or "error" in data:
        print("  ⚠️  No live portfolio — falling back to /portfolio/latest")
        data = _api_get("/portfolio/latest")
    if not data:
        print("  ❌ Could not load portfolio from API.")
        return None
    total_stocks = sum(len(b.get("stocks", [])) for b in data.values())
    print(f"  ✅ Live positions loaded — {len(data)} buckets, {total_stocks} positions")
    return data


def fetch_picks() -> Optional[dict]:
    """Load this month's screener picks for swap comparison."""
    print("  📡 Fetching screener picks from API...")
    data = _api_get("/portfolio/picks")
    if not data or "error" in data:
        return None
    total = sum(len(b.get("stocks", [])) for b in data.values())
    print(f"  ✅ Screener picks loaded — {total} picks this month")
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
    # Prefer the actual fill basis (written by the fill postback) over the
    # screener's scan-day price — gain%, stage triggers and proceeds were
    # all computed off a price the trade never got.
    buy_price      = stock.get("buy_price") or stock["price"]
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
    # Skip stages already recommended in a prior month (last_stage_pct is
    # persisted onto the live record after each run) — a stock parked at
    # +25% used to get the same "sell 30%" recommendation every single
    # month. Only a HIGHER stage than the last one recommended re-fires.
    stage = get_profit_stage(gain_pct)
    last_stage_pct = float(stock.get("last_stage_pct") or 0)
    if stage and stage[0] * 100 <= last_stage_pct:
        stage = None
        result["reason"] = (f"+{gain_pct:.1f}% — stage up to "
                            f"+{last_stage_pct:.0f}% already recommended; "
                            f"next trigger is the following stage.")
    if stage:
        threshold_pct, label, fraction = stage
        result["stage_pct"] = round(threshold_pct * 100, 1)
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
        # keep the stage-already-recommended explanation if one was set
        "reason":  result["reason"] or (
                    f"No action needed. P&L: {gain_pct:+.1f}% "
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

            # Persist the recommended stage onto the live record (saved
            # back by main) so the same stage doesn't re-fire monthly.
            if decision.get("stage_pct"):
                stock["last_stage_pct"] = decision["stage_pct"]
                report["stage_marks"] = report.get("stage_marks", 0) + 1

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
    Returns True on the 1st-3rd of the month, any day of the week.

    The old version also required a weekday, but the cron only fires on
    the 1st — so a month whose 1st fell on a weekend was silently
    SKIPPED entirely (the 2nd/3rd allowance could never trigger).
    Rebalancing doesn't need a live market: yfinance returns the last
    close, and the report is advisory input for the screener on the 3rd.
    """
    return date.today().day <= 3


# ─────────────────────────────────────────────
# SWAP RECOMMENDATIONS
# ─────────────────────────────────────────────

def _build_swap_recommendations(live: dict, picks: dict) -> list:
    """
    Compare live positions vs screener picks bucket by bucket.
    Returns list of swap/keep/new-buy decisions.
    """
    results = []
    all_buckets = set(list(live.keys()) + list(picks.keys()))

    for bucket_key in all_buckets:
        live_bucket  = live.get(bucket_key,  {})
        picks_bucket = picks.get(bucket_key, {})
        bucket_label = live_bucket.get("label") or picks_bucket.get("label") or bucket_key

        live_tickers  = {s["ticker"]: s for s in live_bucket.get("stocks",  [])}
        picks_tickers = {s["ticker"]: s for s in picks_bucket.get("stocks", [])}

        # Stocks in BOTH → KEEP
        for ticker in set(live_tickers) & set(picks_tickers):
            results.append({
                "bucket":    bucket_label,
                "action":    "KEEP",
                "ticker":    ticker,
                "name":      live_tickers[ticker].get("name", ticker),
                "score":     picks_tickers[ticker].get("final_score"),
            })

        # In live but NOT in picks → evaluate for swap
        dropped  = [t for t in live_tickers  if t not in picks_tickers]
        new_ones = [t for t in picks_tickers if t not in live_tickers]

        for i, sell_t in enumerate(dropped):
            sell_s = live_tickers[sell_t]
            if i < len(new_ones):
                buy_t  = new_ones[i]
                buy_s  = picks_tickers[buy_t]
                diff   = (buy_s.get("final_score") or 50) - (sell_s.get("final_score") or 50)
                conviction = "🔴 HIGH" if diff > 15 else "🟡 MEDIUM" if diff > 5 else "🟢 LOW"
                results.append({
                    "bucket":       bucket_label,
                    "action":       "SWAP",
                    "conviction":   conviction,
                    "sell_ticker":  sell_t,
                    "sell_name":    sell_s.get("name", sell_t),
                    "sell_score":   sell_s.get("final_score"),
                    "sell_price":   sell_s.get("price"),
                    "sell_shares":  sell_s.get("approx_shares"),
                    "sell_sl":      sell_s.get("stop_loss_price"),
                    "buy_ticker":   buy_t,
                    "buy_name":     buy_s.get("name", buy_t),
                    "buy_score":    buy_s.get("final_score"),
                    "buy_price":    buy_s.get("price"),
                    "buy_shares":   buy_s.get("approx_shares"),
                    "buy_sl":       buy_s.get("stop_loss_price"),
                    "score_delta":  round(diff, 1),
                })
            else:
                # No replacement found — hold
                results.append({
                    "bucket":    bucket_label,
                    "action":    "HOLD_NO_REPLACE",
                    "ticker":    sell_t,
                    "name":      sell_s.get("name", sell_t),
                    "score":     sell_s.get("final_score"),
                })

        # In picks but no matching dropped → new slot
        for buy_t in new_ones[len(dropped):]:
            buy_s = picks_tickers[buy_t]
            results.append({
                "bucket":      bucket_label,
                "action":      "NEW_BUY",
                "buy_ticker":  buy_t,
                "buy_name":    buy_s.get("name", buy_t),
                "buy_score":   buy_s.get("final_score"),
                "buy_price":   buy_s.get("price"),
                "buy_shares":  buy_s.get("approx_shares"),
                "buy_sl":      buy_s.get("stop_loss_price"),
            })

    return results


def _format_swap_telegram(swaps: list) -> str:
    """Format swap recommendations as a Telegram message."""
    now = datetime.now(IST).strftime("%d %B %Y")
    lines = [f"🔄 *Screener Picks vs Your Holdings — {now}*", ""]

    swap_items = [s for s in swaps if s["action"] == "SWAP"]
    new_items  = [s for s in swaps if s["action"] == "NEW_BUY"]
    keep_items = [s for s in swaps if s["action"] == "KEEP"]
    hold_items = [s for s in swaps if s["action"] == "HOLD_NO_REPLACE"]

    if swap_items:
        lines.append("📊 *Swap Recommendations:*")
        for s in swap_items:
            lines.append(f"  📂 *{s['bucket']}*")
            lines.append(f"  Conviction: {s['conviction']}")
            lines.append(f"  🔴 SELL: {s['sell_ticker'].replace('.NS','')} "
                         f"(Score: {s['sell_score']:.1f}) @ ₹{s['sell_price']:,.2f}")
            lines.append(f"  🟢 BUY:  {s['buy_ticker'].replace('.NS','')} "
                         f"(Score: {s['buy_score']:.1f}) @ ₹{s['buy_price']:,.2f}")
            lines.append(f"  Score improvement: +{s['score_delta']:.1f} pts")
            lines.append(f"  New GTT stop-loss: ₹{s['buy_sl']:,.2f}")
            lines.append(f"  Qty to buy: ~{s['buy_shares']} shares")
            lines.append("")

    if new_items:
        lines.append("🆕 *New Positions (no existing slot):*")
        for s in new_items:
            lines.append(f"  📂 {s['bucket']} — BUY {s['buy_ticker'].replace('.NS','')} "
                         f"@ ₹{s['buy_price']:,.2f} (~{s['buy_shares']} sh)")
        lines.append("")

    if hold_items:
        lines.append("⏸ *Hold — screener found no replacement:*")
        for s in hold_items:
            lines.append(f"  {s['ticker'].replace('.NS','')} — keep holding")
        lines.append("")

    if keep_items:
        lines.append("✅ *Screener confirmed (no change needed):*")
        for s in keep_items:
            lines.append(f"  {s['ticker'].replace('.NS','')} — picked again this month")
        lines.append("")

    if not swap_items and not new_items:
        lines.append("✅ Screener picked all the same stocks as last month.")
        lines.append("No changes recommended.")

    lines.append("─" * 30)
    lines.append("_To act: execute on Kite, then update Live Portfolio on dashboard._")
    return "\n".join(lines)


def _print_swap_report(swaps: list):
    """Print swap report to terminal/Railway logs."""
    print("\n  📊 SWAP RECOMMENDATIONS")
    print("  " + "─" * 56)
    for s in swaps:
        if s["action"] == "SWAP":
            print(f"  🔄 {s['bucket']}")
            print(f"     SELL {s['sell_ticker']:<20} score:{s['sell_score']:.1f}")
            print(f"     BUY  {s['buy_ticker']:<20} score:{s['buy_score']:.1f}  (+{s['score_delta']:.1f} pts)  Conviction:{s['conviction']}")
        elif s["action"] == "NEW_BUY":
            print(f"  🆕 {s['bucket']} — NEW: {s['buy_ticker']} @ ₹{s['buy_price']:,.2f}")
        elif s["action"] == "KEEP":
            print(f"  ✅ {s['ticker']} — picked again, no change")
        elif s["action"] == "HOLD_NO_REPLACE":
            print(f"  ⏸  {s['ticker']} — hold, no replacement found")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Monthly portfolio rebalancer")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print report only — no API save")
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

    # ── Step 1: Load live positions ──────────────
    portfolio = fetch_portfolio()
    if not portfolio:
        print("\n  ❌ Cannot rebalance — no live portfolio data.\n")
        return

    # ── Step 2: Run rebalance logic ──────────────
    n = sum(len(b.get("stocks", [])) for b in portfolio.values())
    print(f"\n  Step 2: Analysing {n} positions...")
    report = run_rebalance(portfolio)

    # ── Step 3: Print full report to logs ────────
    print(format_terminal_report(report))

    if args.dry_run:
        print("\n  [DRY RUN] — report printed, nothing saved to API.\n")
        return

    # ── Step 4: Save to API (screener reads this on the 3rd) ─────────
    print("\n  Step 4: Saving report to API...")
    save_report_to_api(report)

    # ── Step 5: Persist stage markers so trims don't re-fire monthly ──
    if report.get("stage_marks"):
        print(f"\n  Step 5: Saving {report['stage_marks']} stage marker(s) "
              f"to live portfolio...")
        if _api_post("/portfolio/live/upload", portfolio):
            print("  ✅ Stage markers saved.")
        else:
            print("  ⚠️  Could not save stage markers (non-fatal) — "
                  "the same stage may be recommended again next month.")

    print("\n  ✅ Rebalance complete. Screener will read this on the 3rd.\n")


if __name__ == "__main__":
    main()
