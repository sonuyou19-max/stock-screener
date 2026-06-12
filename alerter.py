"""
Alert System — Email Notifications
=====================================
Sends email alerts when:
  - A stock hits its ATR stop-loss price     → 🔴 URGENT
  - A stock reaches a profit target stage    → 🟡/🟢 ACTION
  - Portfolio beta exceeds overheated level  → ⚠️  WARNING

Email sent via Gmail SMTP using App Password.
Only sends email when there's something actionable — no noise.

Setup (one-time):
  1. Enable 2FA on your Gmail account
  2. Go to: Google Account → Security → App Passwords
  3. Generate a password for "Mail"
  4. Create a .env file with:
       ALERT_EMAIL_FROM=your@gmail.com
       ALERT_EMAIL_PASSWORD=your_app_password_here
       ALERT_EMAIL_TO=your@gmail.com

Usage:
  python alerter.py --portfolio portfolio_202504.json
  python alerter.py --portfolio portfolio_202504.json --test
"""

import smtplib

# Sent with every API POST; uploads are rejected when the server has
# UPLOAD_TOKEN set and this env var is missing or wrong.
import os as _os_tok
_UPLOAD_AUTH = {"X-Upload-Token": _os_tok.environ["UPLOAD_TOKEN"]} if _os_tok.getenv("UPLOAD_TOKEN") else {}

import json
import os
import argparse
import time
import glob
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, date
from typing import Optional
from zoneinfo import ZoneInfo

import yfinance as yf

# ─────────────────────────────────────────────
# MARKET HOURS GUARD
# ─────────────────────────────────────────────

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN_HOUR  = 9
MARKET_OPEN_MIN   = 15
MARKET_CLOSE_HOUR = 15
MARKET_CLOSE_MIN  = 30

def is_market_hours() -> bool:
    """
    Returns True if current IST time is within NSE market hours
    on a weekday (Mon–Fri). Skips weekends automatically.
    """
    now = datetime.now(IST)
    if now.weekday() >= 5:          # Saturday=5, Sunday=6
        return False
    market_open  = now.replace(hour=MARKET_OPEN_HOUR,  minute=MARKET_OPEN_MIN,  second=0)
    market_close = now.replace(hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MIN, second=0)
    return market_open <= now <= market_close


# ─────────────────────────────────────────────
# ALERT DEDUPLICATION
# ─────────────────────────────────────────────

# BUG FIX: /tmp is wiped on every Railway container restart (each cron run is
# a fresh container). Dedup MUST live on the persistent volume, not /tmp.
# DATA_DIR points to the Railway volume mount (same volume as alert-tracker-volume).
DATA_DIR  = os.getenv("DATA_DIR", "/data")
DEDUP_FILE = os.path.join(DATA_DIR, "alerts_sent_today.json")


def _load_dedup() -> dict:
    """Load today's sent alert keys. Clears if file is from a previous day."""
    try:
        if not os.path.exists(DEDUP_FILE):
            return {}
        with open(DEDUP_FILE) as f:
            data = json.load(f)
        # Clear if stale (from a previous calendar day)
        if data.get("date") != str(date.today()):
            print(f"  🗓️  Dedup file from {data.get('date')} — clearing for today.")
            return {}
        keys = data.get("keys", {})
        if keys:
            print(f"  ⏭️  Loaded {len(keys)} dedup key(s) from today — will suppress repeats.")
        return keys
    except Exception:
        return {}


