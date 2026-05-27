"""
Swing Scanner — India NSE
==========================
Daily scan of Nifty 500 for swing trade candidates.
Runs after market close (8:00 PM IST weekdays).

Entry signals scored 0–6 (need ≥3 for candidate):
  1. RSI(14)          — 40–60 zone, building momentum
  2. MACD(12,26,9)    — bullish crossover in last 3 days
  3. Bollinger Bands  — price crossing middle band from below
  4. Volume Surge     — today's volume > 2× 20-day average
  5. Momentum Breakout— price within 3% of 52-week high
  6. FII/DII Flow     — net FII positive in 2 of last 3 days

Exit rules (applied by alerter.py):
  - Stop-loss: buy_price − 1.5× ATR-14
  - Target 1:  +5%  (book 50%)
  - Target 2:  +8%  (book remaining 50%)
  - Time exit: force exit after 10 trading days

Schedule: 0 14 * * 1-5   (8:00 PM IST = 14:00 UTC on weekdays)

Usage:
  python swing_scanner.py           # run scan
  python swing_scanner.py --status  # show today's candidates
  python swing_scanner.py --test    # scan without saving
  python swing_scanner.py --ticker RELIANCE.NS  # scan single stock
"""

import yfinance as yf
import pandas as pd
import numpy as np
import json
import os
import time
import argparse
import urllib.request as _urllib
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from typing import Optional

from nse_universe import fetch_nifty500

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

IST             = ZoneInfo("Asia/Kolkata")
DATA_DIR        = os.getenv("DATA_DIR", "/data")
API_URL         = os.getenv("API_URL", "https://web-production-2d832.up.railway.app")
CANDIDATES_FILE = os.path.join(DATA_DIR, "swing_candidates.json")

# Signal parameters
RSI_PERIOD       = 14
RSI_MIN          = 35      # widen — accept stocks building from mild oversold
RSI_MAX          = 70      # widen — allow momentum to be already running
MACD_FAST        = 12
MACD_SLOW        = 26
MACD_SIGNAL      = 9
MACD_CROSS_DAYS  = 5       # crossover within last 5 days
BB_PERIOD        = 20
BB_STD           = 2.0
VOL_SURGE_MULT   = 1.5     # volume > 1.5× 20-day avg (2x is too rare)
BREAKOUT_PCT     = 0.08    # within 8% of 52-week high or broke 20d resistance
FII_LOOKBACK     = 3       # days of FII data to check
FII_MIN_POSITIVE = 1       # FII positive in at least 1 of last 3 days

MIN_SIGNALS      = 3       # minimum signals to qualify as candidate
MAX_CANDIDATES   = 10      # max candidates to report

# Swing-specific ATR stop (tighter than long-term 2.5x)
SWING_ATR_MULT   = 1.5
SWING_ATR_PERIOD = 14
SWING_TRAIL_MULT = 1.0     # tighter trail for swing

# Profit targets
SWING_TARGET_1   = 0.05   # +5%  → book 50%
SWING_TARGET_2   = 0.08   # +8%  → book remaining 50%
SWING_MAX_DAYS   = 10     # force exit after 10 trading days

# Liquidity — swing needs more liquidity than long-term
SWING_MIN_ADV    = 300_000   # 3 lakh shares/day minimum
SWING_MIN_ADTV   = 5.0       # ₹5 crore/day minimum


# ─────────────────────────────────────────────
# DATA FETCHER — one call per stock
# ─────────────────────────────────────────────

def fetch_ohlcv(ticker: str) -> Optional[pd.DataFrame]:
    """
    Fetch 1 year of daily OHLCV — one call gives all signal data.
    """
    try:
        hist = yf.Ticker(ticker).history(period="1y")
        if hist.empty or len(hist) < 60:
            return None
        return hist
    except Exception:
        return None


# ─────────────────────────────────────────────
# SIGNAL CALCULATORS
# ─────────────────────────────────────────────

