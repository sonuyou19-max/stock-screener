"""
Swing Scanner — US (S&P 500)
============================
Daily scan of the S&P 500 for swing trade candidates.
Runs after US market close (weekdays). MANUAL EXECUTION ONLY — this
scanner produces recommendations (dashboard + Telegram); the user places
orders and sets stop-loss/target orders on their broker themselves.
There is no order queue, entry cron, or GTT plumbing for US swings.

Goal: stocks with the potential to return 10–15% within ~2 weeks.
That goal drives the two deliberate differences from the India scanner:
  - Targets: T1 +10% (book 50%), T2 +15% (book rest) — blended +12.5%
  - Volatility gate: ATR-14 must be ≥ MIN_ATR_PCT of price. A stock whose
    normal daily range is 0.8% can't plausibly travel +12.5% in 10
    sessions — however clean its chart, the target is unreachable, so
    low-volatility names are excluded before scoring.

Hard gates (fail any one → skipped, regardless of score):
  - Stock above its OWN 50-DMA, 20-DMA rising (no bear-rally bounces)
  - Not >10% above 20-DMA (overextended setups mean-revert first)
  - Scan-day gain ≤7% (a news spike entered next morning = chasing)
  - Price ≥ $10, liquidity ADV ≥500k shares / $25M ADTV
  - ATR-14 ≥ MIN_ATR_PCT of price (target-reachability, see above)
  - Structural stop within 8% (wider blended reward than India permits a
    wider stop at the same 1.5 R/R; a stop beyond 8% means the setup
    isn't tight — skip the trade, never tighten the stop to force it)
  - Earnings blackout: reports within 14 calendar days → skipped
  - Market regime: S&P 500 below its 50-DMA → composite floor raised by
    BEARISH_SCORE_BUMP points and candidate list halved

Entry signals — continuous composite score 0–100 (same engine as the
India scanner; see swing_scanner.py for the full rationale):
  1. RSI(14)           — 45–75 momentum-zone plateau with direction tilt
  2. MACD(12,26,9)     — histogram magnitude (bps) + crossover recency
  3. Bollinger Bands   — %B position above the middle band
  4. Volume Surge      — surge-ratio strength, gated to UP-DAYS only
  5. Momentum Breakout — proximity to 52w high / 20d resistance break
  6. Market Trend      — S&P 500 distance above its 50-DMA (macro
                         tailwind — replaces India's FII/DII flow, which
                         has no US equivalent, at the same weight)
  7. Sector Sentiment  — weekly LLM synthesis + daily news signals via
                         the GICS→LLM-bucket map; negative → HARD
                         EXCLUDE; uncovered sectors → neutral (50)

  Weighted sum → composite score (0–100):
    momentum 0.20, volume 0.20, macd 0.15, sentiment 0.15,
    rsi 0.10, bollinger 0.10, market 0.10
  Candidate if composite ≥ MIN_COMPOSITE_SCORE (62, bullish regime;
  +BEARISH_SCORE_BUMP when S&P 500 < 50-DMA).

Entry discipline (manual):
  - optimal_entry = the technical anchor the signal fired from
    (broken resistance / 20-DMA / small pullback), capped at scan close
  - limit_price   = scan close +2%. If it opens above that, the R/R you
    scanned no longer exists — skip, don't chase.

Exit rules (manual — set these on your broker when the entry fills):
  - Stop-loss: wider of (buy − 2.0× ATR-14) and 0.5% below the 5-day low
  - Target 1:  +10% (book 50%)
  - Target 2:  +15% (book remaining 50%)
  - Time exit: close whatever remains after 10 trading days

Schedule (VPS cron, UTC): 30 21 * * 1-5
  (US close is 20:00 UTC in EDT / 21:00 UTC in EST; 21:30 UTC is safely
   after the close year-round = 3:00 AM IST)

Usage:
  python swing_scanner_us.py           # run scan
  python swing_scanner_us.py --status  # show today's candidates
  python swing_scanner_us.py --test    # scan without saving
  python swing_scanner_us.py --ticker NVDA  # scan single stock
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
import urllib.error as _urlerr
from datetime import datetime, date
from zoneinfo import ZoneInfo
from typing import Optional

from sp500_universe import fetch_sp500

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

ET              = ZoneInfo("America/New_York")
DATA_DIR        = os.getenv("DATA_DIR", "/data")
API_URL         = os.getenv("API_URL", "https://web-production-50eee.up.railway.app").rstrip("/")
CANDIDATES_FILE = os.path.join(DATA_DIR, "swing_candidates_us.json")

# Signal parameters — identical engine to swing_scanner.py
RSI_PERIOD       = 14
RSI_MIN          = 45
RSI_MAX          = 75
RSI_SLOPE_LOOKBACK = 3
RSI_SLOPE_DEADBAND = 1.0
RSI_SLOPE_FULL_DROP = 6.0
RSI_FALLING_FACTOR = 0.6
MACD_FAST        = 12
MACD_SLOW        = 26
MACD_SIGNAL      = 9
MACD_CROSS_DAYS  = 5
BB_PERIOD        = 20
BB_STD           = 2.0
VOL_SURGE_MULT   = 2.0
BREAKOUT_PCT     = 0.08

MAX_CANDIDATES   = 8       # manual execution — keep the list actionable

SIGNAL_WEIGHTS = {
    "momentum":  0.20,   # near 52w high / broke resistance
    "volume":    0.20,   # surge ON AN UP-DAY
    "macd":      0.15,   # trend momentum
    "sentiment": 0.15,   # LLM + news sector sentiment
    "rsi":       0.10,   # momentum zone
    "bollinger": 0.10,   # overlaps MACD/momentum info
    "market":    0.10,   # S&P 500 trend — macro tailwind (replaces FII)
}
assert abs(sum(SIGNAL_WEIGHTS.values()) - 1.0) < 1e-9

MIN_COMPOSITE_SCORE = 62.0
BEARISH_SCORE_BUMP  = 10.0

# ── Hard gates ──
TREND_DMA            = 50
EXT_MAX_ABOVE_20DMA  = 0.10
MAX_DAY_GAIN         = 0.07
MIN_PRICE            = 10.0    # USD
MAX_STOP_PCT         = 8.0     # blended reward is +12.5% (vs India's +9.5%),
                               # so at the same 1.5 R/R floor the structural
                               # stop may sit up to ~8% away
MAX_CHASE_PCT        = 0.02
# Target-reachability: ATR-14 as % of price. 1.8%/day of normal range is
# the rough floor at which a +12.5% blended move inside 10 sessions is a
# realistic trend leg rather than a lottery ticket.
MIN_ATR_PCT          = 1.8

# Swing ATR stop — same as India
SWING_ATR_MULT   = 2.0
SWING_ATR_PERIOD = 14
SWING_TRAIL_MULT = 1.0

# Profit targets — THE point of this scanner: 10–15% in ~2 weeks
SWING_TARGET_1   = 0.10   # +10% → book 50%
SWING_TARGET_2   = 0.15   # +15% → book remaining 50%
SWING_MAX_DAYS   = 10     # trading days ≈ 2 weeks

# Liquidity
SWING_MIN_ADV      = 500_000   # shares/day
SWING_MIN_ADTV_M   = 25.0      # $25M/day

MAX_PER_SECTOR   = 3

EARNINGS_BLACKOUT_DAYS = 14

# ── Sentiment: GICS → LLM bucket ─────────────────────────────
# Mirrors screener_us.py's SUBIND_TO_LLM_BUCKET / sector routing so the
# swing scanner consumes the same weekly LLM synthesis and daily news
# signals the monthly US screener does. Sub-industry match first, then
# sector. Sectors with no mapping have no sentiment coverage → neutral.
SUBIND_TO_LLM_BUCKET = {
    "Semiconductors":                          "SEMICONDUCTORS",
    "Semiconductor Materials & Equipment":     "SEMICONDUCTORS",
    "Semiconductor Equipment":                 "SEMICONDUCTORS",
    "Semiconductors & Semiconductor Equipment":"SEMICONDUCTORS",
    "Systems Software":                        "AI_CLOUD",
    "Internet Services & Infrastructure":      "AI_CLOUD",
    "IT Consulting & Other Services":          "AI_CLOUD",
    "Data Processing & Outsourced Services":   "AI_CLOUD",
    "Interactive Media & Services":            "AI_CLOUD",
}
SECTOR_TO_LLM_BUCKET = {
    "Information Technology":   "HIGH_GROWTH_TECH",
    "Communication Services":   "AI_CLOUD",
    "Health Care":              "DEFENSIVE_DIV",
    "Consumer Staples":         "DEFENSIVE_DIV",
    "Utilities":                "DEFENSIVE_DIV",
    "Financials":               "DEFENSIVE_DIV",
    # Energy / Industrials / Materials / Real Estate / Consumer
    # Discretionary: no LLM bucket → sentiment neutral (50)
}

LLM_CONF_SCALE   = {"High": 1.0, "Medium": 0.75, "Low": 0.5}
LLM_VERDICT_PTS  = {"Positive": 25.0, "Neutral": 0.0, "Cautious": -15.0}
NEWS_SIGNAL_PTS  = {"positive": 15.0, "mild_positive": 8.0, "neutral": 0.0,
                    "cautious": -10.0}
# "Negative" (LLM) / "negative" (news) → hard exclude, handled separately
LLM_MAX_AGE_DAYS  = 10   # weekly job
NEWS_MAX_AGE_DAYS = 5    # daily job


# ─────────────────────────────────────────────
# DATA FETCHER — yfinance only (no Zerodha for US)
# ─────────────────────────────────────────────

def fetch_ohlcv(ticker: str) -> Optional[pd.DataFrame]:
    """Fetch ~1 year of daily OHLCV for signal calculation."""
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
# SIGNAL CALCULATORS — same math as swing_scanner.py
# ─────────────────────────────────────────────

def _rsi_series(closes: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """RSI with Wilder's smoothing — matches TradingView chart values."""
    delta  = closes.diff().dropna()
    gain   = delta.clip(lower=0)
    loss   = (-delta).clip(lower=0)
    avg_g  = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_l  = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    rs     = avg_g / avg_l.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def calc_rsi_strength(rsi: float, slope: float = 0.0) -> float:
    """0-100 strength: 45-75 plateau with ramps, scaled down when the RSI
    is rolling over (falling while still in-zone)."""
    base = _trapezoid(rsi, RSI_MIN - 15, RSI_MIN, RSI_MAX, RSI_MAX + 15)
    if slope < -RSI_SLOPE_DEADBAND:
        drop   = min(1.0, (-slope - RSI_SLOPE_DEADBAND) /
                          (RSI_SLOPE_FULL_DROP - RSI_SLOPE_DEADBAND))
        base  *= 1.0 - drop * (1.0 - RSI_FALLING_FACTOR)
    return round(base, 1)


