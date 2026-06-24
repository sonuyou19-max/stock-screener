"""
Swing Scanner — India NSE
==========================
Daily scan of Nifty 500 for swing trade candidates.
Runs after market close (8:00 PM IST weekdays).

Hard gates (fail any one → skipped, regardless of score):
  - Stock above its OWN 50-DMA, 20-DMA rising (no bear-rally bounces)
  - Not >10% above 20-DMA (overextended setups mean-revert first)
  - Scan-day gain ≤7% (a news spike entered next morning = chasing)
  - Price ≥ ₹50, liquidity ADV ≥3L shares / ₹5Cr ADTV
  - Structural stop within 6% (a wider stop means the setup isn't tight
    — skip the trade, never tighten the stop to force it)
  - Earnings blackout: reports within 14 calendar days → skipped
  - Market regime: Nifty below its 50-DMA → composite floor raised by
    BEARISH_SCORE_BUMP points and candidate list halved

Entry signals — continuous composite score 0–100 (replaces the old "N of 7"
count-vote; magnitude matters now, not just pass/fail). Each signal's old
binary threshold maps to ~50 strength, so a stock that barely cleared a
gate scores around 50 on it, and one deep in the zone scores near 100:
  1. RSI(14)           — 45–75 momentum-zone plateau, ramps either side
  2. MACD(12,26,9)     — histogram magnitude (bps of price) + crossover recency
  3. Bollinger Bands   — %B position above the middle band (continuation,
                         not lower-band "bounces" — those were knife-catches)
  4. Volume Surge      — surge-ratio strength, gated to UP-DAYS only
                         (direction-blind volume let distribution days score)
  5. Momentum Breakout — proximity to 52w high / size of 20d resistance break
  6. FII/DII Flow      — net 3-day flow MAGNITUDE (market-wide, not
                         stock-specific) — supersedes the old day-count
                         vote, where 1 of 3 days positive was nearly a
                         free signal point regardless of size
  7. Sector Sentiment  — negative → HARD EXCLUDE (overrides all technicals);
                         else mapped to a 0–100 strength (positive=100,
                         mild_positive=75, neutral=50, cautious=25)

  Weighted sum → composite score (0–100):
    momentum 0.20, volume 0.20, macd 0.15, sentiment 0.15,
    rsi 0.10, bollinger 0.10, fii 0.10
  Candidate if composite ≥ MIN_COMPOSITE_SCORE (62, bullish regime;
  +BEARISH_SCORE_BUMP when Nifty < 50-DMA). These floors translate from
  the old 5/7 (71%) and 6/7 (86%) count gates but are NOT yet backtested
  at this granularity — watch real scan output and recalibrate.

Entry discipline:
  - limit_price = scan close +2%. If it opens above that, the R/R you
    scanned no longer exists — skip, don't chase.

Exit rules (applied by swing_alerter.py):
  - Stop-loss: wider of (buy − 2.0× ATR-14) and 0.5% below the 5-day
    swing low — i.e. below structure, outside one normal day's noise
  - Target 1:  +7%  (book 50%)
  - Target 2:  +12% (book remaining 50%)
  - Min R/R:   1.5 on the blended exit (50% T1 + 50% T2 = +9.5%)
  - Time exit: force exit after 10 trading days
  - Sector cap: max 3 candidates from one sector

Schedule: 0 14 * * 1-5   (8:00 PM IST = 14:00 UTC on weekdays)

Usage:
  python swing_scanner.py           # run scan
  python swing_scanner.py --status  # show today's candidates
  python swing_scanner.py --test    # scan without saving
  python swing_scanner.py --ticker RELIANCE.NS  # scan single stock
"""

import yfinance as yf

# Sent with every API POST; uploads are rejected when the server has
# UPLOAD_TOKEN set and this env var is missing or wrong.
import os as _os_tok
_UPLOAD_AUTH = {"X-Upload-Token": _os_tok.environ["UPLOAD_TOKEN"]} if _os_tok.getenv("UPLOAD_TOKEN") else {}

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
API_URL         = os.getenv("API_URL", "https://web-production-50eee.up.railway.app").rstrip("/")
CANDIDATES_FILE = os.path.join(DATA_DIR, "swing_candidates.json")
SCAN_LOCK_FILE        = os.path.join(DATA_DIR, "swing_scan.lock")
SCAN_LOCK_TIMEOUT_SEC = 20 * 60  # stale-lock cutoff — longer than any real scan

# Oracle VPS — used for Zerodha OHLCV (more accurate NSE data than yfinance)
VPS_URL    = os.getenv("ORACLE_VPS_URL", "")
VPS_SECRET = os.getenv("EXECUTOR_SECRET", "")

# Signal parameters
RSI_PERIOD       = 14
RSI_MIN          = 45      # confirmed upswing — not still declining from oversold
RSI_MAX          = 75      # strong breakouts often run 70-75; don't exclude them
MACD_FAST        = 12
MACD_SLOW        = 26
MACD_SIGNAL      = 9
MACD_CROSS_DAYS  = 5       # crossover within last 5 days
BB_PERIOD        = 20
BB_STD           = 2.0
VOL_SURGE_MULT   = 2.0     # real conviction — volume > 2× 20-day avg ON AN UP-DAY
BREAKOUT_PCT     = 0.08    # within 8% of 52-week high or broke 20d resistance
FII_LOOKBACK     = 3       # days of FII data to check
FII_MIN_POSITIVE = 2       # legacy threshold, kept for the "note" text only —
                           # the composite score uses net flow magnitude instead
FII_SCALE_CR     = 5000.0  # ₹Cr of 3-day net flow treated as "large" for the
                           # strength curve — a rough starting calibration,
                           # not backtested against real flow distributions

MAX_CANDIDATES   = 10      # max candidates to report

# ── Continuous scoring (replaces the old "N of 7 signals" count-vote) ──
# Each signal contributes a 0-100 "strength" (magnitude-aware, not just
# pass/fail), weighted and summed into one composite 0-100 score. See the
# module docstring for the rationale and how each strength curve is anchored.
SIGNAL_WEIGHTS = {
    "momentum":  0.20,   # near 52w high / broke resistance — strongest trend confirmation
    "volume":    0.20,   # surge ON AN UP-DAY — conviction confirmation
    "macd":      0.15,   # trend momentum
    "sentiment": 0.15,   # sector news — contextual, not technical, but matters
    "rsi":       0.10,   # momentum zone — overlaps info with MACD, lower weight
    "bollinger": 0.10,   # overlaps info with MACD/momentum, lower weight
    "fii":       0.10,   # market-wide flow — macro tailwind, not stock-specific
}
assert abs(sum(SIGNAL_WEIGHTS.values()) - 1.0) < 1e-9