def calc_rsi(closes: pd.Series, period: int = RSI_PERIOD) -> float:
    """RSI calculation — same formula as TradingView."""
    delta  = closes.diff().dropna()
    gain   = delta.clip(lower=0)
    loss   = (-delta).clip(lower=0)
    avg_g  = gain.rolling(period).mean()
    avg_l  = loss.rolling(period).mean()
    rs     = avg_g / avg_l.replace(0, np.nan)
    rsi    = 100 - (100 / (1 + rs))
    return round(float(rsi.iloc[-1]), 2) if not rsi.empty else 50.0


def calc_macd(closes: pd.Series) -> dict:
    """
    MACD(12,26,9). Returns MACD line, signal line, histogram,
    and whether a bullish crossover happened in last MACD_CROSS_DAYS days.
    """
    ema12  = closes.ewm(span=MACD_FAST,  adjust=False).mean()
    ema26  = closes.ewm(span=MACD_SLOW,  adjust=False).mean()
    macd   = ema12 - ema26
    signal = macd.ewm(span=MACD_SIGNAL, adjust=False).mean()
    hist   = macd - signal

    # Bullish crossover = MACD crossed ABOVE signal in last N days
    crossed = False
    for i in range(1, min(MACD_CROSS_DAYS + 1, len(hist))):
        if hist.iloc[-i] > 0 and hist.iloc[-(i+1)] <= 0:
            crossed = True
            break

    return {
        "macd":         round(float(macd.iloc[-1]), 4),
        "signal":       round(float(signal.iloc[-1]), 4),
        "histogram":    round(float(hist.iloc[-1]), 4),
        "bullish_cross": crossed,
        "macd_above":   float(macd.iloc[-1]) > float(signal.iloc[-1]),
    }


def calc_bollinger(hist: pd.DataFrame) -> dict:
    """
    Bollinger Bands(20, 2σ).
    Signal: price crossed middle band from below in last 2 days.
    """
    closes = hist["Close"]
    sma    = closes.rolling(BB_PERIOD).mean()
    std    = closes.rolling(BB_PERIOD).std()
    upper  = sma + BB_STD * std
    lower  = sma - BB_STD * std

    curr   = float(closes.iloc[-1])
    mid    = float(sma.iloc[-1])
    prev   = float(closes.iloc[-2])
    prev_m = float(sma.iloc[-2])

    # Crossed middle from below: prev < prev_mid AND curr > curr_mid
    crossed_mid = prev < prev_m and curr > mid
    # Bounced from lower band: prev day touched lower band
    near_lower  = float(closes.iloc[-2]) <= float(lower.iloc[-2]) * 1.01

    return {
        "upper":        round(float(upper.iloc[-1]), 2),
        "middle":       round(mid, 2),
        "lower":        round(float(lower.iloc[-1]), 2),
        "current":      round(curr, 2),
        "crossed_mid":  crossed_mid,
        "near_lower":   near_lower,
        "signal":       crossed_mid or near_lower,
        "pct_b":        round((curr - float(lower.iloc[-1])) /
                              (float(upper.iloc[-1]) - float(lower.iloc[-1]) + 1e-9) * 100, 1),
    }


def calc_volume_surge(hist: pd.DataFrame) -> dict:
    """Volume surge: today > 2× 20-day average."""
    vol       = hist["Volume"]
    avg_20    = float(vol.iloc[-21:-1].mean())
    today_vol = float(vol.iloc[-1])
    ratio     = round(today_vol / avg_20, 2) if avg_20 > 0 else 1.0

    return {
        "today_vol":  int(today_vol),
        "avg_20d":    int(avg_20),
        "ratio":      ratio,
        "surge":      ratio >= VOL_SURGE_MULT,
    }


