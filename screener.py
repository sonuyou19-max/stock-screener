"""
Indian Stock Screener — Monthly Top 7
======================================
Fetches live data from Yahoo Finance (yfinance) for all Nifty 500 stocks,
applies universal fundamental + quality filters, scores globally across
20 NSE sectors, integrates swing-news sentiment signals (hard-excludes
negative-sentiment sectors), and selects the top 7 stocks.

Sectors match the 20 NSE classifications used by the swing scanner.
Run monthly to refresh your portfolio picks.

Schedule: 0 3 3 * *  (3:00 AM UTC = 8:30 AM IST, 3rd of each month)
  Runs 2 days after monthly_earnings_sentiment.py (1st) so ticker
  signals are already written to /data before this script reads them.

Railway Cron setup (dashboard → New Service → Cron):
  Start command : python screener.py
  Schedule      : 0 3 3 * *
  Variables     : DATA_DIR, API_URL, ANTHROPIC_API_KEY (inherit from project)
"""

import os

# Sent with every API POST; uploads are rejected when the server has
# UPLOAD_TOKEN set and this env var is missing or wrong.
import os as _os_tok
_UPLOAD_AUTH = {"X-Upload-Token": _os_tok.environ["UPLOAD_TOKEN"]} if _os_tok.getenv("UPLOAD_TOKEN") else {}

import json
import time
import warnings
import urllib.request as _urllib
import urllib.error as _urlerr
from datetime import datetime, timedelta
from typing import Optional

import yfinance as yf
import pandas as pd
import numpy as np

from nse_universe import (
    fetch_nifty500,
    map_to_sectors,
    passes_fundamental_filters,
)

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

BUDGET         = 100_000   # Total corpus in INR
TOP_PICKS      = 7         # Global top-N to select
MAX_PER_SECTOR = 2         # Diversification cap — max picks from one NSE sector

ATR_MULT       = 2.5       # Stop-loss = buy_price - ATR_MULT * ATR
ATR_TRAIL_MULT = 1.5       # Trailing stop = ATR_TRAIL_MULT * ATR below peak
ATR_PERIOD     = 14

MIN_ADV        = 200_000   # Min 30-day avg daily volume (shares)
MIN_ADTV_CR    = 5.0       # Min avg daily traded value (₹ crore)

UNIFIED_SCORING_WEIGHTS = {
    "peg_score":            0.22,   # +2% — valuation matters more for 3M+ hold
    "roe_score":            0.22,   # +2% — capital efficiency compounds over time
    "revenue_growth_score": 0.28,   # +3% — structural growth is the primary driver
    "debt_score":           0.18,   # +3% — balance sheet stress amplifies on longer holds
    "momentum_score":       0.10,   # -10% — less noise from short-term price action
}

# Sector-level sentiment adjustment (applied AFTER global normalisation).
# "negative" sectors are hard-excluded via sector skip; -99 is a safety fallback.
SENTIMENT_SCORE_ADJ = {
    "positive":      5.0,
    "mild_positive": 2.5,
    "neutral":       0.0,
    "cautious":     -3.0,
    "negative":    -99.0,
}

# Ticker-level earnings quality adjustment (from monthly_earnings_sentiment.py).
# Applied per stock — not a hard-exclude, just a significant score delta.
TICKER_EARNINGS_ADJ = {
    "positive":      5.0,
    "mild_positive": 2.5,
    "neutral":       0.0,
    "cautious":     -5.0,
    "negative":    -15.0,
}

# Policy/macro adjustment (from policy_scraper.py).
# Softer than news sentiment — policy signals are slower-moving and less stock-specific.
# No hard-exclude: policy alone shouldn't block a high-quality stock.
POLICY_SCORE_ADJ = {
    "positive":      3.0,
    "mild_positive": 1.5,
    "neutral":       0.0,
    "cautious":     -1.5,
    "negative":     -3.0,
}

DATA_DIR = os.getenv("DATA_DIR", "/data")
API_URL  = os.getenv("API_URL", "https://web-production-50eee.up.railway.app")


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _load_prev_institutional() -> dict:
    """Load institutional_pct per ticker from the most recent portfolio JSON."""
    import glob

    patterns = [
        os.path.join(DATA_DIR, "portfolio_*.json"),
        "./outputs/portfolio_*.json",
        "/mnt/user-data/outputs/portfolio_*.json",
    ]

    files = []
    for pat in patterns:
        files.extend(glob.glob(pat))

    if not files:
        return {}

    latest = sorted(files)[-1]
    try:
        with open(latest) as f:
            portfolio = json.load(f)
        result = {}
        for bucket in portfolio.values():
            for stock in bucket.get("stocks", []):
                ticker = stock.get("ticker")
                pct    = stock.get("institutional_pct")
                if ticker and pct is not None:
                    result[ticker] = pct
        return result
    except Exception:
        return {}


warnings.filterwarnings("ignore")


# ─────────────────────────────────────────────
# ATR CALCULATOR
# ─────────────────────────────────────────────

def calculate_atr(ticker: str, period: int = ATR_PERIOD) -> Optional[float]:
    """
    Calculate Average True Range (ATR) for a stock over `period` trading days.

    True Range for each day = max of:
      1. High - Low
      2. |High - Previous Close|
      3. |Low  - Previous Close|

    ATR = Simple average of True Range over `period` days.

    Returns ATR in rupees, or None if data is insufficient.
    """
    try:
        stock = yf.Ticker(ticker)
        hist  = stock.history(period=f"{period + 10}d")

        if hist.empty or len(hist) < period + 1:
            return None

        hist = hist.tail(period + 1).copy()
        hist["prev_close"] = hist["Close"].shift(1)
        hist["tr1"] = hist["High"] - hist["Low"]
        hist["tr2"] = (hist["High"] - hist["prev_close"]).abs()
        hist["tr3"] = (hist["Low"]  - hist["prev_close"]).abs()
        hist["true_range"] = hist[["tr1", "tr2", "tr3"]].max(axis=1)
        atr = hist["true_range"].iloc[1:].mean()
        return round(float(atr), 2)

    except Exception:
        return None


def compute_atr_stops(ticker: str, buy_price: float) -> dict:
    """
    Return ATR-based stop-loss levels.
    Falls back to a fixed 12% stop if ATR fetch fails.
    """
    FALLBACK_PCT = 0.12

    atr = calculate_atr(ticker)

    if atr and atr > 0:
        stop_loss_price    = round(buy_price - (ATR_MULT * atr), 2)
        trailing_stop_dist = round(ATR_TRAIL_MULT * atr, 2)
        stop_loss_pct      = round((buy_price - stop_loss_price) / buy_price * 100, 2)
        source             = "ATR"
    else:
        stop_loss_price    = round(buy_price * (1 - FALLBACK_PCT), 2)
        trailing_stop_dist = round(buy_price * 0.05, 2)
        stop_loss_pct      = round(FALLBACK_PCT * 100, 2)
        atr                = None
        source             = "FALLBACK_FIXED_PCT"

    return {
        "atr_14day":          atr,
        "atr_multiplier":     ATR_MULT,
        "stop_loss_price":    stop_loss_price,
        "stop_loss_pct":      stop_loss_pct,
        "trailing_stop_dist": trailing_stop_dist,
        "atr_source":         source,
    }


# ─────────────────────────────────────────────
# DATA FETCHER
# ─────────────────────────────────────────────

def fetch_stock_data(ticker: str) -> Optional[dict]:
    """Fetch fundamentals + price data for a single NSE ticker.
    Returns None if stock fails liquidity filter.
    """
    try:
        stock = yf.Ticker(ticker)
        info  = stock.info

        if not info or info.get("regularMarketPrice") is None:
            return None

        hist = stock.history(period="6mo")
        if hist.empty or len(hist) < 20:
            return None

        import math as _math
        current_price = None

        # Source 1: fast_info
        try:
            fi = stock.fast_info
            fp = getattr(fi, 'last_price', None) or getattr(fi, 'regularMarketPrice', None)
            if fp and not _math.isnan(float(fp)) and float(fp) > 0:
                current_price = float(fp)
        except Exception:
            pass

        # Source 2: info dict
        if not current_price:
            rmp = info.get("regularMarketPrice") or info.get("currentPrice")
            if rmp and not _math.isnan(float(rmp)) and float(rmp) > 0:
                current_price = float(rmp)

        # Source 3: last history close
        if not current_price:
            close_val = hist["Close"].dropna().iloc[-1] if not hist["Close"].dropna().empty else None
            if close_val and not _math.isnan(float(close_val)) and float(close_val) > 0:
                current_price = float(close_val)

        if not current_price:
            return None

        # Price sanity check against 52-week range
        high_52w_check = info.get("fiftyTwoWeekHigh")
        low_52w_check  = info.get("fiftyTwoWeekLow")
        if high_52w_check and low_52w_check:
            price_upper = float(high_52w_check) * 1.10
            price_lower = float(low_52w_check)  * 0.90
            if current_price > price_upper or current_price < price_lower:
                hist_close = hist["Close"].dropna().iloc[-1] if not hist["Close"].dropna().empty else None
                if hist_close and price_lower <= float(hist_close) <= price_upper:
                    print(f"  ⚠️  {ticker}: price ₹{current_price:.2f} outside 52w range "
                          f"— using hist close ₹{hist_close:.2f}")
                    current_price = float(hist_close)
                else:
                    print(f"  ❌ {ticker}: price ₹{current_price:.2f} outside 52w range — skipping")
                    return None

        # Liquidity filter
        adv_30d = hist["Volume"].iloc[-30:].mean() if len(hist) >= 30 else hist["Volume"].mean()
        adtv_cr = round((adv_30d * current_price) / 1e7, 2)

        if adv_30d < MIN_ADV:
            print(f"    ⛔ {ticker} excluded — ADV {adv_30d:,.0f} < min {MIN_ADV:,.0f} shares/day")
            return None

        if adtv_cr < MIN_ADTV_CR:
            print(f"    ⛔ {ticker} excluded — ADTV ₹{adtv_cr:.1f}Cr < min ₹{MIN_ADTV_CR}Cr/day")
            return None

        price_1m_ago = hist["Close"].iloc[-22] if len(hist) >= 22 else hist["Close"].iloc[0]
        price_3m_ago = hist["Close"].iloc[-66] if len(hist) >= 66 else hist["Close"].iloc[0]
        price_6m_ago = hist["Close"].iloc[0]

        momentum_1m = (current_price / price_1m_ago - 1) * 100
        momentum_3m = (current_price / price_3m_ago - 1) * 100
        momentum_6m = (current_price / price_6m_ago - 1) * 100

        vol_10d      = hist["Volume"].iloc[-10:].mean()
        vol_30d      = hist["Volume"].iloc[-30:].mean()
        volume_ratio = vol_10d / vol_30d if vol_30d > 0 else 1.0

        high_52w = info.get("fiftyTwoWeekHigh", current_price)
        low_52w  = info.get("fiftyTwoWeekLow", current_price)
        price_position = (
            (current_price - low_52w) / (high_52w - low_52w)
            if high_52w != low_52w else 0.5
        )

        pe         = info.get("trailingPE")
        earn_g_raw = info.get("earningsGrowth")
        roe_raw    = info.get("returnOnEquity")
        rev_g_raw  = info.get("revenueGrowth")

        roe_pct    = round(roe_raw    * 100, 2) if roe_raw    is not None else None
        earn_g_pct = round(earn_g_raw * 100, 2) if earn_g_raw is not None else None
        rev_g_pct  = round(rev_g_raw  * 100, 2) if rev_g_raw  is not None else None

        if pe and pe > 0 and earn_g_pct and earn_g_pct > 0:
            peg_ratio = round(pe / earn_g_pct, 2)
            peg_ratio = min(peg_ratio, 10.0)
        else:
            peg_ratio = None

        return {
            "ticker":               ticker,
            "name":                 info.get("longName", ticker),
            "sector":               info.get("sector", "Unknown"),
            "industry":             info.get("industry", "Unknown"),
            "current_price":        round(current_price, 2),
            "market_cap_cr":        round(info.get("marketCap", 0) / 1e7, 0),
            "pe_ratio":             pe,
            "forward_pe":           info.get("forwardPE"),
            "pb_ratio":             info.get("priceToBook"),
            "peg_ratio":            peg_ratio,
            "roe_pct":              roe_pct,
            "earnings_growth_pct":  earn_g_pct,
            "revenue_growth_pct":   rev_g_pct,
            "roe":                  roe_raw,
            "revenue_growth":       rev_g_raw,
            "earnings_growth":      earn_g_raw,
            # yfinance reports debtToEquity as a percentage (41.2 = 0.41x).
            # Convert to a ratio so filters, scoring, audit and display all use one unit.
            "debt_to_equity":       round(info.get("debtToEquity") / 100.0, 3)
                                    if info.get("debtToEquity") is not None else None,
            "current_ratio":        info.get("currentRatio"),
            "profit_margin":        info.get("profitMargins"),
            "gross_margin":         info.get("grossMargins"),
            "dividend_yield":       info.get("dividendYield"),
            "beta":                 info.get("beta"),
            "momentum_1m":          round(momentum_1m, 2),
            "momentum_3m":          round(momentum_3m, 2),
            "momentum_6m":          round(momentum_6m, 2),
            "volume_ratio":         round(volume_ratio, 2),
            "price_position_52w":   round(price_position, 2),
            "high_52w":             round(high_52w, 2),
            "low_52w":              round(low_52w, 2),
            "adv_30d":              round(adv_30d, 0),
            "adtv_cr":              adtv_cr,
            "insider_pct":          round(info.get("heldPercentInsiders", 0) * 100, 2)
                                    if info.get("heldPercentInsiders") is not None else None,
            "institutional_pct":    round(info.get("heldPercentInstitutions", 0) * 100, 2)
                                    if info.get("heldPercentInstitutions") is not None else None,
        }

    except Exception:
        return None