MIN_COMPOSITE_SCORE = 62.0   # bullish-regime floor, 0-100 scale
BEARISH_SCORE_BUMP  = 10.0   # added to the floor when Nifty < its 50-DMA
# Translated from the old 5/7 (71%) and 6/7 (86%) count thresholds — not
# yet backtested at this granularity. Watch real scan output and adjust.

# ── Hard gates (not scored — fail any one and the stock is skipped) ──
TREND_DMA            = 50    # stock must close above its own 50-DMA
EXT_MAX_ABOVE_20DMA  = 0.10  # skip if >10% above 20-DMA — overextended, mean-reverts
MAX_DAY_GAIN         = 0.07  # skip if scan-day gain >7% — news spike, you'd be chasing
MIN_PRICE            = 50.0  # skip penny-ish stocks — wide spreads, wild gaps
MAX_STOP_PCT         = 6.0   # if the structural stop is >6% away, the setup isn't
                             # tight enough — skip rather than tighten artificially
MAX_CHASE_PCT        = 0.02  # don't enter more than 2% above scan close (gap guard)

# Swing-specific ATR stop
# 2.0× keeps the stop outside one normal day's range after a volume-surge
# day; 1.5× sat inside routine retracement and was the main stop-out driver.
SWING_ATR_MULT   = 2.0
SWING_ATR_PERIOD = 14
SWING_TRAIL_MULT = 1.0     # tighter trail for swing

# Profit targets
SWING_TARGET_1   = 0.07   # +7%  → book 50%
SWING_TARGET_2   = 0.12   # +12% → book remaining 50%
SWING_MAX_DAYS   = 10     # force exit after 10 trading days

# Liquidity — swing needs more liquidity than long-term
SWING_MIN_ADV    = 300_000   # 3 lakh shares/day minimum
SWING_MIN_ADTV   = 5.0       # ₹5 crore/day minimum

# ── Sector → Bucket mapping (from nse_universe.py) ───────────
# ── Sector → Bucket mapping ───────────────────────────────────
# Uses exact NSE sector names from Nifty 500 classification.
# ELECTRONICS_SEMI is carved out from Capital Goods & Consumer Durables.
# All other stocks map directly to their NSE sector name.

# ── Sector → Bucket mapping ───────────────────────────────────
# Uses exact 20 NSE sector names from Nifty 500 (your Excel file).
# Each stock maps to its NSE sector for sentiment lookup.

NSE_SECTOR_BUCKET = {
    "Financial Services":             "Financial Services",
    "Information Technology":         "Information Technology",
    "Oil Gas And Consumable Fuels":   "Oil Gas And Consumable Fuels",
    "Fast Moving Consumer Goods":     "Fast Moving Consumer Goods",
    "Healthcare":                     "Healthcare",
    "Automobile and Auto Components": "Automobile and Auto Components",
    "Capital Goods":                  "Capital Goods",
    "Metals And Mining":              "Metals And Mining",
    "Consumer Durables":              "Consumer Durables",
    "Chemicals":                      "Chemicals",
    "Construction Materials":         "Construction Materials",
    "Power":                          "Power",
    "Telecommunication":              "Telecommunication",
    "Consumer Services":              "Consumer Services",
    "Services And Logistics":         "Services And Logistics",
    "Realty":                         "Realty",
    "Diversified And Infrastructure": "Diversified And Infrastructure",
    "Textiles And Apparel":           "Textiles And Apparel",
    "Media And Entertainment":        "Media And Entertainment",
    "Paper And Forest Products":      "Paper And Forest Products",
}

# ── Sentiment scoring ─────────────────────────────────────────
# negative  → HARD EXCLUDE (overrides all technical signals)
# cautious  → −1 from signal score
# neutral   → no effect
# mild_positive → +0.5 signal
# positive  → +1 full signal pass
SENTIMENT_SCORE = {
    "negative":      -99,   # sentinel for hard exclude
    "cautious":      -1,
    "neutral":        0,
    "mild_positive":  0.5,
    "positive":       1,
}

# 0-100 strength for the composite score. negative never reaches scoring
# (hard-excluded before the composite is computed) — mapped here only so
# the lookup never KeyErrors on an unexpected label.
SENTIMENT_STRENGTH = {
    "negative":       0.0,
    "cautious":      25.0,
    "neutral":       50.0,
    "mild_positive": 75.0,
    "positive":     100.0,
}


# ─────────────────────────────────────────────
# DATA FETCHER — one call per stock
# ─────────────────────────────────────────────

def fetch_ohlcv(ticker: str) -> Optional[pd.DataFrame]:
    """
    Fetch ~1 year of daily OHLCV for signal calculation.
    Tries Zerodha via Oracle VPS first (authoritative NSE feed),
    falls back to yfinance if VPS is unreachable or returns no data.
    """
    symbol = ticker.replace(".NS", "").replace(".BO", "")
    if VPS_URL:
        df = _fetch_ohlcv_kite(symbol)
        if df is not None:
            return df
        print(f"  ↩  {symbol}: Zerodha fetch failed — falling back to yfinance")
    return _fetch_ohlcv_yf(ticker)