def calc_momentum_breakout(hist: pd.DataFrame) -> dict:
    """Price within 3% of 52-week high OR breaking above 20-day resistance."""
    closes    = hist["Close"]
    highs     = hist["High"]
    high_52w  = float(highs.iloc[-252:].max()) if len(highs) >= 252 else float(highs.max())
    curr      = float(closes.iloc[-1])
    pct_from  = round((high_52w - curr) / high_52w * 100, 2)

    # 20-day resistance break: curr > max(high) of last 20 days (excluding today)
    res_20d   = float(highs.iloc[-21:-1].max())
    broke_20d = curr > res_20d

    return {
        "high_52w":     round(high_52w, 2),
        "current":      round(curr, 2),
        "pct_from_52w": pct_from,
        "near_52w":     pct_from <= BREAKOUT_PCT * 100,
        "broke_20d_res":broke_20d,
        "signal":       pct_from <= BREAKOUT_PCT * 100 or broke_20d,
    }


def calc_fii_signal(fii_data: list) -> dict:
    """
    FII net positive in at least 2 of last 3 days.
    fii_data: list of {date, fii_net_cr, dii_net_cr} sorted newest first.
    """
    if not fii_data or len(fii_data) < 1:
        return {"signal": False, "note": "No FII data available", "positive_days": 0}

    recent = fii_data[:FII_LOOKBACK]
    pos    = sum(1 for r in recent if r.get("fii_net_cr", 0) > 0)
    net_3d = sum(r.get("fii_net_cr", 0) for r in recent)

    return {
        "signal":        pos >= FII_MIN_POSITIVE,
        "positive_days": pos,
        "total_days":    len(recent),
        "net_3d_cr":     round(net_3d, 2),
        "note":          f"FII positive {pos}/{len(recent)} days, net ₹{net_3d:.0f}Cr",
    }


def calc_atr(hist: pd.DataFrame, period: int = SWING_ATR_PERIOD) -> Optional[float]:
    """ATR calculation — same as screener.py."""
    try:
        sub = hist.tail(period + 1).copy()
        if len(sub) < period + 1:
            return None
        sub["prev_close"] = sub["Close"].shift(1)
        sub["tr1"] = sub["High"] - sub["Low"]
        sub["tr2"] = (sub["High"] - sub["prev_close"]).abs()
        sub["tr3"] = (sub["Low"]  - sub["prev_close"]).abs()
        sub["tr"]  = sub[["tr1","tr2","tr3"]].max(axis=1)
        return round(float(sub["tr"].iloc[1:].mean()), 2)
    except Exception:
        return None


# ─────────────────────────────────────────────
# LIQUIDITY CHECK
# ─────────────────────────────────────────────

def passes_liquidity(hist: pd.DataFrame, ticker: str) -> tuple[bool, str]:
    """Swing trading needs higher liquidity than long-term investing."""
    vol    = hist["Volume"]
    closes = hist["Close"]
    adv    = float(vol.iloc[-30:].mean())
    curr   = float(closes.iloc[-1])
    adtv   = round(adv * curr / 1e7, 2)  # in ₹ crore

    if adv < SWING_MIN_ADV:
        return False, f"ADV {adv/1e5:.1f}L < min {SWING_MIN_ADV/1e5:.1f}L shares/day"
    if adtv < SWING_MIN_ADTV:
        return False, f"ADTV ₹{adtv:.1f}Cr < min ₹{SWING_MIN_ADTV}Cr/day"
    return True, ""


# ─────────────────────────────────────────────
# STOP-LOSS + TARGETS
# ─────────────────────────────────────────────