def _save_dedup(keys: dict):
    """Save today's sent alert keys to persistent volume."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(DEDUP_FILE, "w") as f:
            json.dump({"date": str(date.today()), "keys": keys}, f)
    except Exception as e:
        print(f"  ⚠️  Could not save dedup file: {e}")


def _make_dedup_key(ticker: str, alert_type: str) -> str:
    """
    Unique key per ticker + alert type per day.
    e.g. 'HDFCBANK.NS_stop_loss' or 'macro_crude_oil'
    """
    return f"{ticker}_{alert_type}"


def filter_new_alerts(alerts: dict) -> tuple[dict, int]:
    """
    Remove alerts already sent today — for stock AND macro alerts.
    Returns (filtered_alerts_dict, count_deduplicated).
    """
    sent_keys   = _load_dedup()
    dedup_count = 0

    filtered = {
        "stop_loss":  [],
        "profit":     [],
        "macro":      [],
        "ok":         alerts["ok"],
        "errors":     alerts["errors"],
        "timestamp":  alerts["timestamp"],
        "any_alerts": False,
    }

    # ── Stock stop-loss alerts ────────────────────────────────
    for s in alerts["stop_loss"]:
        key = _make_dedup_key(s["ticker"], "stop_loss")
        if key not in sent_keys:
            filtered["stop_loss"].append(s)
        else:
            dedup_count += 1
            print(f"  ⏭️  {s['ticker']} stop-loss already alerted today — skipping.")

    # ── Stock profit alerts ───────────────────────────────────
    for s in alerts["profit"]:
        stage_key = s.get("alert_label", "profit").replace(" ", "_").replace("—", "").strip()
        key = _make_dedup_key(s["ticker"], f"profit_{stage_key}")
        if key not in sent_keys:
            filtered["profit"].append(s)
        else:
            dedup_count += 1
            print(f"  ⏭️  {s['ticker']} profit alert already sent today — skipping.")

    # ── Macro alerts — BUG FIX: these were NEVER deduplicated ────
    # Each macro condition gets a stable key based on its CATEGORY (not the
    # full title which changes every run e.g. "$105.07" vs "$106.16").
    # We strip the numeric value so "Crude Oil Alert — $105/bbl" and
    # "Crude Oil Alert — $106/bbl" both map to the same dedup key.
    for m in alerts.get("macro", []):
        title = m.get("title", "")
        # Strip numbers/symbols to get stable category key
        import re as _re
        stable = _re.sub(r"[\d₹$.,+\-/]", "", title).strip().lower()
        stable = _re.sub(r"\s+", "_", stable)
        key = f"macro_{stable}"
        if key not in sent_keys:
            filtered["macro"].append(m)
        else:
            dedup_count += 1
            print(f"  ⏭️  Macro '{title[:40]}' already sent today — skipping.")

    filtered["any_alerts"] = bool(
        filtered["stop_loss"] or filtered["profit"] or filtered["macro"]
    )
    return filtered, dedup_count


def mark_alerts_sent(alerts: dict):
    """Record which alerts were just sent so they're not repeated today."""
    sent_keys = _load_dedup()

    for s in alerts["stop_loss"]:
        key = _make_dedup_key(s["ticker"], "stop_loss")
        sent_keys[key] = alerts["timestamp"]

    for s in alerts["profit"]:
        stage_key = s.get("alert_label", "profit").replace(" ", "_").replace("—", "").strip()
        key = _make_dedup_key(s["ticker"], f"profit_{stage_key}")
        sent_keys[key] = alerts["timestamp"]

    # Mark macro alerts as sent too
    import re as _re
    for m in alerts.get("macro", []):
        title = m.get("title", "")
        stable = _re.sub(r"[\d₹$.,+\-/]", "", title).strip().lower()
        stable = _re.sub(r"\s+", "_", stable)
        key = f"macro_{stable}"
        sent_keys[key] = alerts["timestamp"]

    _save_dedup(sent_keys)
    print(f"  💾 Dedup: {len(sent_keys)} key(s) saved to volume.")

# ─────────────────────────────────────────────
# CONFIG — loaded from environment variables
# ─────────────────────────────────────────────

def _get_config() -> dict:
    """Load email config from environment variables or .env file."""
    # Try loading .env file if python-dotenv is available
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass  # dotenv optional — env vars can be set directly

    return {
        "from_email":   os.getenv("ALERT_EMAIL_FROM", ""),
        "password":     os.getenv("ALERT_EMAIL_PASSWORD", ""),
        "to_email":     os.getenv("ALERT_EMAIL_TO", ""),
        "smtp_host":    "smtp.gmail.com",
        "smtp_port":    587,
    }


# Profit target stages
PROFIT_STAGES = [
    (0.20, "Stage 1 — Sell 30%",  "🟡"),
    (0.35, "Stage 2 — Sell 30%",  "🟡"),
    (0.50, "Stage 3 — Sell 40%",  "🟢"),
]

# Portfolio beta overheated threshold (matches 3.4)
BETA_OVERHEATED = 1.6


# ─────────────────────────────────────────────
# PRICE FETCHER
# ─────────────────────────────────────────────

def get_current_price(ticker: str) -> Optional[float]:
    try:
        hist = yf.Ticker(ticker).history(period="1d")
        if hist.empty:
            return None
        return round(float(hist["Close"].iloc[-1]), 2)
    except Exception:
        return None


# ─────────────────────────────────────────────
# ALERT DETECTOR
# ─────────────────────────────────────────────

# ── Mid-month Macro Alert Thresholds ─────────
MACRO_THRESHOLDS = {
    "crude_high":        100.0,    # Brent > $100/bbl
    "usdinr_stress":     88.0,     # USD/INR > 88
    "fii_extreme_sell": -15_000,   # FII 10d net < -₹15,000 Cr
    "sp500_crash":       -8.0,     # S&P 500 30d < -8%
}

# FII/DII history — API is primary source, local file is fallback only
FIIDII_HISTORY_FILE = os.path.join(os.path.dirname(__file__), "fiidii_history.json")
NEWS_SIGNALS_FILE   = os.path.join(os.path.dirname(__file__), "news_signals.json")