def calc_macd(closes: pd.Series) -> dict:
    """MACD(12,26,9) with continuous 0-100 strength."""
    ema12  = closes.ewm(span=MACD_FAST,  adjust=False).mean()
    ema26  = closes.ewm(span=MACD_SLOW,  adjust=False).mean()
    macd   = ema12 - ema26
    signal = macd.ewm(span=MACD_SIGNAL, adjust=False).mean()
    hist   = macd - signal

    crossed, cross_days_ago = False, None
    for i in range(1, min(MACD_CROSS_DAYS + 1, len(hist))):
        if hist.iloc[-i] > 0 and hist.iloc[-(i+1)] <= 0:
            crossed, cross_days_ago = True, i
            break

    hist_growing = float(hist.iloc[-1]) > float(hist.iloc[-2]) if len(hist) >= 2 else False
    macd_above   = float(macd.iloc[-1]) > float(signal.iloc[-1])

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
    """Bollinger Bands(20, 2σ) with %B-based continuous strength."""
    closes = hist["Close"]
    sma    = closes.rolling(BB_PERIOD).mean()
    std    = closes.rolling(BB_PERIOD).std()
    upper  = sma + BB_STD * std
    lower  = sma - BB_STD * std

    curr   = float(closes.iloc[-1])
    mid    = float(sma.iloc[-1])
    prev   = float(closes.iloc[-2])
    prev_m = float(sma.iloc[-2])

    crossed_mid = prev < prev_m and curr > mid
    near_lower  = float(closes.iloc[-2]) <= float(lower.iloc[-2]) * 1.01

    pct_b = round((curr - float(lower.iloc[-1])) /
                  (float(upper.iloc[-1]) - float(lower.iloc[-1]) + 1e-9) * 100, 1)

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
    """Volume surge: today > 2× 20-day average — on an UP-day only."""
    vol       = hist["Volume"]
    closes    = hist["Close"]
    opens     = hist["Open"]
    avg_20    = float(vol.iloc[-21:-1].mean())
    today_vol = float(vol.iloc[-1])
    ratio     = round(today_vol / avg_20, 2) if avg_20 > 0 else 1.0
    up_day    = (float(closes.iloc[-1]) > float(closes.iloc[-2])
                 and float(closes.iloc[-1]) > float(opens.iloc[-1]))
    surge     = ratio >= VOL_SURGE_MULT and up_day

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
    """Price near 52-week high OR breaking above 20-day resistance."""
    closes    = hist["Close"]
    highs     = hist["High"]
    high_52w  = float(highs.iloc[-252:].max()) if len(highs) >= 252 else float(highs.max())
    curr      = float(closes.iloc[-1])
    pct_from  = round((high_52w - curr) / high_52w * 100, 2)

    res_20d   = float(highs.iloc[-21:-1].max())
    broke_20d = curr > res_20d
    signal    = pct_from <= BREAKOUT_PCT * 100 or broke_20d

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
    """Hard disqualifiers, checked before any scoring. Returns a reason
    string if the stock fails, else None."""
    closes = hist["Close"]
    curr   = float(closes.iloc[-1])
    prev   = float(closes.iloc[-2])

    if curr < MIN_PRICE:
        return f"price ${curr:.2f} < ${MIN_PRICE:.0f} floor"

    if len(closes) >= TREND_DMA:
        dma50 = float(closes.rolling(TREND_DMA).mean().iloc[-1])
        if curr < dma50:
            return f"below own 50-DMA (${curr:,.1f} < ${dma50:,.1f}) — downtrend"
    dma20_series = closes.rolling(20).mean()
    if len(dma20_series.dropna()) >= 6:
        if float(dma20_series.iloc[-1]) <= float(dma20_series.iloc[-6]):
            return "20-DMA falling — no established short-term uptrend"

    dma20 = float(dma20_series.iloc[-1]) if not np.isnan(dma20_series.iloc[-1]) else None
    if dma20 and curr > dma20 * (1 + EXT_MAX_ABOVE_20DMA):
        return (f"{(curr/dma20-1)*100:.1f}% above 20-DMA "
                f"(max {EXT_MAX_ABOVE_20DMA*100:.0f}%) — overextended")

    day_gain = (curr - prev) / prev if prev > 0 else 0
    if day_gain > MAX_DAY_GAIN:
        return (f"scan-day spike +{day_gain*100:.1f}% "
                f"(max +{MAX_DAY_GAIN*100:.0f}%) — would be chasing")

    return None