# ─────────────────────────────────────────────
# SCORING ENGINE
# ─────────────────────────────────────────────

def score_stock(row: dict) -> dict:
    """
    Score a stock across 5 dimensions.
    Raw values returned here; normalisation happens after
    all stocks are fetched (global normalisation across all sectors).
    """
    scores = {}

    peg = row.get("peg_ratio")
    pe  = row.get("pe_ratio")

    if peg and peg > 0:
        scores["peg_raw"] = peg
        scores["pe_raw"]  = pe
    elif pe and pe > 0:
        scores["peg_raw"] = round(pe / 10, 2)
        scores["pe_raw"]  = pe
    else:
        scores["peg_raw"] = None
        scores["pe_raw"]  = None

    roe = row.get("roe")
    scores["roe_raw"] = (roe * 100) if roe else None

    rg = row.get("revenue_growth")
    scores["revenue_growth_raw"] = (rg * 100) if rg is not None else None

    de = row.get("debt_to_equity")
    scores["debt_raw"] = de if de is not None else None

    m1 = row.get("momentum_1m", 0) or 0
    m3 = row.get("momentum_3m", 0) or 0
    m6 = row.get("momentum_6m", 0) or 0
    vr = row.get("volume_ratio", 1.0) or 1.0
    # 3M+ hold: shift weight from short-term (1M) to medium-term (3M+6M) price trend
    scores["momentum_raw"] = (0.1 * m1) + (0.4 * m3) + (0.4 * m6) + (0.1 * (vr - 1) * 10)

    return scores


def normalise_and_compute_final(df: pd.DataFrame, weights: dict) -> pd.DataFrame:
    """Normalise raw scores to 0-100 globally and compute weighted final score."""

    def minmax(series, invert=False):
        clean = series.dropna()
        if clean.empty or clean.max() == clean.min():
            return pd.Series([50.0] * len(series), index=series.index)
        # Percentile rank instead of min-max: one extreme outlier no longer
        # compresses every other stock's score toward 0 on that dimension.
        normed = series.rank(pct=True) * 100
        if invert:
            normed = 100 - normed
        return normed.fillna(50)

    df["peg_score"]            = minmax(df["peg_raw"], invert=True)
    df["roe_score"]            = minmax(df["roe_raw"])
    df["revenue_growth_score"] = minmax(df["revenue_growth_raw"])
    df["debt_score"]           = minmax(df["debt_raw"], invert=True)
    df["momentum_score"]       = minmax(df["momentum_raw"])

    df["final_score"] = (
        df["peg_score"]            * weights["peg_score"] +
        df["roe_score"]            * weights["roe_score"] +
        df["revenue_growth_score"] * weights["revenue_growth_score"] +
        df["debt_score"]           * weights["debt_score"] +
        df["momentum_score"]       * weights["momentum_score"]
    )

    return df.sort_values("final_score", ascending=False)


# ─────────────────────────────────────────────
# EARNINGS FRESHNESS CHECKER (2.2)
# ─────────────────────────────────────────────

STALE_DATA_DAYS    = 120
DETERIORATION_PCT  = 20
MISS_THRESHOLD_PCT = 10

def check_earnings_freshness(ticker: str) -> dict:
    """
    Check quarterly earnings quality for a stock.

    Returns dict with:
      last_reported_date  : date of most recent quarterly result
      data_age_days       : how many days since last result
      earnings_trend      : improving | stable | deteriorating | double_deterioration
      earnings_miss       : True if latest quarter missed estimates >10%
      freshness_penalty   : score points to deduct (0, 5, 10, or 15)
      exclude             : True if double deterioration detected
      notes               : human-readable summary
    """
    result = {
        "last_reported_date":  None,
        "data_age_days":       None,
        "earnings_trend":      "unknown",
        "earnings_miss":       False,
        "freshness_penalty":   0,
        "exclude":             False,
        "notes":               "",
    }

    try:
        stock = yf.Ticker(ticker)

        quarterly = stock.quarterly_financials
        if quarterly is None or quarterly.empty:
            result["notes"] = "No quarterly financials available"
            result["freshness_penalty"] = 5
            return result

        last_date = quarterly.columns[0]
        if hasattr(last_date, "date"):
            last_date = last_date.date()
        age_days = (datetime.now().date() - last_date).days

        result["last_reported_date"] = str(last_date)
        result["data_age_days"]      = age_days

        if age_days > STALE_DATA_DAYS:
            result["notes"]            += f"Stale data ({age_days} days old). "
            result["freshness_penalty"] += 5

        # Net income QoQ trend from quarterly_financials (already fetched above).
        # The old quarterly_earnings "Actual"/"Estimate" check was dead code —
        # that API was removed from yfinance and never carried those columns.
        ni_series = None
        for label in ("Net Income", "Net Income Common Stockholders"):
            if label in quarterly.index:
                ni_series = quarterly.loc[label].dropna()
                break

        if ni_series is not None and len(ni_series) >= 2:
            latest_ni = float(ni_series.iloc[0])
            prior_ni  = float(ni_series.iloc[1])

            if prior_ni != 0:
                qoq_change = (latest_ni - prior_ni) / abs(prior_ni) * 100
            else:
                qoq_change = 0

            if qoq_change < -DETERIORATION_PCT:
                result["notes"] += f"Net income down {abs(qoq_change):.1f}% QoQ. "

                if len(ni_series) >= 3:
                    prior2_ni = float(ni_series.iloc[2])
                    if prior2_ni != 0:
                        prev_change = (prior_ni - prior2_ni) / abs(prior2_ni) * 100
                        if prev_change < -DETERIORATION_PCT:
                            result["earnings_trend"]    = "double_deterioration"
                            result["exclude"]           = True
                            result["freshness_penalty"] += 15
                            result["notes"]             += "⛔ Double deterioration — excluding. "
                            return result

                result["earnings_trend"]    = "deteriorating"
                result["freshness_penalty"] += 10

            elif qoq_change > 5:
                result["earnings_trend"] = "improving"
                result["notes"]         += f"Net income up {qoq_change:.1f}% QoQ ✅. "
            else:
                result["earnings_trend"] = "stable"
                result["notes"]         += f"Net income stable ({qoq_change:+.1f}% QoQ). "

            if latest_ni < 0:
                result["freshness_penalty"] += 5
                result["notes"]             += "⚠️ Latest quarter loss-making. "

        result["freshness_penalty"] = min(result["freshness_penalty"], 20)

        if not result["notes"]:
            result["notes"] = "Earnings data looks healthy ✅"

    except Exception as e:
        result["notes"]            = f"Could not fetch earnings data: {e}"
        result["freshness_penalty"] = 3

    return result


# ─────────────────────────────────────────────
# MARGIN HEALTH CHECKER (2.3)
# ─────────────────────────────────────────────

DIVERGENCE_WARN   = 15
DIVERGENCE_SEVERE = 30
MARGIN_COMPRESS   = 2.0
MARGIN_SEVERE     = 5.0

def check_margin_health(data: dict) -> dict:
    """
    Compare revenue growth vs profit growth to detect margin compression.
    Uses data already fetched by fetch_stock_data() — no extra API call needed.
    """
    result = {
        "divergence":     None,
        "margin_trend":   "unknown",
        "margin_penalty": 0,
        "margin_bonus":   0,
        "net_adjustment": 0,
        "margin_notes":   "",
    }

    rev_g    = data.get("revenue_growth_pct")
    profit_g = data.get("earnings_growth_pct")
    gross_m  = data.get("gross_margin")
    profit_m = data.get("profit_margin")

    if rev_g is not None and profit_g is not None:
        divergence = rev_g - profit_g
        result["divergence"] = round(divergence, 1)

        if divergence > DIVERGENCE_SEVERE:
            result["margin_trend"]   = "severe_compression"
            result["margin_penalty"] = 10
            result["margin_notes"]  += (
                f"⛔ Severe margin compression: revenue +{rev_g:.1f}% "
                f"but profit only +{profit_g:.1f}% (gap: {divergence:.1f}%). "
            )
        elif divergence > DIVERGENCE_WARN:
            result["margin_trend"]   = "compressing"
            result["margin_penalty"] = 5
            result["margin_notes"]  += (
                f"⚠️  Margin compression: revenue +{rev_g:.1f}% "
                f"but profit +{profit_g:.1f}% (gap: {divergence:.1f}%). "
            )
        elif divergence < -5:
            result["margin_trend"]  = "expanding"
            result["margin_bonus"]  = 5
            result["margin_notes"] += (
                f"✅ Margin expansion: profit +{profit_g:.1f}% "
                f"outpacing revenue +{rev_g:.1f}%. "
            )
        else:
            result["margin_trend"]  = "stable"
            result["margin_notes"] += (
                f"Revenue +{rev_g:.1f}% | Profit +{profit_g:.1f}% — stable margins. "
            )
    elif rev_g is not None and profit_g is None:
        result["margin_notes"] += "Profit growth data unavailable — revenue only. "

    if gross_m is not None:
        gross_m_pct = round(gross_m * 100, 1)
        if gross_m_pct < 10:
            result["margin_penalty"] += 5
            result["margin_notes"]   += f"⚠️  Low gross margin ({gross_m_pct}%) — thin pricing power. "
        elif gross_m_pct > 40:
            result["margin_bonus"]   += 3
            result["margin_notes"]   += f"✅ Strong gross margin ({gross_m_pct}%). "

    if profit_m is not None:
        profit_m_pct = round(profit_m * 100, 1)
        if profit_m_pct < 5:
            result["margin_penalty"] += 3
            result["margin_notes"]   += f"⚠️  Thin net margin ({profit_m_pct}%). "
        elif profit_m_pct > 20:
            result["margin_bonus"]   += 2
            result["margin_notes"]   += f"✅ Strong net margin ({profit_m_pct}%). "

    result["margin_penalty"]  = min(result["margin_penalty"], 15)
    result["margin_bonus"]    = min(result["margin_bonus"], 8)
    result["net_adjustment"]  = result["margin_bonus"] - result["margin_penalty"]

    if not result["margin_notes"]:
        result["margin_notes"] = "Insufficient margin data."

    return result


# ─────────────────────────────────────────────
# PROMOTER ACTIVITY CHECKER (3.1)
# ─────────────────────────────────────────────

PROMOTER_HIGH      = 50.0
PROMOTER_NORMAL    = 35.0
PROMOTER_LOW       = 20.0
INSTITUTION_HIGH   = 40.0
INSTITUTION_NORMAL = 15.0