def detect_macro_alerts() -> list:
    """
    Check real-time macro indicators against critical thresholds.
    Fires mid-month warnings independent of stop-loss/profit checks.

    Returns list of macro alert dicts — empty if all clear.
    """
    macro_alerts = []

    # ── 1. Brent Crude ────────────────────────────────────────
    try:
        import yfinance as yf
        crude_hist = yf.Ticker("BZ=F").history(period="2d")
        if not crude_hist.empty:
            crude_price = round(float(crude_hist["Close"].iloc[-1]), 2)
            if crude_price > MACRO_THRESHOLDS["crude_high"]:
                macro_alerts.append({
                    "type":    "macro",
                    "emoji":   "🛢️",
                    "title":   f"Crude Oil Alert — ${crude_price}/bbl",
                    "message": (
                        f"Brent Crude has crossed ${MACRO_THRESHOLDS['crude_high']}/bbl "
                        f"(currently ${crude_price}). "
                        f"Consider tightening stop-losses on FMCG and Infra positions. "
                        f"Green Energy holdings may benefit — review at next rebalance."
                    ),
                    "urgency": "warning",
                })
    except Exception:
        pass

    # ── 2. USD/INR ────────────────────────────────────────────
    try:
        fx_hist = yf.Ticker("INR=X").history(period="2d")
        if not fx_hist.empty:
            usdinr = round(float(fx_hist["Close"].iloc[-1]), 2)
            if usdinr > MACRO_THRESHOLDS["usdinr_stress"]:
                macro_alerts.append({
                    "type":    "macro",
                    "emoji":   "💱",
                    "title":   f"Rupee Stress Alert — ₹{usdinr}/USD",
                    "message": (
                        f"USD/INR has crossed {MACRO_THRESHOLDS['usdinr_stress']} "
                        f"(currently ₹{usdinr}). "
                        f"FII outflows may accelerate. "
                        f"IT stocks may get short-term boost (export earnings). "
                        f"Green Energy at risk (import costs). Review stop-losses."
                    ),
                    "urgency": "warning",
                })
    except Exception:
        pass

    # ── 3. FII Extreme Selling ────────────────────────────────
    # FIX: Read from API (single source of truth) not local file.
    # Local fiidii_history.json on the alert-tracker volume can be stale
    # or contain different records than what the collector has posted to
    # the API — causing the 10d sum to differ from the LLM Synth / dashboard.
    try:
        import urllib.request as _ur
        api_url = os.getenv("API_URL", "https://web-production-2d832.up.railway.app")
        req = _ur.Request(f"{api_url}/fiidii",
                          headers={"Accept": "application/json"})
        with _ur.urlopen(req, timeout=8) as resp:
            history = json.loads(resp.read().decode())

        if not history:
            raise ValueError("Empty FII/DII response from API")

        recent  = sorted(history, key=lambda r: r["date"], reverse=True)[:10]
        fii_10d = sum(r["fii_net_cr"] for r in recent)
        days    = len(recent)

        if fii_10d < MACRO_THRESHOLDS["fii_extreme_sell"]:
            macro_alerts.append({
                "type":    "macro",
                "emoji":   "📊",
                "title":   f"FII Extreme Selling — ₹{fii_10d:,.0f}Cr (10d)",
                "message": (
                    f"FII 10-day net flow: ₹{fii_10d:,.0f}Cr "
                    f"(threshold: ₹{MACRO_THRESHOLDS['fii_extreme_sell']:,.0f}Cr). "
                    f"Consider tightening stop-losses by 3% across all positions. "
                    f"Defensive FMCG/Pharma holdings are best protected."
                ),
                "urgency": "warning",
            })
    except Exception as _e:
        # Fallback to local file if API unreachable
        try:
            if os.path.exists(FIIDII_HISTORY_FILE):
                with open(FIIDII_HISTORY_FILE) as f:
                    history = json.load(f)
                recent  = sorted(history, key=lambda r: r["date"], reverse=True)[:10]
                fii_10d = sum(r["fii_net_cr"] for r in recent)
                if fii_10d < MACRO_THRESHOLDS["fii_extreme_sell"]:
                    macro_alerts.append({
                        "type":    "macro",
                        "emoji":   "📊",
                        "title":   f"FII Extreme Selling — ₹{fii_10d:,.0f}Cr (10d) [local]",
                        "message": (
                            f"FII 10-day net flow: ₹{fii_10d:,.0f}Cr "
                            f"(threshold: ₹{MACRO_THRESHOLDS['fii_extreme_sell']:,.0f}Cr). "
                            f"Consider tightening stop-losses by 3% across all positions."
                        ),
                        "urgency": "warning",
                    })
        except Exception:
            pass

    # ── 4. S&P 500 Crash ──────────────────────────────────────
    try:
        sp_hist = yf.Ticker("^GSPC").history(period="45d")
        if not sp_hist.empty and len(sp_hist) >= 30:
            sp_now   = float(sp_hist["Close"].iloc[-1])
            sp_30d   = float(sp_hist["Close"].iloc[-30])
            sp_chg   = round((sp_now / sp_30d - 1) * 100, 1)
            if sp_chg < MACRO_THRESHOLDS["sp500_crash"]:
                macro_alerts.append({
                    "type":    "macro",
                    "emoji":   "🌍",
                    "title":   f"Global Risk-Off — S&P 500 {sp_chg:+.1f}% (30d)",
                    "message": (
                        f"S&P 500 has fallen {sp_chg:.1f}% over 30 days "
                        f"(threshold: {MACRO_THRESHOLDS['sp500_crash']}%). "
                        f"Indian markets likely to face continued FII pressure. "
                        f"Avoid adding new positions until stabilisation. "
                        f"Keep stop-losses tight."
                    ),
                    "urgency": "warning",
                })
    except Exception:
        pass

    # ── 5. News Sentiment — 2+ Buckets Negative ──────────────
    try:
        if os.path.exists(NEWS_SIGNALS_FILE):
            with open(NEWS_SIGNALS_FILE) as f:
                news_data = json.load(f)
            signals = news_data.get("signals", {})
            negative_buckets = [
                b for b, s in signals.items()
                if s.get("signal") in ("negative", "cautious")
                and s.get("matches", 0) >= 3
            ]
            if len(negative_buckets) >= 2:
                macro_alerts.append({
                    "type":    "macro",
                    "emoji":   "📰",
                    "title":   f"News Sentiment Deteriorating — {len(negative_buckets)} buckets",
                    "message": (
                        f"Negative/cautious news sentiment detected in: "
                        f"{', '.join(negative_buckets)}. "
                        f"Consider reviewing positions in these sectors. "
                        f"Monitor for continuation over next 2-3 days."
                    ),
                    "urgency": "info",
                })
    except Exception:
        pass

    return macro_alerts