def compute_swing_levels(hist: pd.DataFrame, buy_price: float) -> dict:
    """Compute stop-loss and profit targets for a swing trade."""
    atr = calc_atr(hist)
    if atr and atr > 0:
        stop = round(buy_price - SWING_ATR_MULT * atr, 2)
        trail = round(SWING_TRAIL_MULT * atr, 2)
        src = "ATR"
    else:
        stop = round(buy_price * 0.96, 2)   # 4% fallback
        trail = round(buy_price * 0.02, 2)
        atr = None
        src = "FALLBACK"

    stop_pct   = round((buy_price - stop) / buy_price * 100, 2)
    target1    = round(buy_price * (1 + SWING_TARGET_1), 2)
    target2    = round(buy_price * (1 + SWING_TARGET_2), 2)
    rr_ratio   = round((target1 - buy_price) / (buy_price - stop), 2) if stop < buy_price else 0

    return {
        "atr":          atr,
        "atr_mult":     SWING_ATR_MULT,
        "stop_loss":    stop,
        "stop_pct":     stop_pct,
        "trailing":     trail,
        "target1":      target1,   # +5% — book 50%
        "target2":      target2,   # +8% — book 50%
        "rr_ratio":     rr_ratio,  # reward/risk ratio
        "max_days":     SWING_MAX_DAYS,
        "source":       src,
    }


# ─────────────────────────────────────────────
# SINGLE STOCK ANALYSER
# ─────────────────────────────────────────────

def analyse_stock(ticker: str, fii_data: list) -> Optional[dict]:
    """
    Run all 6 signals on a single stock.
    Returns candidate dict if ≥ MIN_SIGNALS pass, else None.
    """
    hist = fetch_ohlcv(ticker)
    if hist is None:
        return None

    # Liquidity gate first
    liquid, liq_reason = passes_liquidity(hist, ticker)
    if not liquid:
        return None

    closes = hist["Close"]
    curr   = float(closes.iloc[-1])

    # ── Calculate all 6 signals ───────────────────────────────
    rsi     = calc_rsi(closes)
    macd    = calc_macd(closes)
    bb      = calc_bollinger(hist)
    vol     = calc_volume_surge(hist)
    mom     = calc_momentum_breakout(hist)
    fii     = calc_fii_signal(fii_data)

    # ── Score each signal ─────────────────────────────────────
    signals = {
        "rsi": {
            "pass":  RSI_MIN <= rsi <= RSI_MAX,
            "value": rsi,
            "note":  f"RSI {rsi:.1f} ({'✅ momentum zone' if RSI_MIN <= rsi <= RSI_MAX else '❌ outside 40-65'})",
        },
        "macd": {
            "pass":  macd["bullish_cross"] or macd["macd_above"] or macd["histogram"] > 0,
            "value": macd["histogram"],
            "note":  f"MACD {'✅ bullish cross' if macd['bullish_cross'] else ('✅ above signal' if macd['macd_above'] else '❌ bearish')}",
        },
        "bollinger": {
            "pass":  bb["signal"],
            "value": bb["pct_b"],
            "note":  f"BB %B={bb['pct_b']:.0f}% {'✅ crossed middle' if bb['crossed_mid'] else ('✅ near lower' if bb['near_lower'] else '❌')}",
        },
        "volume": {
            "pass":  vol["surge"],
            "value": vol["ratio"],
            "note":  f"Volume {vol['ratio']:.1f}× avg {'✅ surge' if vol['surge'] else '❌ normal'}",
        },
        "momentum": {
            "pass":  mom["signal"],
            "value": mom["pct_from_52w"],
            "note":  f"{'✅ near 52w high' if mom['near_52w'] else ('✅ broke 20d resistance' if mom['broke_20d_res'] else '❌')} ({mom['pct_from_52w']:.1f}% from 52w)",
        },
        "fii": {
            "pass":  fii["signal"],
            "value": fii["net_3d_cr"],
            "note":  fii["note"],
        },
    }

    score = sum(1 for s in signals.values() if s["pass"])

    if score < MIN_SIGNALS:
        return None

    # ── Compute swing levels ──────────────────────────────────
    levels = compute_swing_levels(hist, curr)

    # Skip if reward/risk ratio is poor (<1.0)
    if levels["rr_ratio"] < 1.0:
        return None

    # ── Conviction label ──────────────────────────────────────
    conviction = "HIGH" if score >= 5 else "MEDIUM" if score >= 4 else "LOW"

    # ── Get stock name ────────────────────────────────────────
    try:
        info = yf.Ticker(ticker).info
        name = info.get("longName", ticker.replace(".NS",""))
        sector = info.get("sector", "")
    except Exception:
        name = ticker.replace(".NS","")
        sector = ""

    return {
        "ticker":        ticker,
        "name":          name,
        "sector":        sector,
        "scanned_at":    datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"),
        "current_price": round(curr, 2),
        "score":         score,
        "max_score":     6,
        "conviction":    conviction,
        # Signals detail
        "signals":       signals,
        # Technical values
        "rsi":           rsi,
        "macd_hist":     macd["histogram"],
        "bb_pct_b":      bb["pct_b"],
        "vol_ratio":     vol["ratio"],
        "pct_from_52w":  mom["pct_from_52w"],
        "fii_net_3d":    fii["net_3d_cr"],
        # Swing levels
        "stop_loss":     levels["stop_loss"],
        "stop_pct":      levels["stop_pct"],
        "trailing_stop": levels["trailing"],
        "target1":       levels["target1"],
        "target2":       levels["target2"],
        "rr_ratio":      levels["rr_ratio"],
        "max_days":      SWING_MAX_DAYS,
        "atr":           levels["atr"],
        "atr_source":    levels["source"],
        # Liquidity
        "adv":           int(hist["Volume"].iloc[-30:].mean()),
        "adtv_cr":       round(float(hist["Volume"].iloc[-30:].mean()) * curr / 1e7, 2),
    }