def calc_atr(hist: pd.DataFrame, period: int = SWING_ATR_PERIOD) -> Optional[float]:
    """ATR — same math as swing_scanner.py."""
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


def fetch_market_regime() -> dict:
    """S&P 500 vs its 50-DMA. Also feeds the 'market' composite signal:
    strength 50 exactly at the DMA, 100 at ≥4% above, 0 at ≥4% below —
    the macro-tailwind role FII/DII flow plays in the India scanner."""
    try:
        hist = yf.Ticker("^GSPC").history(period="6mo")
        # yfinance can return a trailing row with NaN Close (partial/holiday
        # session); a NaN here poisons every stock's composite score — and
        # NaN < floor is False, so NaN-scored stocks then BYPASS the score
        # gate entirely. Drop NaNs and verify finiteness before using.
        closes = hist["Close"].dropna() if not hist.empty else hist
        if len(closes) < 50:
            raise ValueError("insufficient index history")
        close = float(closes.iloc[-1])
        dma50 = float(closes.rolling(50).mean().iloc[-1])
        if not (np.isfinite(close) and np.isfinite(dma50)) or dma50 <= 0:
            raise ValueError("index data contains NaN")
        pct_above = (close / dma50 - 1) * 100
        return {
            "spx_close":  round(close, 1),
            "dma_50":     round(dma50, 1),
            "bullish":    close > dma50,
            "pct_above_dma": round(pct_above, 2),
            "strength":   round(_ramp(pct_above, -4, 4), 1),
        }
    except Exception as e:
        return {"spx_close": None, "dma_50": None, "bullish": True,
                "pct_above_dma": 0.0, "strength": 50.0,
                "note": f"regime check failed ({e}) — defaulting to bullish/neutral"}