def check_promoter_signal(data: dict) -> dict:
    """
    Evaluate promoter and institutional holding levels.
    Uses data already fetched by fetch_stock_data() — zero extra API calls.
    """
    result = {
        "insider_pct":       data.get("insider_pct"),
        "institutional_pct": data.get("institutional_pct"),
        "promoter_signal":   "unknown",
        "institution_signal":"unknown",
        "promoter_bonus":    0,
        "promoter_penalty":  0,
        "net_promoter_adj":  0,
        "promoter_notes":    "",
    }

    insider = data.get("insider_pct")
    instit  = data.get("institutional_pct")

    industry_str = (data.get("industry") or "").lower()
    is_bank_or_insurance = any(k in industry_str for k in ("bank", "insurance", "life insurance"))

    if insider is not None:
        if is_bank_or_insurance and insider < PROMOTER_LOW:
            result["promoter_signal"] = "normal"
            result["promoter_notes"] += (
                f"Professionally managed institution — 0% promoter normal "
                f"({insider:.1f}%). "
            )
        elif insider >= PROMOTER_HIGH:
            result["promoter_signal"] = "strong"
            result["promoter_bonus"]  = 5
            result["promoter_notes"] += f"✅ Strong promoter holding ({insider:.1f}%). "
        elif insider >= PROMOTER_NORMAL:
            result["promoter_signal"] = "normal"
            result["promoter_notes"] += f"Promoter holding normal ({insider:.1f}%). "
        elif insider >= PROMOTER_LOW:
            result["promoter_signal"] = "low"
            result["promoter_penalty"] = 3
            result["promoter_notes"] += f"⚠️  Low promoter holding ({insider:.1f}%). "
        else:
            result["promoter_signal"] = "very_low"
            result["promoter_penalty"] = 7
            result["promoter_notes"] += f"⛔ Very low promoter holding ({insider:.1f}%). "
    else:
        result["promoter_notes"] += "Promoter data unavailable. "

    if instit is not None:
        if instit >= INSTITUTION_HIGH:
            result["institution_signal"] = "high"
            result["promoter_bonus"]    += 3
            result["promoter_notes"]    += f"✅ High institutional confidence ({instit:.1f}%). "
        elif instit >= INSTITUTION_NORMAL:
            result["institution_signal"] = "normal"
            result["promoter_notes"]    += f"Institutional holding normal ({instit:.1f}%). "
        else:
            result["institution_signal"] = "low"
            result["promoter_penalty"]  += 3
            result["promoter_notes"]    += f"⚠️  Low institutional interest ({instit:.1f}%). "
    else:
        result["promoter_notes"] += "Institutional data unavailable. "

    if (insider is not None and insider >= PROMOTER_HIGH and
            instit is not None and instit >= INSTITUTION_HIGH):
        result["promoter_bonus"] += 2
        result["promoter_notes"] += "🏆 Double conviction — promoter + institutions both high. "

    if (insider is not None and insider < PROMOTER_LOW and
            instit is not None and instit < INSTITUTION_NORMAL):
        result["promoter_penalty"] += 3
        result["promoter_notes"]   += "⛔ Both promoter and institutional holding very low. "

    result["promoter_bonus"]   = min(result["promoter_bonus"], 10)
    result["promoter_penalty"] = min(result["promoter_penalty"], 10)
    result["net_promoter_adj"] = result["promoter_bonus"] - result["promoter_penalty"]

    if not result["promoter_notes"].strip():
        result["promoter_notes"] = "No holding data available."

    return result


# ─────────────────────────────────────────────
# INSTITUTIONAL TREND CHECKER (3.2)
# ─────────────────────────────────────────────

INST_ACCUMULATING =  2.0
INST_DISTRIBUTING = -2.0
INST_EXITING_FAST = -5.0

def check_institutional_trend(
    ticker: str,
    current_inst_pct: Optional[float],
    prev_inst_pct: Optional[float] = None,
) -> dict:
    """
    Detect whether institutions are accumulating or distributing.

    Tier 1 — QoQ comparison if prev_inst_pct available
    Tier 2 — Holder-level signals from yfinance institutional_holders
    """
    result = {
        "inst_change_pp":     None,
        "inst_trend":         "unknown",
        "inst_trend_bonus":   0,
        "inst_trend_penalty": 0,
        "net_inst_adj":       0,
        "inst_trend_notes":   "",
        "holder_count":       None,
    }

    if current_inst_pct is not None and prev_inst_pct is not None:
        change_pp = round(current_inst_pct - prev_inst_pct, 2)
        result["inst_change_pp"] = change_pp

        if change_pp >= INST_ACCUMULATING:
            result["inst_trend"]       = "accumulating"
            result["inst_trend_bonus"] = 5
            result["inst_trend_notes"] += (
                f"✅ Institutions accumulating: +{change_pp:.1f}pp QoQ "
                f"({prev_inst_pct:.1f}% → {current_inst_pct:.1f}%). "
            )
        elif change_pp <= INST_EXITING_FAST:
            result["inst_trend"]        = "exiting_fast"
            result["inst_trend_penalty"]= 10
            result["inst_trend_notes"] += (
                f"⛔ Fast institutional exit: {change_pp:.1f}pp QoQ "
                f"({prev_inst_pct:.1f}% → {current_inst_pct:.1f}%). "
            )
        elif change_pp <= INST_DISTRIBUTING:
            result["inst_trend"]        = "distributing"
            result["inst_trend_penalty"]= 5
            result["inst_trend_notes"] += (
                f"⚠️  Institutions distributing: {change_pp:.1f}pp QoQ "
                f"({prev_inst_pct:.1f}% → {current_inst_pct:.1f}%). "
            )
        else:
            result["inst_trend"]       = "stable"
            result["inst_trend_notes"] += (
                f"Institutional holding stable: {change_pp:+.1f}pp QoQ. "
            )

    try:
        stock   = yf.Ticker(ticker)
        holders = stock.institutional_holders

        if holders is not None and not holders.empty:
            holder_count = len(holders)
            result["holder_count"] = holder_count

            if prev_inst_pct is None:
                if holder_count >= 15:
                    result["inst_trend"]       = "well_covered"
                    result["inst_trend_bonus"] = 3
                    result["inst_trend_notes"] += (
                        f"✅ {holder_count} institutional holders "
                        f"(well covered — no prior data for QoQ). "
                    )
                elif holder_count >= 5:
                    result["inst_trend"]       = "moderate_coverage"
                    result["inst_trend_notes"] += (
                        f"{holder_count} institutional holders "
                        f"(moderate — no prior data for QoQ). "
                    )
                else:
                    result["inst_trend"]        = "low_coverage"
                    result["inst_trend_penalty"]= 3
                    result["inst_trend_notes"] += (
                        f"⚠️  Only {holder_count} institutional holders "
                        f"(low coverage). "
                    )
        else:
            if prev_inst_pct is None:
                result["inst_trend_notes"] += "No institutional holder data available. "

    except Exception:
        if prev_inst_pct is None:
            result["inst_trend_notes"] += "Could not fetch institutional holder data. "

    result["inst_trend_bonus"]   = min(result["inst_trend_bonus"], 8)
    result["inst_trend_penalty"] = min(result["inst_trend_penalty"], 10)
    result["net_inst_adj"]       = result["inst_trend_bonus"] - result["inst_trend_penalty"]

    if not result["inst_trend_notes"].strip():
        result["inst_trend_notes"] = "Institutional trend data unavailable."

    return result


# ─────────────────────────────────────────────
# CIRCUIT BREAKER RISK CHECKER (3.3)
# ─────────────────────────────────────────────

BETA_HIGH_RISK     = 2.0
BETA_EXTREME_RISK  = 2.5
ADTV_STRESS_CR     = 8.0
DRAWDOWN_THRESHOLD = 0.40

def check_circuit_risk(data: dict) -> dict:
    """
    Assess circuit breaker risk using already-fetched data.
    Zero extra API calls.
    """
    result = {
        "circuit_risk":    "low",
        "circuit_penalty": 0,
        "circuit_exclude": False,
        "circuit_notes":   "",
    }

    beta          = data.get("beta")
    adtv_cr       = data.get("adtv_cr", 0) or 0
    price_pos     = data.get("price_position_52w")
    current_price = data.get("current_price", 0)
    high_52w      = data.get("high_52w", current_price)

    if beta is not None:
        if beta > BETA_EXTREME_RISK:
            result["circuit_penalty"] += 8
            result["circuit_risk"]     = "extreme"
            result["circuit_notes"]   += f"⛔ Extreme beta ({beta:.1f}) — high circuit risk. "
        elif beta > BETA_HIGH_RISK:
            result["circuit_penalty"] += 5
            result["circuit_risk"]     = "elevated"
            result["circuit_notes"]   += f"⚠️  High beta ({beta:.1f}) — elevated volatility. "
        elif beta > 1.5:
            result["circuit_penalty"] += 2
            result["circuit_notes"]   += f"Beta {beta:.1f} — moderate volatility. "
        else:
            result["circuit_notes"]   += f"Beta {beta:.1f} — acceptable volatility. "
    else:
        result["circuit_notes"] += "Beta data unavailable. "

    if price_pos is not None and high_52w and current_price:
        drawdown = 1 - (current_price / high_52w)
        if drawdown > DRAWDOWN_THRESHOLD:
            result["circuit_penalty"] += 5
            if result["circuit_risk"] == "low":
                result["circuit_risk"] = "moderate"
            result["circuit_notes"] += (
                f"⚠️  Stock is {drawdown*100:.0f}% below 52w high "
                f"(₹{current_price:.0f} vs ₹{high_52w:.0f}) — deep drawdown. "
            )
        elif drawdown > 0.25:
            result["circuit_penalty"] += 2
            result["circuit_notes"]   += f"Stock {drawdown*100:.0f}% below 52w high. "
        else:
            result["circuit_notes"] += f"Price healthy — {drawdown*100:.0f}% below 52w high. "

    if 0 < adtv_cr < ADTV_STRESS_CR:
        result["circuit_penalty"] += 3
        if result["circuit_risk"] == "low":
            result["circuit_risk"] = "moderate"
        result["circuit_notes"] += (
            f"⚠️  ADTV ₹{adtv_cr:.1f}Cr — stress zone "
            f"(above minimum but thin). "
        )

    if beta and beta > BETA_EXTREME_RISK and adtv_cr < ADTV_STRESS_CR:
        result["circuit_exclude"] = True
        result["circuit_risk"]    = "extreme"
        result["circuit_notes"]  += (
            f"⛔ HARD EXCLUDE — beta {beta:.1f} + ADTV ₹{adtv_cr:.1f}Cr: "
            f"cannot safely exit on stop-loss. "
        )

    result["circuit_penalty"] = min(result["circuit_penalty"], 12)

    if not result["circuit_notes"].strip():
        result["circuit_notes"] = "Circuit risk: low ✅"

    return result


# ─────────────────────────────────────────────
# PLEDGE / DILUTION CHECKER (5.5)
# ─────────────────────────────────────────────

SHORT_INTEREST_HIGH     = 5.0
SHORT_INTEREST_ELEVATED = 2.0
FLOAT_RATIO_SUSPICIOUS  = 0.65
DILUTION_THRESHOLD      = 5.0

def check_pledge_dilution(ticker: str, data: dict) -> dict:
    """
    Flag promoter pledge risk (proxy) and share dilution.
    Makes one extra yfinance call per stock.
    """
    result = {
        "pledge_risk":      "low",
        "dilution_flag":    False,
        "short_interest":   None,
        "float_ratio":      None,
        "shares_growth":    None,
        "pledge_penalty":   0,
        "dilution_penalty": 0,
        "net_pledge_adj":   0,
        "pledge_notes":     "",
    }

    try:
        stock = yf.Ticker(ticker)
        info  = stock.info

        short_pct_float = info.get("shortPercentOfFloat")

        if short_pct_float is not None:
            short_pct = round(short_pct_float * 100, 2)
            result["short_interest"] = short_pct

            if short_pct >= SHORT_INTEREST_HIGH:
                result["pledge_risk"]    = "high"
                result["pledge_penalty"] = 8
                result["pledge_notes"]  += (
                    f"⛔ High short interest ({short_pct:.1f}% of float) — "
                    f"institutional shorts may anticipate pledge cascade. "
                )
            elif short_pct >= SHORT_INTEREST_ELEVATED:
                result["pledge_risk"]    = "elevated"
                result["pledge_penalty"] = 4
                result["pledge_notes"]  += (
                    f"⚠️  Elevated short interest ({short_pct:.1f}% of float) — "
                    f"worth monitoring. "
                )
            else:
                result["pledge_notes"]  += f"Short interest low ({short_pct:.1f}%). "

        shares_float       = info.get("floatShares")
        shares_outstanding = info.get("sharesOutstanding")
        insider_pct        = data.get("insider_pct", 0) or 0

        if shares_float and shares_outstanding and shares_outstanding > 0:
            float_ratio = round(shares_float / shares_outstanding, 3)
            result["float_ratio"] = float_ratio

            if float_ratio > FLOAT_RATIO_SUSPICIOUS and insider_pct > 40:
                result["pledge_penalty"] = max(result["pledge_penalty"], 5)
                if result["pledge_risk"] == "low":
                    result["pledge_risk"] = "elevated"
                result["pledge_notes"] += (
                    f"⚠️  Float ratio {float_ratio:.2f} elevated vs promoter "
                    f"holding {insider_pct:.1f}% — possible pledge activity. "
                )

        try:
            bs = stock.quarterly_balance_sheet
            if bs is not None and not bs.empty:
                if "Ordinary Shares Number" in bs.index:
                    shares_series = bs.loc["Ordinary Shares Number"].dropna()
                    if len(shares_series) >= 2:
                        latest_shares   = float(shares_series.iloc[0])
                        year_ago_shares = float(shares_series.iloc[-1])
                        if year_ago_shares > 0:
                            growth_pct = round(
                                (latest_shares / year_ago_shares - 1) * 100, 2
                            )
                            result["shares_growth"] = growth_pct
                            if growth_pct > DILUTION_THRESHOLD:
                                result["dilution_flag"]    = True
                                result["dilution_penalty"] = 5
                                result["pledge_notes"]    += (
                                    f"⚠️  Share dilution: shares outstanding grew "
                                    f"{growth_pct:.1f}% YoY — existing holders diluted. "
                                )
                            elif growth_pct > 2.0:
                                result["dilution_penalty"] = 2
                                result["pledge_notes"]    += (
                                    f"Shares grew {growth_pct:.1f}% YoY — mild dilution. "
                                )
                            else:
                                result["pledge_notes"]    += (
                                    f"Shares stable ({growth_pct:+.1f}% YoY). "
                                )
        except Exception:
            result["pledge_notes"] += "Share count history unavailable. "

        if not result["pledge_notes"].strip():
            result["pledge_notes"] = "No pledge or dilution concerns detected. ✅"

    except Exception as e:
        result["pledge_notes"] = f"Pledge/dilution check failed: {e}"

    result["pledge_penalty"]   = min(result["pledge_penalty"], 8)
    result["dilution_penalty"] = min(result["dilution_penalty"], 5)
    result["net_pledge_adj"]   = -(result["pledge_penalty"] + result["dilution_penalty"])

    return result