# ─────────────────────────────────────────────
# FII DATA FETCHER
# ─────────────────────────────────────────────

def fetch_fii_data() -> list:
    """Fetch FII/DII data from API. Returns list sorted newest first."""
    try:
        url = f"{API_URL}/fiidii"
        req = _urllib.Request(url, headers={"Accept": "application/json"})
        with _urllib.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        if isinstance(data, list):
            return sorted(data, key=lambda x: x.get("date",""), reverse=True)
        return []
    except Exception as e:
        print(f"  ⚠️  Could not fetch FII data: {e}")
        return []


# ─────────────────────────────────────────────
# MAIN SCANNER
# ─────────────────────────────────────────────

def run_scan(test_mode: bool = False, single_ticker: str = None) -> list:
    """
    Scan Nifty 500 for swing trade candidates.
    Returns sorted list of candidates (best score first).
    """
    print(f"\n{'='*58}")
    print(f"  📈 SWING SCANNER — INDIA NSE")
    print(f"  {datetime.now(IST).strftime('%d %B %Y, %I:%M %p IST')}")
    print(f"  Min signals: {MIN_SIGNALS}/6  |  Max candidates: {MAX_CANDIDATES}")
    print(f"  Holding: 1-2 weeks  |  Targets: +{SWING_TARGET_1*100:.0f}% / +{SWING_TARGET_2*100:.0f}%")
    print(f"{'='*58}\n")

    # ── Step 1: Get universe ──────────────────────────────────
    if single_ticker:
        tickers = [single_ticker if single_ticker.endswith(".NS")
                   else single_ticker + ".NS"]
        print(f"  Single ticker mode: {tickers[0]}")
    else:
        print("  Fetching Nifty 500 universe...")
        nifty_df = fetch_nifty500()
        tickers  = nifty_df["nse_ticker"].tolist() if not nifty_df.empty else []
        print(f"  Universe: {len(tickers)} stocks")

    # ── Step 2: Fetch FII data once ───────────────────────────
    print("  Fetching FII/DII data...")
    fii_data = fetch_fii_data()
    print(f"  FII data: {len(fii_data)} days available")

    # ── Step 3: Scan each stock ───────────────────────────────
    candidates  = []
    scanned     = 0
    liq_fail    = 0
    sig_fail    = 0

    print(f"\n  Scanning {len(tickers)} stocks...\n")

    for ticker in tickers:
        try:
            result = analyse_stock(ticker, fii_data)
            scanned += 1

            if result is None:
                sig_fail += 1
            else:
                candidates.append(result)
                conv = result["conviction"]
                emoji = "🔥" if conv == "HIGH" else "⚡" if conv == "MEDIUM" else "✳️"
                print(f"  {emoji} {ticker:<20} Score:{result['score']}/6  "
                      f"RSI:{result['rsi']:.0f}  "
                      f"Vol:{result['vol_ratio']:.1f}×  "
                      f"52w:{result['pct_from_52w']:.1f}%  "
                      f"{conv}")

            if scanned % 50 == 0:
                print(f"  ... {scanned}/{len(tickers)} scanned, "
                      f"{len(candidates)} candidates so far")

            time.sleep(0.3)

        except Exception as e:
            print(f"  ⚠️  {ticker}: {e}")
            continue

    # ── Step 4: Sort and filter ───────────────────────────────
    candidates.sort(key=lambda x: (x["score"], x["rr_ratio"]), reverse=True)
    top = candidates[:MAX_CANDIDATES]

    # ── Step 5: Print report ──────────────────────────────────
    print(f"\n{'='*58}")
    print(f"  SCAN COMPLETE")
    print(f"  Scanned: {scanned} | Candidates: {len(candidates)} | Showing: {len(top)}")
    print(f"{'='*58}")

    for i, c in enumerate(top, 1):
        print(f"\n  {'─'*54}")
        print(f"  {i}. {c['ticker']:<18} [{c['conviction']}]  Score: {c['score']}/6")
        print(f"     {c['name']}")
        print(f"     Price: ₹{c['current_price']:,.2f}  |  "
              f"Vol: {c['vol_ratio']:.1f}× avg  |  "
              f"RSI: {c['rsi']:.0f}")
        print(f"     Stop:  ₹{c['stop_loss']:,.2f}  ({c['stop_pct']:.1f}% below)")
        print(f"     T1:    ₹{c['target1']:,.2f}  (+{SWING_TARGET_1*100:.0f}% — sell 50%)")
        print(f"     T2:    ₹{c['target2']:,.2f}  (+{SWING_TARGET_2*100:.0f}% — sell 50%)")
        print(f"     R/R:   {c['rr_ratio']:.2f}×  |  Max hold: {c['max_days']} days")
        print(f"     Signals:")
        for sig_name, sig in c["signals"].items():
            print(f"       {'✅' if sig['pass'] else '❌'} {sig_name:<12} {sig['note']}")

    # ── Step 6: Save and post ─────────────────────────────────
    if not test_mode:
        save_candidates(top)
        send_telegram_alert(top)

    return top