def earnings_within_blackout(ticker: str) -> Optional[str]:
    """Return the earnings date string if the stock reports within the
    blackout window, else None. A 2-week swing that holds through results
    is a coin flip, not a setup."""
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


# ─────────────────────────────────────────────
# SENTIMENT — weekly LLM synthesis + daily news signals
# ─────────────────────────────────────────────

def _signal_age_days(payload: dict) -> Optional[float]:
    """Age from a 'generated_at' like '2026-06-30 08:15 IST'."""
    try:
        gen = str(payload.get("generated_at", ""))[:10]
        return (datetime.now() - datetime.strptime(gen, "%Y-%m-%d")).days
    except Exception:
        return None


def fetch_us_sentiment() -> dict:
    """Load LLM synthesis + news signals from DATA_DIR (fallback: the API
    signal store). Stale or missing signals are dropped — the scan runs
    fine with sentiment neutral everywhere."""
    out = {"llm": None, "news": None, "notes": []}

    sources = {}
    for key, fname in [("us_llm_synthesis", "us_llm_synthesis.json"),
                       ("us_news_signals", "us_news_signals.json")]:
        try:
            with open(os.path.join(DATA_DIR, fname)) as f:
                sources[key] = json.load(f)
        except Exception:
            pass
    missing = [k for k in ("us_llm_synthesis", "us_news_signals") if k not in sources]
    if missing:
        try:
            req = _urllib.Request(f"{API_URL}/signals",
                                  headers={"Accept": "application/json"})
            with _urllib.urlopen(req, timeout=15) as r:
                api_signals = json.loads(r.read())
            for k in missing:
                if isinstance(api_signals.get(k), dict):
                    src = api_signals[k]
                    sources[k] = src.get("payload", src)
        except Exception as e:
            out["notes"].append(f"API signal fetch failed: {e}")

    llm = sources.get("us_llm_synthesis") or {}
    age = _signal_age_days(llm)
    if llm.get("verdict") and age is not None and age <= LLM_MAX_AGE_DAYS:
        out["llm"] = llm["verdict"]
        out["notes"].append(f"LLM synthesis: {age:.0f}d old ✓")
    elif llm:
        out["notes"].append(f"LLM synthesis stale/unreadable (age={age}) — ignored")

    news = sources.get("us_news_signals") or {}
    age = _signal_age_days(news)
    if news.get("signals") and age is not None and age <= NEWS_MAX_AGE_DAYS:
        out["news"] = news["signals"]
        out["notes"].append(f"News signals: {age:.0f}d old ✓")
    elif news:
        out["notes"].append(f"News signals stale/unreadable (age={age}) — ignored")
    return out


def sentiment_for_stock(sector: str, subindustry: str, sentiment: dict) -> dict:
    """Map a stock to its LLM bucket and produce a 0-100 sentiment
    strength for the composite. A Negative LLM verdict or a negative
    news signal on the stock's bucket → hard exclude (mirrors the India
    scanner's negative-sector rule). Uncovered sectors → neutral 50."""
    bucket = (SUBIND_TO_LLM_BUCKET.get(subindustry)
              or SECTOR_TO_LLM_BUCKET.get(sector))
    if not bucket:
        return {"bucket": None, "label": "neutral", "strength": 50.0,
                "exclude": False, "note": f"➖ {sector or 'unmapped'} — no sentiment coverage (neutral)"}

    strength, parts, exclude = 50.0, [], False

    verdicts = sentiment.get("llm") or {}
    bv = verdicts.get(bucket)
    label = "neutral"
    if isinstance(bv, dict) and bv.get("verdict"):
        v = bv["verdict"]
        label = v.lower()
        if v == "Negative":
            exclude = True
            parts.append(f"LLM {bucket}: Negative")
        elif v in LLM_VERDICT_PTS:
            pts = LLM_VERDICT_PTS[v] * LLM_CONF_SCALE.get(bv.get("confidence"), 0.75)
            strength += pts
            if pts:
                parts.append(f"LLM {bucket}: {v} ({pts:+.0f})")

    news = sentiment.get("news") or {}
    news_bucket = "DEFENSIVE_DIV" if bucket == "DEFENSIVE_DIV" else "TECH"
    ns = (news.get(news_bucket) or {}).get("signal")
    if ns == "negative":
        exclude = True
        parts.append(f"news {news_bucket}: negative")
    elif ns in NEWS_SIGNAL_PTS and NEWS_SIGNAL_PTS[ns]:
        strength += NEWS_SIGNAL_PTS[ns]
        parts.append(f"news {news_bucket}: {ns} ({NEWS_SIGNAL_PTS[ns]:+.0f})")

    strength = float(np.clip(strength, 0, 100))
    emoji = "🚫" if exclude else "✅" if strength > 60 else "⚠️" if strength < 40 else "➖"
    note = f"{emoji} {bucket} → " + ("; ".join(parts) if parts else "neutral")
    if exclude:
        note += " (HARD EXCLUDE)"
    return {"bucket": bucket, "label": label, "strength": round(strength, 1),
            "exclude": exclude, "note": note}


# ─────────────────────────────────────────────
# LIQUIDITY
# ─────────────────────────────────────────────

def passes_liquidity(hist: pd.DataFrame) -> tuple[bool, str]:
    vol    = hist["Volume"]
    closes = hist["Close"]
    adv    = float(vol.iloc[-30:].mean())
    curr   = float(closes.iloc[-1])
    adtv_m = round(adv * curr / 1e6, 2)  # in $M

    if adv < SWING_MIN_ADV:
        return False, f"ADV {adv/1e3:.0f}k < min {SWING_MIN_ADV/1e3:.0f}k shares/day"
    if adtv_m < SWING_MIN_ADTV_M:
        return False, f"ADTV ${adtv_m:.1f}M < min ${SWING_MIN_ADTV_M}M/day"
    return True, ""


