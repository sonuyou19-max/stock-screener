"""
Swing Alerter — India NSE
==========================
Dedicated alerter for open swing trading positions.
Separate from the long-term alerter — runs independently.

Monitors open swing positions every 30 minutes during market hours and alerts on:
  1. Stop-loss breach  → 🔴 EXIT NOW on Kite
  2. Target 1 hit (+7%)→ 🟡 Sell 50% on Kite
  3. Target 2 hit (+12%)→ 🟢 Sell remaining 50% on Kite
  4. Time exit         → ⏰ 10 trading days elapsed — exit today
  5. Trailing stop     → 📈 Update your GTT stop on Kite

Separate from alerter.py because:
  - Different exit rules (tighter stops, +7%/+12% targets vs +20%/+35%/+50%)
  - Different holding period (10 days vs monthly)
  - Different dedup file (swing_dedup.json)
  - Can be deployed/modified independently

Schedule: */30 3-10 * * 1-5  (every 30 min, 8:45 AM–4:00 PM UTC = 9:15–15:30 IST)

Usage:
  python swing_alerter.py           # run check
  python swing_alerter.py --force   # bypass market hours
  python swing_alerter.py --test    # run without sending Telegram
"""

import json

# Sent with every API POST; uploads are rejected when the server has
# UPLOAD_TOKEN set and this env var is missing or wrong.
import os as _os_tok
_UPLOAD_AUTH = {"X-Upload-Token": _os_tok.environ["UPLOAD_TOKEN"]} if _os_tok.getenv("UPLOAD_TOKEN") else {}

import os
import time
import argparse
import urllib.request as _urllib
import urllib.error   as _urlerr
import math
from datetime import datetime, date, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import yfinance as yf

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

IST     = ZoneInfo("Asia/Kolkata")
API_URL = os.getenv("API_URL", "https://web-production-50eee.up.railway.app")
DATA_DIR= os.getenv("DATA_DIR", "/data")

# Dedup file — separate from long-term alerter's dedup
SWING_DEDUP_FILE = os.path.join(DATA_DIR, "swing_alerts_sent_today.json")

# Swing exit rules — exact same values as swing_scanner.py
SWING_STOP_MULT  = 2.0     # ATR multiplier for stop-loss (wider of 2 ATR / 5-day swing low)
SWING_TRAIL_MULT = 1.0     # ATR multiplier for trailing stop
SWING_TARGET_1   = 0.07   # +7%  → sell 50%
SWING_TARGET_2   = 0.12   # +12% → sell 50%
SWING_MAX_DAYS   = 10     # force exit after 10 trading days

# Market hours (NSE)
MARKET_OPEN_H  = 9
MARKET_OPEN_M  = 15
MARKET_CLOSE_H = 15
MARKET_CLOSE_M = 30


# ─────────────────────────────────────────────
# MARKET HOURS GUARD
# ─────────────────────────────────────────────

def is_market_hours() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    open_  = now.replace(hour=MARKET_OPEN_H,  minute=MARKET_OPEN_M,  second=0)
    close_ = now.replace(hour=MARKET_CLOSE_H, minute=MARKET_CLOSE_M, second=0)
    return open_ <= now <= close_


# ─────────────────────────────────────────────
# DEDUP (separate from long-term alerter)
# ─────────────────────────────────────────────

def _load_dedup() -> dict:
    try:
        if not os.path.exists(SWING_DEDUP_FILE):
            return {}
        with open(SWING_DEDUP_FILE) as f:
            data = json.load(f)
        if data.get("date") != str(date.today()):
            print("  🗓️  Swing dedup cleared for today.")
            return {}
        keys = data.get("keys", {})
        if keys:
            print(f"  ⏭️  {len(keys)} swing dedup key(s) loaded.")
        return keys
    except Exception:
        return {}


def _save_dedup(keys: dict):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(SWING_DEDUP_FILE, "w") as f:
            json.dump({"date": str(date.today()), "keys": keys}, f)
    except Exception as e:
        print(f"  ⚠️  Could not save swing dedup: {e}")