# ─────────────────────────────────────────────
# SENTIMENT SIGNAL FETCHER
# ─────────────────────────────────────────────

def _parse_sentiment(raw: dict) -> dict:
    """Extract {sector: signal_label} from raw sentiment payload."""
    result = {}
    for sector, val in raw.items():
        if isinstance(val, dict):
            sig = val.get("signal") or val.get("sentiment") or "neutral"
        elif isinstance(val, str):
            sig = val
        else:
            sig = "neutral"
        result[sector] = sig.lower()
    return result


def fetch_sentiment_signals() -> dict:
    """
    Return {sector_name: sentiment_label}.

    Priority order:
      1. monthly_earnings_sentiment.json → sector_signals (30-day structural view)
      2. swing_news_sentiment.json        (7-day news view — fallback)
      3. API /signals endpoint            (remote fallback)
    """
    # 1. Monthly structural signals (preferred for longer-term picks)
    monthly_path = os.path.join(DATA_DIR, "monthly_earnings_sentiment.json")
    try:
        if os.path.exists(monthly_path):
            with open(monthly_path) as f:
                raw = json.load(f)
            parsed = _parse_sentiment(raw.get("sector_signals", raw))
            if parsed:
                print(f"  📂 Sector sentiment loaded from monthly file ({len(parsed)} sectors)")
                return parsed
    except Exception:
        pass

    # 2. Swing sentiment (shorter-window fallback)
    swing_path = os.path.join(DATA_DIR, "swing_news_sentiment.json")
    try:
        if os.path.exists(swing_path):
            with open(swing_path) as f:
                raw = json.load(f)
            parsed = _parse_sentiment(raw.get("signals", raw))
            if parsed:
                print(f"  📂 Sector sentiment loaded from swing file ({len(parsed)} sectors, fallback)")
                return parsed
    except Exception:
        pass

    # 3. API fallback — try monthly then swing signal type
    for signal_type in ("monthly_earnings_sentiment", "swing_news_sentiment"):
        try:
            req = _urllib.Request(
                f"{API_URL}/signals",
                headers={"Accept": "application/json"},
            )
            with _urllib.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            raw = data.get(signal_type, {})
            # monthly type nests under sector_signals
            if signal_type == "monthly_earnings_sentiment" and "sector_signals" in raw:
                raw = raw["sector_signals"]
            parsed = _parse_sentiment(raw)
            if parsed:
                print(f"  🌐 Sector sentiment loaded from API/{signal_type} ({len(parsed)} sectors)")
                return parsed
        except Exception:
            pass

    print("  ⚠️  Could not load sentiment signals — all sectors treated as neutral")
    return {}


def fetch_ticker_earnings_signals() -> dict:
    """
    Return {ticker: signal_label} from monthly_earnings_sentiment.json.
    Used to apply per-ticker earnings quality adjustment in Stage 3.
    Returns {} if no monthly file available.
    """
    monthly_path = os.path.join(DATA_DIR, "monthly_earnings_sentiment.json")
    try:
        if os.path.exists(monthly_path):
            with open(monthly_path) as f:
                raw = json.load(f)
            ticker_sigs = raw.get("ticker_signals", {})
            result = {}
            for ticker, val in ticker_sigs.items():
                if isinstance(val, dict):
                    sig = val.get("signal", "neutral")
                elif isinstance(val, str):
                    sig = val
                else:
                    sig = "neutral"
                result[ticker] = sig.lower()
            if result:
                nonneut = sum(1 for s in result.values() if s != "neutral")
                print(f"  📂 Ticker earnings signals loaded: {len(result)} tickers ({nonneut} non-neutral)")
            return result
    except Exception:
        pass

    # API fallback
    try:
        req = _urllib.Request(
            f"{API_URL}/signals",
            headers={"Accept": "application/json"},
        )
        with _urllib.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        monthly = data.get("monthly_earnings_sentiment", {})
        ticker_sigs = monthly.get("ticker_signals", {})
        result = {t: v.get("signal", "neutral") if isinstance(v, dict) else "neutral"
                  for t, v in ticker_sigs.items()}
        if result:
            print(f"  🌐 Ticker earnings signals from API: {len(result)} tickers")
        return result
    except Exception:
        pass

    print("  ℹ️  No ticker earnings signals — monthly_earnings_sentiment.py not yet run")
    return {}


def fetch_policy_signals() -> dict:
    """
    Return {sector_name: signal_label} from policy_signals.json.
    Used as Stage 3C softer macro adjustment — no hard-exclude.
    Returns {} if policy_scraper.py has not yet run.
    """
    policy_path = os.path.join(DATA_DIR, "policy_signals.json")
    try:
        if os.path.exists(policy_path):
            with open(policy_path) as f:
                raw = json.load(f)
            signals = raw.get("signals", {})
            result = {}
            for sector, val in signals.items():
                if isinstance(val, dict):
                    sig = val.get("signal", "neutral")
                elif isinstance(val, str):
                    sig = val
                else:
                    sig = "neutral"
                result[sector] = sig.lower()
            if result:
                nonneut = sum(1 for s in result.values() if s != "neutral")
                gen_at  = raw.get("generated_at", "unknown")
                print(f"  📂 Policy signals loaded: {len(result)} sectors "
                      f"({nonneut} non-neutral, generated {gen_at})")
            return result
    except Exception:
        pass

    # API fallback
    try:
        req = _urllib.Request(
            f"{API_URL}/signals",
            headers={"Accept": "application/json"},
        )
        with _urllib.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        raw     = data.get("policy_signals", {})
        signals = raw.get("signals", {})
        result  = {s: v.get("signal", "neutral") if isinstance(v, dict) else "neutral"
                   for s, v in signals.items()}
        if result:
            print(f"  🌐 Policy signals from API: {len(result)} sectors")
        return result
    except Exception:
        pass

    print("  ℹ️  No policy signals — policy_scraper.py not yet run")
    return {}


# ─────────────────────────────────────────────
# UNIVERSE SCREENER
# ─────────────────────────────────────────────