def detect_alerts(portfolio: dict) -> dict:
    """
    Fetch live prices for all holdings and detect
    stop-loss hits and profit target triggers.

    Returns:
      {
        "stop_loss": [...],   # urgent — exit now
        "profit":    [...],   # action — partial sell
        "ok":        [...],   # no action needed
        "errors":    [...],   # could not fetch price
        "timestamp": str,
        "any_alerts": bool,
      }
    """
    alerts = {
        "stop_loss": [],
        "profit":    [],
        "ok":        [],
        "errors":    [],
        "timestamp": datetime.now().strftime("%d %B %Y, %I:%M %p IST"),
        "any_alerts": False,
    }

    for bucket_key, bucket in portfolio.items():
        if not isinstance(bucket, dict):
            continue

        for s in bucket.get("stocks", []):
            ticker    = s.get("ticker", "")
            buy_price = s.get("price", 0)
            name      = s.get("name", ticker)
            sl_price  = s.get("stop_loss_price")
            trail_dist = s.get("trailing_stop_dist")

            if not ticker or not buy_price:
                continue

            current = get_current_price(ticker)
            time.sleep(0.3)

            if current is None:
                alerts["errors"].append({
                    "ticker": ticker,
                    "name":   name,
                    "reason": "Could not fetch price",
                })
                continue

            change_pct = round((current / buy_price - 1) * 100, 2)

            # Fallback stop-loss if ATR fields missing
            if sl_price is None:
                sl_price = round(buy_price * 0.85, 2)

            suggested_trail = (
                round(current - trail_dist, 2)   # in profit → show trailing stop
                if trail_dist and current > buy_price
                else sl_price                      # not in profit → show stop-loss
            )

            record = {
                "ticker":          ticker,
                "name":            name,
                "bucket":          bucket.get("label", bucket_key),
                "buy_price":       buy_price,
                "current":         current,
                "change_pct":      change_pct,
                "sl_price":        sl_price,
                "sl_pct":          s.get("stop_loss_pct", 15.0),
                "atr_14day":       s.get("atr_14day"),
                "trail_dist":      trail_dist,
                "suggested_trail": suggested_trail,
                "allocation_inr":  s.get("allocation_inr", 0),
                "approx_shares":   s.get("approx_shares", 0),
            }

            # ── Check stop-loss ───────────────────────────────
            if current <= sl_price:
                record["alert_type"]  = "stop_loss"
                record["alert_emoji"] = "🔴"
                record["alert_label"] = "STOP-LOSS HIT — EXIT NOW"
                alerts["stop_loss"].append(record)

            # ── Check profit stages ───────────────────────────
            else:
                triggered_stage = None
                for threshold, label, emoji in reversed(PROFIT_STAGES):
                    if change_pct >= threshold * 100:
                        triggered_stage = (threshold, label, emoji)
                        break

                if triggered_stage:
                    record["alert_type"]  = "profit"
                    record["alert_emoji"] = triggered_stage[2]
                    record["alert_label"] = triggered_stage[1]
                    record["threshold"]   = triggered_stage[0] * 100
                    alerts["profit"].append(record)
                else:
                    record["alert_type"]  = "ok"
                    record["alert_emoji"] = "✅"
                    record["alert_label"] = "OK"
                    alerts["ok"].append(record)

    alerts["any_alerts"] = bool(alerts["stop_loss"] or alerts["profit"])
    return alerts


# ─────────────────────────────────────────────
# EMAIL FORMATTER
# ─────────────────────────────────────────────