def _dedup_key(ticker: str, alert_type: str) -> str:
    return f"swing_{ticker}_{alert_type}"


# ─────────────────────────────────────────────
# API HELPERS
# ─────────────────────────────────────────────

def _fetch(endpoint: str):
    """Fetch JSON from API endpoint."""
    try:
        req = _urllib.Request(
            f"{API_URL}{endpoint}",
            headers={"Accept": "application/json"}
        )
        with _urllib.urlopen(req, timeout=12) as r:
            text = r.read().decode()
            text = text.replace(":NaN", ":null").replace(":Infinity", ":null")
            return json.loads(text)
    except Exception as e:
        print(f"  ⚠️  Could not fetch {endpoint}: {e}")
        return None


def _post(endpoint: str, payload: dict) -> bool:
    """POST JSON to API endpoint."""
    try:
        body = json.dumps(payload, default=str).encode("utf-8")
        req = _urllib.Request(
            f"{API_URL}{endpoint}",
            data=body,
            headers={"Content-Type": "application/json", **_UPLOAD_AUTH},
            method="POST"
        )
        with _urllib.urlopen(req, timeout=12) as r:
            r.read()
        return True
    except Exception as e:
        print(f"  ⚠️  POST {endpoint} failed: {e}")
        return False


def _post_json(endpoint: str, payload: dict):
    """POST and return the parsed JSON response (None on failure).
    Longer timeout — the reconcile endpoint talks to the VPS/Zerodha."""
    try:
        body = json.dumps(payload, default=str).encode("utf-8")
        req = _urllib.Request(
            f"{API_URL}{endpoint}",
            data=body,
            headers={"Content-Type": "application/json", **_UPLOAD_AUTH},
            method="POST"
        )
        with _urllib.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"  ⚠️  POST {endpoint} failed: {e}")
        return None


# ─────────────────────────────────────────────
# PRICE FETCHER
# ─────────────────────────────────────────────

def get_price(ticker: str) -> Optional[dict]:
    """
    Fetch current price, previous close, change %.
    Returns None if price unavailable.
    """
    try:
        fi = yf.Ticker(ticker).fast_info
        price = getattr(fi, "last_price", None)
        prev  = getattr(fi, "previous_close", None)

        if price is None or (isinstance(price, float) and math.isnan(price)):
            # Fallback to history
            hist = yf.Ticker(ticker).history(period="2d")
            if hist.empty:
                return None
            price = float(hist["Close"].iloc[-1])
            prev  = float(hist["Close"].iloc[-2]) if len(hist) > 1 else price

        price = float(price)
        prev  = float(prev) if prev else price
        chg   = round((price - prev) / prev * 100, 2) if prev else 0.0

        return {
            "price":      round(price, 2),
            "prev_close": round(prev, 2),
            "change_pct": chg,
        }
    except Exception:
        return None


# ─────────────────────────────────────────────
# TRADING DAYS COUNTER
# ─────────────────────────────────────────────

# NSE trading holidays that fall on weekdays. Without this, every holiday
# over-counted the holding period and time exits fired a day early.
# Update each January from NSE's trading-holiday circular.
NSE_HOLIDAYS = {
    # 2026 (weekday closures)
    "2026-01-26",  # Republic Day
    "2026-03-04",  # Holi
    "2026-04-03",  # Good Friday
    "2026-04-14",  # Dr. Ambedkar Jayanti
    "2026-05-01",  # Maharashtra Day
    "2026-10-02",  # Gandhi Jayanti
    "2026-12-25",  # Christmas
}


def count_trading_days(from_date_str: str) -> int:
    """Trading days (Mon-Fri, minus NSE holidays) since entry date."""
    try:
        start = datetime.strptime(from_date_str, "%Y-%m-%d").date()
        today = date.today()
        count = 0
        current = start
        while current <= today:
            if current.weekday() < 5 and current.isoformat() not in NSE_HOLIDAYS:
                count += 1
            current += timedelta(days=1)
        return max(0, count - 1)  # exclude entry day
    except Exception:
        return 0