# ─────────────────────────────────────────────
# SAVE + POST TO API
# ─────────────────────────────────────────────

def save_candidates(candidates: list):
    """Save candidates to disk and POST to API."""
    os.makedirs(DATA_DIR, exist_ok=True)

    output = {
        "generated_at":   datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"),
        "scan_date":      str(date.today()),
        "total_candidates": len(candidates),
        "candidates":     candidates,
    }

    with open(CANDIDATES_FILE, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n  ✅ Candidates saved: {CANDIDATES_FILE}")

    # POST to API
    try:
        payload = json.dumps(
            {"type": "swing_candidates", "payload": output},
            default=str
        ).encode("utf-8")
        req = _urllib.Request(
            f"{API_URL}/signals/upload",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with _urllib.urlopen(req, timeout=15) as r:
            print(f"  ✅ Candidates POSTed to API: {r.read().decode()}")
    except Exception as e:
        print(f"  ⚠️  Could not POST to API (non-fatal): {e}")


# ─────────────────────────────────────────────
# TELEGRAM ALERT
# ─────────────────────────────────────────────

def send_telegram_alert(candidates: list):
    """Send daily swing scan summary to Telegram."""
    import urllib.request as _ur
    import urllib.error   as _ure

    token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        print("  ⚠️  Telegram not configured.")
        return

    if not candidates:
        msg = (
            f"📈 *Swing Scanner — {date.today().strftime('%d %b %Y')}*\n\n"
            f"No swing candidates found today.\n"
            f"Market conditions may not be favourable."
        )
    else:
        lines = [f"📈 *Swing Candidates — {date.today().strftime('%d %b %Y')}*\n"]
        lines.append(f"Found *{len(candidates)}* candidates\n")

        for i, c in enumerate(candidates[:5], 1):
            conv_emoji = "🔥" if c["conviction"] == "HIGH" else "⚡" if c["conviction"] == "MEDIUM" else "✳️"
            sigs_passed = [k for k, v in c["signals"].items() if v["pass"]]

            lines.append(
                f"{conv_emoji} *{i}. {c['ticker'].replace('.NS','')}* "
                f"[{c['conviction']}] Score: {c['score']}/6\n"
                f"  Price: ₹{c['current_price']:,.2f}\n"
                f"  Stop:  ₹{c['stop_loss']:,.2f} ({c['stop_pct']:.1f}% below)\n"
                f"  T1: ₹{c['target1']:,.2f} (+5%) | T2: ₹{c['target2']:,.2f} (+8%)\n"
                f"  R/R: {c['rr_ratio']:.2f}× | Signals: {', '.join(sigs_passed)}\n"
            )

        if len(candidates) > 5:
            lines.append(f"_...and {len(candidates)-5} more on dashboard_")

        lines.append(f"\n⏱ Hold up to {SWING_MAX_DAYS} trading days")
        lines.append(f"🛑 Set stop-loss on Kite before entering")
        msg = "\n".join(lines)

    # Trim to Telegram limit
    if len(msg) > 4096:
        msg = msg[:4000] + "\n\n_(truncated)_"

    try:
        payload = json.dumps({
            "chat_id":    chat_id,
            "text":       msg,
            "parse_mode": "Markdown",
        }).encode("utf-8")
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        req = _ur.Request(url, data=payload,
                          headers={"Content-Type": "application/json"},
                          method="POST")
        with _ur.urlopen(req, timeout=15) as r:
            r.read()
            print(f"  ✅ Telegram alert sent")
    except Exception as e:
        print(f"  ⚠️  Telegram failed: {e}")


# ─────────────────────────────────────────────
# STATUS
# ─────────────────────────────────────────────

def show_status():
    """Show today's saved candidates."""
    if not os.path.exists(CANDIDATES_FILE):
        print("No swing candidates found. Run: python swing_scanner.py")
        return

    with open(CANDIDATES_FILE) as f:
        data = json.load(f)

    print(f"\n{'='*58}")
    print(f"  SWING CANDIDATES — {data.get('scan_date','')}")
    print(f"  Generated: {data.get('generated_at','')}")
    print(f"  Total: {data.get('total_candidates', 0)}")
    print(f"{'='*58}")

    for c in data.get("candidates", []):
        conv = c["conviction"]
        emoji = "🔥" if conv == "HIGH" else "⚡" if conv == "MEDIUM" else "✳️"
        print(f"\n  {emoji} {c['ticker']:<20} Score:{c['score']}/6  "
              f"RSI:{c['rsi']:.0f}  R/R:{c['rr_ratio']:.2f}×")
        print(f"     ₹{c['current_price']:,.2f}  →  "
              f"Stop:₹{c['stop_loss']:,.2f}  "
              f"T1:₹{c['target1']:,.2f}  "
              f"T2:₹{c['target2']:,.2f}")


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Swing Scanner — India NSE")
    parser.add_argument("--status", action="store_true", help="Show latest candidates")
    parser.add_argument("--test",   action="store_true", help="Scan without saving")
    parser.add_argument("--ticker", type=str, default=None,
                        help="Scan a single ticker (e.g. RELIANCE or RELIANCE.NS)")
    args = parser.parse_args()

    if args.status:
        show_status()
    else:
        run_scan(test_mode=args.test, single_ticker=args.ticker)
