#!/usr/bin/env python3
"""
tracker_us.py — US holdings monitor (the US pipeline had no exit
discipline between monthly screener runs: stops were computed at pick
time and then nothing ever watched them).

Every 30 min during US market hours (9:30-16:00 ET, gated internally):
  - STOP BREACH   price <= stop_loss_price          -> EXIT NOW alert
  - PROFIT +50%   gain >= 50%                        -> exit all alert
  - PROFIT +35%   gain >= 35%                        -> trim 30% alert
  - PROFIT +20%   gain >= 20%                        -> trim 30% alert
  - DEAD MONEY    held >= 3 months and gain < 5%     -> exit alert
  - WATCH         price within 5% of stop            -> heads-up alert
(Same staged rules as the India rebalancer, applied daily instead of
monthly. Alerts are advisory — US execution is manual.)

Once per day it also POSTs a performance snapshot (portfolio vs S&P 500
vs Nasdaq day-change) to /us/performance/upload — the dashboard's US
performance chart had no producer and was permanently empty.

Deduplication: max 1 Telegram per (ticker, alert_type) per day.

Suggested cron (UTC, covers 9:30-16:00 ET in both EST and EDT; the
in-script gate handles the rest):
  */30 13-21 * * 1-5 /home/ubuntu/kite/run_tracker_us.sh >> /home/ubuntu/kite/tracker_us.log 2>&1

Usage:
  python tracker_us.py            # normal run (market-hours gated)
  python tracker_us.py --force    # bypass market-hours gate
  python tracker_us.py --no-telegram
"""

import os
import json
import argparse
from datetime import datetime, date
from zoneinfo import ZoneInfo
import urllib.request as _req

API_URL       = os.getenv("API_URL", "https://web-production-50eee.up.railway.app").rstrip("/")
UPLOAD_TOKEN  = os.getenv("UPLOAD_TOKEN", "")
TELEGRAM_BOT  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
DATA_DIR      = os.getenv("DATA_DIR", ".")

US_EASTERN = ZoneInfo("America/New_York")

# Staged exit rules — mirror rebalancer.py's India stages
PROFIT_STAGES = [
    (50.0, "PROFIT_50", "🎉", "Sell ALL — +50% target reached, book the win"),
    (35.0, "PROFIT_35", "💰", "Sell 30% — book partial profits at +35%"),
    (20.0, "PROFIT_20", "💵", "Sell 30% — book partial profits at +20%"),
]
DEAD_MONEY_MONTHS = 3
DEAD_MONEY_MAX_GAIN = 5.0
WATCH_ZONE_PCT = 5.0

DEDUP_FILE = os.path.join(DATA_DIR, "us_alerts_sent.json")
PERF_DEDUP_FILE = os.path.join(DATA_DIR, "us_perf_snapshot_dedup.txt")


def _get(url, headers=None):
    r = _req.Request(url, headers={**(headers or {}), "Accept": "application/json"})
    with _req.urlopen(r, timeout=20) as resp:
        return json.loads(resp.read())


def _post(url, payload, headers=None):
    body = json.dumps(payload).encode()
    h = {"Content-Type": "application/json", **(headers or {})}
    r = _req.Request(url, data=body, headers=h, method="POST")
    with _req.urlopen(r, timeout=20) as resp:
        return json.loads(resp.read())