def screen_all(
    sector_universe: dict,
    sentiment_signals: dict,
    prev_institutional: dict,
    ticker_earnings_signals: dict = None,
    policy_signals: dict = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Screen all Nifty 500 stocks across 20 NSE sectors.

    Pipeline per sector:
      1. Sentiment gate — hard-skip negative sectors
      2. yfinance data fetch + liquidity filter
      3. Fundamental filters (nse_universe.passes_fundamental_filters)
      4. Earnings freshness (2.2)
      5. Margin health (2.3)
      6. Promoter signal (3.1)
      7. Institutional trend (3.2)
      8. Circuit risk (3.3)
      9. Pledge / dilution (5.5)

    After all stocks collected:
      10. Global normalisation (scores relative to entire Nifty 500 universe)
      11. All quality adjustments applied to final_score
      12. Sentiment bonus/penalty applied per sector
      13. Sort globally → return (top_7_df, all_df)
    """
    records            = []
    excluded_sentiment = 0
    excluded_liq       = 0
    excluded_fund      = 0
    excluded_earnings  = 0

    sentiment_emoji = {
        "positive": "🟢", "mild_positive": "🟡", "neutral": "⚪",
        "cautious": "🟠", "negative": "🔴",
    }

    for sector_name, tickers in sorted(sector_universe.items()):
        if not tickers:
            continue

        sentiment = sentiment_signals.get(sector_name, "neutral")

        if sentiment == "negative":
            print(f"\n  ⛔ Skipping {sector_name} — negative sentiment ({len(tickers)} stocks excluded)")
            excluded_sentiment += len(tickers)
            continue

        semj = sentiment_emoji.get(sentiment, "⚪")
        print(f"\n  Screening {sector_name} ({len(tickers)} stocks) {semj} {sentiment}...")

        for ticker in tickers:
            data = fetch_stock_data(ticker)
            if data is None:
                excluded_liq += 1
                time.sleep(0.3)
                continue

            data["nse_sector"]       = sector_name
            data["sector_sentiment"] = sentiment

            passed, reason = passes_fundamental_filters(data)
            if not passed:
                print(f"    ⛔ {ticker} excluded — {reason}")
                excluded_fund += 1
                time.sleep(0.3)
                continue

            freshness = check_earnings_freshness(ticker)
            time.sleep(0.3)
            if freshness["exclude"]:
                print(f"    ⛔ {ticker} excluded — {freshness['notes'].strip()}")
                excluded_earnings += 1
                continue

            data["last_reported_date"] = freshness["last_reported_date"]
            data["data_age_days"]      = freshness["data_age_days"]
            data["earnings_trend"]     = freshness["earnings_trend"]
            data["earnings_miss"]      = freshness["earnings_miss"]
            data["freshness_penalty"]  = freshness["freshness_penalty"]
            data["earnings_notes"]     = freshness["notes"]

            margin = check_margin_health(data)
            data["divergence"]     = margin["divergence"]
            data["margin_trend"]   = margin["margin_trend"]
            data["margin_penalty"] = margin["margin_penalty"]
            data["margin_bonus"]   = margin["margin_bonus"]
            data["net_adjustment"] = margin["net_adjustment"]
            data["margin_notes"]   = margin["margin_notes"]

            promoter = check_promoter_signal(data)
            data["promoter_signal"]    = promoter["promoter_signal"]
            data["institution_signal"] = promoter["institution_signal"]
            data["promoter_bonus"]     = promoter["promoter_bonus"]
            data["promoter_penalty"]   = promoter["promoter_penalty"]
            data["net_promoter_adj"]   = promoter["net_promoter_adj"]
            data["promoter_notes"]     = promoter["promoter_notes"]

            prev_pct   = (prev_institutional or {}).get(ticker)
            inst_trend = check_institutional_trend(
                ticker,
                current_inst_pct=data.get("institutional_pct"),
                prev_inst_pct=prev_pct,
            )
            time.sleep(0.2)
            data["inst_change_pp"]   = inst_trend["inst_change_pp"]
            data["inst_trend"]       = inst_trend["inst_trend"]
            data["net_inst_adj"]     = inst_trend["net_inst_adj"]
            data["inst_trend_notes"] = inst_trend["inst_trend_notes"]
            data["holder_count"]     = inst_trend["holder_count"]

            circuit = check_circuit_risk(data)
            if circuit["circuit_exclude"]:
                print(f"    ⛔ {ticker} excluded — {circuit['circuit_notes'].strip()}")
                excluded_earnings += 1
                continue
            data["circuit_risk"]    = circuit["circuit_risk"]
            data["circuit_penalty"] = circuit["circuit_penalty"]
            data["circuit_notes"]   = circuit["circuit_notes"]

            pledge = check_pledge_dilution(ticker, data)
            time.sleep(0.3)
            data["pledge_risk"]    = pledge["pledge_risk"]
            data["dilution_flag"]  = pledge["dilution_flag"]
            data["short_interest"] = pledge["short_interest"]
            data["float_ratio"]    = pledge["float_ratio"]
            data["shares_growth"]  = pledge["shares_growth"]
            data["net_pledge_adj"] = pledge["net_pledge_adj"]
            data["pledge_notes"]   = pledge["pledge_notes"]

            scores = score_stock(data)
            records.append({**data, **scores})
            time.sleep(0.3)

    total_excl = excluded_sentiment + excluded_liq + excluded_fund + excluded_earnings
    print(
        f"\n  ── Screening Summary ──────────────────────────────"
        f"\n  Passed:              {len(records)}"
        f"\n  Sentiment excluded:  {excluded_sentiment}"
        f"\n  Liquidity excluded:  {excluded_liq}"
        f"\n  Fundamental excl:    {excluded_fund}"
        f"\n  Earnings excluded:   {excluded_earnings}"
    )

    if not records:
        return pd.DataFrame(), pd.DataFrame()

    # Global normalisation — all sectors compete on the same scale
    all_df = pd.DataFrame(records)
    all_df = normalise_and_compute_final(all_df, UNIFIED_SCORING_WEIGHTS)

    # Apply all quality adjustments
    for col, sign in [
        ("freshness_penalty", -1),
        ("net_adjustment",     1),
        ("net_promoter_adj",   1),
        ("net_inst_adj",       1),
        ("circuit_penalty",   -1),
        ("net_pledge_adj",     1),
    ]:
        if col in all_df.columns:
            all_df["final_score"] = (
                all_df["final_score"] + sign * all_df[col]
            ).clip(lower=0, upper=100)

    # Stage 3A — Sector sentiment adjustment
    all_df["sentiment_adj"] = (
        all_df["sector_sentiment"]
        .map(SENTIMENT_SCORE_ADJ)
        .fillna(0.0)
    )
    all_df["final_score"] = (
        all_df["final_score"] + all_df["sentiment_adj"]
    ).clip(0, 100)

    # Stage 3B — Ticker earnings quality adjustment (from monthly_earnings_sentiment.py)
    if ticker_earnings_signals:
        def _earnings_adj(ticker):
            sig = ticker_earnings_signals.get(ticker, "neutral")
            return TICKER_EARNINGS_ADJ.get(sig, 0.0)

        all_df["earnings_signal"]   = all_df["ticker"].map(
            lambda t: ticker_earnings_signals.get(t, "neutral")
        )
        all_df["earnings_news_adj"] = all_df["ticker"].map(_earnings_adj)
        all_df["final_score"] = (
            all_df["final_score"] + all_df["earnings_news_adj"]
        ).clip(0, 100)

        n_adj = (all_df["earnings_news_adj"] != 0).sum()
        if n_adj:
            print(f"\n  Stage 3B: Ticker earnings adjustment applied to {n_adj} stocks")
    else:
        all_df["earnings_signal"]   = "neutral"
        all_df["earnings_news_adj"] = 0.0

    # Stage 3C — Policy/macro adjustment (from policy_scraper.py)
    # Softer than news sentiment; no hard-exclude — policy signals affect all stocks
    # in a sector equally and are slower-moving than earnings/news signals.
    if policy_signals:
        def _policy_adj(sector):
            sig = policy_signals.get(sector, "neutral")
            return POLICY_SCORE_ADJ.get(sig, 0.0)

        all_df["policy_signal"] = all_df["nse_sector"].map(
            lambda s: policy_signals.get(s, "neutral")
        )
        all_df["policy_adj"] = all_df["nse_sector"].map(_policy_adj)
        all_df["final_score"] = (
            all_df["final_score"] + all_df["policy_adj"]
        ).clip(0, 100)

        n_pol = (all_df["policy_adj"] != 0).sum()
        if n_pol:
            print(f"\n  Stage 3C: Policy adjustment applied to {n_pol} stocks")
    else:
        all_df["policy_signal"] = "neutral"
        all_df["policy_adj"]    = 0.0

    all_df = all_df.sort_values("final_score", ascending=False).reset_index(drop=True)
    top_df = select_top_diversified(all_df, TOP_PICKS, MAX_PER_SECTOR)

    return top_df, all_df


def select_top_diversified(all_df: pd.DataFrame, n_picks: int, max_per_sector: int) -> pd.DataFrame:
    """
    Walk down the ranked list, capping picks per NSE sector so one hot
    sector (sentiment boost + sector-wide rally) cannot fill the whole
    portfolio. Skipped stocks remain in all_df for the full ranking report.
    """
    picked_idx   = []
    sector_count: dict[str, int] = {}

    for idx, row in all_df.iterrows():
        sector = row.get("nse_sector", "Unknown")
        if sector_count.get(sector, 0) >= max_per_sector:
            print(f"  ⏭️  {row['ticker']} skipped for diversification — "
                  f"already {max_per_sector} picks from {sector}")
            continue
        picked_idx.append(idx)
        sector_count[sector] = sector_count.get(sector, 0) + 1
        if len(picked_idx) >= n_picks:
            break

    return all_df.loc[picked_idx].copy()


# ─────────────────────────────────────────────
# PORTFOLIO BUILDER
# ─────────────────────────────────────────────

def build_portfolio(budget: int = BUDGET) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    """Run screener across all 20 NSE sectors and build the top-7 portfolio."""

    print("\n" + "="*60)
    print("  INDIAN STOCK SCREENER — MONTHLY TOP 7")
    print(f"  Date: {datetime.now().strftime('%d %B %Y')}")
    print(f"  Budget: ₹{budget:,.0f}")
    print("="*60)

    # Step 1: Universe
    print("\n  Step 1: Building stock universe from Nifty 500...")
    nifty500_df     = fetch_nifty500()
    sector_universe = map_to_sectors(nifty500_df)

    # Step 2: Sentiment + earnings signals
    print("\n  Step 2: Loading sentiment and earnings signals...")
    semj = {"positive": "🟢", "mild_positive": "🟡", "neutral": "⚪",
            "cautious": "🟠", "negative": "🔴"}

    sentiment_signals = fetch_sentiment_signals()
    if sentiment_signals:
        print(f"  Sector sentiments:")
        for sec, sig in sorted(sentiment_signals.items()):
            print(f"    {semj.get(sig,'⚪')} {sec:<42} {sig}")
    else:
        print("  No sector sentiment data — all treated as neutral")

    ticker_earnings_signals = fetch_ticker_earnings_signals()
    policy_signals = fetch_policy_signals()

    # Step 3: Previous institutional holdings for QoQ comparison
    prev_institutional = _load_prev_institutional()
    if prev_institutional:
        print(f"\n  Loaded prior institutional holdings for {len(prev_institutional)} stocks")
    else:
        print("\n  No prior institutional data — first run (Tier 2 only for 3.2)")

    # Step 4: Screen
    print("\n  Step 3: Screening Nifty 500 across all sectors...")
    top_df, all_df = screen_all(
        sector_universe, sentiment_signals, prev_institutional,
        ticker_earnings_signals=ticker_earnings_signals,
        policy_signals=policy_signals,
    )

    if top_df.empty:
        print("  ⚠️  No stocks passed all filters.")
        return {}, top_df, all_df

    # Step 5: Build portfolio dict
    per_stock = budget / TOP_PICKS

    stocks_list = []
    for _, row in top_df.iterrows():
        ticker    = row["ticker"]
        buy_price = row["current_price"]

        atr_stops = compute_atr_stops(ticker, buy_price)
        time.sleep(0.2)

        stocks_list.append({
            "ticker":             ticker,
            "name":               row["name"],
            "price":              buy_price,
            "nse_sector":         row.get("nse_sector", "Unknown"),
            "sector_sentiment":   row.get("sector_sentiment", "neutral"),
            "sentiment_adj":      float(row.get("sentiment_adj", 0.0)),
            "earnings_signal":    row.get("earnings_signal", "neutral"),
            "earnings_news_adj":  float(row.get("earnings_news_adj", 0.0)),
            "final_score":        round(row["final_score"], 1),
            # Valuation
            "pe_ratio":           round(row["pe_raw"], 1)  if row.get("pe_raw")  else "N/A",
            "peg_ratio":          round(row["peg_raw"], 2) if row.get("peg_raw") else "N/A",
            "pb_ratio":           round(row.get("pb_ratio"), 1) if row.get("pb_ratio") else "N/A",
            "roe_pct":            round(row["roe_raw"], 1) if row.get("roe_raw") else "N/A",
            "rev_growth_pct":     round(row["revenue_growth_raw"], 1) if row.get("revenue_growth_raw") else "N/A",
            "debt_equity":        round(row["debt_raw"], 2) if row.get("debt_raw") else "N/A",
            "momentum_1m":        row["momentum_1m"],
            "momentum_3m":        row["momentum_3m"],
            "momentum_6m":        row.get("momentum_6m", 0),
            "allocation_inr":     round(per_stock, 0),
            "approx_shares":      int(per_stock // buy_price) if buy_price > 0 else 0,
            # Liquidity
            "adv_30d":            int(row.get("adv_30d", 0)),
            "adtv_cr":            row.get("adtv_cr", 0),
            # ATR Stop-Loss
            "atr_14day":          atr_stops["atr_14day"],
            "atr_multiplier":     atr_stops["atr_multiplier"],
            "stop_loss_price":    atr_stops["stop_loss_price"],
            "stop_loss_pct":      atr_stops["stop_loss_pct"],
            "trailing_stop_dist": atr_stops["trailing_stop_dist"],
            "atr_source":         atr_stops["atr_source"],
            # Earnings Freshness (2.2)
            "last_reported_date": row.get("last_reported_date", "N/A"),
            "data_age_days":      row.get("data_age_days", "N/A"),
            "earnings_trend":     row.get("earnings_trend", "unknown"),
            "earnings_miss":      row.get("earnings_miss", False),
            "freshness_penalty":  row.get("freshness_penalty", 0),
            "earnings_notes":     row.get("earnings_notes", ""),
            # Margin Health (2.3)
            "divergence":         row.get("divergence"),
            "margin_trend":       row.get("margin_trend", "unknown"),
            "margin_penalty":     row.get("margin_penalty", 0),
            "margin_bonus":       row.get("margin_bonus", 0),
            "net_adjustment":     row.get("net_adjustment", 0),
            "margin_notes":       row.get("margin_notes", ""),
            # Promoter Activity (3.1)
            "insider_pct":        row.get("insider_pct"),
            "institutional_pct":  row.get("institutional_pct"),
            "promoter_signal":    row.get("promoter_signal", "unknown"),
            "institution_signal": row.get("institution_signal", "unknown"),
            "net_promoter_adj":   row.get("net_promoter_adj", 0),
            "promoter_notes":     row.get("promoter_notes", ""),
            # Institutional Trend (3.2)
            "inst_change_pp":     row.get("inst_change_pp"),
            "inst_trend":         row.get("inst_trend", "unknown"),
            "net_inst_adj":       row.get("net_inst_adj", 0),
            "inst_trend_notes":   row.get("inst_trend_notes", ""),
            "holder_count":       row.get("holder_count"),
            # Circuit Risk (3.3)
            "circuit_risk":       row.get("circuit_risk", "low"),
            "circuit_penalty":    row.get("circuit_penalty", 0),
            "circuit_notes":      row.get("circuit_notes", ""),
            # Portfolio volatility
            "beta":               row.get("beta"),
            # Tax tracking
            "buy_date":           datetime.now().strftime("%Y-%m-%d"),
            # Pledge / Dilution (5.5)
            "pledge_risk":        row.get("pledge_risk", "low"),
            "dilution_flag":      row.get("dilution_flag", False),
            "short_interest":     row.get("short_interest"),
            "shares_growth":      row.get("shares_growth"),
            "net_pledge_adj":     row.get("net_pledge_adj", 0),
            "pledge_notes":       row.get("pledge_notes", ""),
            # Audit trail (5.2)
            "audit_trail":        generate_audit_trail(row),
        })

    portfolio = {
        "top_picks": {
            "label":                "Monthly Top 7 — Nifty 500",
            "total_allocation":     budget,
            "per_stock_allocation": round(per_stock, 0),
            "stocks":               stocks_list,
        }
    }

    vol_assessment = assess_portfolio_volatility(portfolio)
    print(f"\n  {vol_assessment['health_summary']}")
    for w in vol_assessment["warnings"]:
        print(f"  ⚠️  {w}")

    # Step 6: Monthly advisory — compare with current holdings, ask Claude
    print("\n  Step 4: Generating monthly portfolio advisory...")
    advisory = generate_monthly_advisory(portfolio, all_df)
    try:
        adv_path = os.path.join(DATA_DIR, "monthly_advisory.json")
        with open(adv_path, "w") as f:
            json.dump(advisory, f, indent=2)
        _post_to_api("/portfolio/advisory/upload", advisory)
    except Exception as e:
        print(f"  ⚠️  Could not save advisory: {e}")

    return portfolio, top_df, all_df


def rebalance_holdings(live_holdings: list, all_df: pd.DataFrame) -> list:
    """
    Fallback rebalancer — mirrors rebalancer.py decision rules exactly.
    Only used when the API rebalancer report is not yet available.

    Rules (same thresholds as rebalancer.py PROFIT_STAGES):
      Stop-loss breach            → EXIT
      PnL >= +50%  (Stage 3)     → EXIT  (sell all)
      PnL >= +35%  (Stage 2)     → TRIM  (sell 30%)
      PnL >= +20%  (Stage 1)     → TRIM  (sell 30%)
      3+ months held, PnL < 5%   → EXIT  (dead money, redeploy)
      Within 5% of stop-loss     → WATCH
      Everything else            → HOLD
    """
    for h in live_holdings:
        ticker    = h["ticker"]
        buy_price = float(h.get("buy_price") or 0)
        stop_loss = float(h.get("stop_loss_price") or 0)

        try:
            current_price = float(yf.Ticker(ticker).fast_info.last_price or 0)
        except Exception as exc:
            h["rebalancer_verdict"] = "unknown"
            h["rebalancer_reason"]  = f"price unavailable: {exc}"
            continue

        pnl_pct = ((current_price - buy_price) / buy_price * 100) if buy_price > 0 else 0
        try:
            days_held = (datetime.now() -
                         datetime.strptime(h.get("buy_date", ""), "%Y-%m-%d")).days
        except ValueError:
            days_held = 0

        months_held = days_held / 30.44
        h["current_price"] = round(current_price, 2)
        h["pnl_pct"]       = round(pnl_pct, 1)
        h["days_held"]     = days_held

        # ── Mirror rebalancer.py priority order ─────────────────────────────
        if stop_loss and current_price <= stop_loss:
            verdict = "EXIT"; exit_score = 100
            reason  = f"Stop-loss breached — ₹{current_price:,.2f} ≤ GTT ₹{stop_loss:,.2f}"
        elif pnl_pct >= 50:
            verdict = "EXIT"; exit_score = 90
            reason  = f"Stage 3 (+50%) — full exit, P&L +{pnl_pct:.0f}%"
        elif pnl_pct >= 35:
            verdict = "TRIM"; exit_score = 60
            reason  = f"Stage 2 (+35%) — sell 30%, P&L +{pnl_pct:.0f}%"
        elif pnl_pct >= 20:
            verdict = "TRIM"; exit_score = 40
            reason  = f"Stage 1 (+20%) — sell 30%, P&L +{pnl_pct:.0f}%"
        elif months_held >= 3 and pnl_pct < 5:
            verdict = "EXIT"; exit_score = 70
            reason  = (f"Dead money — {months_held:.0f} months held, "
                       f"only {pnl_pct:+.0f}% gain. Redeploy.")
        elif stop_loss and current_price <= stop_loss * 1.05:
            verdict = "WATCH"; exit_score = 25
            reason  = f"Near stop-loss ₹{stop_loss:,.2f} — monitor closely"
        else:
            verdict = "HOLD"; exit_score = 0
            next_t  = buy_price * 1.20 if buy_price > 0 else 0
            to_go   = ((next_t - current_price) / current_price * 100) if current_price > 0 else 0
            reason  = (f"No trigger — P&L {pnl_pct:+.0f}%, "
                       f"needs +{to_go:.0f}% more for Stage 1 (₹{next_t:,.0f})")

        h["exit_score"]         = exit_score
        h["rebalancer_verdict"] = verdict
        h["rebalancer_reason"]  = reason
        print(f"    fallback-rebalancer {ticker.replace('.NS',''):12s} "
              f"pnl={pnl_pct:+.0f}%  held={months_held:.1f}mo  → {verdict}")

    return live_holdings


def generate_monthly_advisory(portfolio: dict, all_df: pd.DataFrame) -> dict:
    """
    Monthly advisory using two separate frameworks:

    REBALANCER  — evaluates existing holdings (hold vs exit).
                  Uses fixed rules: TRIM at +20%/+35%, EXIT at +50%,
                  EXIT if dead money (3+ months, <5% gain), EXIT on stop-loss.
                  Primary source: rebalancer.py report from API.
                  Fallback: rebalance_holdings() with identical rules.

    SCREENER    — identifies new buy candidates from the Nifty 500 universe.
                  Screener top picks are offered only as a reinvestment idea
                  when the rebalancer flags an exit.

    Advisory outputs one action per month: HOLD / EXIT / ADD / EXIT_AND_ADD.
    """
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M IST")

    # ── Build full score/rank lookup from all_df ────────────────────────────
    sorted_df = all_df.sort_values("final_score", ascending=False).reset_index(drop=True)
    all_scores = {
        row["ticker"]: {
            "rank":   i + 1,
            "score":  round(row["final_score"], 1),
            "sector": row.get("nse_sector", ""),
        }
        for i, (_, row) in enumerate(sorted_df.iterrows())
    }

    # ── Load current live holdings ──────────────────────────────────────────
    live_holdings = []
    try:
        req = _urllib.Request(
            f"{API_URL}/portfolio/live",
            headers={"Accept": "application/json"},
        )
        with _urllib.urlopen(req, timeout=10) as resp:
            live_data = json.loads(resp.read())
        for bucket in live_data.values():
            for stock in bucket.get("stocks", []):
                tick = stock.get("ticker", "")
                si   = all_scores.get(tick)
                live_holdings.append({
                    "ticker":    tick,
                    "name":      stock.get("name", ""),
                    "sector":    stock.get("nse_sector", stock.get("sector", "")),
                    "buy_price": stock.get("price", stock.get("buy_price", 0)),
                    "buy_date":  stock.get("buy_date", ""),
                    # screener rank (used as context only, not exit trigger)
                    "current_score": si["score"] if si else None,
                    "current_rank":  si["rank"]  if si else None,
                    "status": (
                        "top7"     if si and si["rank"] <= 7  else
                        "top15"    if si and si["rank"] <= 15 else
                        "top30"    if si and si["rank"] <= 30 else
                        "low"      if si else
                        "excluded"
                    ),
                })
    except Exception as e:
        print(f"  ⚠️  Could not load live portfolio for advisory: {e}")

    # ── Try to fetch authoritative rebalancer report from API ───────────────
    # rebalancer.py runs on the 1st of month; screener runs on the 3rd.
    # If the report is available use it — otherwise fall back to lightweight check.
    rb_report = None
    try:
        req = _urllib.Request(
            f"{API_URL}/rebalance/report",
            headers={"Accept": "application/json"},
        )
        with _urllib.urlopen(req, timeout=10) as resp:
            rb_data = json.loads(resp.read())
        if "error" not in rb_data and rb_data.get("actions"):
            rb_report = rb_data
            print(f"  ✅ Using rebalancer report from API (date: {rb_data.get('date','?')})")
            # Merge rebalancer decisions into live_holdings for the health check
            rb_by_ticker = {d["ticker"]: d for d in rb_data.get("actions", [])}
            for h in live_holdings:
                rb = rb_by_ticker.get(h["ticker"], {})
                h["rebalancer_verdict"] = rb.get("action", "HOLD")
                h["rebalancer_reason"]  = rb.get("reason", "")
                h["pnl_pct"]            = rb.get("gain_pct", h.get("pnl_pct"))
                h["current_price"]      = rb.get("current_price")
                h["exit_score"]         = {"EXIT": 80, "TRIM": 50, "WATCH": 30, "HOLD": 10}.get(
                    rb.get("action", "HOLD"), 10)
        else:
            raise ValueError("empty report")
    except Exception as e:
        print(f"  ⚠️  Rebalancer API report not available ({e}) — running lightweight check")
        live_holdings = rebalance_holdings(live_holdings, all_df)

    # ── Build screener top-10 (new buys only, not already in portfolio) ───────
    new_picks    = []
    live_tickers = {h["ticker"] for h in live_holdings}
    for _, row in sorted_df.head(10).iterrows():
        new_picks.append({
            "ticker":    row["ticker"],
            "name":      row["name"],
            "sector":    row.get("nse_sector", ""),
            "score":     round(row["final_score"], 1),
            "sentiment": row.get("sector_sentiment", "neutral"),
            "policy":    row.get("policy_signal", "neutral"),
            "earnings":  row.get("earnings_signal", "neutral"),
            "mom_3m":    round(float(row.get("momentum_3m", 0) or 0), 1),
            "roe":       round(float(row.get("roe_pct", 0) or 0), 1),
            "pe":        round(float(row.get("pe_ratio", 0) or 0), 1),
        })
    new_entry_picks = [p for p in new_picks if p["ticker"] not in live_tickers]

    base = {
        "generated_at":        generated_at,
        "current_holdings":    len(live_holdings),
        "top_picks":           new_picks[:7],
        "holdings_health":     live_holdings,   # rebalancer data per holding
        "rebalance_report_date": rb_report.get("date") if rb_report else None,
    }

    api_key = os.getenv("ANTHROPIC_API_KEY", "")

    # ── LLM path ────────────────────────────────────────────────────────────
    if api_key and live_holdings:
        live_str = "\n".join(
            f"  {i+1}. {h['ticker'].replace('.NS','')} ({h['name'][:28]}) "
            f"| bought ₹{(h.get('buy_price') or 0):,.0f} on {h.get('buy_date','?')} "
            f"| PnL {(h.get('pnl_pct') or 0):+.1f}% "
            f"| rebalancer verdict: {h.get('rebalancer_verdict','?')} "
            f"| reason: {str(h.get('rebalancer_reason',''))[:60]}"
            for i, h in enumerate(live_holdings)
        )
        picks_str = "\n".join(
            f"  {i+1}. {p['ticker'].replace('.NS','')} ({p['name'][:28]}) "
            f"| {p['sector']} | Score {p['score']} | Mom-3M {(p.get('mom_3m') or 0):+.1f}% "
            f"| ROE {(p.get('roe') or 0):.0f}% | PE {(p.get('pe') or 0):.0f}"
            for i, p in enumerate(new_entry_picks[:7])
        )

        rb_note = (
            f"(Rebalancer ran on {rb_report['date']} — verdicts shown above are authoritative)"
            if rb_report else
            "(Rebalancer report not available — verdicts estimated from price data)"
        )

        prompt = f"""You are a conservative Indian equity portfolio advisor. Today is {datetime.now().strftime('%B %Y')}.
{rb_note}

PORTFOLIO FRAMEWORK — two separate jobs:
1. REBALANCER  decides on EXISTING holdings: HOLD, TRIM (+20% sell 30%), or EXIT (+50% / dead money).
   Rebalancer verdict is authoritative — do NOT second-guess it with screener rank.
   A stock can fail the BUY screener because it's fully priced (PE rose after rally) but still HOLD.

2. SCREENER finds NEW entry candidates — used only when cash is freed from an exit.

CURRENT HOLDINGS with rebalancer verdict:
{live_str}

SCREENER TOP NEW ENTRIES (stocks not already in portfolio):
{picks_str}

TASK: Pick exactly ONE action:
A) HOLD        — no exits flagged, all positions healthy
B) EXIT        [ticker] — bank profits from this position, hold cash
C) EXIT_AND_ADD [sell] → [buy] — exit one position, immediately redeploy into screener top pick
D) ADD         [ticker] — add a new position if portfolio < 7 and cash is available