# ─────────────────────────────────────────────
# ALERT DETECTOR — core swing logic
# ─────────────────────────────────────────────

def check_position(pos: dict, price_info: dict) -> Optional[dict]:
    """
    Check a single open swing position against all exit rules.
    Returns alert dict if action needed, None if hold.

    Exit priority (same as rebalancer.py logic):
      1. Stop-loss breach
      2. Target 2 (+12%)
      3. Target 1 (+7%)
      4. Time exit (10 trading days)
      5. Trailing stop update
    """
    ticker      = pos.get("ticker", "")
    buy_price   = pos.get("buy_price", 0) or pos.get("price", 0)
    stop_loss   = pos.get("stop_loss", 0) or pos.get("stop_loss_price", 0)
    target1     = pos.get("target1", 0)
    target2     = pos.get("target2", 0)
    trail_dist  = pos.get("trailing_stop", 0) or pos.get("trailing_stop_dist", 0)
    entry_date  = pos.get("entry_date", "") or pos.get("buy_date", "")
    name        = pos.get("name", ticker.replace(".NS", ""))

    curr_price  = price_info["price"]
    change_pct  = price_info["change_pct"]

    if not buy_price or not curr_price:
        return None

    gain_pct    = round((curr_price - buy_price) / buy_price * 100, 2)
    trading_days= count_trading_days(entry_date) if entry_date else 0

    # ── Priority 1: Stop-loss breach ──────────────────────────
    if stop_loss and curr_price <= stop_loss:
        return {
            "ticker":      ticker,
            "name":        name,
            "alert_type":  "stop_loss",
            "urgency":     "HIGH",
            "emoji":       "🔴",
            "title":       f"STOP-LOSS HIT — {ticker.replace('.NS','')}",
            "message":     (
                f"Price ₹{curr_price:.2f} ≤ Stop ₹{stop_loss:.2f}\n"
                f"P&L: {gain_pct:+.1f}%\n"
                f"⚠️  EXIT NOW on Kite — do not wait."
            ),
            "buy_price":   buy_price,
            "curr_price":  curr_price,
            "gain_pct":    gain_pct,
            "stop_loss":   stop_loss,
            "trading_days":trading_days,
        }

    # ── Priority 2: Target 2 hit (+12%) ────────────────────────
    if target2 and curr_price >= target2:
        # Check if target1 was already booked
        t1_booked = pos.get("target1_booked", False)
        return {
            "ticker":      ticker,
            "name":        name,
            "alert_type":  "target2",
            "urgency":     "HIGH",
            "emoji":       "🟢",
            "title":       f"TARGET 2 HIT (+12%) — {ticker.replace('.NS','')}",
            "message":     (
                f"Price ₹{curr_price:.2f} ≥ Target ₹{target2:.2f} (+12%)\n"
                f"{'Sell remaining 50% on Kite' if t1_booked else 'Sell 50% on Kite (T1 may not have been booked yet)'}\n"
                f"P&L: {gain_pct:+.1f}% in {trading_days} trading days 🎯"
            ),
            "buy_price":   buy_price,
            "curr_price":  curr_price,
            "gain_pct":    gain_pct,
            "target":      target2,
            "sell_pct":    50,
            "trading_days":trading_days,
        }

    # ── Priority 3: Target 1 hit (+7%) ────────────────────────
    if target1 and curr_price >= target1 and not pos.get("target1_booked", False):
        return {
            "ticker":      ticker,
            "name":        name,
            "alert_type":  "target1",
            "urgency":     "MEDIUM",
            "emoji":       "🟡",
            "title":       f"TARGET 1 HIT (+7%) — {ticker.replace('.NS','')}",
            "message":     (
                f"Price ₹{curr_price:.2f} ≥ Target ₹{target1:.2f} (+7%)\n"
                f"Sell 50% on Kite. Hold rest for T2 at ₹{target2:.2f} (+12%)\n"
                f"Update stop-loss to break-even ₹{buy_price:.2f}"
            ),
            "buy_price":   buy_price,
            "curr_price":  curr_price,
            "gain_pct":    gain_pct,
            "target":      target1,
            "sell_pct":    50,
            "trading_days":trading_days,
        }

    # ── Priority 4: Time exit (10 trading days) ────────────────
    if trading_days >= SWING_MAX_DAYS and not pos.get("time_exit_alerted", False):
        return {
            "ticker":      ticker,
            "name":        name,
            "alert_type":  "time_exit",
            "urgency":     "MEDIUM",
            "emoji":       "⏰",
            "title":       f"TIME EXIT — {ticker.replace('.NS','')} ({trading_days} days)",
            "message":     (
                f"{trading_days} trading days elapsed — max holding period reached.\n"
                f"P&L: {gain_pct:+.1f}% at ₹{curr_price:.2f}\n"
                f"Exit today on Kite regardless of price."
            ),
            "buy_price":   buy_price,
            "curr_price":  curr_price,
            "gain_pct":    gain_pct,
            "trading_days":trading_days,
        }

    # ── Priority 5: Trailing stop update ──────────────────────
    # A trailing stop RATCHETS UP ONLY. Base it on the high-water mark
    # (highest price seen), NOT the current price — otherwise it drops when
    # the stock pulls back and you get "raise to 321" then "raise to 318".
    # Compare to the EFFECTIVE current stop (which may already have been
    # trailed up / moved to break-even), not the original stop.
    if trail_dist and curr_price > buy_price * 1.02:
        high_water     = max(float(pos.get("trail_high") or 0), curr_price)
        effective_stop = max(float(stop_loss or 0),
                             float(pos.get("live_stop") or 0),
                             float(pos.get("tsl_stop") or 0))
        new_trail_stop = round(high_water - trail_dist, 2)
        # Only suggest when the high-water-based stop is a real improvement
        # ABOVE the current stop → monotonic, never a lower number.
        if effective_stop and new_trail_stop > effective_stop * 1.01:
            return {
                "ticker":      ticker,
                "name":        name,
                "alert_type":  "trail_update",
                "urgency":     "LOW",
                "emoji":       "📈",
                "title":       f"UPDATE TRAILING STOP — {ticker.replace('.NS','')}",
                "message":     (
                    f"High so far ₹{high_water:.2f} (P&L {gain_pct:+.1f}%) — raise your stop.\n"
                    f"Old stop: ₹{effective_stop:.2f}\n"
                    f"New stop: ₹{new_trail_stop:.2f} ({trail_dist:.2f} below the high)\n"
                    f"Update GTT on Kite."
                ),
                "buy_price":   buy_price,
                "curr_price":  curr_price,
                "gain_pct":    gain_pct,
                "old_stop":    effective_stop,
                "new_stop":    new_trail_stop,
                "trading_days":trading_days,
            }

    return None