def _tg(msg: str, enabled: bool = True):
    if not enabled or not TELEGRAM_BOT or not TELEGRAM_CHAT:
        return
    try:
        _post(f"https://api.telegram.org/bot{TELEGRAM_BOT}/sendMessage",
              {"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "HTML"})
    except Exception as e:
        print(f"  ⚠️  Telegram failed: {e}")


def us_market_open(now=None) -> bool:
    now = now or datetime.now(US_EASTERN)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return (9 * 60 + 30) <= minutes <= (16 * 60)


# ─────────────────────────────────────────────
# PRICES
# ─────────────────────────────────────────────

def fetch_prices(tickers: list) -> dict:
    """{ticker: {"last": float, "prev_close": float}} — last intraday price
    plus previous close (for day-change). Failures skip the ticker."""
    import yfinance as yf
    out = {}
    for t in tickers:
        try:
            tk = yf.Ticker(t)
            last = None
            try:
                last = float(tk.fast_info["last_price"])
            except Exception:
                pass
            hist = tk.history(period="5d", interval="1d")
            closes = hist["Close"].dropna()
            if last is None and not closes.empty:
                last = float(closes.iloc[-1])
            prev = float(closes.iloc[-2]) if len(closes) >= 2 else None
            if last and last > 0:
                out[t] = {"last": last, "prev_close": prev}
        except Exception as e:
            print(f"  ⚠️  Price fetch failed for {t}: {e}")
    return out


# ─────────────────────────────────────────────
# ALERT LOGIC (pure — unit-testable)
# ─────────────────────────────────────────────

def months_held(buy_date_str: str, today=None) -> float:
    try:
        bd = datetime.strptime(str(buy_date_str)[:10], "%Y-%m-%d").date()
        return ((today or date.today()) - bd).days / 30.44
    except Exception:
        return 0.0


def check_position(stock: dict, price: float, today=None) -> dict | None:
    """Highest-priority alert for one holding, or None. Returns
    {alert, emoji, headline, detail}."""
    ticker = stock.get("ticker", "?")
    buy = float(stock.get("buy_price") or stock.get("price")
                or stock.get("current_price") or 0)
    stop = float(stock.get("stop_loss_price") or stock.get("stop_loss") or 0)
    if buy <= 0 or price <= 0:
        return None
    gain = (price / buy - 1) * 100

    if stop > 0 and price <= stop:
        return {"alert": "STOP_BREACH", "emoji": "🛑",
                "headline": f"STOP BREACHED — {ticker}",
                "detail": (f"${price:,.2f} ≤ stop ${stop:,.2f} "
                           f"({gain:+.1f}% vs buy ${buy:,.2f}). "
                           f"EXIT NOW on your broker — no automated US stops exist.")}

    for threshold, name, emoji, action in PROFIT_STAGES:
        if gain >= threshold:
            return {"alert": name, "emoji": emoji,
                    "headline": f"{name.replace('_', ' +')}% — {ticker}",
                    "detail": f"${price:,.2f} ({gain:+.1f}% vs buy ${buy:,.2f}). {action}."}

    held = months_held(stock.get("buy_date") or stock.get("entry_date") or "", today)
    if held >= DEAD_MONEY_MONTHS and gain < DEAD_MONEY_MAX_GAIN:
        return {"alert": "DEAD_MONEY", "emoji": "🪦",
                "headline": f"DEAD MONEY — {ticker}",
                "detail": (f"Held {held:.1f} months, only {gain:+.1f}%. "
                           f"Capital is idle — exit and redeploy into a current pick.")}

    if stop > 0 and price <= stop * (1 + WATCH_ZONE_PCT / 100):
        return {"alert": "WATCH", "emoji": "⚠️",
                "headline": f"WATCH — {ticker} near stop",
                "detail": (f"${price:,.2f} is within {WATCH_ZONE_PCT:.0f}% of "
                           f"stop ${stop:,.2f} ({gain:+.1f}%). Be ready to exit.")}
    return None


# ─────────────────────────────────────────────
# DEDUP
# ─────────────────────────────────────────────

def _load_dedup() -> dict:
    try:
        with open(DEDUP_FILE) as f:
            data = json.load(f)
        return data if data.get("date") == date.today().isoformat() else {}
    except Exception:
        return {}


def _mark_sent(sent: dict, key: str):
    sent.setdefault("date", date.today().isoformat())
    sent.setdefault("sent", {})[key] = True
    try:
        with open(DEDUP_FILE, "w") as f:
            json.dump(sent, f)
    except Exception as e:
        print(f"  ⚠️  Could not persist dedup state: {e}")


# ─────────────────────────────────────────────
# DAILY PERFORMANCE SNAPSHOT
# ─────────────────────────────────────────────

def capture_performance(holdings: list, prices: dict):
    """Once per day: portfolio vs S&P 500 vs Nasdaq day-change % ->
    /us/performance/upload. This is the producer the dashboard's US
    performance chart never had."""
    today_str = date.today().isoformat()
    try:
        with open(PERF_DEDUP_FILE) as f:
            if f.read().strip() == today_str:
                return
    except Exception:
        pass

    total_prev = total_chg = 0.0
    for s in holdings:
        t = s.get("ticker")
        shares = float(s.get("approx_shares") or s.get("shares") or 0)
        p = prices.get(t) or {}
        if shares > 0 and p.get("last") and p.get("prev_close"):
            total_prev += p["prev_close"] * shares
            total_chg += (p["last"] - p["prev_close"]) * shares
    portfolio_pct = round(total_chg / total_prev * 100, 2) if total_prev > 0 else None

    bench = fetch_prices(["^GSPC", "^IXIC"])
    def _day_pct(sym):
        b = bench.get(sym) or {}
        if b.get("last") and b.get("prev_close"):
            return round((b["last"] / b["prev_close"] - 1) * 100, 2)
        return None
    sp500_pct, nasdaq_pct = _day_pct("^GSPC"), _day_pct("^IXIC")

    if portfolio_pct is None and sp500_pct is None:
        print("  ⚠️  No prices for snapshot — skipping.")
        return
    rec = {"date": today_str, "portfolio_pct": portfolio_pct,
           "sp500_pct": sp500_pct, "nasdaq_pct": nasdaq_pct}
    try:
        headers = {"X-Upload-Token": UPLOAD_TOKEN} if UPLOAD_TOKEN else {}
        _post(f"{API_URL}/us/performance/upload", rec, headers)
        with open(PERF_DEDUP_FILE, "w") as f:
            f.write(today_str)
        print(f"  ✅ Perf snapshot: portfolio {portfolio_pct}%, "
              f"S&P {sp500_pct}%, Nasdaq {nasdaq_pct}%")
    except Exception as e:
        print(f"  ⚠️  Snapshot upload failed: {e}")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main(force=False, telegram=True):
    now_et = datetime.now(US_EASTERN)
    print(f"=== US Tracker — {now_et:%Y-%m-%d %H:%M} ET ===")
    if not force and not us_market_open(now_et):
        print("  US market closed — exiting (use --force to override).")
        return

    try:
        raw = _get(f"{API_URL}/us/portfolio/live") or {}
        # Live portfolio is a dict of buckets: {key: {label, stocks: [...]}}
        holdings = []
        if isinstance(raw, dict):
            for b in raw.values():
                if isinstance(b, dict):
                    holdings.extend(b.get("stocks") or [])
        elif isinstance(raw, list):
            holdings = raw
    except Exception as e:
        print(f"  ❌ Could not fetch US live portfolio: {e}")
        return
    if not holdings:
        print("  No US holdings — nothing to monitor.")
        return
    print(f"  {len(holdings)} holding(s): "
          + ", ".join(s.get("ticker", "?") for s in holdings))

    prices = fetch_prices([s["ticker"] for s in holdings if s.get("ticker")])
    capture_performance(holdings, prices)

    sent = _load_dedup()
    already = sent.get("sent", {})
    alerts = []
    for s in holdings:
        t = s.get("ticker")
        p = (prices.get(t) or {}).get("last")
        if not p:
            continue
        a = check_position(s, p)
        if not a:
            continue
        key = f"{t}:{a['alert']}"
        if already.get(key):
            print(f"  (deduped) {a['headline']}")
            continue
        alerts.append((key, a))
        print(f"  {a['emoji']} {a['headline']} — {a['detail']}")

    if not alerts:
        print("  No new alerts.")
        return
    msg = "🇺🇸 <b>US Portfolio Alerts</b>\n\n" + "\n\n".join(
        f"{a['emoji']} <b>{a['headline']}</b>\n{a['detail']}" for _, a in alerts)
    _tg(msg, telegram)
    for key, _ in alerts:
        _mark_sent(sent, key)
    print(f"  📨 {len(alerts)} alert(s) sent.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="US holdings stop/profit/dead-money monitor")
    ap.add_argument("--force", action="store_true", help="bypass market-hours gate")
    ap.add_argument("--no-telegram", action="store_true", help="print only")
    args = ap.parse_args()
    main(force=args.force, telegram=not args.no_telegram)