RULES:
- Only recommend EXIT/EXIT_AND_ADD for a holding whose rebalancer verdict is EXIT or TRIM
- Rebalancer triggers: Stage 1 +20% (TRIM), Stage 2 +35% (TRIM), Stage 3 +50% (EXIT),
  dead money (3+ months <5%), stop-loss breach (EXIT). No other triggers.
- Do NOT recommend exiting a stock just because it passed its 52W high or screener filtered it
- Do NOT recommend exiting a stock with negative PnL — that locks in a loss
- If multiple holdings have EXIT/TRIM verdict, pick the one with highest PnL
- LTCG: holding >1 year is more tax-efficient (10% vs 15% STCG)
- 2-3 sentences citing the specific PnL% and which stage/trigger fired

Respond with ONLY valid JSON (action must be: HOLD / EXIT / EXIT_AND_ADD / ADD):
{{"action":"HOLD","sell_ticker":null,"sell_name":null,"buy_ticker":null,"buy_name":null,"buy_sector":null,"reasoning":"..."}}"""

        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "Content-Type":    "application/json",
                    "x-api-key":       api_key,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model":    "claude-sonnet-4-20250514",
                    "max_tokens": 500,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=30,
            )
            resp.raise_for_status()
            raw = resp.json()["content"][0]["text"].strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```[a-z]*\n?", "", raw)
                raw = re.sub(r"\n?```$", "", raw)
            result = json.loads(raw)
            base.update(result)
            base["source"] = "llm"
            print(f"\n  🤖 Advisory: {result.get('action','?')} "
                  f"{'→ ' + result.get('buy_ticker','') if result.get('buy_ticker') else ''}")
            print(f"     {str(result.get('reasoning',''))[:140]}")
            return base
        except Exception as e:
            print(f"  ⚠️  Advisory LLM call failed: {e} — using rule-based fallback")

    # ── Rule-based fallback ──────────────────────────────────────────────────
    base["source"] = "rule"

    if not live_holdings:
        if new_entry_picks:
            p = new_entry_picks[0]
            base.update({
                "action": "ADD",
                "sell_ticker": None, "sell_name": None,
                "buy_ticker": p["ticker"], "buy_name": p["name"],
                "buy_sector": p["sector"],
                "reasoning": (
                    f"No existing portfolio detected. Top screener pick this month: "
                    f"{p['name']} ({p['sector']}, score {p['score']}). Consider starting here."
                ),
            })
        else:
            base.update({"action": "HOLD", "sell_ticker": None, "buy_ticker": None,
                         "reasoning": "No screener data available."})
        return base

    # Find the best exit candidate — TRIM also qualifies (partial profit-booking)
    # Sort by PnL descending so we bank the most profit first
    exit_candidates = sorted(
        [h for h in live_holdings
         if h.get("rebalancer_verdict") in ("EXIT", "TRIM") and (h.get("pnl_pct") or 0) > 0],
        key=lambda h: (h.get("pnl_pct") or 0), reverse=True,
    )
    watch_candidates = sorted(
        [h for h in live_holdings
         if h.get("rebalancer_verdict") == "WATCH" and (h.get("pnl_pct") or 0) > 0],
        key=lambda h: (h.get("exit_score") or 0), reverse=True,
    )

    if exit_candidates:
        sell    = exit_candidates[0]
        pnl     = (sell.get("pnl_pct") or 0)
        verdict = sell.get("rebalancer_verdict", "EXIT")
        rb_rsn  = sell.get("rebalancer_reason", "")
        rsn     = (
            f"{sell['name']} is up {pnl:+.0f}% from your buy price. "
            f"Rebalancer verdict: {verdict}. "
            + (f"Reason: {rb_rsn}. " if rb_rsn else "")
        )
        if new_entry_picks:
            best = new_entry_picks[0]
            base.update({
                "action": "EXIT_AND_ADD",
                "sell_ticker": sell["ticker"], "sell_name": sell["name"],
                "buy_ticker":  best["ticker"], "buy_name":  best["name"],
                "buy_sector":  best["sector"],
                "reasoning": rsn + (
                    f"Redeploy proceeds into {best['name']} ({best['sector']}, "
                    f"screener score {best['score']})."
                ),
            })
        else:
            base.update({
                "action": "EXIT",
                "sell_ticker": sell["ticker"], "sell_name": sell["name"],
                "buy_ticker": None, "buy_name": None, "buy_sector": None,
                "reasoning": rsn + "Bank the gains; reinvest next month when screener picks are ready.",
            })
    elif watch_candidates:
        w = watch_candidates[0]
        base.update({
            "action": "HOLD",
            "sell_ticker": None, "buy_ticker": None,
            "reasoning": (
                f"{w['name']} is in WATCH territory (PnL {(w.get('pnl_pct') or 0):+.0f}%). "
                f"{w.get('rebalancer_reason', '')} Not strong enough to act yet — monitor next month."
            ),
        })
    else:
        base.update({
            "action": "HOLD",
            "sell_ticker": None, "buy_ticker": None,
            "reasoning": (
                "Rebalancer sees no exit signals this month. "
                "All holdings are within normal range — no profit-booking stage triggered, "
                "no stop-loss near breach. Hold all positions."
            ),
        })
    return base


def _post_to_api(path: str, payload: dict):
    """POST payload to API endpoint (non-fatal if fails)."""
    try:
        body = json.dumps(payload).encode("utf-8")
        req = _urllib.Request(
            f"{API_URL}{path}", data=body,
            headers={"Content-Type": "application/json", **_UPLOAD_AUTH},
            method="POST",
        )
        with _urllib.urlopen(req, timeout=10) as resp:
            print(f"  ✅ Posted to {path}: {resp.read().decode()[:60]}")
    except Exception as e:
        print(f"  ⚠️  Could not POST to {path}: {e}")


# ─────────────────────────────────────────────
# AUDIT TRAIL GENERATOR (5.2)
# ─────────────────────────────────────────────

def generate_audit_trail(row: dict) -> dict:
    """
    Generate a plain English explanation of why a stock was picked
    and what risks to watch. Uses only data already in the scored row.
    """
    why      = []
    risks    = []
    adjs     = []
    score_bd = {}

    peg   = row.get("peg_raw")
    roe   = row.get("roe_raw")
    rev_g = row.get("revenue_growth_raw")
    debt  = row.get("debt_raw")

    for dim, label in [
        ("peg_score",            "PEG"),
        ("roe_score",            "ROE"),
        ("revenue_growth_score", "Revenue Growth"),
        ("debt_score",           "Debt Level"),
        ("momentum_score",       "Momentum"),
    ]:
        score = row.get(dim)
        if score is not None:
            score_bd[label] = round(score, 0)

    # ── Why picked ────────────────────────────────────────────
    if rev_g is not None:
        if rev_g >= 25:
            why.append(f"Exceptional revenue growth ({rev_g:.1f}% YoY) — top of universe")
        elif rev_g >= 15:
            why.append(f"Strong revenue growth ({rev_g:.1f}% YoY)")
        elif rev_g >= 8:
            why.append(f"Solid revenue growth ({rev_g:.1f}% YoY)")

    if roe is not None:
        if roe >= 25:
            why.append(f"Excellent ROE ({roe:.1f}%) — highly efficient capital use")
        elif roe >= 18:
            why.append(f"Strong ROE ({roe:.1f}%)")
        elif roe >= 12:
            why.append(f"Acceptable ROE ({roe:.1f}%)")

    if peg is not None:
        if peg < 1.0:
            why.append(f"Undervalued for growth — PEG {peg:.2f} (< 1.0 is attractive)")
        elif peg < 2.0:
            why.append(f"Reasonably valued — PEG {peg:.2f}")

    m1 = row.get("momentum_1m", 0) or 0
    m3 = row.get("momentum_3m", 0) or 0
    m6 = row.get("momentum_6m", 0) or 0
    if m3 >= 15 and m6 >= 20:
        why.append(f"Strong medium-term momentum: +{m3:.1f}% (3M), +{m6:.1f}% (6M)")
    elif m3 >= 8 or m6 >= 15:
        why.append(f"Positive medium-term momentum: +{m3:.1f}% (3M), +{m6:.1f}% (6M)")
    elif m1 >= 5:
        why.append(f"Recent positive momentum: +{m1:.1f}% (1M), {m3:+.1f}% (3M)")

    insider = row.get("insider_pct")
    if insider is not None and insider >= 50:
        why.append(f"Strong promoter conviction ({insider:.1f}% holding)")

    et = row.get("earnings_trend", "")
    if et == "improving":
        why.append("Earnings trend improving QoQ")
    elif et == "stable":
        why.append("Earnings stable — no deterioration")

    mt = row.get("margin_trend", "")
    if mt == "expanding":
        why.append("Margins expanding — profit growing faster than revenue")

    it = row.get("inst_trend", "")
    if it == "accumulating":
        why.append("Institutional investors accumulating this stock")
    elif it == "well_covered":
        hc = row.get("holder_count")
        why.append(f"Well covered by institutions ({hc} holders)")

    sentiment = row.get("sector_sentiment", "neutral")
    sector    = row.get("nse_sector", "")
    if sentiment == "positive":
        why.append(f"Sector '{sector}' has positive news sentiment")
    elif sentiment == "mild_positive":
        why.append(f"Sector '{sector}' has mild positive news sentiment")

    # ── Adjustments applied ───────────────────────────────────
    fp  = row.get("freshness_penalty", 0) or 0
    na  = row.get("net_adjustment",    0) or 0
    np_ = row.get("net_promoter_adj",  0) or 0
    ni  = row.get("net_inst_adj",      0) or 0
    cp  = row.get("circuit_penalty",   0) or 0
    sa  = row.get("sentiment_adj",     0.0) or 0.0

    if fp != 0:
        adjs.append(f"Earnings freshness: {-fp:+.0f} pts ({row.get('earnings_trend','')})")
    if na != 0:
        adjs.append(f"Margin health: {na:+.0f} pts ({row.get('margin_trend','')})")
    if np_ != 0:
        adjs.append(f"Promoter signal: {np_:+.0f} pts ({row.get('promoter_signal','')})")
    if ni != 0:
        adjs.append(f"Institutional trend: {ni:+.0f} pts ({row.get('inst_trend','')})")
    if cp != 0:
        adjs.append(f"Circuit risk: {-cp:+.0f} pts ({row.get('circuit_risk','')})")
    if sa != 0.0:
        adjs.append(f"Sentiment ({sentiment}): {sa:+.1f} pts")

    # ── Risks ─────────────────────────────────────────────────
    if debt is not None and debt > 2.5:
        risks.append(f"D/E {debt:.2f} — elevated debt (max: 3.0)")

    price_pos = row.get("price_position_52w")
    if price_pos is not None and price_pos > 0.85:
        risks.append(f"Trading at {price_pos*100:.0f}% of 52-week high — limited near-term upside")

    if row.get("earnings_miss"):
        risks.append("Missed earnings estimates last quarter — monitor next result")

    beta = row.get("beta")
    if beta and beta > 1.5:
        risks.append(f"High beta ({beta:.1f}) — volatile in market downturns")

    if insider is not None and insider < 20:
        risks.append(f"Low promoter holding ({insider:.1f}%) — limited insider conviction")

    cr = row.get("circuit_risk", "low")
    if cr in ("elevated", "high", "extreme"):
        risks.append(f"Circuit risk: {cr.title()} — ensure GTT stop-loss is set on Kite")

    age = row.get("data_age_days")
    if age and age > 120:
        risks.append(f"Fundamental data is {age} days old — verify before buying")

    pr = row.get("pledge_risk", "low")
    si = row.get("short_interest")
    if pr == "high":
        risks.append(f"High short interest ({si:.1f}%) — possible pledge cascade risk")
    elif pr == "elevated" and si is not None:
        risks.append(f"Elevated short interest ({si:.1f}%) — monitor promoter pledge")

    if row.get("dilution_flag"):
        sg = row.get("shares_growth")
        risks.append(f"Share dilution detected ({sg:+.1f}% YoY) — review capital usage")

    if sentiment == "cautious":
        risks.append(f"Sector '{sector}' has cautious news sentiment — monitor closely")

    # ── One-line summary ──────────────────────────────────────
    score      = row.get("final_score", 0)
    top_reason = why[0]   if why   else "Passed all filters with balanced scores"
    top_risk   = risks[0] if risks else "No significant risks identified"

    summary = (
        f"Score {score:.1f}/100 — "
        f"Primary driver: {top_reason}. "
        f"Main risk: {top_risk}."
    )

    return {
        "why_picked":      why   if why   else ["Balanced across all scoring dimensions"],
        "score_breakdown": score_bd,
        "adjustments":     adjs  if adjs  else ["No score adjustments applied"],
        "risks":           risks if risks else ["No significant risks identified"],
        "summary":         summary,
    }


# ─────────────────────────────────────────────
# PORTFOLIO VOLATILITY (3.4)
# ─────────────────────────────────────────────

DEFAULT_BETA               = 0.80
PORTFOLIO_BETA_BALANCED    = 1.0
PORTFOLIO_BETA_AGGRESSIVE  = 1.3
PORTFOLIO_BETA_OVERHEATED  = 1.6
STRESS_SCENARIO_PCT        = 15.0

def assess_portfolio_volatility(portfolio: dict) -> dict:
    """
    Calculate portfolio-level beta and volatility metrics.
    Uses beta values already stored in each stock's record — no extra API calls.
    """
    result = {
        "weighted_beta":    None,
        "beta_label":       "unknown",
        "est_max_drawdown": None,
        "warnings":         [],
        "health_summary":   "",
    }

    total_allocation = 0.0
    weighted_beta    = 0.0
    import math

    for bucket in portfolio.values():
        for s in bucket.get("stocks", []):
            beta      = s.get("beta")
            alloc_inr = s.get("allocation_inr", 0)

            if beta is None or (isinstance(beta, float) and math.isnan(beta)):
                beta   = DEFAULT_BETA
                s["beta"] = beta

            if alloc_inr > 0:
                weighted_beta    += beta * alloc_inr
                total_allocation += alloc_inr

    if total_allocation > 0:
        port_beta = round(weighted_beta / total_allocation, 2)
        result["weighted_beta"] = port_beta

        if port_beta < PORTFOLIO_BETA_BALANCED:
            result["beta_label"] = "conservative"
        elif port_beta < PORTFOLIO_BETA_AGGRESSIVE:
            result["beta_label"] = "balanced"
        elif port_beta < PORTFOLIO_BETA_OVERHEATED:
            result["beta_label"] = "aggressive"
        else:
            result["beta_label"] = "overheated"
            result["warnings"].append(
                f"Portfolio beta {port_beta:.2f} is overheated (>{PORTFOLIO_BETA_OVERHEATED}). "
                "Consider swapping one high-beta stock for a defensive pick."
            )

        est_drawdown = round(port_beta * STRESS_SCENARIO_PCT, 1)
        result["est_max_drawdown"] = est_drawdown

        if est_drawdown > 25:
            result["warnings"].append(
                f"Stress test: Nifty -15% → portfolio could fall -{est_drawdown}%. "
                "Ensure stop-losses are set on Kite."
            )

    beta_emoji = {
        "conservative": "🟢",
        "balanced":     "🟡",
        "aggressive":   "🟠",
        "overheated":   "🔴",
        "unknown":      "⚪",
    }.get(result["beta_label"], "⚪")

    if result["weighted_beta"] is not None:
        result["health_summary"] = (
            f"Portfolio Beta: {result['weighted_beta']:.2f}  "
            f"{beta_emoji} {result['beta_label'].title()}  |  "
            f"Stress Test (Nifty -15%): -{result['est_max_drawdown']}%"
        )
    else:
        result["health_summary"] = "Beta data insufficient for portfolio assessment."

    return result


# ─────────────────────────────────────────────
# REPORT PRINTER
# ─────────────────────────────────────────────

def print_portfolio_report(portfolio: dict, vol: dict = None):
    print("\n" + "="*60)
    print("  📊 MONTHLY TOP 7 — NIFTY 500")
    print("="*60)

    if vol:
        print(f"\n  📊 PORTFOLIO HEALTH")
        print(f"  {vol['health_summary']}")
        for w in vol.get("warnings", []):
            print(f"  ⚠️  {w}")

    bucket = portfolio.get("top_picks", {})
    total_invested = 0

    sentiment_emoji = {
        "positive": "🟢", "mild_positive": "🟡", "neutral": "⚪",
        "cautious": "🟠", "negative": "🔴",
    }

    for rank, s in enumerate(bucket.get("stocks", []), 1):
        sec  = s.get("nse_sector", "Unknown")
        sent = s.get("sector_sentiment", "neutral")
        sadj = s.get("sentiment_adj", 0.0)
        semj = sentiment_emoji.get(sent, "⚪")

        print(f"\n  #{rank}  {s['ticker']:<20} Score: {s['final_score']:>5.1f}/100")
        print(f"    Name:        {s['name']}")
        print(f"    Sector:      {sec}  {semj} {sent.replace('_',' ').title()} ({sadj:+.1f} pts)")
        print(f"    Price:       ₹{s['price']:>10,.2f}")
        print(f"    Allocation:  ₹{s['allocation_inr']:>10,.0f}  (~{s['approx_shares']} shares)")
        print(f"    PE:          {s['pe_ratio']:<8}  PEG: {s['peg_ratio']:<8}  PB: {s['pb_ratio']}")
        print(f"    ROE:         {s['roe_pct']}%")
        print(f"    Rev Growth:  {s['rev_growth_pct']}%    D/E: {s['debt_equity']}")
        print(f"    Momentum:    1M: {s['momentum_1m']:+.1f}%   3M: {s['momentum_3m']:+.1f}%   6M: {s.get('momentum_6m', 0):+.1f}%")
        print(f"    ── Liquidity ─────────────────────────────────")
        print(f"    Avg Daily Vol: {s['adv_30d']:>12,.0f} shares/day")
        print(f"    Avg Daily Val: ₹{s['adtv_cr']:>9.1f} Cr/day")
        atr_label = f"₹{s['atr_14day']}" if s['atr_14day'] else "N/A"
        src_label = "" if s['atr_source'] == "ATR" else " ⚠️ fallback"
        print(f"    ── Kite GTT Stop-Loss Setup ──────────────────")
        print(f"    ATR (14-day):       {atr_label}{src_label}")
        print(f"    ATR Multiplier:     {s['atr_multiplier']}x")
        print(f"    ➡️  Set GTT at:     ₹{s['stop_loss_price']:,.2f}  ({s['stop_loss_pct']}% below buy)")
        print(f"    📈 Trail by:        ₹{s['trailing_stop_dist']:,.2f} below each new high")
        trend_emoji = {"improving": "📈", "stable": "➡️", "deteriorating": "⚠️", "unknown": "❓"}.get(s['earnings_trend'], "❓")
        print(f"    ── Earnings Health ───────────────────────────")
        print(f"    Last Result:   {s['last_reported_date']}  ({s['data_age_days']} days ago)")
        print(f"    Trend:         {trend_emoji} {s['earnings_trend'].title()}")
        if s['earnings_miss']:
            print(f"    Surprise:      ⚠️  Missed estimates last quarter")
        if s['freshness_penalty'] > 0:
            print(f"    Score Penalty: -{s['freshness_penalty']} pts applied")
        print(f"    Notes:         {s['earnings_notes']}")
        margin_emoji = {
            "expanding": "📈", "stable": "➡️", "compressing": "⚠️",
            "severe_compression": "🔴", "unknown": "❓",
        }.get(s.get("margin_trend", "unknown"), "❓")
        div = s.get("divergence")
        div_str = f"{div:+.1f}%" if div is not None else "N/A"
        adj_str = f"{s.get('net_adjustment', 0):+d} pts" if s.get('net_adjustment', 0) != 0 else "0 pts"
        print(f"    ── Margin Health ─────────────────────────────")
        print(f"    Margin Trend:  {margin_emoji} {s.get('margin_trend','unknown').replace('_',' ').title()}")
        print(f"    Rev/Profit Gap:{div_str}   Score Adj: {adj_str}")
        print(f"    Notes:         {s.get('margin_notes','')}")
        ins  = s.get("insider_pct")
        inst = s.get("institutional_pct")
        ins_str  = f"{ins:.1f}%"  if ins  is not None else "N/A"
        inst_str = f"{inst:.1f}%" if inst is not None else "N/A"
        prom_adj_str = f"{s.get('net_promoter_adj', 0):+d} pts" if s.get('net_promoter_adj', 0) != 0 else "0 pts"
        print(f"    ── Promoter & Institutional ──────────────────")
        print(f"    Promoter:      {ins_str}  ({s.get('promoter_signal','unknown').title()})")
        print(f"    Institutional: {inst_str}  ({s.get('institution_signal','unknown').title()})")
        print(f"    Score Adj:     {prom_adj_str}")
        print(f"    Notes:         {s.get('promoter_notes','')}")
        inst_trend_emoji = {
            "accumulating": "📈", "stable": "➡️", "distributing": "⚠️",
            "exiting_fast": "🔴", "well_covered": "✅", "moderate_coverage": "➡️",
            "low_coverage": "⚠️", "unknown": "❓",
        }.get(s.get("inst_trend", "unknown"), "❓")
        chg = s.get("inst_change_pp")
        chg_str     = f"{chg:+.1f}pp QoQ" if chg is not None else "No prior data"
        holders     = s.get("holder_count")
        holders_str = f"{holders} holders" if holders else "N/A"
        inst_adj    = s.get("net_inst_adj", 0)
        inst_adj_str = f"{inst_adj:+d} pts" if inst_adj != 0 else "0 pts"
        print(f"    ── Institutional Trend ───────────────────────")
        print(f"    Trend:         {inst_trend_emoji} {s.get('inst_trend','unknown').replace('_',' ').title()}")
        print(f"    Change:        {chg_str}   Holders: {holders_str}")
        print(f"    Score Adj:     {inst_adj_str}")
        print(f"    Notes:         {s.get('inst_trend_notes','')}")
        risk_emoji = {
            "low": "🟢", "moderate": "🟡", "elevated": "🟠",
            "high": "🔴", "extreme": "🔴",
        }.get(s.get("circuit_risk", "low"), "🟡")
        c_pen = s.get("circuit_penalty", 0)
        c_pen_str = f"-{c_pen} pts" if c_pen > 0 else "0 pts"
        print(f"    ── Circuit Risk ──────────────────────────────")
        print(f"    Risk Level:    {risk_emoji} {s.get('circuit_risk','low').title()}")
        print(f"    Penalty:       {c_pen_str}")
        print(f"    Notes:         {s.get('circuit_notes','')}")
        pledge_emoji = {
            "low": "🟢", "elevated": "🟠", "high": "🔴",
        }.get(s.get("pledge_risk", "low"), "🟢")
        si   = s.get("short_interest")
        sg   = s.get("shares_growth")
        si_str  = f"{si:.1f}% of float" if si is not None else "N/A"
        sg_str  = f"{sg:+.1f}% YoY"     if sg is not None else "N/A"
        padj    = s.get("net_pledge_adj", 0)
        padj_str = f"{padj:+d} pts" if padj else "0 pts"
        dil_str  = "⚠️ YES" if s.get("dilution_flag") else "No"
        print(f"    ── Pledge & Dilution ─────────────────────────")
        print(f"    Pledge Risk:   {pledge_emoji} {s.get('pledge_risk','low').title()}")
        print(f"    Short Interest:{si_str}   Shares Growth: {sg_str}")
        print(f"    Dilution:      {dil_str}   Score Adj: {padj_str}")
        print(f"    Notes:         {s.get('pledge_notes','')[:100]}")
        at = s.get("audit_trail", {})
        if at:
            print(f"    ── Why Picked ────────────────────────────────")
            for reason in at.get("why_picked", [])[:3]:
                print(f"    ✅ {reason}")
            print(f"    ── Score Breakdown ───────────────────────────")
            for dim, score in at.get("score_breakdown", {}).items():
                bar_filled = int(score / 10)
                bar = "█" * bar_filled + "░" * (10 - bar_filled)
                print(f"    {dim:<18} [{bar}] {score:.0f}/100")
            if at.get("adjustments") and at["adjustments"] != ["No score adjustments applied"]:
                print(f"    ── Score Adjustments ─────────────────────────")
                for adj in at["adjustments"]:
                    print(f"    {adj}")
            if at.get("risks") and at["risks"] != ["No significant risks identified"]:
                print(f"    ── Risks to Watch ────────────────────────────")
                for risk in at["risks"][:3]:
                    print(f"    ⚠️  {risk}")
        print()
        total_invested += s.get("allocation_inr", 0)

    print("="*60)
    print(f"  TOTAL DEPLOYED: ₹{total_invested:,.0f}")
    print(f"  REMAINING:      ₹{BUDGET - total_invested:,.0f} (keep as cash buffer)")
    print("="*60)


def print_full_ranking(all_df: pd.DataFrame):
    """Print full ranked list across all sectors (for reference)."""
    print("\n" + "="*60)
    print("  📋 FULL UNIVERSE RANKING (for reference)")
    print("="*60)

    if all_df.empty:
        print("  No stocks to display.")
        return

    display_cols = [
        "ticker", "nse_sector", "sector_sentiment", "final_score",
        "peg_raw", "pe_raw", "roe_raw", "revenue_growth_raw",
        "debt_raw", "momentum_1m", "momentum_3m", "momentum_6m", "adv_30d", "adtv_cr",
    ]
    col_rename = {
        "ticker":               "Ticker",
        "nse_sector":           "Sector",
        "sector_sentiment":     "Sentiment",
        "final_score":          "Score",
        "peg_raw":              "PEG",
        "pe_raw":               "PE",
        "roe_raw":              "ROE%",
        "revenue_growth_raw":   "RevGrowth%",
        "debt_raw":             "D/E",
        "momentum_1m":          "Mom1M%",
        "momentum_3m":          "Mom3M%",
        "momentum_6m":          "Mom6M%",
        "adv_30d":              "ADV(shares)",
        "adtv_cr":              "ADTV(₹Cr)",
    }

    cols = [c for c in display_cols if c in all_df.columns]
    show = all_df[cols].rename(columns=col_rename).round(2)
    show.index = range(1, len(show) + 1)
    print(show.to_string())


# ─────────────────────────────────────────────
# SAVE RESULTS
# ─────────────────────────────────────────────

def save_results(portfolio: dict, all_df: pd.DataFrame):
    """Save portfolio JSON, full-ranking CSV, and POST to API."""
    timestamp = datetime.now().strftime("%Y%m")
    os.makedirs(DATA_DIR, exist_ok=True)

    portfolio_path = os.path.join(DATA_DIR, f"portfolio_{timestamp}.json")
    with open(portfolio_path, "w") as f:
        json.dump(portfolio, f, indent=2, default=str)
    print(f"\n  ✅ Portfolio saved: {portfolio_path}")

    if not all_df.empty:
        ranking_path = os.path.join(DATA_DIR, f"ranking_all_{timestamp}.csv")
        all_df.to_csv(ranking_path, index=False)
        print(f"  ✅ Full ranking saved: {ranking_path}")

    def _post(url, payload_bytes):
        req = _urllib.Request(
            url, data=payload_bytes,
            headers={"Content-Type": "application/json", **_UPLOAD_AUTH},
            method="POST",
        )
        with _urllib.urlopen(req, timeout=15) as resp:
            return resp.read().decode()

    payload = json.dumps(portfolio, default=str).encode("utf-8")

    try:
        body = _post(f"{API_URL}/portfolio/picks/upload", payload)
        print(f"  ✅ Screener picks POSTed to API: {body}")
    except Exception as e:
        print(f"  ⚠️  Could not POST picks to API (non-fatal): {e}")

    try:
        _post(f"{API_URL}/portfolio/upload", payload)
    except Exception:
        pass

    try:
        with _urllib.urlopen(f"{API_URL}/portfolio/live", timeout=8) as r:
            existing_live = r.read().decode()
        if '"error"' in existing_live:
            body = _post(f"{API_URL}/portfolio/live/upload", payload)
            print(f"  ✅ Live portfolio seeded from picks (first run): {body}")
        else:
            print(f"  ℹ️  Live portfolio already exists — not overwriting")
    except Exception as e:
        print(f"  ⚠️  Could not check/seed live portfolio (non-fatal): {e}")

    return portfolio_path


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    portfolio, top_df, all_df = build_portfolio(BUDGET)
    vol = assess_portfolio_volatility(portfolio)
    print_portfolio_report(portfolio, vol)
    print_full_ranking(all_df)
    save_results(portfolio, all_df)