# ─────────────────────────────────────────────
# TELEGRAM SENDER
# ─────────────────────────────────────────────

def send_telegram(subject: str, body: str) -> bool:
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        print("  ⚠️  Telegram not configured.")
        return False

    message = f"*{subject}*\n\n{body}"
    if len(message) > 4096:
        message = message[:4000] + "\n\n_(truncated)_"

    try:
        payload = json.dumps({
            "chat_id":    chat_id,
            "text":       message,
            "parse_mode": "Markdown",
        }).encode("utf-8")
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        req = _urllib.Request(url, data=payload,
                              headers={"Content-Type": "application/json"},
                              method="POST")
        with _urllib.urlopen(req, timeout=15) as r:
            r.read()
            print(f"  ✅ Telegram sent: {subject[:50]}")
            return True
    except Exception as e:
        print(f"  ❌ Telegram failed: {e}")
        return False


# ─────────────────────────────────────────────
# MAIN ALERTER RUN
# ─────────────────────────────────────────────

def run_swing_alerter(force: bool = False, test: bool = False):
    """
    Full swing alert cycle:
    1. Market hours guard
    2. Load open swing positions from API
    3. Fetch live prices for each position
    4. Check each position for exit signals
    5. Deduplicate — skip alerts already sent today
    6. Send Telegram for each actionable alert
    7. Mark sent alerts
    """
    now_ist = datetime.now(IST)

    print(f"\n{'='*58}")
    print(f"  📊 SWING ALERTER — INDIA NSE")
    print(f"  {now_ist.strftime('%d %B %Y, %I:%M %p IST')}")
    print(f"{'='*58}\n")

    # ── Market hours guard ────────────────────────────────────
    if not force and not test and not is_market_hours():
        print(f"  ⏰ Outside market hours — skipping.")
        return

    # ── Reconcile missed SELL fills FIRST ─────────────────────
    # A GTT exit whose postback was missed leaves the position looking
    # open forever — and this alerter would keep alerting on a stock we
    # no longer hold. The API scans today's Zerodha order book for
    # COMPLETE SELLs on held tickers and processes any not yet in
    # history (deduped by order_id, so this is safe every run).
    rec = _post_json("/swing/reconcile-sells", {})
    if rec and rec.get("processed"):
        for r_ in rec["processed"]:
            print(f"  🔄 Missed exit reconciled: {r_.get('symbol')} → {r_.get('outcome')}")
    elif rec:
        print("  ✅ Sell reconcile: nothing missing")

    # ── Load open swing positions ─────────────────────────────
    positions = _fetch("/swing/live")
    if not positions:
        print("  ℹ️  No open swing positions. Nothing to monitor.")
        return

    if not isinstance(positions, list):
        positions = []

    print(f"  📋 Monitoring {len(positions)} open swing position(s):\n")

    # ── Load dedup ────────────────────────────────────────────
    sent_keys = _load_dedup()
    new_keys  = {}
    alerts_sent = 0

    for pos in positions:
        ticker = pos.get("ticker", "")
        if not ticker:
            continue

        print(f"  ⏳ {ticker:<20} ", end="", flush=True)

        # ── Fetch live price ──────────────────────────────────
        price_info = get_price(ticker)
        if not price_info:
            print("⚠️  price unavailable")
            time.sleep(0.3)
            continue

        curr   = price_info["price"]
        chg    = price_info["change_pct"]
        bp     = pos.get("buy_price", 0) or pos.get("price", 0)
        gain   = round((curr - bp) / bp * 100, 2) if bp else 0
        tdays  = count_trading_days(pos.get("entry_date","") or pos.get("buy_date",""))

        print(f"₹{curr:.2f}  {chg:+.1f}%  P&L:{gain:+.1f}%  {tdays}d", flush=True)

        # ── Check for alerts ──────────────────────────────────
        alert = check_position(pos, price_info)
        if not alert:
            time.sleep(0.3)
            continue

        # ── Dedup check ───────────────────────────────────────
        dedup_key = _dedup_key(ticker, alert["alert_type"])

        # Trail updates can repeat daily — don't dedup them hard
        if alert["alert_type"] == "trail_update":
            # Only send once every 4 hours (check timestamp)
            existing = sent_keys.get(dedup_key, "")
            if existing:
                try:
                    sent_at = datetime.fromisoformat(existing)
                    if (now_ist - sent_at).seconds < 4 * 3600:
                        print(f"    ⏭️  Trail update already sent within 4h — skipping")
                        time.sleep(0.3)
                        continue
                except Exception:
                    pass
        else:
            # All other alerts: once per day
            if dedup_key in sent_keys:
                print(f"    ⏭️  {alert['alert_type']} already sent today — skipping")
                time.sleep(0.3)
                continue

        # ── Send alert ────────────────────────────────────────
        urgency_prefix = {
            "HIGH":   "🚨",
            "MEDIUM": "⚡",
            "LOW":    "📌",
        }.get(alert["urgency"], "📌")

        subject = f"{urgency_prefix} Swing: {alert['title']}"

        if not test:
            sent = send_telegram(subject, alert["message"])
            if sent:
                new_keys[dedup_key] = now_ist.isoformat()
                alerts_sent += 1
        else:
            print(f"\n    [TEST] Would send: {subject}")
            print(f"    {alert['message']}")
            alerts_sent += 1

        time.sleep(0.5)

    # ── Save updated dedup ────────────────────────────────────
    if new_keys and not test:
        sent_keys.update(new_keys)
        _save_dedup(sent_keys)

    # ── Summary ───────────────────────────────────────────────
    print(f"\n{'='*58}")
    print(f"  ✅ Done — {len(positions)} positions checked, {alerts_sent} alert(s) sent")
    print(f"{'='*58}\n")