# ─────────────────────────────────────────────
# STOP-LOSS + TARGETS
# ─────────────────────────────────────────────

def compute_swing_levels(hist: pd.DataFrame, buy_price: float) -> dict:
    """Stop = the WIDER (lower) of 2.0×ATR and 0.5% under the 5-day low —
    below actual structure. Targets +10%/+15%. R/R uses the blended exit
    (50% at T1 + 50% at T2 = +12.5% expected reward)."""
    atr = calc_atr(hist)
    if atr and atr > 0:
        atr_stop    = buy_price - SWING_ATR_MULT * atr
        struct_stop = float(hist["Low"].iloc[-5:].min()) * 0.995
        stop  = round(min(atr_stop, struct_stop), 2)
        trail = round(SWING_TRAIL_MULT * atr, 2)
        src   = "ATR+STRUCT"
    else:
        stop  = round(buy_price * 0.94, 2)
        trail = round(buy_price * 0.02, 2)
        atr   = None
        src   = "FALLBACK"

    stop_pct   = round((buy_price - stop) / buy_price * 100, 2)
    target1    = round(buy_price * (1 + SWING_TARGET_1), 2)
    target2    = round(buy_price * (1 + SWING_TARGET_2), 2)
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
        "rr_ratio":     rr_ratio,
        "max_days":     SWING_MAX_DAYS,
        "source":       src,
    }


# ─────────────────────────────────────────────
# SINGLE STOCK ANALYSER
# ─────────────────────────────────────────────