def _fetch_ohlcv_kite(symbol: str) -> Optional[pd.DataFrame]:
    """Fetch OHLCV from Zerodha via Oracle VPS. Returns DataFrame matching yfinance format."""
    try:
        req = _urllib.Request(
            f"{VPS_URL}/get-historical?symbol={symbol}&days=400",
            headers={"X-Executor-Secret": VPS_SECRET},
        )
        with _urllib.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
        rows = data.get("rows", [])
        if len(rows) < 60:
            return None
        df = pd.DataFrame(rows)
        df["Date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
        df = df.set_index("Date").rename(columns={
            "open": "Open", "high": "High",
            "low":  "Low",  "close": "Close", "volume": "Volume",
        })
        return df[["Open", "High", "Low", "Close", "Volume"]]
    except Exception as e:
        print(f"  ⚠️  Kite OHLCV error for {symbol}: {e}")
        return None


def _fetch_ohlcv_yf(ticker: str) -> Optional[pd.DataFrame]:
    """Fetch OHLCV from yfinance (fallback)."""
    try:
        hist = yf.Ticker(ticker).history(period="1y")
        if hist.empty or len(hist) < 60:
            return None
        return hist
    except Exception:
        return None


# ─────────────────────────────────────────────
# CONTINUOUS STRENGTH HELPERS
# ─────────────────────────────────────────────

def _ramp(x: float, lo: float, hi: float) -> float:
    """Linear 0→100 as x goes lo→hi. Clamped outside [lo, hi]."""
    if hi == lo:
        return 100.0 if x >= hi else 0.0
    return float(np.clip((x - lo) / (hi - lo) * 100, 0, 100))


def _trapezoid(x: float, a: float, b: float, c: float, d: float) -> float:
    """0 below a, ramps to 100 at b, flat 100 between b and c, ramps to 0 at d."""
    if x <= a or x >= d:
        return 0.0
    if x < b:
        return _ramp(x, a, b)
    if x <= c:
        return 100.0
    return 100.0 - _ramp(x, c, d)


# ─────────────────────────────────────────────
# SIGNAL CALCULATORS
# ─────────────────────────────────────────────

def calc_rsi(closes: pd.Series, period: int = RSI_PERIOD) -> float:
    """RSI with Wilder's smoothing — matches TradingView/Kite chart values.
    (A plain rolling mean gives noticeably different readings near the 42/70 gates.)"""
    delta  = closes.diff().dropna()
    gain   = delta.clip(lower=0)
    loss   = (-delta).clip(lower=0)
    avg_g  = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_l  = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    rs     = avg_g / avg_l.replace(0, np.nan)
    rsi    = 100 - (100 / (1 + rs))
    return round(float(rsi.iloc[-1]), 2) if not rsi.empty else 50.0


def calc_rsi_strength(rsi: float) -> float:
    """0-100 strength. The old binary zone (RSI_MIN-RSI_MAX) is the
    full-strength plateau; partial credit ramps in/out over a 15-point
    band on each side instead of a hard cliff at the old gate edges."""
    return round(_trapezoid(rsi, RSI_MIN - 15, RSI_MIN, RSI_MAX, RSI_MAX + 15), 1)


def calc_macd(closes: pd.Series) -> dict:
    """
    MACD(12,26,9). Returns MACD line, signal line, histogram, whether a
    bullish crossover happened in last MACD_CROSS_DAYS days, and a
    continuous 0-100 strength for the composite score.
    """
    ema12  = closes.ewm(span=MACD_FAST,  adjust=False).mean()
    ema26  = closes.ewm(span=MACD_SLOW,  adjust=False).mean()
    macd   = ema12 - ema26
    signal = macd.ewm(span=MACD_SIGNAL, adjust=False).mean()
    hist   = macd - signal

    # Bullish crossover = MACD crossed ABOVE signal in last N days
    crossed, cross_days_ago = False, None
    for i in range(1, min(MACD_CROSS_DAYS + 1, len(hist))):
        if hist.iloc[-i] > 0 and hist.iloc[-(i+1)] <= 0:
            crossed, cross_days_ago = True, i
            break

    hist_growing = float(hist.iloc[-1]) > float(hist.iloc[-2]) if len(hist) >= 2 else False
    macd_above   = float(macd.iloc[-1]) > float(signal.iloc[-1])

    # Strength: histogram magnitude in basis points of price (comparable
    # across stocks regardless of absolute share price), boosted by a
    # fresh crossover — a cross is the strongest form of this signal even
    # before the histogram has had time to build magnitude.
    curr_price = float(closes.iloc[-1])
    hist_bps   = (float(hist.iloc[-1]) / curr_price) * 10000 if curr_price else 0.0
    strength   = _ramp(hist_bps, -10, 50)
    if crossed:
        strength = max(strength, 100.0 - (cross_days_ago - 1) * (100.0 / MACD_CROSS_DAYS))
    elif macd_above and hist_growing:
        strength = max(strength, 50.0)

    return {
        "macd":          round(float(macd.iloc[-1]), 4),
        "signal":        round(float(signal.iloc[-1]), 4),
        "histogram":     round(float(hist.iloc[-1]), 4),
        "bullish_cross": crossed,
        "cross_days_ago":cross_days_ago,
        "macd_above":    macd_above,
        "hist_growing":  hist_growing,
        "strength":      round(strength, 1),
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
    # near_lower kept for info only — buying a lower-band touch is a
    # mean-reversion entry, and pairing it with a momentum-style ATR stop
    # produced knife-catches that drove the stop-out rate. Not a signal.
    near_lower  = float(closes.iloc[-2]) <= float(lower.iloc[-2]) * 1.01

    pct_b = round((curr - float(lower.iloc[-1])) /
                  (float(upper.iloc[-1]) - float(lower.iloc[-1]) + 1e-9) * 100, 1)

    # Strength: plateau just above the middle band (confirmed continuation),
    # ramping back down near the upper band (extended, mean-reversion risk).
    strength = _trapezoid(pct_b, 30, 50, 80, 100)
    if crossed_mid:
        strength = max(strength, 50.0)

    return {
        "upper":        round(float(upper.iloc[-1]), 2),
        "middle":       round(mid, 2),
        "lower":        round(float(lower.iloc[-1]), 2),
        "current":      round(curr, 2),
        "crossed_mid":  crossed_mid,
        "near_lower":   near_lower,
        "signal":       crossed_mid,
        "pct_b":        pct_b,
        "strength":     round(strength, 1),
    }


def calc_volume_surge(hist: pd.DataFrame) -> dict:
    """Volume surge: today > 2× 20-day average — on an UP-day.

    Volume alone is direction-blind: a stock dumping 8% on 3× volume is
    distribution, not accumulation, yet it used to score this point and
    then get bought as a 'bounce'. Surge now requires close > prev close
    AND close > open (buyers finished in control of the day)."""
    vol       = hist["Volume"]
    closes    = hist["Close"]
    opens     = hist["Open"]
    avg_20    = float(vol.iloc[-21:-1].mean())
    today_vol = float(vol.iloc[-1])
    ratio     = round(today_vol / avg_20, 2) if avg_20 > 0 else 1.0
    up_day    = (float(closes.iloc[-1]) > float(closes.iloc[-2])
                 and float(closes.iloc[-1]) > float(opens.iloc[-1]))
    surge     = ratio >= VOL_SURGE_MULT and up_day

    # Strength: zero on a down/flat day regardless of volume — this is the
    # exact distribution-day case the up_day requirement exists to exclude,
    # so the continuous score must not partially reward it either.
    strength = _ramp(ratio, 0.5, 3.5) if up_day else 0.0
    if surge:
        strength = max(strength, 50.0)

    return {
        "today_vol":  int(today_vol),
        "avg_20d":    int(avg_20),
        "ratio":      ratio,
        "up_day":     up_day,
        "surge":      surge,
        "strength":   round(strength, 1),
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
    signal    = pct_from <= BREAKOUT_PCT * 100 or broke_20d

    # Strength: take the stronger of the two paths (OR logic preserved).
    # near_52w_strength: 100 at the high itself, 0 at 2x the old 8% gate.
    # breakout_strength: how far above (or below) the 20d resistance, in %.
    near_52w_strength = max(0.0, 100.0 * (1 - pct_from / (BREAKOUT_PCT * 100 * 2)))
    breakout_pct      = (curr - res_20d) / res_20d * 100 if res_20d else 0.0
    breakout_strength = _ramp(breakout_pct, -4, 4)
    strength          = max(near_52w_strength, breakout_strength)
    if signal:
        strength = max(strength, 50.0)

    return {
        "high_52w":     round(high_52w, 2),
        "current":      round(curr, 2),
        "pct_from_52w": pct_from,
        "near_52w":     pct_from <= BREAKOUT_PCT * 100,
        "broke_20d_res":broke_20d,
        "res_20d":      round(res_20d, 2),
        "signal":       signal,
        "strength":     round(strength, 1),
    }


def check_hard_gates(hist: pd.DataFrame) -> Optional[str]:
    """
    Hard disqualifiers, checked before any scoring. Returns a reason
    string if the stock fails, else None.

    These are not scored signals — a breakout setup in a downtrending
    stock, or one already 12% above its 20-DMA, fails at a far higher
    rate regardless of how many momentum boxes it ticks.
    """
    closes = hist["Close"]
    curr   = float(closes.iloc[-1])
    prev   = float(closes.iloc[-2])

    # Penny / illiquid price floor
    if curr < MIN_PRICE:
        return f"price ₹{curr:.0f} < ₹{MIN_PRICE:.0f} floor"

    # Stock's own trend: must close above its 50-DMA, and the 20-DMA
    # must be rising (vs 5 sessions ago). Bear-rally bounces fail here.
    if len(closes) >= TREND_DMA:
        dma50 = float(closes.rolling(TREND_DMA).mean().iloc[-1])
        if curr < dma50:
            return f"below own 50-DMA (₹{curr:,.1f} < ₹{dma50:,.1f}) — downtrend"
    dma20_series = closes.rolling(20).mean()
    if len(dma20_series.dropna()) >= 6:
        if float(dma20_series.iloc[-1]) <= float(dma20_series.iloc[-6]):
            return "20-DMA falling — no established short-term uptrend"

    # Overextension: >10% above 20-DMA mean-reverts before it continues
    dma20 = float(dma20_series.iloc[-1]) if not np.isnan(dma20_series.iloc[-1]) else None
    if dma20 and curr > dma20 * (1 + EXT_MAX_ABOVE_20DMA):
        return (f"{(curr/dma20-1)*100:.1f}% above 20-DMA "
                f"(max {EXT_MAX_ABOVE_20DMA*100:.0f}%) — overextended")

    # News-spike day: entering tomorrow means chasing today's +7%+ pop
    day_gain = (curr - prev) / prev if prev > 0 else 0
    if day_gain > MAX_DAY_GAIN:
        return (f"scan-day spike +{day_gain*100:.1f}% "
                f"(max +{MAX_DAY_GAIN*100:.0f}%) — would be chasing")

    return None


def calc_fii_signal(fii_data: list) -> dict:
    """
    Market-wide FII/DII net flow (same value applied to every ticker that
    day — this is a macro tailwind/headwind, not a stock-specific signal).
    fii_data: list of {date, fii_net_cr, dii_net_cr} sorted newest first.

    Strength is driven by NET FLOW MAGNITUDE over the last FII_LOOKBACK
    days, not the day-count vote the old binary signal used — "2 of 3
    days positive" was nearly always true regardless of size (a free
    signal point), so magnitude is a more honest read of how strong the
    institutional tailwind actually is.

    When the collector feed is down, fii_data is empty. The old "no data"
    return was missing the net_3d_cr key that analyse_stock() reads
    unconditionally — every ticker raised a KeyError here, was caught by
    the scan loop's generic except, and was silently skipped. So a broken
    FII feed didn't just make this one signal harder to pass; it crashed
    every stock before scoring and produced zero candidates outright.
    Treat "no data" as neutral pass-through (strength 50, same as missing
    sector sentiment) instead of a penalty, and log loudly so a real
    outage is still visible in Railway logs.
    """
    if not fii_data or len(fii_data) < 1:
        return {"signal": True, "net_3d_cr": 0.0, "positive_days": 0, "total_days": 0,
                "data_available": False, "strength": 50.0,
                "note": "⚠️ No FII data available — excluded from scoring (neutral)"}

    recent   = fii_data[:FII_LOOKBACK]
    pos      = sum(1 for r in recent if r.get("fii_net_cr", 0) > 0)
    net_3d   = sum(r.get("fii_net_cr", 0) for r in recent)
    strength = float(np.clip(50 + 50 * (net_3d / FII_SCALE_CR), 0, 100))

    return {
        "signal":        pos >= FII_MIN_POSITIVE,
        "positive_days": pos,
        "total_days":    len(recent),
        "net_3d_cr":     round(net_3d, 2),
        "strength":      round(strength, 1),
        "note":          f"FII net ₹{net_3d:.0f}Cr over {len(recent)}d ({pos}/{len(recent)} days positive)",
    }


def fetch_market_regime() -> dict:
    """
    Nifty 50 vs its 50-DMA. Breakout/momentum entries have a much lower
    hit rate when the index is below trend, so the scan raises the signal
    bar and cuts the candidate count instead of buying every bounce in a
    falling market.
    """
    try:
        hist = yf.Ticker("^NSEI").history(period="6mo")
        if hist.empty or len(hist) < 50:
            raise ValueError("insufficient index history")
        close = float(hist["Close"].iloc[-1])
        dma50 = float(hist["Close"].rolling(50).mean().iloc[-1])
        return {
            "nifty_close": round(close, 1),
            "dma_50":      round(dma50, 1),
            "bullish":     close > dma50,
        }
    except Exception as e:
        return {"nifty_close": None, "dma_50": None, "bullish": True,
                "note": f"regime check failed ({e}) — defaulting to bullish"}


EARNINGS_BLACKOUT_DAYS = 14   # calendar days — covers the 10-trading-day max hold

def earnings_within_blackout(ticker: str) -> Optional[str]:
    """
    Return the earnings date string if the stock reports within the blackout
    window, else None. A swing entry that holds through results is a coin
    flip, not a setup. Only called for stocks that already passed signals,
    so the extra API call is cheap.
    """
    try:
        cal = yf.Ticker(ticker).calendar
        dates = []
        if isinstance(cal, dict):
            ed = cal.get("Earnings Date")
            if isinstance(ed, (list, tuple)):
                dates = list(ed)
            elif ed is not None:
                dates = [ed]
        elif cal is not None and hasattr(cal, "loc"):   # old DataFrame API
            if "Earnings Date" in getattr(cal, "index", []):
                dates = [d for d in cal.loc["Earnings Date"] if d is not None]
        for d in dates:
            if hasattr(d, "date") and not isinstance(d, date):
                d = d.date()
            if isinstance(d, date):
                delta = (d - date.today()).days
                if 0 <= delta <= EARNINGS_BLACKOUT_DAYS:
                    return str(d)
    except Exception:
        pass
    return None


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
    """Compute stop-loss and profit targets for a swing trade.

    Stop placement (the old version was the main loss driver):
      - OLD: buy − 1.5×ATR from the surge-day close. After a high-volume
        breakout day, a routine 1-2 ATR retracement walked straight
        through it. Plus the R/R≥1.5-to-T1 filter mathematically capped
        the stop at 4.67%, so the scanner *selected for* stops inside
        daily noise.
      - NEW: the WIDER (lower) of 2.0×ATR and the 5-day swing low —
        i.e. below actual structure, where the breakout is genuinely
        invalidated. If that level is >MAX_STOP_PCT away the setup
        isn't tight enough and the trade is skipped (in analyse_stock),
        instead of artificially tightening the stop.

    R/R uses the blended exit (50% at T1, 50% at T2 = +9.5% expected
    reward), not T1 alone, so a structurally-correct stop isn't
    filtered out for being honest about risk.
    """
    atr = calc_atr(hist)
    if atr and atr > 0:
        atr_stop    = buy_price - SWING_ATR_MULT * atr
        struct_stop = float(hist["Low"].iloc[-5:].min()) * 0.995  # under 5-day swing low
        stop  = round(min(atr_stop, struct_stop), 2)
        trail = round(SWING_TRAIL_MULT * atr, 2)
        src   = "ATR+STRUCT"
    else:
        stop  = round(buy_price * 0.95, 2)   # 5% fallback
        trail = round(buy_price * 0.02, 2)
        atr   = None
        src   = "FALLBACK"

    stop_pct   = round((buy_price - stop) / buy_price * 100, 2)
    target1    = round(buy_price * (1 + SWING_TARGET_1), 2)
    target2    = round(buy_price * (1 + SWING_TARGET_2), 2)
    # Blended reward: half booked at T1, half at T2
    blended_reward = buy_price * (0.5 * SWING_TARGET_1 + 0.5 * SWING_TARGET_2)
    rr_ratio   = round(blended_reward / (buy_price - stop), 2) if stop < buy_price else 0

    return {
        "atr":          atr,
        "atr_mult":     SWING_ATR_MULT,
        "stop_loss":    stop,
        "stop_pct":     stop_pct,
        "trailing":     trail,
        "target1":      target1,
        "target2":      target2,
        "rr_ratio":     rr_ratio,  # blended reward / risk
        "max_days":     SWING_MAX_DAYS,
        "source":       src,
    }


# ─────────────────────────────────────────────
# SINGLE STOCK ANALYSER
# ─────────────────────────────────────────────

def analyse_stock(ticker: str, fii_data: list, sentiment_signals: dict,
                  min_composite: float = MIN_COMPOSITE_SCORE) -> Optional[dict]:
    """
    Run all signals on a single stock.
    Returns candidate dict if the weighted composite score (0-100) ≥
    min_composite, else None. min_composite is raised by BEARISH_SCORE_BUMP
    when the market regime is bearish.
    """
    hist = fetch_ohlcv(ticker)
    if hist is None:
        return None

    # Liquidity gate first
    liquid, liq_reason = passes_liquidity(hist, ticker)
    if not liquid:
        return None

    # Hard gates: own-trend, overextension, news-spike, price floor.
    # Checked before scoring — a high technical score doesn't rescue
    # a setup that fails any of these.
    gate_fail = check_hard_gates(hist)
    if gate_fail:
        return None

    closes = hist["Close"]
    curr   = float(closes.iloc[-1])

    # ── Calculate all 6 technical signals ─────────────────────
    rsi          = calc_rsi(closes)
    rsi_strength = calc_rsi_strength(rsi)
    macd    = calc_macd(closes)
    bb      = calc_bollinger(hist)
    vol     = calc_volume_surge(hist)
    mom     = calc_momentum_breakout(hist)
    fii     = calc_fii_signal(fii_data)

    # ── Score each signal — "pass" kept for the existing pass/fail
    # display (dashboard's "signals passed/failed" breakdown); "strength"
    # (0-100) and "weight" feed the composite score below. ─────────────
    signals = {
        "rsi": {
            "pass":     RSI_MIN <= rsi <= RSI_MAX,
            "value":    rsi,
            "strength": rsi_strength,
            "weight":   SIGNAL_WEIGHTS["rsi"],
            "note":     f"RSI {rsi:.1f} ({'✅ momentum zone' if RSI_MIN <= rsi <= RSI_MAX else f'❌ outside {RSI_MIN}-{RSI_MAX}'})",
        },
        "macd": {
            "pass":     macd["bullish_cross"] or (macd["macd_above"] and macd["hist_growing"]),
            "value":    macd["histogram"],
            "strength": macd["strength"],
            "weight":   SIGNAL_WEIGHTS["macd"],
            "note":     f"MACD {'✅ bullish cross' if macd['bullish_cross'] else ('✅ above+growing' if macd['macd_above'] and macd['hist_growing'] else '❌ no momentum')}",
        },
        "bollinger": {
            "pass":     bb["signal"],
            "value":    bb["pct_b"],
            "strength": bb["strength"],
            "weight":   SIGNAL_WEIGHTS["bollinger"],
            "note":     f"BB %B={bb['pct_b']:.0f}% {'✅ crossed middle' if bb['crossed_mid'] else ('✅ near lower' if bb['near_lower'] else '❌')}",
        },
        "volume": {
            "pass":     vol["surge"],
            "value":    vol["ratio"],
            "strength": vol["strength"],
            "weight":   SIGNAL_WEIGHTS["volume"],
            "note":     f"Volume {vol['ratio']:.1f}× avg {'✅ surge' if vol['surge'] else '❌ normal'}",
        },
        "momentum": {
            "pass":     mom["signal"],
            "value":    mom["pct_from_52w"],
            "strength": mom["strength"],
            "weight":   SIGNAL_WEIGHTS["momentum"],
            "note":     f"{'✅ near 52w high' if mom['near_52w'] else ('✅ broke 20d resistance' if mom['broke_20d_res'] else '❌')} ({mom['pct_from_52w']:.1f}% from 52w)",
        },
        "fii": {
            "pass":     fii["signal"],
            "value":    fii["net_3d_cr"],
            "strength": fii["strength"],
            "weight":   SIGNAL_WEIGHTS["fii"],
            "note":     fii["note"],
        },
    }

    # ── Legacy count (informational only — composite score below is
    # what actually gates and ranks candidates now) ───────────────────
    tech_score = sum(1 for s in signals.values() if s["pass"])

    # ── Signal 7: Sector Sentiment ────────────────────────────
    # Fetch stock info once — reused for sentiment + name below
    try:
        info       = yf.Ticker(ticker).info
        name       = info.get("longName", ticker.replace(".NS",""))
        sector_raw = info.get("sector","") or info.get("industry","") or ""
    except Exception:
        info       = {}
        name       = ticker.replace(".NS","")
        sector_raw = ""

    # Map yfinance sector string → NSE sector bucket
    sector_lower = sector_raw.lower()
    bucket_key   = None
    for nse_sec in NSE_SECTOR_BUCKET:
        if nse_sec.lower() in sector_lower or sector_lower in nse_sec.lower():
            bucket_key = nse_sec
            break

    sentiment_val         = "neutral"
    sentiment_score_adj   = 0.0
    excluded_by_sentiment = False

    if bucket_key and sentiment_signals:
        sentiment_val = sentiment_signals.get(bucket_key, "neutral")
        adj = SENTIMENT_SCORE.get(sentiment_val, 0)
        if adj == -99:
            excluded_by_sentiment = True   # HARD EXCLUDE
        else:
            sentiment_score_adj = float(adj)

    sent_emoji = (
        "🚫" if excluded_by_sentiment else
        "✅" if sentiment_score_adj > 0 else
        "⚠️" if sentiment_score_adj < 0 else "➖"
    )
    sentiment_strength = SENTIMENT_STRENGTH.get(sentiment_val, 50.0)
    signals["sentiment"] = {
        "pass":     sentiment_score_adj > 0,
        "value":    sentiment_val,
        "strength": sentiment_strength,
        "weight":   SIGNAL_WEIGHTS["sentiment"],
        "note":     (
            f"{sent_emoji} {bucket_key or 'unmapped'} → {sentiment_val}"
            + (" (HARD EXCLUDE)" if excluded_by_sentiment else
               f" ({sentiment_score_adj:+.1f})" if sentiment_score_adj != 0 else "")
        ),
    }

    # Hard exclude — negative sentiment disqualifies regardless of technicals
    if excluded_by_sentiment:
        print(f"  🚫 {ticker} EXCLUDED — negative sentiment ({bucket_key})")
        return None

    # ── Composite score: weighted sum of all 7 strengths (0-100) ──────
    # Replaces the old "tech_score (0-6) + sentiment modifier" count-vote.
    score = sum(sig["strength"] * sig["weight"] for sig in signals.values())
    for sig in signals.values():
        sig["contribution"] = round(sig["strength"] * sig["weight"], 1)

    if score < min_composite:
        return None

    # Earnings blackout — don't hold a 10-day swing through quarterly results
    earnings_date = earnings_within_blackout(ticker)
    if earnings_date:
        print(f"  📅 {ticker} skipped — earnings {earnings_date} inside "
              f"{EARNINGS_BLACKOUT_DAYS}-day blackout window")
        return None

    # ── Swing levels ──────────────────────────────────────────
    levels = compute_swing_levels(hist, curr)

    # Structural stop too far away → setup isn't tight, skip the trade
    # (do NOT tighten the stop to force the trade — that was the old failure mode)
    if levels["stop_pct"] > MAX_STOP_PCT:
        return None

    # Filter poor R/R — blended reward must be 1.5× the structural risk
    if levels["rr_ratio"] < 1.5:
        return None

    # ── Conviction (based on the composite 0-100 score) ─────────
    conviction = "HIGH" if score >= 80 else "MEDIUM" if score >= 65 else "LOW"

    # ── Optimal entry calculation ──────────────────────────────
    # The scan close is the signal confirmation price, not the entry price.
    # Entry should be placed at the technical level the signal is anchored to:
    #
    # Breakout trade (volume surge + broke 20d resistance):
    #   The breakout happened FROM the 20d resistance. A pullback to that
    #   level is the ideal entry — you're buying confirmed support, not the spike.
    #
    # BB-cross trade (price crossed 20-DMA from below):
    #   The 20-DMA is the anchor. Enter just above it (0.3% buffer for slippage).
    #
    # Pure momentum (RSI/MACD only, no structural breakout):
    #   No strong structural level nearby; use a small pullback from close.
    #
    # In all cases: optimal_entry is capped at the scan close (never above).
    bb_mid  = bb["middle"]                    # 20-DMA = Bollinger middle band
    r20d    = mom.get("res_20d", curr)        # 20-day resistance level
    vol_fired = signals["volume"]["pass"]
    mom_fired = signals["momentum"]["pass"]
    bb_fired  = signals["bollinger"]["pass"]

    if vol_fired and mom_fired:
        # Breakout: ideal entry is the broken resistance (now support)
        raw_entry = max(bb_mid, r20d * 1.001)
    elif bb_fired:
        # BB-cross: anchor is the 20-DMA just crossed
        raw_entry = bb_mid * 1.003
    else:
        # RSI/MACD momentum: small pullback from close
        raw_entry = curr * 0.985

    optimal_entry = round(min(raw_entry, curr), 2)

    # Entry type label for the UI
    if vol_fired and mom_fired:
        entry_type = "breakout-pullback"
        entry_basis = f"broken 20d resistance ₹{r20d:.2f}"
    elif bb_fired:
        entry_type = "bb-cross"
        entry_basis = f"20-DMA ₹{bb_mid:.2f}"
    else:
        entry_type = "momentum-dip"
        entry_basis = "1.5% pullback from scan close"

    return {
        "ticker":        ticker,
        "name":          name,
        "sector":        sector_raw,
        "scanned_at":    datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"),
        "current_price": round(curr, 2),
        "score":         round(score, 1),
        "tech_score":    tech_score,   # legacy 0-6 count, informational only
        "max_score":     100,
        "conviction":    conviction,
        # Signals detail
        "signals":       signals,
        # Sentiment
        "sentiment_val":    sentiment_val,
        "sentiment_bucket": bucket_key or "unmapped",
        # Technical values
        "rsi":           rsi,
        "macd_hist":     macd["histogram"],
        "bb_pct_b":      bb["pct_b"],
        "bb_middle":     round(bb_mid, 2),
        "vol_ratio":     vol["ratio"],
        "pct_from_52w":  mom["pct_from_52w"],
        "res_20d":       round(r20d, 2),
        "fii_net_3d":    fii["net_3d_cr"],
        # Entry levels
        "optimal_entry": optimal_entry,
        "entry_type":    entry_type,
        "entry_basis":   entry_basis,
        # Absolute maximum chase price (R/R breaks above this)
        "limit_price":   round(curr * (1 + MAX_CHASE_PCT), 2),
        "entry_note":    f"Optimal entry ₹{optimal_entry:.2f} ({entry_basis}). Hard limit ₹{curr * (1 + MAX_CHASE_PCT):.2f}.",
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


def fetch_sentiment_signals() -> dict:
    """
    Fetch per-sector swing sentiment signals.
    Priority:
      1. swing_news_sentiment.json (from swing_news_sentiment.py) — direct file read
      2. /signals API endpoint — fallback if file not available
    Returns {sector_name: signal_string} e.g. {"Healthcare": "positive", ...}
    """
    # ── Priority 1: local file ────────────────────────────────
    local_file = os.path.join(DATA_DIR, "swing_news_sentiment.json")
    if os.path.exists(local_file):
        try:
            with open(local_file) as f:
                data = json.load(f)
            raw = data.get("signals", {})
            # Flatten to {sector: signal_string}
            signals = {k: v["signal"] if isinstance(v, dict) else v
                       for k, v in raw.items()}
            if signals:
                print(f"  ✅ Sentiment loaded from file ({len(signals)} sectors)")
                return signals
        except Exception as e:
            print(f"  ⚠️  Could not read swing sentiment file: {e}")

    # ── Priority 2: API /signals endpoint ────────────────────
    try:
        req = _urllib.Request(
            f"{API_URL}/signals",
            headers={"Accept": "application/json"}
        )
        with _urllib.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())

        # swing_news_sentiment is stored as {signals: {sector: {signal, score, ...}}}
        swing_raw = data.get("swing_news_sentiment", {})
        if swing_raw:
            # Handle both direct and payload-wrapped formats
            inner = swing_raw.get("payload", swing_raw)
            sector_signals = inner.get("signals", {})
            signals = {}
            for sector, val in sector_signals.items():
                if isinstance(val, dict):
                    signals[sector] = val.get("signal", "neutral")
                elif isinstance(val, str):
                    signals[sector] = val
            if signals:
                print(f"  ✅ Swing sentiment loaded from API ({len(signals)} sectors)")
                return signals

        # Fallback: long-term news_signals (4 broad buckets — partial coverage)
        news = data.get("news_signals", {})
        if isinstance(news, dict):
            inner = news.get("payload", news)
            sector_signals = inner.get("signals", {})
            signals = {}
            for k, v in sector_signals.items():
                if isinstance(v, dict):
                    signals[k] = v.get("signal", "neutral")
                elif isinstance(v, str):
                    signals[k] = v
            if signals:
                print(f"  ⚠️  Swing sentiment not found — using long-term signals "
                      f"({len(signals)} buckets, partial coverage)")
                return signals

        print("  ⚠️  No sentiment signals available — sentiment check skipped")
        return {}

    except Exception as e:
        print(f"  ⚠️  Could not fetch sentiment signals: {e}")
        return {}


def fetch_fii_data() -> list:
    """Fetch FII/DII data from API. Returns list sorted newest first."""
    url = f"{API_URL}/fiidii"
    try:
        req = _urllib.Request(url, headers={"Accept": "application/json"})
        with _urllib.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        if isinstance(data, list):
            print(f"  ✅ FII data loaded from {url}: {len(data)} days")
            return sorted(data, key=lambda x: x.get("date",""), reverse=True)
        return []
    except Exception as e:
        print(f"  ⚠️  Could not fetch FII data from {url} ({e}) — "
              f"FII signal will fail for every stock today")
        return []


# ─────────────────────────────────────────────
# MAIN SCANNER
# ─────────────────────────────────────────────

def _acquire_scan_lock() -> bool:
    """True if lock acquired. False if a fresh lock from another run already exists."""
    if os.path.exists(SCAN_LOCK_FILE):
        age = time.time() - os.path.getmtime(SCAN_LOCK_FILE)
        if age < SCAN_LOCK_TIMEOUT_SEC:
            return False
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SCAN_LOCK_FILE, "w") as f:
        f.write(datetime.now(IST).isoformat())
    return True


def _release_scan_lock():
    try:
        os.remove(SCAN_LOCK_FILE)
    except OSError:
        pass


def run_scan(test_mode: bool = False, single_ticker: str = None) -> list:
    """Lock-guarded entry point — protects the cron and the manual dashboard
    trigger from stepping on each other. Single-ticker/test runs are exempt
    since they don't save and are used for quick manual debugging."""
    guard = not test_mode and not single_ticker
    if guard and not _acquire_scan_lock():
        print("⏭  Scan already in progress (lock held) — skipping this run.")
        return []
    try:
        return _run_scan_impl(test_mode=test_mode, single_ticker=single_ticker)
    finally:
        if guard:
            _release_scan_lock()


def _run_scan_impl(test_mode: bool = False, single_ticker: str = None) -> list:
    """
    Scan Nifty 500 for swing trade candidates.
    Returns sorted list of candidates (best score first).
    """
    # ── Step 0: Market regime — tighten the bar in a falling market ──
    regime = fetch_market_regime()
    if regime["bullish"]:
        min_composite  = MIN_COMPOSITE_SCORE
        max_candidates = MAX_CANDIDATES
        regime_label   = "🟢 BULLISH (Nifty above 50-DMA)"
    else:
        min_composite  = MIN_COMPOSITE_SCORE + BEARISH_SCORE_BUMP
        max_candidates = max(MAX_CANDIDATES // 2, 3)
        regime_label   = "🔴 BEARISH (Nifty below 50-DMA) — bar raised"

    print(f"\n{'='*58}")
    print(f"  📈 SWING SCANNER — INDIA NSE")
    print(f"  {datetime.now(IST).strftime('%d %B %Y, %I:%M %p IST')}")
    print(f"  Regime: {regime_label}")
    if regime.get("nifty_close"):
        print(f"  Nifty: {regime['nifty_close']:,.1f}  |  50-DMA: {regime['dma_50']:,.1f}")
    print(f"  Min composite score: {min_composite:.0f}/100  |  Max candidates: {max_candidates}")
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

    # ── Step 3: Fetch sentiment signals once ──────────────────
    print("  Fetching sentiment signals...")
    sentiment_signals = fetch_sentiment_signals()
    if sentiment_signals:
        for bkt, sent in sentiment_signals.items():
            excl = " ← 🚫 HARD EXCLUDE for this bucket's stocks" if sent == "negative" else ""
            print(f"    {bkt}: {sent}{excl}")

    # ── Step 3: Scan each stock ───────────────────────────────
    candidates  = []
    scanned     = 0
    liq_fail    = 0
    sig_fail    = 0

    ohlcv_src = f"Zerodha ({VPS_URL})" if VPS_URL else "yfinance (fallback)"
    print(f"\n  OHLCV source: {ohlcv_src}")
    print(f"  Scanning {len(tickers)} stocks...\n")

    for ticker in tickers:
        try:
            result = analyse_stock(ticker, fii_data, sentiment_signals, min_composite)
            scanned += 1

            if result is None:
                sig_fail += 1
            else:
                candidates.append(result)
                conv = result["conviction"]
                emoji = "🔥" if conv == "HIGH" else "⚡" if conv == "MEDIUM" else "✳️"
                sent_icon = {"positive":"🟢","mild_positive":"🟡","neutral":"⬜","cautious":"🟠","negative":"🔴"}.get(result.get("sentiment_val","neutral"),"⬜")
                print(f"  {emoji} {ticker:<20} Score:{result['score']:.1f}/100  "
                      f"RSI:{result['rsi']:.0f}  "
                      f"Vol:{result['vol_ratio']:.1f}×  "
                      f"Sent:{sent_icon}{result.get('sentiment_val','—')[:4]}  "
                      f"{conv}")

            if scanned % 50 == 0:
                print(f"  ... {scanned}/{len(tickers)} scanned, "
                      f"{len(candidates)} candidates so far")

            # Zerodha historical data limit: 3 req/sec → sleep ≥ 0.35s
            time.sleep(0.4 if VPS_URL else 0.3)

        except Exception as e:
            print(f"  ⚠️  {ticker}: {e}")
            continue

    # ── Step 4: Sort and filter (max 3 per sector so one hot sector
    #            can't fill the whole list) ─────────────────────
    candidates.sort(key=lambda x: (x["score"], x["rr_ratio"]), reverse=True)
    MAX_PER_SECTOR = 3
    top, sector_count = [], {}
    for c in candidates:
        sec = c.get("sector") or "Unknown"
        if sector_count.get(sec, 0) >= MAX_PER_SECTOR:
            continue
        top.append(c)
        sector_count[sec] = sector_count.get(sec, 0) + 1
        if len(top) >= max_candidates:
            break

    # ── Step 5: Print report ──────────────────────────────────
    print(f"\n{'='*58}")
    print(f"  SCAN COMPLETE")
    print(f"  Scanned: {scanned} | Candidates: {len(candidates)} | Showing: {len(top)}")
    print(f"{'='*58}")

    for i, c in enumerate(top, 1):
        print(f"\n  {'─'*54}")
        print(f"  {i}. {c['ticker']:<18} [{c['conviction']}]  Score: {c['score']}/100")
        print(f"     {c['name']}")
        print(f"     Price: ₹{c['current_price']:,.2f}  |  "
              f"Vol: {c['vol_ratio']:.1f}× avg  |  "
              f"RSI: {c['rsi']:.0f}")
        print(f"     Stop:  ₹{c['stop_loss']:,.2f}  ({c['stop_pct']:.1f}% below)")
        print(f"     T1:    ₹{c['target1']:,.2f}  (+{SWING_TARGET_1*100:.0f}% — sell 50%)")
        print(f"     T2:    ₹{c['target2']:,.2f}  (+{SWING_TARGET_2*100:.0f}% — sell 50%)")
        print(f"     R/R:   {c['rr_ratio']:.2f}×  |  Max hold: {c['max_days']} days")
        print(f"     Signals (strength × weight = contribution):")
        for sig_name, sig in c["signals"].items():
            print(f"       {'✅' if sig['pass'] else '❌'} {sig_name:<12} "
                  f"{sig.get('strength', 0):.0f} × {sig.get('weight', 0):.2f} "
                  f"= {sig.get('contribution', 0):.1f}   {sig['note']}")

    # ── Step 6: Save and post ─────────────────────────────────
    if not test_mode:
        save_candidates(top, regime)
        send_telegram_alert(top)

    return top


# ─────────────────────────────────────────────
# SAVE + POST TO API
# ─────────────────────────────────────────────

def save_candidates(candidates: list, regime: dict = None):
    """Save candidates to disk and POST to API."""
    os.makedirs(DATA_DIR, exist_ok=True)

    output = {
        "generated_at":   datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"),
        "scan_date":      str(date.today()),
        "total_candidates": len(candidates),
        "market_regime":  regime or {},
        "candidates":     candidates,
    }

    with open(CANDIDATES_FILE, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n  ✅ Candidates saved: {CANDIDATES_FILE}")

    # POST to API
    upload_url = f"{API_URL}/swing/candidates/upload"
    print(f"  📤 POSTing {len(candidates)} candidates to: {upload_url}")
    try:
        payload = json.dumps(
            {"type": "swing_candidates", "payload": output},
            default=str
        ).encode("utf-8")
        req1 = _urllib.Request(
            upload_url,
            data=payload,
            headers={"Content-Type": "application/json", **_UPLOAD_AUTH},
            method="POST"
        )
        with _urllib.urlopen(req1, timeout=15) as r:
            print(f"  ✅ Candidates POSTed to API: {r.read().decode()}")
    except Exception as e:
        print(f"  ⚠️  Could not POST candidates to {upload_url} ({e}) — "
              f"dashboard will not see today's scan results")


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

            limit = c.get("limit_price")
            lines.append(
                f"{conv_emoji} *{i}. {c['ticker'].replace('.NS','')}* "
                f"[{c['conviction']}] Score: {c['score']:.0f}/100\n"
                f"  Price: ₹{c['current_price']:,.2f}"
                + (f" | Enter ≤ ₹{limit:,.2f} (skip if gaps above)" if limit else "") + "\n"
                f"  Stop:  ₹{c['stop_loss']:,.2f} ({c['stop_pct']:.1f}% below)\n"
                f"  T1: ₹{c['target1']:,.2f} (+{SWING_TARGET_1*100:.0f}%) | "
                f"T2: ₹{c['target2']:,.2f} (+{SWING_TARGET_2*100:.0f}%)\n"
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
        print(f"\n  {emoji} {c['ticker']:<20} Score:{c['score']}/100  "
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