def format_email_body(alerts: dict) -> tuple[str, str]:
    """
    Build plain text and HTML email body from alerts dict.
    Returns (subject, html_body).
    """
    n_sl     = len(alerts["stop_loss"])
    n_profit = len(alerts["profit"])
    n_ok     = len(alerts["ok"])
    n_macro  = len(alerts.get("macro", []))
    ts       = alerts["timestamp"]

    # Subject line
    if n_sl > 0:
        subject = f"🔴 URGENT: {n_sl} Stop-Loss Hit — Exit on Kite Now [{ts}]"
    elif n_profit > 0:
        subject = f"🟡 Action Required: {n_profit} Profit Target Reached [{ts}]"
    elif n_macro > 0:
        subject = f"⚠️ Macro Alert: {n_macro} Condition(s) Triggered [{ts}]"
    else:
        subject = f"✅ Portfolio Check: All Positions Healthy [{ts}]"

    # ── HTML body ─────────────────────────────────────────────
    macro_alerts = alerts.get("macro", [])
    macro_section = ""
    if macro_alerts:
        macro_rows = ""
        for m in macro_alerts:
            urgency_color = "#f57f17" if m.get("urgency") == "warning" else "#1565c0"
            macro_rows += f"""
            <div style="border-left:4px solid {urgency_color};padding:12px;margin:10px 0;background:#fff8e1;">
              <b>{m['emoji']} {m['title']}</b><br/>
              {m['message']}
            </div>"""
        macro_section = f"""
        <h2 style="color:#f57f17;">⚠️ Mid-Month Macro Alerts ({len(macro_alerts)})</h2>
        <p>These are informational alerts — no immediate trade action required unless combined with stop-loss hits.</p>
        {macro_rows}
        """
    rows_sl     = _build_alert_rows(alerts["stop_loss"], urgent=True)
    rows_profit = _build_alert_rows(alerts["profit"],    urgent=False)
    rows_ok     = _build_ok_rows(alerts["ok"])

    sl_section = f"""
    <h2 style="color:#d32f2f;">🔴 Stop-Loss Alerts ({n_sl})</h2>
    <p style="color:#d32f2f;"><b>EXIT THESE POSITIONS ON KITE IMMEDIATELY.</b></p>
    {rows_sl}
    """ if n_sl > 0 else ""

    profit_section = f"""
    <h2 style="color:#f57f17;">🟡 Profit Target Alerts ({n_profit})</h2>
    <p>Book partial profits on Kite as per your strategy.</p>
    {rows_profit}
    """ if n_profit > 0 else ""

    ok_section = f"""
    <h2 style="color:#388e3c;">✅ Healthy Positions ({n_ok})</h2>
    {rows_ok}
    """ if n_ok > 0 else ""

    html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:700px;margin:auto;">
    <div style="background:#1a237e;color:white;padding:20px;border-radius:8px 8px 0 0;">
      <h1 style="margin:0;">📈 Portfolio Alert</h1>
      <p style="margin:5px 0 0 0;opacity:0.8;">{ts}</p>
    </div>
    <div style="padding:20px;background:#f5f5f5;">
      <div style="background:white;padding:20px;border-radius:8px;">
        {macro_section}
        {sl_section}
        {profit_section}
        {ok_section}
        <hr/>
        <p style="color:#757575;font-size:12px;">
          Summary: {n_sl} stop-loss | {n_profit} profit targets | {n_ok} healthy<br/>
          This alert was generated automatically by your Indian Stock Screener.<br/>
          Always verify prices on Kite before placing orders.
        </p>
      </div>
    </div>
    </body></html>
    """

    return subject, html


def _build_alert_rows(stocks: list, urgent: bool) -> str:
    if not stocks:
        return "<p>None</p>"

    border_color = "#d32f2f" if urgent else "#f57f17"
    rows = ""
    for s in stocks:
        action = ""
        if urgent:
            action = f"<b style='color:#d32f2f;'>⚠️ EXIT on Kite at market price</b>"
        else:
            action = f"<b>Sell partial on Kite — {s.get('alert_label','')}</b>"
            if s.get("suggested_trail"):
                action += f"<br/>📈 Update GTT trailing stop to ₹{s['suggested_trail']:.2f}"

        rows += f"""
        <div style="border-left:4px solid {border_color};padding:12px;margin:10px 0;background:#fff8f8;">
          <b>{s['alert_emoji']} {s['ticker']} — {s['name']}</b>
          <span style="color:#757575;font-size:12px;"> ({s['bucket']})</span><br/>
          Buy: ₹{s['buy_price']:.2f} &nbsp;|&nbsp;
          Now: ₹{s['current']:.2f} &nbsp;|&nbsp;
          Change: <b>{s['change_pct']:+.2f}%</b><br/>
          Stop-Loss: ₹{s['sl_price']:.2f} ({s['sl_pct']}% below buy)<br/>
          {action}
        </div>
        """
    return rows


def _build_ok_rows(stocks: list) -> str:
    if not stocks:
        return "<p>No positions to show.</p>"

    rows = "<table style='width:100%;border-collapse:collapse;font-size:13px;'>"
    rows += "<tr style='background:#e8f5e9;'><th>Ticker</th><th>Buy</th><th>Now</th><th>Change</th><th>GTT Trail</th></tr>"
    for s in stocks:
        color = "#388e3c" if s["change_pct"] >= 0 else "#d32f2f"
        trail = f"₹{s['suggested_trail']:.2f}" if s.get("suggested_trail") else "—"
        rows += f"""
        <tr style='border-bottom:1px solid #eee;'>
          <td><b>{s['ticker']}</b></td>
          <td>₹{s['buy_price']:.2f}</td>
          <td>₹{s['current']:.2f}</td>
          <td style='color:{color};'><b>{s['change_pct']:+.2f}%</b></td>
          <td>{trail}</td>
        </tr>"""
    rows += "</table>"
    return rows


# ─────────────────────────────────────────────
# EMAIL SENDER
# ─────────────────────────────────────────────

def send_email_alert(subject: str, html_body: str) -> bool:
    """
    Send alert via Telegram Bot API (HTTPS — works on Railway, zero setup).
    Railway blocks outbound SMTP. Telegram uses port 443 which is always open.

    Required env vars:
      TELEGRAM_BOT_TOKEN  → get from @BotFather on Telegram
      TELEGRAM_CHAT_ID    → your personal chat ID

    Setup (5 min):
      1. Message @BotFather on Telegram → /newbot → follow prompts → copy token
      2. Message your new bot once (say "hi")
      3. Visit https://api.telegram.org/bot<TOKEN>/getUpdates → copy "id" from result
      4. Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to Railway env vars
    """
    import urllib.request as _ur
    import urllib.error   as _ure
    import html as _html
    import re as _re

    token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        print("  ⚠️  Telegram not configured.")
        print("      Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to Railway env vars.")
        print("      Setup: Message @BotFather on Telegram to create a free bot.")
        return False

    # Convert HTML alert to clean Telegram markdown
    # Strip HTML tags and format as readable text
    text = _re.sub(r'<br\s*/?>', '\n', html_body)
    text = _re.sub(r'<h[1-6][^>]*>(.*?)</h[1-6]>', r'*\1*\n', text, flags=_re.DOTALL)
    text = _re.sub(r'<b>(.*?)</b>', r'*\1*', text, flags=_re.DOTALL)
    text = _re.sub(r'<strong>(.*?)</strong>', r'*\1*', text, flags=_re.DOTALL)
    text = _re.sub(r'<[^>]+>', '', text)
    text = _html.unescape(text)
    # Collapse excessive blank lines
    text = _re.sub(r'\n{3,}', '\n\n', text).strip()

    # Prepend subject as bold header
    message = f"*{subject}*\n\n{text}"

    # Telegram message limit is 4096 chars
    if len(message) > 4096:
        message = message[:4000] + "\n\n... (truncated)"

    try:
        payload = json.dumps({
            "chat_id":    chat_id,
            "text":       message,
            "parse_mode": "Markdown",
        }).encode("utf-8")

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        req = _ur.Request(url, data=payload,
                          headers={"Content-Type": "application/json"},
                          method="POST")

        with _ur.urlopen(req, timeout=15) as resp:
            resp.read()
            print(f"  ✅ Alert sent via Telegram (chat {chat_id})")
            return True

    except _ure.HTTPError as e:
        err_body = e.read().decode() if e.fp else ""
        print(f"  ❌ Telegram API error {e.code}: {err_body}")
        return False
    except Exception as e:
        print(f"  ❌ Failed to send Telegram alert: {e}")
        return False


# ─────────────────────────────────────────────
# AUTO-DETECT LATEST PORTFOLIO
# ─────────────────────────────────────────────

def find_latest_portfolio(search_dirs: list = None) -> Optional[str]:
    """Find the most recent portfolio JSON file, or fetch from API."""
    import os as _os
    data_dir = _os.getenv("DATA_DIR", ".")
    if search_dirs is None:
        search_dirs = [data_dir, "./outputs", "/mnt/user-data/outputs", "."]

    files = []
    for d in search_dirs:
        files.extend(glob.glob(_os.path.join(d, "portfolio_*.json")))

    if files:
        return sorted(files)[-1]

    # Fallback: fetch from API and save to temp file
    api_url = _os.getenv("API_URL", "https://web-production-2d832.up.railway.app")
    try:
        import urllib.request as _ur
        import json as _json
        import tempfile as _tmp
        print(f"  📡 No local portfolio found — fetching from API...")
        with _ur.urlopen(f"{api_url}/portfolio/live", timeout=15) as r:
            text = r.read().decode()
            # Strip NaN values
            text = text.replace(":NaN", ":null").replace(":Infinity", ":null")
            data = _json.loads(text)
        # Reject if error response
        if isinstance(data, dict) and "error" in data:
            # Fall back to legacy endpoint
            with _ur.urlopen(f"{api_url}/portfolio/latest", timeout=15) as r:
                text = r.read().decode()
                text = text.replace(":NaN", ":null").replace(":Infinity", ":null")
                data = _json.loads(text)
        # Save to temp file
        tmp = _tmp.NamedTemporaryFile(
            mode="w", suffix=".json",
            prefix="portfolio_api_", delete=False
        )
        _json.dump(data, tmp)
        tmp.close()
        print(f"  ✅ Portfolio loaded from API → {tmp.name}")
        return tmp.name
    except Exception as e:
        print(f"  ⚠️  Could not fetch portfolio from API: {e}")
        return None


# ─────────────────────────────────────────────
# MAIN ENTRY POINT
# ─────────────────────────────────────────────

def capture_eod_snapshot(portfolio: dict) -> None:
    """
    Called once per day after market close (15:30 IST).
    Fetches closing prices for portfolio, Nifty50, Nifty500 and
    POSTs a performance record to /performance/upload.
    Uses the same weighted P&L calculation as the dashboard.
    """
    import urllib.request as _ur
    import urllib.error   as _ure
    import math

    today_str = date.today().isoformat()
    api_url   = os.getenv("API_URL", "https://web-production-2d832.up.railway.app")

    # ── Check if already captured today ──────────────────────
    dedup_file = os.path.join(os.getenv("DATA_DIR", "/data"), "perf_snapshot_dedup.txt")
    if os.path.exists(dedup_file):
        with open(dedup_file) as f:
            if f.read().strip() == today_str:
                print("  📊 EOD snapshot already captured today — skipping.")
                return

    print(f"\n  📊 Capturing EOD performance snapshot for {today_str}...")

    try:
        # ── Fetch index closing prices ────────────────────────
        nifty50_pct  = None
        nifty500_pct = None
        try:
            n50  = yf.Ticker("^NSEI")
            n500 = yf.Ticker("^CRSLDX")
            fi50  = n50.fast_info
            fi500 = n500.fast_info
            p50   = getattr(fi50,  "last_price",    None)
            pr50  = getattr(fi50,  "previous_close", None)
            p500  = getattr(fi500, "last_price",    None)
            pr500 = getattr(fi500, "previous_close", None)
            if p50 and pr50:
                nifty50_pct  = round(((p50  - pr50)  / pr50)  * 100, 2)
            if p500 and pr500:
                nifty500_pct = round(((p500 - pr500) / pr500) * 100, 2)
        except Exception as e:
            print(f"  ⚠️  Index fetch failed: {e}")

        # ── Calculate portfolio weighted % change ─────────────
        # Same formula as dashboard Today's P&L
        total_prev_value = 0.0
        total_day_chg    = 0.0
        portfolio_pct    = None

        all_tickers = []
        for bucket in portfolio.values():
            for s in bucket.get("stocks", []):
                t = s.get("ticker")
                if t:
                    all_tickers.append((t, s.get("approx_shares", 0)))

        for ticker, shares in all_tickers:
            try:
                fi    = yf.Ticker(ticker).fast_info
                price = getattr(fi, "last_price",    None)
                prev  = getattr(fi, "previous_close", None)
                if price and prev and shares:
                    price = float(price); prev = float(prev)
                    total_day_chg    += (price - prev) * shares
                    total_prev_value += prev * shares
            except Exception:
                pass

        if total_prev_value > 0:
            portfolio_pct = round((total_day_chg / total_prev_value) * 100, 2)

        if portfolio_pct is None and nifty50_pct is None:
            print("  ⚠️  Could not fetch any prices — skipping snapshot.")
            return

        # ── POST to API ───────────────────────────────────────
        rec = {
            "date":          today_str,
            "portfolio_pct": portfolio_pct,
            "nifty50_pct":   nifty50_pct,
            "nifty500_pct":  nifty500_pct,
        }
        payload = json.dumps(rec).encode()
        req = _ur.Request(
            f"{api_url}/performance/upload",
            data=payload,
            headers={"Content-Type": "application/json", **_UPLOAD_AUTH},
            method="POST",
        )
        with _ur.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read())
            print(f"  ✅ Snapshot saved: portfolio={portfolio_pct}%, "
                  f"nifty50={nifty50_pct}%, nifty500={nifty500_pct}%")

        # Mark as done for today
        with open(dedup_file, "w") as f:
            f.write(today_str)

    except Exception as e:
        print(f"  ⚠️  EOD snapshot failed: {e}")


def check_and_alert(
    portfolio_path: str,
    send_email:  bool = True,
    test_mode:   bool = False,
    force_run:   bool = False,
) -> Optional[dict]:
    """
    Full alert cycle:
    1. Market hours guard — skip if outside 9:15–15:30 IST weekdays
    2. Load portfolio JSON
    3. Fetch live prices
    4. Detect alerts
    5. Deduplicate — skip alerts already sent today
    6. Print to terminal
    7. Send email only for new alerts (max 1 email per alert per day)
    8. Mark sent alerts so they don't repeat

    Args:
      force_run  : bypass market hours check (for testing)
      test_mode  : send email even with no alerts
    """
    # ── EOD snapshot — runs even outside market hours ─────────
    # Must check BEFORE the market hours guard below, since
    # is_market_hours() returns False after 15:30 IST.
    now_ist = datetime.now(IST)
    is_weekday = now_ist.weekday() < 5  # Mon–Fri
    if is_weekday and (now_ist.hour > 15 or (now_ist.hour == 15 and now_ist.minute >= 25)):
        # Load portfolio from API for EOD snapshot
        try:
            import urllib.request as _ur
            api_url = os.getenv("API_URL", "https://web-production-2d832.up.railway.app")
            with _ur.urlopen(f"{api_url}/portfolio/live", timeout=15) as r:
                eod_portfolio = json.loads(r.read())
            capture_eod_snapshot(eod_portfolio)
        except Exception as e:
            print(f"  ⚠️  Could not load portfolio for EOD snapshot: {e}")

    # ── Market hours guard ────────────────────────────────────
    if not force_run and not test_mode and not is_market_hours():
        now_ist = datetime.now(IST).strftime("%d %B %Y, %I:%M %p IST")
        print(f"  ⏰ Outside market hours ({now_ist}) — skipping run.")
        return None

    print("\n" + "="*60)
    print("  📡 ALERT SYSTEM — RUNNING PRICE CHECK")
    print(f"  {datetime.now(IST).strftime('%d %B %Y, %I:%M %p IST')}")
    print(f"  Portfolio: {portfolio_path}")
    print("="*60)

    with open(portfolio_path) as f:
        portfolio = json.load(f)

    # ── Detect all triggered alerts ───────────────────────────
    all_alerts = detect_alerts(portfolio)

    # ── Mid-month macro alerts ────────────────────────────────
    macro_alerts = detect_macro_alerts()
    if macro_alerts:
        print(f"\n⚠️  MACRO ALERTS — {len(macro_alerts)} condition(s) triggered!")
        for m in macro_alerts:
            print(f"\n  {m['emoji']} {m['title']}")
            print(f"  {m['message'][:120]}...")
        all_alerts["macro"] = macro_alerts
        all_alerts["any_alerts"] = True
    else:
        all_alerts["macro"] = []
        print(f"\n  ✅ Macro conditions: all clear")

    # ── Deduplicate — remove alerts sent earlier today ────────
    if not test_mode:
        new_alerts, dedup_count = filter_new_alerts(all_alerts)
        if dedup_count > 0:
            print(f"  ⏭️  {dedup_count} alert(s) already sent today — deduplicated.")
    else:
        new_alerts  = all_alerts
        dedup_count = 0

    # ── Terminal output ───────────────────────────────────────
    if new_alerts["stop_loss"]:
        print(f"\n🔴 STOP-LOSS ALERTS — {len(new_alerts['stop_loss'])} position(s)!")
        for s in new_alerts["stop_loss"]:
            print(f"\n  {s['ticker']} — {s['name']}")
            print(f"  Buy: ₹{s['buy_price']:.2f} | Now: ₹{s['current']:.2f} | {s['change_pct']:+.2f}%")
            print(f"  GTT Stop: ₹{s['sl_price']:.2f} | ⚠️  EXIT ON KITE NOW")

    if new_alerts["profit"]:
        print(f"\n🟡 PROFIT TARGET ALERTS — {len(new_alerts['profit'])} position(s)!")
        for s in new_alerts["profit"]:
            print(f"\n  {s['ticker']} — {s['name']}")
            print(f"  Buy: ₹{s['buy_price']:.2f} | Now: ₹{s['current']:.2f} | {s['change_pct']:+.2f}%")
            print(f"  {s['alert_emoji']} {s['alert_label']}")
            if s.get("suggested_trail"):
                print(f"  📈 Update GTT to: ₹{s['suggested_trail']:.2f}")

    print(f"\n✅ Healthy: {len(all_alerts['ok'])} | "
          f"⚠️  Errors: {len(all_alerts['errors'])} | "
          f"⏭️  Deduplicated: {dedup_count}")
    print("="*60)

    # ── Email — only for new alerts ───────────────────────────
    should_send = send_email and (new_alerts["any_alerts"] or test_mode)

    if should_send:
        subject, html_body = format_email_body(new_alerts)
        print(f"\n  📧 Sending alert email...")
        sent_ok = send_email_alert(subject, html_body)
        if sent_ok and not test_mode:
            mark_alerts_sent(new_alerts)
    elif send_email and not new_alerts["any_alerts"]:
        print(f"\n  📧 No new alerts — email suppressed.")

    return new_alerts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Portfolio alert system")
    parser.add_argument("--portfolio", help="Path to portfolio JSON (auto-detected if omitted)")
    parser.add_argument("--test",      action="store_true",
                        help="Send test email even if no alerts triggered")
    parser.add_argument("--no-email",  action="store_true",
                        help="Print to terminal only — no email")
    parser.add_argument("--force",     action="store_true",
                        help="Bypass market hours check (for testing)")
    args = parser.parse_args()

    portfolio_path = args.portfolio or find_latest_portfolio()
    if not portfolio_path:
        print("❌ No portfolio file found. Run screener.py first.")
        exit(1)

    check_and_alert(
        portfolio_path,
        send_email = not args.no_email,
        test_mode  = args.test,
        force_run  = args.force,
    )