def analyse_stock(ticker: str, market: dict, sentiment: dict,
                  min_composite: float = MIN_COMPOSITE_SCORE,
                  sector: str = "", subindustry: str = "",
                  name: str = "") -> Optional[dict]:
    """Run all signals on a single stock. Returns candidate dict if the
    weighted composite ≥ min_composite, else None."""
    hist = fetch_ohlcv(ticker)
    if hist is None:
        return None

    liquid, _liq_reason = passes_liquidity(hist)
    if not liquid:
        return None

    gate_fail = check_hard_gates(hist)
    if gate_fail:
        return None

    closes = hist["Close"]
    curr   = float(closes.iloc[-1])

    # Target-reachability gate: normal daily range must support a +12.5%
    # blended move inside 10 sessions.
    atr = calc_atr(hist)
    atr_pct = round(atr / curr * 100, 2) if atr and curr else 0.0
    if atr_pct < MIN_ATR_PCT:
        return None

    # ── Technical signals ─────────────────────
    rsi_ser      = _rsi_series(closes).dropna()
    rsi          = round(float(rsi_ser.iloc[-1]), 2) if not rsi_ser.empty else 50.0
    rsi_prev     = (float(rsi_ser.iloc[-1 - RSI_SLOPE_LOOKBACK])
                    if len(rsi_ser) > RSI_SLOPE_LOOKBACK else rsi)
    rsi_slope    = round(rsi - rsi_prev, 2)
    rsi_strength = calc_rsi_strength(rsi, rsi_slope)
    rsi_dir      = ("rising"  if rsi_slope >  RSI_SLOPE_DEADBAND else
                    "falling" if rsi_slope < -RSI_SLOPE_DEADBAND else "flat")
    macd    = calc_macd(closes)
    bb      = calc_bollinger(hist)
    vol     = calc_volume_surge(hist)
    mom     = calc_momentum_breakout(hist)

    # ── Sentiment (bucket-level; may hard-exclude) ──
    sent = sentiment_for_stock(sector, subindustry, sentiment)
    if sent["exclude"]:
        print(f"  🚫 {ticker} EXCLUDED — negative sentiment ({sent['bucket']})")
        return None

    signals = {
        "rsi": {
            "pass":     RSI_MIN <= rsi <= RSI_MAX,
            "value":    rsi,
            "strength": rsi_strength,
            "weight":   SIGNAL_WEIGHTS["rsi"],
            "note":     (
                f"RSI {rsi:.1f} {rsi_dir} "
                + ("✅ momentum zone" if RSI_MIN <= rsi <= RSI_MAX
                   else f"❌ outside {RSI_MIN}-{RSI_MAX}")
                + (" ⚠️ rolling over — momentum fading" if rsi_dir == "falling"
                   and RSI_MIN <= rsi <= RSI_MAX else "")
            ),
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
        "market": {
            "pass":     market.get("bullish", True),
            "value":    market.get("pct_above_dma", 0.0),
            "strength": market.get("strength", 50.0),
            "weight":   SIGNAL_WEIGHTS["market"],
            "note":     (f"S&P 500 {market.get('pct_above_dma', 0):+.1f}% vs 50-DMA "
                         f"{'✅ tailwind' if market.get('bullish', True) else '❌ below trend'}"),
        },
        "sentiment": {
            "pass":     sent["strength"] > 60,
            "value":    sent["label"],
            "strength": sent["strength"],
            "weight":   SIGNAL_WEIGHTS["sentiment"],
            "note":     sent["note"],
        },
    }

    tech_score = sum(1 for s in signals.values() if s["pass"])

    # A NaN strength from any signal (bad index fetch, data glitch) makes
    # the composite NaN — and since NaN < floor is False, such stocks used
    # to sail PAST the score gate. Never score a stock on corrupt inputs.
    bad_sigs = [k for k, s in signals.items()
                if not np.isfinite(float(s.get("strength") or 0.0))]
    if bad_sigs:
        print(f"  ⚠️  {ticker}: non-finite strength in {', '.join(bad_sigs)} — skipped")
        return None

    score = sum(sig["strength"] * sig["weight"] for sig in signals.values())
    for sig in signals.values():
        sig["contribution"] = round(sig["strength"] * sig["weight"], 1)

    if not np.isfinite(score) or score < min_composite:
        return None

    # Earnings blackout — never hold a 2-week swing through results
    earnings_date = earnings_within_blackout(ticker)
    if earnings_date:
        print(f"  📅 {ticker} skipped — earnings {earnings_date} inside "
              f"{EARNINGS_BLACKOUT_DAYS}-day blackout window")
        return None

    levels = compute_swing_levels(hist, curr)

    if levels["stop_pct"] > MAX_STOP_PCT:
        return None
    if levels["rr_ratio"] < 1.5:
        return None

    conviction = "HIGH" if score >= 80 else "MEDIUM" if score >= 65 else "LOW"

    # ── Optimal entry — anchored to the level the signal fired from,
    # capped at scan close (same logic as the India scanner) ─────────
    bb_mid  = bb["middle"]
    r20d    = mom.get("res_20d", curr)
    vol_fired = signals["volume"]["pass"]
    mom_fired = signals["momentum"]["pass"]
    bb_fired  = signals["bollinger"]["pass"]

    if vol_fired and mom_fired:
        raw_entry = max(bb_mid, r20d * 1.001)
        entry_type  = "breakout-pullback"
        entry_basis = f"broken 20d resistance ${r20d:.2f}"
    elif bb_fired:
        raw_entry = bb_mid * 1.003
        entry_type  = "bb-cross"
        entry_basis = f"20-DMA ${bb_mid:.2f}"
    else:
        raw_entry = curr * 0.985
        entry_type  = "momentum-dip"
        entry_basis = "1.5% pullback from scan close"

    optimal_entry = round(min(raw_entry, curr), 2)

    if not name:
        try:
            name = yf.Ticker(ticker).info.get("longName", ticker)
        except Exception:
            name = ticker

    return {
        "ticker":        ticker,
        "name":          name,
        "sector":        sector or "Unknown",
        "subindustry":   subindustry,
        "scanned_at":    datetime.now(ET).strftime("%Y-%m-%d %H:%M ET"),
        "current_price": round(curr, 2),
        "score":         round(score, 1),
        "tech_score":    tech_score,
        "max_score":     100,
        "conviction":    conviction,
        "signals":       signals,
        "sentiment_val":    sent["label"],
        "sentiment_bucket": sent["bucket"] or "unmapped",
        "rsi":           rsi,
        "rsi_slope":     rsi_slope,
        "macd_hist":     macd["histogram"],
        "bb_pct_b":      bb["pct_b"],
        "bb_middle":     round(bb_mid, 2),
        "vol_ratio":     vol["ratio"],
        "pct_from_52w":  mom["pct_from_52w"],
        "res_20d":       round(r20d, 2),
        "atr_pct":       atr_pct,
        "optimal_entry": optimal_entry,
        "entry_type":    entry_type,
        "entry_basis":   entry_basis,
        "limit_price":   round(curr * (1 + MAX_CHASE_PCT), 2),
        "entry_note":    f"Optimal entry ${optimal_entry:.2f} ({entry_basis}). Hard limit ${curr * (1 + MAX_CHASE_PCT):.2f}.",
        "stop_loss":     levels["stop_loss"],
        "stop_pct":      levels["stop_pct"],
        "trailing_stop": levels["trailing"],
        "target1":       levels["target1"],
        "target2":       levels["target2"],
        "rr_ratio":      levels["rr_ratio"],
        "max_days":      SWING_MAX_DAYS,
        "atr":           levels["atr"],
        "atr_source":    levels["source"],
        "adv":           int(hist["Volume"].iloc[-30:].mean()),
        "adtv_usd_m":    round(float(hist["Volume"].iloc[-30:].mean()) * curr / 1e6, 2),
    }


# ─────────────────────────────────────────────
# MAIN SCANNER
# ─────────────────────────────────────────────

def run_scan(test_mode: bool = False, single_ticker: str = None) -> list:
    """Scan the S&P 500 for swing trade candidates."""
    # ── Step 0: Market regime ─────────────────
    regime = fetch_market_regime()
    if regime["bullish"]:
        min_composite  = MIN_COMPOSITE_SCORE
        max_candidates = MAX_CANDIDATES
        regime_label   = "🟢 BULLISH (S&P 500 above 50-DMA)"
    else:
        min_composite  = MIN_COMPOSITE_SCORE + BEARISH_SCORE_BUMP
        max_candidates = max(MAX_CANDIDATES // 2, 3)
        regime_label   = "🔴 BEARISH (S&P 500 below 50-DMA) — bar raised"

    print(f"\n{'='*58}")
    print(f"  📈 SWING SCANNER — US (S&P 500) · MANUAL EXECUTION")
    print(f"  {datetime.now(ET).strftime('%d %B %Y, %I:%M %p ET')}")
    print(f"  Regime: {regime_label}")
    if regime.get("spx_close"):
        print(f"  S&P 500: {regime['spx_close']:,.1f}  |  50-DMA: {regime['dma_50']:,.1f}")
    print(f"  Min composite score: {min_composite:.0f}/100  |  Max candidates: {max_candidates}")
    print(f"  Holding: ≤{SWING_MAX_DAYS} trading days  |  Targets: +{SWING_TARGET_1*100:.0f}% / +{SWING_TARGET_2*100:.0f}%")
    print(f"{'='*58}\n")

    # ── Step 1: Universe ──────────────────────
    meta = {}   # ticker → {sector, subindustry, name}
    if single_ticker:
        tickers = [single_ticker.upper()]
        print(f"  Single ticker mode: {tickers[0]}")
    else:
        print("  Fetching S&P 500 universe...")
        sp500_df = fetch_sp500()
        tickers  = []
        for _, row in sp500_df.iterrows():
            sym = str(row.get("Symbol", "")).strip()
            if not sym or sym == "Unknown":
                continue
            tickers.append(sym)
            meta[sym] = {
                "sector":      str(row.get("GICS Sector", "")),
                "subindustry": str(row.get("GICS Sub-Industry", "")),
                "name":        str(row.get("Security", sym)),
            }
        print(f"  Universe: {len(tickers)} stocks")

    # ── Step 2: Sentiment ─────────────────────
    print("  Fetching US sentiment signals...")
    sentiment = fetch_us_sentiment()
    for n in sentiment.get("notes", []):
        print(f"    {n}")
    if sentiment.get("llm"):
        for bkt, v in sentiment["llm"].items():
            if isinstance(v, dict):
                excl = " ← 🚫 HARD EXCLUDE for this bucket" if v.get("verdict") == "Negative" else ""
                print(f"    LLM {bkt}: {v.get('verdict')} ({v.get('confidence','?')}){excl}")

    # ── Step 3: Scan ──────────────────────────
    candidates, scanned, sig_fail = [], 0, 0
    print(f"\n  OHLCV source: yfinance")
    print(f"  Scanning {len(tickers)} stocks...\n")

    for ticker in tickers:
        try:
            m = meta.get(ticker, {})
            result = analyse_stock(ticker, regime, sentiment, min_composite,
                                   m.get("sector", ""), m.get("subindustry", ""),
                                   m.get("name", ""))
            scanned += 1

            if result is None:
                sig_fail += 1
            else:
                candidates.append(result)
                conv = result["conviction"]
                emoji = "🔥" if conv == "HIGH" else "⚡" if conv == "MEDIUM" else "✳️"
                print(f"  {emoji} {ticker:<8} Score:{result['score']:.1f}/100  "
                      f"RSI:{result['rsi']:.0f}  "
                      f"Vol:{result['vol_ratio']:.1f}×  "
                      f"ATR:{result['atr_pct']:.1f}%  "
                      f"{conv}")

            if scanned % 50 == 0:
                print(f"  ... {scanned}/{len(tickers)} scanned, "
                      f"{len(candidates)} candidates so far")

            time.sleep(0.25)

        except Exception as e:
            print(f"  ⚠️  {ticker}: {e}")
            continue

    # ── Step 4: Sort + sector cap ─────────────
    candidates.sort(key=lambda x: (x["score"], x["rr_ratio"]), reverse=True)
    top, sector_count = [], {}
    for c in candidates:
        sec = c.get("sector") or "Unknown"
        if sector_count.get(sec, 0) >= MAX_PER_SECTOR:
            continue
        top.append(c)
        sector_count[sec] = sector_count.get(sec, 0) + 1
        if len(top) >= max_candidates:
            break

    # ── Step 5: Report ────────────────────────
    print(f"\n{'='*58}")
    print(f"  SCAN COMPLETE")
    print(f"  Scanned: {scanned} | Candidates: {len(candidates)} | Showing: {len(top)}")
    print(f"{'='*58}")

    for i, c in enumerate(top, 1):
        print(f"\n  {'─'*54}")
        print(f"  {i}. {c['ticker']:<8} [{c['conviction']}]  Score: {c['score']}/100")
        print(f"     {c['name']}  ({c['sector']})")
        print(f"     Price: ${c['current_price']:,.2f}  |  "
              f"Vol: {c['vol_ratio']:.1f}× avg  |  "
              f"RSI: {c['rsi']:.0f}  |  ATR: {c['atr_pct']:.1f}%")
        print(f"     Entry: ${c['optimal_entry']:,.2f} ({c['entry_basis']})  "
              f"|  Max ${c['limit_price']:,.2f}")
        print(f"     Stop:  ${c['stop_loss']:,.2f}  ({c['stop_pct']:.1f}% below)")
        print(f"     T1:    ${c['target1']:,.2f}  (+{SWING_TARGET_1*100:.0f}% — sell 50%)")
        print(f"     T2:    ${c['target2']:,.2f}  (+{SWING_TARGET_2*100:.0f}% — sell 50%)")
        print(f"     R/R:   {c['rr_ratio']:.2f}×  |  Max hold: {c['max_days']} days")
        print(f"     Signals (strength × weight = contribution):")
        for sig_name, sig in c["signals"].items():
            print(f"       {'✅' if sig['pass'] else '❌'} {sig_name:<12} "
                  f"{sig.get('strength', 0):.0f} × {sig.get('weight', 0):.2f} "
                  f"= {sig.get('contribution', 0):.1f}   {sig['note']}")

    # ── Step 6: Save + alert ──────────────────
    if not test_mode:
        save_candidates(top, regime)
        send_telegram_alert(top)

    return top


# ─────────────────────────────────────────────
# SAVE + POST TO API
# ─────────────────────────────────────────────

def _scanner_tg(html_msg: str):
    """Minimal Telegram sender for scanner-level alerts (e.g. upload failed)."""
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        body = json.dumps({"chat_id": chat_id, "text": html_msg,
                           "parse_mode": "HTML"}).encode()
        req = _urllib.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=body, headers={"Content-Type": "application/json"}, method="POST")
        _urllib.urlopen(req, timeout=10)
    except Exception as e:
        print(f"  ⚠️  Scanner Telegram failed: {e}")


def save_candidates(candidates: list, regime: dict = None):
    """Save candidates to disk and POST to API."""
    os.makedirs(DATA_DIR, exist_ok=True)

    output = {
        "generated_at":   datetime.now(ET).strftime("%Y-%m-%d %H:%M ET"),
        "scan_date":      datetime.now(ET).strftime("%Y-%m-%d"),
        "total_candidates": len(candidates),
        "market_regime":  regime or {},
        "candidates":     candidates,
    }

    with open(CANDIDATES_FILE, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n  ✅ Candidates saved: {CANDIDATES_FILE}")

    upload_url = f"{API_URL}/swing/us/candidates/upload"
    print(f"  📤 POSTing {len(candidates)} candidates to: {upload_url}")
    if not _UPLOAD_AUTH:
        print("  ⚠️  UPLOAD_TOKEN not set — the API will reject this upload (401).")
    payload = json.dumps(
        {"type": "swing_candidates_us", "payload": output}, default=str
    ).encode("utf-8")
    last_err = None
    for attempt in range(3):
        try:
            req1 = _urllib.Request(
                upload_url, data=payload,
                headers={"Content-Type": "application/json", **_UPLOAD_AUTH},
                method="POST",
            )
            with _urllib.urlopen(req1, timeout=15) as r:
                print(f"  ✅ Candidates POSTed to API: {r.read().decode()}")
            return
        except _urlerr.HTTPError as e:
            try:
                body = e.read().decode()
            except Exception:
                body = ""
            last_err = f"HTTP {e.code} {body[:120]}"
            if e.code in (401, 403):
                break
        except Exception as e:
            last_err = str(e)
        time.sleep(2 ** attempt)

    print(f"  🚨 Could not POST candidates to {upload_url} ({last_err}) — "
          f"dashboard will NOT see today's scan results")
    _scanner_tg(
        f"🚨 <b>US swing scan upload FAILED</b>\n"
        f"Found {len(candidates)} candidates but could not send them to the "
        f"dashboard ({last_err}).\n"
        f"Fix: check UPLOAD_TOKEN / API_URL on the VPS."
    )


# ─────────────────────────────────────────────
# TELEGRAM ALERT
# ─────────────────────────────────────────────

def send_telegram_alert(candidates: list):
    """Send daily US swing scan summary to Telegram."""
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        print("  ⚠️  Telegram not configured.")
        return

    if not candidates:
        msg = (
            f"🇺🇸 *US Swing Scanner — {date.today().strftime('%d %b %Y')}*\n\n"
            f"No swing candidates found today.\n"
            f"Market conditions may not be favourable."
        )
    else:
        lines = [f"🇺🇸 *US Swing Candidates — {date.today().strftime('%d %b %Y')}*\n"]
        lines.append(f"Found *{len(candidates)}* candidates · _manual execution_\n")

        for i, c in enumerate(candidates[:5], 1):
            conv_emoji = "🔥" if c["conviction"] == "HIGH" else "⚡" if c["conviction"] == "MEDIUM" else "✳️"
            sigs_passed = [k for k, v in c["signals"].items() if v["pass"]]

            limit = c.get("limit_price")
            lines.append(
                f"{conv_emoji} *{i}. {c['ticker']}* "
                f"[{c['conviction']}] Score: {c['score']:.0f}/100\n"
                f"  Price: ${c['current_price']:,.2f}"
                + (f" | Enter ≤ ${limit:,.2f} (skip if gaps above)" if limit else "") + "\n"
                f"  Entry: ${c['optimal_entry']:,.2f} ({c['entry_type']})\n"
                f"  Stop:  ${c['stop_loss']:,.2f} ({c['stop_pct']:.1f}% below)\n"
                f"  T1: ${c['target1']:,.2f} (+{SWING_TARGET_1*100:.0f}%) | "
                f"T2: ${c['target2']:,.2f} (+{SWING_TARGET_2*100:.0f}%)\n"
                f"  R/R: {c['rr_ratio']:.2f}× | Signals: {', '.join(sigs_passed)}\n"
            )

        if len(candidates) > 5:
            lines.append(f"_...and {len(candidates)-5} more on dashboard_")

        lines.append(f"\n⏱ Hold up to {SWING_MAX_DAYS} trading days")
        lines.append("✍️ MANUAL: place the order, stop and targets on your broker yourself")
        msg = "\n".join(lines)

    if len(msg) > 4096:
        msg = msg[:4000] + "\n\n_(truncated)_"

    try:
        payload = json.dumps({
            "chat_id":    chat_id,
            "text":       msg,
            "parse_mode": "Markdown",
        }).encode("utf-8")
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        req = _urllib.Request(url, data=payload,
                              headers={"Content-Type": "application/json"},
                              method="POST")
        with _urllib.urlopen(req, timeout=15) as r:
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
        print("No US swing candidates found. Run: python swing_scanner_us.py")
        return

    with open(CANDIDATES_FILE) as f:
        data = json.load(f)

    print(f"\n{'='*58}")
    print(f"  US SWING CANDIDATES — {data.get('scan_date','')}")
    print(f"  Generated: {data.get('generated_at','')}")
    print(f"  Total: {data.get('total_candidates', 0)}")
    print(f"{'='*58}")

    for c in data.get("candidates", []):
        conv = c["conviction"]
        emoji = "🔥" if conv == "HIGH" else "⚡" if conv == "MEDIUM" else "✳️"
        print(f"\n  {emoji} {c['ticker']:<8} Score:{c['score']}/100  "
              f"RSI:{c['rsi']:.0f}  R/R:{c['rr_ratio']:.2f}×")
        print(f"     ${c['current_price']:,.2f}  →  "
              f"Stop:${c['stop_loss']:,.2f}  "
              f"T1:${c['target1']:,.2f}  "
              f"T2:${c['target2']:,.2f}")


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Swing Scanner — US S&P 500 (manual execution)")
    parser.add_argument("--status", action="store_true", help="Show latest candidates")
    parser.add_argument("--test",   action="store_true", help="Scan without saving")
    parser.add_argument("--ticker", type=str, default=None,
                        help="Scan a single ticker (e.g. NVDA)")
    args = parser.parse_args()

    if args.status:
        show_status()
    else:
        run_scan(test_mode=args.test, single_ticker=args.ticker)