# ─────────────────────────────────────────────
# STATUS
# ─────────────────────────────────────────────

def show_status():
    """Show all open swing positions with current P&L."""
    positions = _fetch("/swing/live")
    history   = _fetch("/swing/history")

    print(f"\n{'='*58}")
    print(f"  📊 SWING PORTFOLIO STATUS")
    print(f"  {datetime.now(IST).strftime('%d %B %Y, %I:%M %p IST')}")
    print(f"{'='*58}")

    if not positions or not isinstance(positions, list) or len(positions) == 0:
        print("\n  No open swing positions.")
    else:
        print(f"\n  OPEN POSITIONS ({len(positions)}):")
        print(f"  {'Ticker':<14} {'Buy':>8} {'Current':>8} {'P&L':>8} {'Days':>5} {'Target1':>9} {'Stop':>9}")
        print(f"  {'-'*14} {'-'*8} {'-'*8} {'-'*8} {'-'*5} {'-'*9} {'-'*9}")

        for pos in positions:
            ticker = pos.get("ticker","")
            bp     = pos.get("buy_price", 0) or pos.get("price", 0)
            t1     = pos.get("target1", 0)
            sl     = pos.get("stop_loss", 0) or pos.get("stop_loss_price", 0)
            edate  = pos.get("entry_date","") or pos.get("buy_date","")
            tdays  = count_trading_days(edate)

            price_info = get_price(ticker)
            if price_info:
                curr = price_info["price"]
                gain = round((curr - bp) / bp * 100, 2) if bp else 0
                sign = "▲" if gain >= 0 else "▼"
                print(f"  {ticker.replace('.NS',''):<14} ₹{bp:>7.2f} ₹{curr:>7.2f} "
                      f"{sign}{abs(gain):>6.1f}% {tdays:>5}d ₹{t1:>8.2f} ₹{sl:>8.2f}")
            else:
                print(f"  {ticker.replace('.NS',''):<14} ₹{bp:>7.2f} {'—':>8} {'—':>8} "
                      f"{tdays:>5}d ₹{t1:>8.2f} ₹{sl:>8.2f}")
            time.sleep(0.3)

    if history:
        trades = history.get("trades", [])
        if trades:
            total_pnl = history.get("total_pnl", 0)
            winners   = history.get("winners", 0)
            win_rate  = round(winners / len(trades) * 100, 1) if trades else 0
            print(f"\n  CLOSED TRADES: {len(trades)} | "
                  f"Win Rate: {win_rate}% | "
                  f"Total P&L: ₹{total_pnl:,.0f}")

    print(f"{'='*58}\n")


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Swing Alerter — India NSE")
    parser.add_argument("--force",  action="store_true", help="Bypass market hours check")
    parser.add_argument("--test",   action="store_true", help="Run without sending Telegram")
    parser.add_argument("--status", action="store_true", help="Show open swing positions")
    args = parser.parse_args()

    if args.status:
        show_status()
    else:
        run_swing_alerter(force=args.force, test=args.test)
