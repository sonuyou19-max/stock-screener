"""
Indian Stock Market Screener
=============================
Fetches live data from Yahoo Finance (yfinance) for NSE-listed stocks,
scores them across fundamental + technical metrics, and picks the top
stocks per bucket for a given month.

Buckets:
  1. BFSI + IT        (Large Cap Anchor)
  2. Defence + Infra  (Mid Cap Growth)
  3. Green Energy + EV (High Conviction)
  4. FMCG + Pharma    (Defensive Balance)

Run monthly to refresh your portfolio picks.
"""

import yfinance as yf
import pandas as pd
import numpy as np
import json
import time
import warnings
from datetime import datetime, timedelta
from typing import Optional

from nse_universe import (
    fetch_nifty500,
    map_to_buckets,
    passes_fundamental_filters,
)
from macro_signals import get_macro_adjustments

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _load_prev_institutional(output_dir: str = "./outputs") -> dict:
    """
    Load institutional_pct per ticker from the most recent
    saved portfolio JSON. Used by 3.2 for QoQ comparison.

    Returns {ticker: institutional_pct} or {} if no prior file.
    """
    import glob
    import os

    patterns = [
        os.path.join(output_dir, "portfolio_*.json"),
        "/mnt/user-data/outputs/portfolio_*.json",
    ]

    files = []
    for pat in patterns:
        files.extend(glob.glob(pat))

    if not files:
        return {}

    # Most recent file
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
# CONFIGURATION
# ─────────────────────────────────────────────

BUDGET = 100_000          # Total corpus in INR
MONTHLY_REFRESH = True    # If True, re-screen every run

# ATR multipliers per bucket (controls stop-loss width)
# Higher multiplier = wider stop = more room for volatile stocks
ATR_MULTIPLIERS = {
    "BFSI_IT":        2.5,
    "DEFENCE_INFRA":  3.0,
    "GREEN_ENERGY_EV":3.5,
    "FMCG_PHARMA":    2.0,
}
ATR_PERIOD        = 14    # Standard ATR lookback in trading days
ATR_TRAIL_MULT    = 1.5   # Trailing stop = 1.5x ATR below peak price

# ── Liquidity Filters (1.2) ───────────────────
# Minimum 30-day Average Daily Volume (shares) per bucket
MIN_ADV = {
    "BFSI_IT":        500_000,   # 5 lakh shares/day
    "DEFENCE_INFRA":  200_000,   # 2 lakh shares/day
    "GREEN_ENERGY_EV":150_000,   # 1.5 lakh shares/day
    "FMCG_PHARMA":    100_000,   # Revised from 2L: high-priced pharma naturally trades fewer shares; value liquidity matters more
}
# Minimum Average Daily Traded Value in ₹ crore (applies to ALL buckets)
MIN_ADTV_CR = 5.0               # ₹5 crore/day minimum

BUCKETS = {
    "BFSI_IT": {
        "label": "🏦 Large Cap — BFSI + IT",
        "allocation_pct": 0.30,
        "picks": 3,
        "scoring_weights": {
            "peg_score":           0.20,   # 1.3: PEG replaces pure PE
            "roe_score":           0.25,
            "revenue_growth_score":0.20,
            "debt_score":          0.15,
            "momentum_score":      0.20,
        },
    },
    "DEFENCE_INFRA": {
        "label": "⚙️ Mid Cap — Defence + Infra",
        "allocation_pct": 0.30,
        "picks": 3,
        "scoring_weights": {
            "peg_score":           0.15,
            "roe_score":           0.20,
            "revenue_growth_score":0.30,
            "debt_score":          0.15,
            "momentum_score":      0.20,
        },
    },
    "GREEN_ENERGY_EV": {
        "label": "⚡ High Conviction — Green Energy + EV",
        "allocation_pct": 0.20,
        "picks": 2,
        "scoring_weights": {
            "peg_score":           0.10,
            "roe_score":           0.15,
            "revenue_growth_score":0.40,
            "debt_score":          0.10,
            "momentum_score":      0.25,
        },
    },
    "FMCG_PHARMA": {
        "label": "🌾 Defensive — FMCG + Pharma",
        "allocation_pct": 0.20,
        "picks": 2,
        "scoring_weights": {
            "peg_score":           0.25,
            "roe_score":           0.30,
            "revenue_growth_score":0.15,
            "debt_score":          0.20,
            "momentum_score":      0.10,
        },
    },
}

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
        # Fetch period + 5 extra days as buffer for weekends/holidays
        hist = stock.history(period=f"{period + 10}d")

        if hist.empty or len(hist) < period + 1:
            return None

        hist = hist.tail(period + 1).copy()

        # True Range components
        hist["prev_close"] = hist["Close"].shift(1)
        hist["tr1"] = hist["High"] - hist["Low"]
        hist["tr2"] = (hist["High"] - hist["prev_close"]).abs()
        hist["tr3"] = (hist["Low"]  - hist["prev_close"]).abs()
        hist["true_range"] = hist[["tr1", "tr2", "tr3"]].max(axis=1)

        # Drop the first row (no prev_close) and average the rest
        atr = hist["true_range"].iloc[1:].mean()

        return round(float(atr), 2)

    except Exception:
        return None


def compute_atr_stops(
    ticker: str,
    buy_price: float,
    bucket_key: str,
) -> dict:
    """
    Given a ticker, buy price, and bucket, return:
      - atr_14day          : raw ATR in ₹
      - stop_loss_price    : GTT price to set on Kite (buy_price - multiplier * ATR)
      - stop_loss_pct      : effective stop-loss as % (for reference)
      - trailing_stop_dist : how much to trail below peak (1.5 * ATR)

    Falls back to bucket-default fixed % if ATR fetch fails.
    """
    FALLBACK_PCT = {
        "BFSI_IT":        0.12,
        "DEFENCE_INFRA":  0.15,
        "GREEN_ENERGY_EV":0.18,
        "FMCG_PHARMA":    0.10,
    }

    atr = calculate_atr(ticker)
    multiplier = ATR_MULTIPLIERS.get(bucket_key, 3.0)

    if atr and atr > 0:
        stop_loss_price    = round(buy_price - (multiplier * atr), 2)
        trailing_stop_dist = round(ATR_TRAIL_MULT * atr, 2)
        stop_loss_pct      = round((buy_price - stop_loss_price) / buy_price * 100, 2)
        source             = "ATR"
    else:
        # Fallback to fixed % if ATR unavailable
        fallback_pct       = FALLBACK_PCT.get(bucket_key, 0.15)
        stop_loss_price    = round(buy_price * (1 - fallback_pct), 2)
        trailing_stop_dist = round(buy_price * 0.05, 2)   # 5% trail as fallback
        stop_loss_pct      = round(fallback_pct * 100, 2)
        atr                = None
        source             = "FALLBACK_FIXED_PCT"

    return {
        "atr_14day":          atr,
        "atr_multiplier":     multiplier,
        "stop_loss_price":    stop_loss_price,
        "stop_loss_pct":      stop_loss_pct,
        "trailing_stop_dist": trailing_stop_dist,
        "atr_source":         source,
    }


# ─────────────────────────────────────────────
# DATA FETCHER
# ─────────────────────────────────────────────

def fetch_stock_data(ticker: str, bucket_key: str = "") -> Optional[dict]:
    """Fetch fundamentals + price data for a single NSE ticker.
    Returns None if stock fails liquidity filter (1.2).
    """
    try:
        stock = yf.Ticker(ticker)
        info = stock.info

        # Skip if no meaningful data returned
        if not info or info.get("regularMarketPrice") is None:
            return None

        # Price & momentum
        hist = stock.history(period="6mo")
        if hist.empty or len(hist) < 20:
            return None

        # Get current price — try multiple sources to handle after-hours/stale data
        import math as _math
        current_price = None

        # Source 1: fast_info (most reliable for live/recent price)
        try:
            fi = stock.fast_info
            fp = getattr(fi, 'last_price', None) or getattr(fi, 'regularMarketPrice', None)
            if fp and not _math.isnan(float(fp)) and float(fp) > 0:
                current_price = float(fp)
        except Exception:
            pass

        # Source 2: info dict regularMarketPrice
        if not current_price:
            rmp = info.get("regularMarketPrice") or info.get("currentPrice")
            if rmp and not _math.isnan(float(rmp)) and float(rmp) > 0:
                current_price = float(rmp)

        # Source 3: last close from history (always non-zero for traded stocks)
        if not current_price:
            close_val = hist["Close"].dropna().iloc[-1] if not hist["Close"].dropna().empty else None
            if close_val and not _math.isnan(float(close_val)) and float(close_val) > 0:
                current_price = float(close_val)

        if not current_price:
            return None  # Cannot determine price — skip stock

        # ── LIQUIDITY FILTER (1.2) ────────────────────────────────
        # Check 1: Minimum Average Daily Volume (30-day)
        adv_30d = hist["Volume"].iloc[-30:].mean() if len(hist) >= 30 else hist["Volume"].mean()
        min_adv  = MIN_ADV.get(bucket_key, 150_000)

        # Check 2: Minimum Average Daily Traded Value (₹ crore)
        adtv_cr  = round((adv_30d * current_price) / 1e7, 2)   # in crores

        if adv_30d < min_adv:
            print(f"    ⛔ {ticker} excluded — ADV {adv_30d:,.0f} < min {min_adv:,.0f} shares/day")
            return None

        if adtv_cr < MIN_ADTV_CR:
            print(f"    ⛔ {ticker} excluded — ADTV ₹{adtv_cr:.1f}Cr < min ₹{MIN_ADTV_CR}Cr/day")
            return None
        # ─────────────────────────────────────────────────────────

        price_1m_ago = hist["Close"].iloc[-22] if len(hist) >= 22 else hist["Close"].iloc[0]
        price_3m_ago = hist["Close"].iloc[-66] if len(hist) >= 66 else hist["Close"].iloc[0]
        price_6m_ago = hist["Close"].iloc[0]

        momentum_1m = (current_price / price_1m_ago - 1) * 100
        momentum_3m = (current_price / price_3m_ago - 1) * 100
        momentum_6m = (current_price / price_6m_ago - 1) * 100

        # Volume trend (avg last 10 days vs avg last 30 days)
        vol_10d = hist["Volume"].iloc[-10:].mean()
        vol_30d = hist["Volume"].iloc[-30:].mean()
        volume_ratio = vol_10d / vol_30d if vol_30d > 0 else 1.0

        # 52-week high/low position (0=at low, 1=at high)
        high_52w = info.get("fiftyTwoWeekHigh", current_price)
        low_52w  = info.get("fiftyTwoWeekLow", current_price)
        price_position = (
            (current_price - low_52w) / (high_52w - low_52w)
            if high_52w != low_52w else 0.5
        )

        # ── PEG Ratio (1.3) ──────────────────────────────────
        pe         = info.get("trailingPE")
        earn_g_raw = info.get("earningsGrowth")           # decimal e.g. 0.25
        roe_raw    = info.get("returnOnEquity")            # decimal e.g. 0.18
        rev_g_raw  = info.get("revenueGrowth")            # decimal e.g. 0.12

        # Convert to % for filter checks and display
        roe_pct        = round(roe_raw * 100, 2)       if roe_raw    is not None else None
        earn_g_pct     = round(earn_g_raw * 100, 2)    if earn_g_raw is not None else None
        rev_g_pct      = round(rev_g_raw * 100, 2)     if rev_g_raw  is not None else None

        # PEG = PE / earnings_growth(%)
        # Valid only when both PE and growth are positive
        if pe and pe > 0 and earn_g_pct and earn_g_pct > 0:
            peg_ratio = round(pe / earn_g_pct, 2)
            peg_ratio = min(peg_ratio, 10.0)  # cap at 10 to prevent outliers
        else:
            peg_ratio = None

        return {
            "ticker":               ticker,
            "name":                 info.get("longName", ticker),
            "sector":               info.get("sector", "Unknown"),
            "industry":             info.get("industry", "Unknown"),
            "current_price":        round(current_price, 2),
            "market_cap_cr":        round(info.get("marketCap", 0) / 1e7, 0),
            # Raw ratios (for display)
            "pe_ratio":             pe,
            "forward_pe":           info.get("forwardPE"),
            "pb_ratio":             info.get("priceToBook"),
            "peg_ratio":            peg_ratio,             # 1.3
            # % versions (for filter checks in nse_universe)
            "roe_pct":              roe_pct,
            "earnings_growth_pct":  earn_g_pct,
            "revenue_growth_pct":   rev_g_pct,
            # Raw decimals (kept for backward compat)
            "roe":                  roe_raw,
            "revenue_growth":       rev_g_raw,
            "earnings_growth":      earn_g_raw,
            "debt_to_equity":       info.get("debtToEquity"),
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
            # ── Liquidity fields (1.2) ──
            "adv_30d":              round(adv_30d, 0),
            "adtv_cr":              adtv_cr,
            # ── Promoter / Institutional Holdings (3.1) ──
            # heldPercentInsiders  ≈ promoter + management holding
            # heldPercentInstitutions ≈ FII + DII + MF holding
            "insider_pct":          round(info.get("heldPercentInsiders", 0) * 100, 2)
                                    if info.get("heldPercentInsiders") is not None else None,
            "institutional_pct":    round(info.get("heldPercentInstitutions", 0) * 100, 2)
                                    if info.get("heldPercentInstitutions") is not None else None,
        }

    except Exception as e:
        return None


# ─────────────────────────────────────────────
# SCORING ENGINE
# ─────────────────────────────────────────────

def score_stock(row: dict, weights: dict) -> dict:
    """
    Score a stock 0-100 across 5 dimensions.
    Raw values returned here; normalisation happens after
    all stocks in a bucket are fetched.

    1.3: PE replaced with PEG ratio as valuation metric.
         Falls back to PE if PEG unavailable.
    """
    scores = {}

    # 1. PEG Score (1.3) — lower PEG = better
    #    Fallback to PE if PEG missing
    peg = row.get("peg_ratio")
    pe  = row.get("pe_ratio")

    if peg and peg > 0:
        scores["peg_raw"] = peg          # used for scoring
        scores["pe_raw"]  = pe           # kept for display only
    elif pe and pe > 0:
        # Fallback: treat PE/10 as a rough PEG proxy
        scores["peg_raw"] = round(pe / 10, 2)
        scores["pe_raw"]  = pe
    else:
        scores["peg_raw"] = None
        scores["pe_raw"]  = None

    # 2. ROE Score — higher = better
    roe = row.get("roe")
    scores["roe_raw"] = (roe * 100) if roe else None

    # 3. Revenue Growth Score — higher = better
    rg = row.get("revenue_growth")
    scores["revenue_growth_raw"] = (rg * 100) if rg is not None else None

    # 4. Debt Score — lower D/E = better
    de = row.get("debt_to_equity")
    scores["debt_raw"] = de if de is not None else None

    # 5. Momentum Score — composite of 1M + 3M momentum + volume surge
    m1 = row.get("momentum_1m", 0) or 0
    m3 = row.get("momentum_3m", 0) or 0
    vr = row.get("volume_ratio", 1.0) or 1.0
    scores["momentum_raw"] = (0.4 * m1) + (0.4 * m3) + (0.2 * (vr - 1) * 10)

    return scores


def normalise_and_compute_final(df: pd.DataFrame, weights: dict) -> pd.DataFrame:
    """Normalise raw scores to 0-100 and compute weighted final score."""

    def minmax(series, invert=False):
        """Min-max normalise; invert=True means lower raw = higher score."""
        clean = series.dropna()
        if clean.empty or clean.max() == clean.min():
            return pd.Series([50.0] * len(series), index=series.index)
        normed = (series - clean.min()) / (clean.max() - clean.min()) * 100
        if invert:
            normed = 100 - normed
        return normed.fillna(50)

    df["peg_score"]            = minmax(df["peg_raw"], invert=True)   # lower PEG = better (1.3)
    df["roe_score"]            = minmax(df["roe_raw"])
    df["revenue_growth_score"] = minmax(df["revenue_growth_raw"])
    df["debt_score"]           = minmax(df["debt_raw"], invert=True)  # lower debt = better
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
# CORRELATION ENGINE (2.1)
# ─────────────────────────────────────────────

def calculate_correlation_matrix(tickers: list[str], period: str = "60d") -> pd.DataFrame:
    """
    Fetch 60 days of daily closing prices for a list of tickers
    and return a correlation matrix of their daily returns.

    Returns empty DataFrame if fewer than 2 tickers have valid data.
    """
    print(f"    📐 Calculating correlations for {len(tickers)} stocks...")
    price_data = {}

    for ticker in tickers:
        try:
            hist = yf.Ticker(ticker).history(period=period)
            if not hist.empty and len(hist) >= 20:
                price_data[ticker] = hist["Close"]
            time.sleep(0.2)
        except Exception:
            continue

    if len(price_data) < 2:
        return pd.DataFrame()

    prices_df = pd.DataFrame(price_data).dropna(how="all")
    returns   = prices_df.pct_change().dropna()

    return returns.corr()


def select_low_correlation_picks(
    df: pd.DataFrame,
    n_picks: int,
    corr_matrix: pd.DataFrame,
    max_corr: float = 0.75,
) -> pd.DataFrame:
    """
    Pick top n_picks stocks by score while ensuring no two picks
    have correlation above max_corr.

    Logic:
      1. Always include the highest scoring stock
      2. For each next candidate (in score order), include only if
         its correlation with ALL already selected stocks < max_corr
      3. If not enough picks found, relax threshold to 0.85 and retry

    Returns a DataFrame of selected stocks (subset of input df).
    """
    # If no correlation data, fall back to simple top-n
    if corr_matrix.empty:
        print(f"    ⚠️  No correlation data — using top-{n_picks} by score")
        return df.head(n_picks)

    def _pick(threshold: float) -> list[str]:
        selected = []
        for _, row in df.iterrows():
            ticker = row["ticker"]
            if len(selected) >= n_picks:
                break

            if not selected:
                selected.append(ticker)
                continue

            # Check correlation with all already selected stocks
            corr_ok = True
            for sel in selected:
                if ticker in corr_matrix.index and sel in corr_matrix.index:
                    corr_val = corr_matrix.loc[ticker, sel]
                    if abs(corr_val) > threshold:
                        print(
                            f"    ↩️  {ticker} skipped — "
                            f"corr({ticker},{sel}) = {corr_val:.2f} > {threshold}"
                        )
                        corr_ok = False
                        break

            if corr_ok:
                selected.append(ticker)

        return selected

    # First pass — strict threshold
    picks = _pick(max_corr)

    # Relax if not enough picks found
    if len(picks) < n_picks:
        print(f"    ⚠️  Only {len(picks)} low-corr picks at {max_corr} — relaxing to 0.85")
        picks = _pick(0.85)

    # Final fallback — just take top n by score
    if len(picks) < n_picks:
        print(f"    ⚠️  Still short — falling back to top-{n_picks} by score")
        picks = df["ticker"].head(n_picks).tolist()

    # ── Sub-sector cap for BFSI_IT: max 2 banks ─────────────────
    # If 3+ picks are all banks, swap the lowest-scoring bank
    # for the best available non-bank (NBFC, Insurance, IT, Software)
    # This prevents 100% bank concentration when IT/NBFC options exist.
    #
    # Why: Banks share the same macro driver (RBI rate decisions).
    # A mix of bank + NBFC/Insurance reduces correlated downside risk.
    BANK_KEYWORDS = ("bank", "Bank")
    final_df = df[df["ticker"].isin(picks)].copy()

    bank_mask = final_df["industry"].str.lower().str.contains("bank", na=False)
    n_banks   = bank_mask.sum()

    if n_banks > 2 and len(picks) >= 3:
        # Find non-bank candidates not already selected
        non_bank_candidates = df[
            ~df["ticker"].isin(picks) &
            ~df["industry"].str.lower().str.contains("bank", na=False)
        ].head(5)  # top 5 non-banks by score

        if not non_bank_candidates.empty:
            # Remove the lowest-scoring bank from picks
            banks_in_picks = final_df[bank_mask].sort_values("final_score")
            weakest_bank   = banks_in_picks.iloc[0]["ticker"]
            best_nonbank   = non_bank_candidates.iloc[0]["ticker"]

            picks = [t for t in picks if t != weakest_bank] + [best_nonbank]
            final_df = df[df["ticker"].isin(picks)].copy()
            print(
                f"    🔄 Sub-sector cap: swapped bank {weakest_bank} "
                f"→ {best_nonbank} (max 2 banks rule)"
            )
        else:
            print(f"    ℹ️  All 3 picks are banks — no non-bank alternative passed filters")

    print(f"    ✅ Selected {len(picks)} diversified picks: {picks}")
    return final_df


# ─────────────────────────────────────────────
# EARNINGS FRESHNESS CHECKER (2.2)
# ─────────────────────────────────────────────

# Thresholds
STALE_DATA_DAYS      = 120   # flag if last result > 120 days ago
DETERIORATION_PCT    = 20    # flag if latest EPS down >20% vs prior quarter
MISS_THRESHOLD_PCT   = 10    # flag if missed estimates by >10%

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

        # ── Check 1: Data Freshness ───────────────────────────
        quarterly = stock.quarterly_financials
        if quarterly is None or quarterly.empty:
            result["notes"] = "No quarterly financials available"
            result["freshness_penalty"] = 5
            return result

        # Most recent quarter date
        last_date = quarterly.columns[0]
        if hasattr(last_date, "date"):
            last_date = last_date.date()
        age_days = (datetime.now().date() - last_date).days

        result["last_reported_date"] = str(last_date)
        result["data_age_days"]      = age_days

        if age_days > STALE_DATA_DAYS:
            result["notes"]            += f"Stale data ({age_days} days old). "
            result["freshness_penalty"] += 5

        # ── Check 2: EPS Quarter-on-Quarter Trend ─────────────
        earnings = stock.quarterly_earnings
        if earnings is not None and not earnings.empty and len(earnings) >= 2:
            # yfinance quarterly_earnings has Actual and Estimate columns
            if "Actual" in earnings.columns:
                eps_vals = earnings["Actual"].dropna()

                if len(eps_vals) >= 2:
                    latest_eps = float(eps_vals.iloc[0])
                    prior_eps  = float(eps_vals.iloc[1])

                    # Avoid division by zero
                    if prior_eps != 0:
                        qoq_change = (latest_eps - prior_eps) / abs(prior_eps) * 100
                    else:
                        qoq_change = 0

                    # Check for single deterioration
                    if qoq_change < -DETERIORATION_PCT:
                        result["notes"] += (
                            f"EPS down {abs(qoq_change):.1f}% QoQ "
                            f"(₹{latest_eps:.2f} vs ₹{prior_eps:.2f}). "
                        )

                        # Check for double deterioration (3 quarters needed)
                        if len(eps_vals) >= 3:
                            prior2_eps = float(eps_vals.iloc[2])
                            if prior2_eps != 0:
                                prev_change = (prior_eps - prior2_eps) / abs(prior2_eps) * 100
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
                        result["notes"]         += f"EPS up {qoq_change:.1f}% QoQ ✅. "
                    else:
                        result["earnings_trend"] = "stable"
                        result["notes"]         += f"EPS stable ({qoq_change:+.1f}% QoQ). "

                # ── Check 3: Earnings Surprise ────────────────
                if "Estimate" in earnings.columns:
                    estimates = earnings["Estimate"].dropna()
                    actuals   = earnings["Actual"].dropna()

                    if len(estimates) >= 1 and len(actuals) >= 1:
                        latest_actual   = float(actuals.iloc[0])
                        latest_estimate = float(estimates.iloc[0])

                        if latest_estimate != 0:
                            surprise_pct = (
                                (latest_actual - latest_estimate)
                                / abs(latest_estimate) * 100
                            )
                            if surprise_pct < -MISS_THRESHOLD_PCT:
                                result["earnings_miss"]      = True
                                result["freshness_penalty"] += 10
                                result["notes"]             += (
                                    f"Missed estimates by {abs(surprise_pct):.1f}% ⚠️. "
                                )
                            elif surprise_pct > 5:
                                result["notes"] += (
                                    f"Beat estimates by {surprise_pct:.1f}% ✅. "
                                )

        # Cap total penalty at 20 to avoid over-penalising
        result["freshness_penalty"] = min(result["freshness_penalty"], 20)

        if not result["notes"]:
            result["notes"] = "Earnings data looks healthy ✅"

    except Exception as e:
        result["notes"]            = f"Could not fetch earnings data: {e}"
        result["freshness_penalty"] = 3   # small penalty for missing data

    return result


# ─────────────────────────────────────────────
# MARGIN HEALTH CHECKER (2.3)
# ─────────────────────────────────────────────

# Thresholds
DIVERGENCE_WARN    = 15   # revenue growth outpacing profit growth by >15%
DIVERGENCE_SEVERE  = 30   # severe compression threshold
MARGIN_COMPRESS    = 2.0  # gross margin contraction >2% = warning
MARGIN_SEVERE      = 5.0  # gross margin contraction >5% = serious

def check_margin_health(data: dict) -> dict:
    """
    Compare revenue growth vs profit growth to detect margin compression.
    Uses data already fetched by fetch_stock_data() — no extra API call needed.

    Returns dict with:
      divergence       : revenue_growth% - profit_growth%
      margin_trend     : expanding | stable | compressing | severe_compression
      margin_penalty   : score points to deduct
      margin_bonus     : score points to add
      net_adjustment   : bonus - penalty (applied to final_score)
      notes            : plain English explanation
    """
    result = {
        "divergence":     None,
        "margin_trend":   "unknown",
        "margin_penalty": 0,
        "margin_bonus":   0,
        "net_adjustment": 0,
        "margin_notes":   "",
    }

    rev_g    = data.get("revenue_growth_pct")   # already in %
    profit_g = data.get("earnings_growth_pct")  # already in %
    gross_m  = data.get("gross_margin")         # decimal e.g. 0.42
    profit_m = data.get("profit_margin")        # decimal e.g. 0.12

    # ── Signal 1: Revenue vs Profit Growth Divergence ─────────
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
            # Profit growing faster than revenue = margin expansion
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

    # ── Signal 2: Gross Margin Level Check ────────────────────
    # yfinance only gives current gross margin, not historical
    # We use it as a quality signal — very low gross margin = cost pressure
    if gross_m is not None:
        gross_m_pct = round(gross_m * 100, 1)
        if gross_m_pct < 10:
            result["margin_penalty"] += 5
            result["margin_notes"]   += f"⚠️  Low gross margin ({gross_m_pct}%) — thin pricing power. "
        elif gross_m_pct > 40:
            result["margin_bonus"]   += 3
            result["margin_notes"]   += f"✅ Strong gross margin ({gross_m_pct}%). "

    # ── Signal 3: Net Profit Margin Quality ───────────────────
    if profit_m is not None:
        profit_m_pct = round(profit_m * 100, 1)
        if profit_m_pct < 5:
            result["margin_penalty"] += 3
            result["margin_notes"]   += f"⚠️  Thin net margin ({profit_m_pct}%). "
        elif profit_m_pct > 20:
            result["margin_bonus"]   += 2
            result["margin_notes"]   += f"✅ Strong net margin ({profit_m_pct}%). "

    # Cap penalty and bonus
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

    Returns score adjustment (bonus/penalty) based on:
      1. Promoter holding level
      2. Institutional holding level
      3. Combined conviction check
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

    # ── Signal 1: Promoter Holding Level ──────────────────────
    # Special exemption: Banks and Insurance companies are professionally
    # managed institutions — 0% promoter is structurally normal (e.g. Karur Vysya,
    # Federal Bank, LIC). We treat 0% promoter as neutral for these, not a red flag.
    industry_str = (data.get("industry") or "").lower()
    is_bank_or_insurance = any(k in industry_str for k in ("bank", "insurance", "life insurance"))

    if insider is not None:
        if is_bank_or_insurance and insider < PROMOTER_LOW:
            # Exempt: professionally managed bank/insurer with 0% promoter is normal
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

    # ── Signal 2: Institutional Holding Level ─────────────────
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

    # ── Signal 3: Combined conviction check ───────────────────
    if (insider is not None and insider >= PROMOTER_HIGH and
            instit is not None and instit >= INSTITUTION_HIGH):
        result["promoter_bonus"] += 2
        result["promoter_notes"] += "🏆 Double conviction — promoter + institutions both high. "

    if (insider is not None and insider < PROMOTER_LOW and
            instit is not None and instit < INSTITUTION_NORMAL):
        result["promoter_penalty"] += 3
        result["promoter_notes"]   += "⛔ Both promoter and institutional holding very low. "

    # Cap adjustments
    result["promoter_bonus"]   = min(result["promoter_bonus"], 10)
    result["promoter_penalty"] = min(result["promoter_penalty"], 10)
    result["net_promoter_adj"] = result["promoter_bonus"] - result["promoter_penalty"]

    if not result["promoter_notes"].strip():
        result["promoter_notes"] = "No holding data available."

    return result


# ─────────────────────────────────────────────
# INSTITUTIONAL TREND CHECKER (3.2)
# ─────────────────────────────────────────────

# QoQ change thresholds (percentage points)
INST_ACCUMULATING  =  2.0   # > +2pp QoQ = accumulating
INST_DISTRIBUTING  = -2.0   # < -2pp QoQ = distributing
INST_EXITING_FAST  = -5.0   # < -5pp QoQ = fast exit

def check_institutional_trend(
    ticker: str,
    current_inst_pct: Optional[float],
    prev_inst_pct: Optional[float] = None,
) -> dict:
    """
    Detect whether institutions are accumulating or distributing.

    Two-tier approach:
    Tier 1 — QoQ comparison if prev_inst_pct available (from last month's JSON)
    Tier 2 — Holder-level signals from yfinance institutional_holders

    Returns:
      inst_change_pp    : QoQ change in institutional % (pp)
      inst_trend        : accumulating | stable | distributing | exiting_fast
      inst_trend_bonus  : score points to add
      inst_trend_penalty: score points to deduct
      net_inst_adj      : bonus - penalty
      inst_trend_notes  : plain English explanation
      holder_count      : number of institutional holders
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

    # ── Tier 1: QoQ comparison from saved portfolio data ──────
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

    # ── Tier 2: yfinance holder count signal ──────────────────
    # When no prior data exists, use holder count as a proxy
    # More holders = broader institutional conviction
    try:
        stock   = yf.Ticker(ticker)
        holders = stock.institutional_holders

        if holders is not None and not holders.empty:
            holder_count = len(holders)
            result["holder_count"] = holder_count

            if prev_inst_pct is None:
                # First run — use holder count as signal
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

    # Cap adjustments
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
ADTV_STRESS_CR     = 8.0    # ₹8 Cr/day — warning zone above our ₹5 Cr minimum
DRAWDOWN_THRESHOLD = 0.40   # >40% below 52w high = significant drawdown

def check_circuit_risk(data: dict) -> dict:
    """
    Assess circuit breaker risk using already-fetched data.
    Zero extra API calls.

    Checks:
      1. Beta — high beta = more likely to hit circuits
      2. 52w drawdown — already deep below high = volatility history
      3. ADTV stress zone — liquid but not highly so
      4. Hard exclude: beta > 2.5 AND ADTV < ₹8 Cr

    Returns:
      circuit_risk     : low | moderate | elevated | high | extreme
      circuit_penalty  : score points to deduct
      circuit_exclude  : True if hard exclude triggered
      circuit_notes    : plain English explanation
    """
    result = {
        "circuit_risk":    "low",
        "circuit_penalty": 0,
        "circuit_exclude": False,
        "circuit_notes":   "",
    }

    beta        = data.get("beta")
    adtv_cr     = data.get("adtv_cr", 0) or 0
    price_pos   = data.get("price_position_52w")  # 0=at low, 1=at high
    current_price = data.get("current_price", 0)
    high_52w    = data.get("high_52w", current_price)

    # ── Signal 1: Beta check ──────────────────────────────────
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

    # ── Signal 2: 52-week drawdown ────────────────────────────
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

    # ── Signal 3: ADTV stress zone ────────────────────────────
    if 0 < adtv_cr < ADTV_STRESS_CR:
        result["circuit_penalty"] += 3
        if result["circuit_risk"] == "low":
            result["circuit_risk"] = "moderate"
        result["circuit_notes"] += (
            f"⚠️  ADTV ₹{adtv_cr:.1f}Cr — stress zone "
            f"(above minimum but thin). "
        )

    # ── Hard exclude: extreme beta + thin liquidity ───────────
    if beta and beta > BETA_EXTREME_RISK and adtv_cr < ADTV_STRESS_CR:
        result["circuit_exclude"] = True
        result["circuit_risk"]    = "extreme"
        result["circuit_notes"]  += (
            f"⛔ HARD EXCLUDE — beta {beta:.1f} + ADTV ₹{adtv_cr:.1f}Cr: "
            f"cannot safely exit on stop-loss. "
        )

    # Cap penalty
    result["circuit_penalty"] = min(result["circuit_penalty"], 12)

    if not result["circuit_notes"].strip():
        result["circuit_notes"] = "Circuit risk: low ✅"

    return result


# ─────────────────────────────────────────────
# PROMOTER PLEDGE / DILUTION CHECKER (5.5)
# ─────────────────────────────────────────────

SHORT_INTEREST_HIGH     = 5.0    # % of float — high short interest
SHORT_INTEREST_ELEVATED = 2.0    # % of float — elevated
FLOAT_RATIO_SUSPICIOUS  = 0.65   # float/outstanding > 0.65 with high promoter
DILUTION_THRESHOLD      = 5.0    # % YoY increase in shares outstanding

def check_pledge_dilution(ticker: str, data: dict) -> dict:
    """
    Flag promoter pledge risk (proxy) and share dilution.
    Makes one extra yfinance call per stock.

    Pledge risk proxy signals:
      1. Short interest % of float — institutional shorts betting on cascade
      2. Float ratio anomaly — high float despite high promoter stake

    Dilution signal:
      3. Shares outstanding growth YoY > 5%

    Returns:
      pledge_risk      : low | elevated | high
      dilution_flag    : True | False
      pledge_penalty   : score points to deduct
      dilution_penalty : score points to deduct
      net_pledge_adj   : total adjustment
      pledge_notes     : plain English explanation
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

        # ── Signal 1: Short interest as pledge proxy ───────────
        shares_float     = info.get("floatShares")
        shares_short     = info.get("sharesShort")
        short_pct_float  = info.get("shortPercentOfFloat")  # decimal e.g. 0.03 = 3%

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

        # ── Signal 2: Float ratio anomaly ─────────────────────
        shares_outstanding = info.get("sharesOutstanding")
        insider_pct        = data.get("insider_pct", 0) or 0

        if shares_float and shares_outstanding and shares_outstanding > 0:
            float_ratio = round(shares_float / shares_outstanding, 3)
            result["float_ratio"] = float_ratio

            # Suspicious: high promoter holding but float is large
            # Suggests some promoter shares are pledged and circulating
            expected_max_float = 1 - (insider_pct / 100)
            if float_ratio > FLOAT_RATIO_SUSPICIOUS and insider_pct > 40:
                result["pledge_penalty"] = max(result["pledge_penalty"], 5)
                if result["pledge_risk"] == "low":
                    result["pledge_risk"] = "elevated"
                result["pledge_notes"] += (
                    f"⚠️  Float ratio {float_ratio:.2f} elevated vs promoter "
                    f"holding {insider_pct:.1f}% — possible pledge activity. "
                )

        # ── Signal 3: Share dilution check ────────────────────
        # yfinance provides shares_outstanding from info
        # For YoY comparison we use the quarterly shares data
        try:
            quarterly = stock.quarterly_shares_full_time_employees
        except Exception:
            quarterly = None

        # Fallback: use sharesOutstanding vs implicit prior
        # yfinance info sometimes has sharesOutstandingForward
        shares_now  = info.get("sharesOutstanding")
        implied_old = info.get("sharesOutstandingForward")   # analyst estimate

        # Alternative: check from balance sheet if available
        try:
            bs = stock.quarterly_balance_sheet
            if bs is not None and not bs.empty:
                if "Ordinary Shares Number" in bs.index:
                    shares_series = bs.loc["Ordinary Shares Number"].dropna()
                    if len(shares_series) >= 2:
                        latest_shares = float(shares_series.iloc[0])
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

        # ── Final notes if nothing flagged ─────────────────────
        if not result["pledge_notes"].strip():
            result["pledge_notes"] = "No pledge or dilution concerns detected. ✅"

    except Exception as e:
        result["pledge_notes"] = f"Pledge/dilution check failed: {e}"

    # ── Net adjustment ─────────────────────────────────────────
    result["pledge_penalty"]   = min(result["pledge_penalty"], 8)
    result["dilution_penalty"] = min(result["dilution_penalty"], 5)
    result["net_pledge_adj"]   = -(result["pledge_penalty"] + result["dilution_penalty"])

    return result


# ─────────────────────────────────────────────
# BUCKET SCREENER
# ─────────────────────────────────────────────

def screen_bucket(
    bucket_key: str,
    bucket_config: dict,
    bucket_tickers: list[str],
    prev_institutional: dict = None,
) -> pd.DataFrame:
    """
    Fetch, filter, score, and rank all stocks in a bucket.

    Pipeline:
      1. yfinance data fetch
      2. Liquidity filter        (1.2)
      3. Fundamental filters     (1.3 / 1.4)
      4. Earnings freshness      (2.2)
      5. Margin health           (2.3)
      6. Promoter signal         (3.1)
      7. Institutional trend     (3.2)
      8. Circuit risk            (3.3)
      9. Score + normalise + all adjustments
    """
    label = bucket_config["label"]
    print(f"\n  Screening {label} ({len(bucket_tickers)} candidates)...")

    records           = []
    excluded_liq      = 0
    excluded_fund     = 0
    excluded_earnings = 0

    for ticker in bucket_tickers:
        data = fetch_stock_data(ticker, bucket_key=bucket_key)

        if data is None:
            excluded_liq += 1
            time.sleep(0.3)
            continue

        # ── Fundamental Filter Gate (1.3 / 1.4) ──────────────
        passed, reason = passes_fundamental_filters(data, bucket_key)
        if not passed:
            print(f"    ⛔ {ticker} excluded — {reason}")
            excluded_fund += 1
            time.sleep(0.3)
            continue

        # ── Earnings Freshness Check (2.2) ────────────────────
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

        # ── Margin Health Check (2.3) — uses already-fetched data ─
        margin = check_margin_health(data)
        data["divergence"]      = margin["divergence"]
        data["margin_trend"]    = margin["margin_trend"]
        data["margin_penalty"]  = margin["margin_penalty"]
        data["margin_bonus"]    = margin["margin_bonus"]
        data["net_adjustment"]  = margin["net_adjustment"]
        data["margin_notes"]    = margin["margin_notes"]

        # ── Promoter Signal Check (3.1) — uses already-fetched data ─
        promoter = check_promoter_signal(data)
        data["promoter_signal"]   = promoter["promoter_signal"]
        data["institution_signal"]= promoter["institution_signal"]
        data["promoter_bonus"]    = promoter["promoter_bonus"]
        data["promoter_penalty"]  = promoter["promoter_penalty"]
        data["net_promoter_adj"]  = promoter["net_promoter_adj"]
        data["promoter_notes"]    = promoter["promoter_notes"]

        # ── Institutional Trend Check (3.2) ───────────────────
        prev_pct = (prev_institutional or {}).get(ticker)
        inst_trend = check_institutional_trend(
            ticker,
            current_inst_pct = data.get("institutional_pct"),
            prev_inst_pct    = prev_pct,
        )
        time.sleep(0.2)
        data["inst_change_pp"]    = inst_trend["inst_change_pp"]
        data["inst_trend"]        = inst_trend["inst_trend"]
        data["net_inst_adj"]      = inst_trend["net_inst_adj"]
        data["inst_trend_notes"]  = inst_trend["inst_trend_notes"]
        data["holder_count"]      = inst_trend["holder_count"]

        # ── Circuit Risk Check (3.3) — uses already-fetched data ─
        circuit = check_circuit_risk(data)
        if circuit["circuit_exclude"]:
            print(f"    ⛔ {ticker} excluded — {circuit['circuit_notes'].strip()}")
            excluded_earnings += 1
            continue
        data["circuit_risk"]    = circuit["circuit_risk"]
        data["circuit_penalty"] = circuit["circuit_penalty"]
        data["circuit_notes"]   = circuit["circuit_notes"]

        # ── Pledge / Dilution Check (5.5) — one extra yfinance call ─
        pledge = check_pledge_dilution(ticker, data)
        time.sleep(0.3)
        data["pledge_risk"]     = pledge["pledge_risk"]
        data["dilution_flag"]   = pledge["dilution_flag"]
        data["short_interest"]  = pledge["short_interest"]
        data["float_ratio"]     = pledge["float_ratio"]
        data["shares_growth"]   = pledge["shares_growth"]
        data["net_pledge_adj"]  = pledge["net_pledge_adj"]
        data["pledge_notes"]    = pledge["pledge_notes"]

        scores = score_stock(data, bucket_config["scoring_weights"])
        records.append({**data, **scores})
        time.sleep(0.3)

    print(
        f"    📊 {len(records)} passed | "
        f"{excluded_liq} liquidity ⛔ | "
        f"{excluded_fund} fundamentals ⛔ | "
        f"{excluded_earnings} earnings ⛔"
    )

    if not records:
        print(f"  ⚠️  No stocks passed filters for {bucket_key}.")
        print(f"  ⚠️  Consider relaxing filters in nse_universe.BUCKET_FILTERS.")
        return pd.DataFrame()

    df = pd.DataFrame(records)
    df = normalise_and_compute_final(df, bucket_config["scoring_weights"])

    # Apply all score adjustments (2.2 + 2.3 + 3.1 + 3.2 + 3.3 + 5.5)
    if "freshness_penalty" in df.columns:
        df["final_score"] = (df["final_score"] - df["freshness_penalty"]).clip(lower=0)
    if "net_adjustment" in df.columns:
        df["final_score"] = (df["final_score"] + df["net_adjustment"]).clip(lower=0, upper=100)
    if "net_promoter_adj" in df.columns:
        df["final_score"] = (df["final_score"] + df["net_promoter_adj"]).clip(lower=0, upper=100)
    if "net_inst_adj" in df.columns:
        df["final_score"] = (df["final_score"] + df["net_inst_adj"]).clip(lower=0, upper=100)
    if "circuit_penalty" in df.columns:
        df["final_score"] = (df["final_score"] - df["circuit_penalty"]).clip(lower=0)
    if "net_pledge_adj" in df.columns:
        df["final_score"] = (df["final_score"] + df["net_pledge_adj"]).clip(lower=0)
    df = df.sort_values("final_score", ascending=False)

    return df


# ─────────────────────────────────────────────
# PORTFOLIO BUILDER
# ─────────────────────────────────────────────

def build_portfolio(budget: int = BUDGET) -> dict:
    """Run screener across all buckets and build final portfolio."""
    portfolio    = {}
    all_results  = {}

    print("\n" + "="*60)
    print("  INDIAN STOCK SCREENER — MONTHLY RUN")
    print(f"  Date: {datetime.now().strftime('%d %B %Y')}")
    print(f"  Budget: ₹{budget:,.0f}")
    print("="*60)

    # ── Step 1: Fetch Nifty 500 Universe ─────────────────────
    print("\n  Step 1: Building stock universe from Nifty 500...")
    nifty500_df     = fetch_nifty500()
    bucket_universe = map_to_buckets(nifty500_df)

    # ── Step 2: Macro Signal Adjustments (2.4/2.5/2.6) ───────
    print("\n  Step 2: Fetching macro signals...")
    base_allocations = {k: v["allocation_pct"] for k, v in BUCKETS.items()}
    macro            = get_macro_adjustments(base_allocations)
    adj_allocations  = macro["adjusted_allocations"]

    # ── Step 2b: Load previous institutional holdings (3.2) ───
    # Looks for the most recent saved portfolio JSON and extracts
    # institutional_pct per ticker for QoQ comparison
    prev_institutional = _load_prev_institutional()
    if prev_institutional:
        print(f"  📂 Loaded prior institutional holdings for {len(prev_institutional)} stocks")
    else:
        print(f"  📂 No prior institutional data — first run (Tier 2 only for 3.2)")

    # ── Step 3: Screen each bucket ───────────────────────────
    print("\n  Step 3: Screening each bucket...")
    for bucket_key, bucket_config in BUCKETS.items():
        tickers = bucket_universe.get(bucket_key, [])

        if not tickers:
            print(f"  ⚠️  No stocks mapped to {bucket_key}")
            continue

        df = screen_bucket(bucket_key, bucket_config, tickers, prev_institutional)
        all_results[bucket_key] = df

        if df.empty:
            continue

        n_picks    = bucket_config["picks"]
        # ── Use macro-adjusted allocation (2.4) ──────────────
        allocation = budget * adj_allocations.get(bucket_key, bucket_config["allocation_pct"])
        per_stock  = allocation / n_picks

        # ── Correlation-aware picking (2.1) ──────────────────
        # Build correlation matrix from all stocks that passed filters
        scored_tickers = df["ticker"].tolist()
        corr_matrix    = calculate_correlation_matrix(scored_tickers)
        top_picks      = select_low_correlation_picks(
            df, n_picks, corr_matrix, max_corr=0.75
        )

        # ── Minimum share count guard ─────────────────────────
        # For a ₹1L portfolio, buying fewer than 3 shares of any stock
        # is impractical (too concentrated, rounding risk).
        # Flag stocks where allocation buys < 3 shares.
        MIN_SHARES = 3
        flagged_low_shares = []
        for _, row in top_picks.iterrows():
            price = row.get("current_price", 0)
            if price > 0:
                approx_qty = int(per_stock // price)
                if approx_qty < MIN_SHARES:
                    flagged_low_shares.append(
                        f"{row['ticker']} (~{approx_qty} share{'s' if approx_qty != 1 else ''} "
                        f"@ ₹{price:,.0f} — consider skipping or increasing allocation)"
                    )
        if flagged_low_shares:
            print(f"  ⚠️  Low share count warning ({bucket_key}):")
            for msg in flagged_low_shares:
                print(f"      {msg}")

        portfolio[bucket_key] = {
            "label":                bucket_config["label"],
            "total_allocation":     allocation,
            "per_stock_allocation": per_stock,
            "macro_adjustment":     round(adj_allocations.get(bucket_key, bucket_config["allocation_pct"]) * 100, 1),
            "base_allocation":      round(bucket_config["allocation_pct"] * 100, 1),
            "stocks":               [],
        }

        for _, row in top_picks.iterrows():
            ticker    = row["ticker"]
            buy_price = row["current_price"]

            atr_stops = compute_atr_stops(ticker, buy_price, bucket_key)
            time.sleep(0.2)

            portfolio[bucket_key]["stocks"].append({
                "ticker":             ticker,
                "name":               row["name"],
                "price":              buy_price,
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
                "allocation_inr":     round(per_stock, 0),
                "approx_shares":      int(per_stock // buy_price) if buy_price > 0 else 0,
                # Liquidity (1.2)
                "adv_30d":            int(row.get("adv_30d", 0)),
                "adtv_cr":            row.get("adtv_cr", 0),
                # ATR Stop-Loss (1.1)
                "atr_14day":          atr_stops["atr_14day"],
                "atr_multiplier":     atr_stops["atr_multiplier"],
                "stop_loss_price":    atr_stops["stop_loss_price"],
                "stop_loss_pct":      atr_stops["stop_loss_pct"],
                "trailing_stop_dist": atr_stops["trailing_stop_dist"],
                "atr_source":         atr_stops["atr_source"],
                # Correlation (2.1)
                "corr_checked":       True,
                "max_corr_threshold": 0.75,
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
                # Portfolio volatility (3.4)
                "beta":               row.get("beta"),
                # Tax tracking (3.6)
                "buy_date":           datetime.now().strftime("%Y-%m-%d"),
                # Pledge / Dilution (5.5)
                "pledge_risk":        row.get("pledge_risk", "low"),
                "dilution_flag":      row.get("dilution_flag", False),
                "short_interest":     row.get("short_interest"),
                "shares_growth":      row.get("shares_growth"),
                "net_pledge_adj":     row.get("net_pledge_adj", 0),
                "pledge_notes":       row.get("pledge_notes", ""),
                # Audit trail (5.2)
                "audit_trail":        generate_audit_trail(row, bucket_key),
            })

    # ── Step 4: Portfolio volatility assessment (3.4) ────────
    vol_assessment = assess_portfolio_volatility(portfolio)
    print(f"\n  📊 {vol_assessment['health_summary']}")
    for w in vol_assessment["warnings"]:
        print(f"  ⚠️  {w}")

    return portfolio, all_results, macro, vol_assessment


# ─────────────────────────────────────────────
# AUDIT TRAIL GENERATOR (5.2)
# ─────────────────────────────────────────────

def generate_audit_trail(row: dict, bucket_key: str) -> dict:
    """
    Generate a plain English explanation of why a stock
    was picked and what risks to watch.

    Uses only data already in the scored row — no extra API calls.

    Returns:
      why_picked    : list of positive reasons
      score_breakdown: dict of dimension → score
      adjustments   : list of adjustments applied
      risks         : list of risk warnings
      summary       : one-line plain English verdict
    """
    why        = []
    risks      = []
    adjs       = []
    score_bd   = {}

    # ── Score breakdown ───────────────────────────────────────
    peg   = row.get("peg_raw")
    roe   = row.get("roe_raw")
    rev_g = row.get("revenue_growth_raw")
    debt  = row.get("debt_raw")

    # Map normalised 0-100 scores
    for dim, raw_val, label in [
        ("peg_score",            peg,   "PEG"),
        ("roe_score",            roe,   "ROE"),
        ("revenue_growth_score", rev_g, "Revenue Growth"),
        ("debt_score",           debt,  "Debt Level"),
        ("momentum_score",       row.get("momentum_raw"), "Momentum"),
    ]:
        score = row.get(dim)
        if score is not None:
            score_bd[label] = round(score, 0)

    # ── Why picked ────────────────────────────────────────────
    # Revenue growth
    if rev_g is not None:
        if rev_g >= 25:
            why.append(f"Exceptional revenue growth ({rev_g:.1f}% YoY) — top of bucket")
        elif rev_g >= 15:
            why.append(f"Strong revenue growth ({rev_g:.1f}% YoY)")
        elif rev_g >= 8:
            why.append(f"Solid revenue growth ({rev_g:.1f}% YoY)")

    # ROE
    if roe is not None:
        if roe >= 25:
            why.append(f"Excellent ROE ({roe:.1f}%) — highly efficient capital use")
        elif roe >= 18:
            why.append(f"Strong ROE ({roe:.1f}%)")
        elif roe >= 12:
            why.append(f"Acceptable ROE ({roe:.1f}%)")

    # PEG
    if peg is not None:
        if peg < 1.0:
            why.append(f"Undervalued for growth — PEG {peg:.2f} (< 1.0 is attractive)")
        elif peg < 2.0:
            why.append(f"Reasonably valued — PEG {peg:.2f}")

    # Momentum
    m1 = row.get("momentum_1m", 0) or 0
    m3 = row.get("momentum_3m", 0) or 0
    if m1 >= 10 and m3 >= 15:
        why.append(f"Strong price momentum: +{m1:.1f}% (1M), +{m3:.1f}% (3M)")
    elif m1 >= 5 or m3 >= 10:
        why.append(f"Positive momentum: +{m1:.1f}% (1M), +{m3:.1f}% (3M)")

    # Promoter
    insider = row.get("insider_pct")
    if insider is not None and insider >= 50:
        why.append(f"Strong promoter conviction ({insider:.1f}% holding)")

    # Earnings trend
    et = row.get("earnings_trend","")
    if et == "improving":
        why.append("Earnings trend improving QoQ")
    elif et == "stable":
        why.append("Earnings stable — no deterioration")

    # Margin trend
    mt = row.get("margin_trend","")
    if mt == "expanding":
        why.append("Margins expanding — profit growing faster than revenue")

    # Institutional trend
    it = row.get("inst_trend","")
    if it == "accumulating":
        why.append("Institutional investors accumulating this stock")
    elif it == "well_covered":
        hc = row.get("holder_count")
        why.append(f"Well covered by institutions ({hc} holders)")

    # ── Adjustments applied ───────────────────────────────────
    fp = row.get("freshness_penalty", 0) or 0
    na = row.get("net_adjustment", 0) or 0
    np = row.get("net_promoter_adj", 0) or 0
    ni = row.get("net_inst_adj", 0) or 0
    cp = row.get("circuit_penalty", 0) or 0

    if fp != 0:
        adjs.append(f"Earnings freshness: {-fp:+.0f} pts ({row.get('earnings_trend','')})")
    if na != 0:
        adjs.append(f"Margin health: {na:+.0f} pts ({row.get('margin_trend','')})")
    if np != 0:
        adjs.append(f"Promoter signal: {np:+.0f} pts ({row.get('promoter_signal','')})")
    if ni != 0:
        adjs.append(f"Institutional trend: {ni:+.0f} pts ({row.get('inst_trend','')})")
    if cp != 0:
        adjs.append(f"Circuit risk: {-cp:+.0f} pts ({row.get('circuit_risk','')})")

    # ── Risks ─────────────────────────────────────────────────
    # Debt
    if debt is not None:
        de_limits = {"BFSI_IT": 2.0, "DEFENCE_INFRA": 1.5, "GREEN_ENERGY_EV": 3.0, "FMCG_PHARMA": 0.5}
        limit = de_limits.get(bucket_key, 2.0)
        if debt > limit * 0.75:
            risks.append(f"D/E {debt:.2f} — elevated debt for sector (limit: {limit})")

    # 52-week position
    price_pos = row.get("price_position_52w")
    if price_pos is not None and price_pos > 0.85:
        risks.append(f"Trading at {price_pos*100:.0f}% of 52-week high — limited near-term upside")

    # Earnings miss
    if row.get("earnings_miss"):
        risks.append("Missed earnings estimates last quarter — monitor next result")

    # Beta
    beta = row.get("beta")
    if beta and beta > 1.5:
        risks.append(f"High beta ({beta:.1f}) — volatile in market downturns")

    # Promoter low
    if insider is not None and insider < 20:
        risks.append(f"Low promoter holding ({insider:.1f}%) — limited insider conviction")

    # Circuit risk
    cr = row.get("circuit_risk","low")
    if cr in ("elevated","high","extreme"):
        risks.append(f"Circuit risk: {cr.title()} — ensure GTT stop-loss is set on Kite")

    # Stale data
    age = row.get("data_age_days")
    if age and age > 120:
        risks.append(f"Fundamental data is {age} days old — verify before buying")

    # Pledge risk
    pr = row.get("pledge_risk", "low")
    si = row.get("short_interest")
    if pr == "high":
        risks.append(f"High short interest ({si:.1f}%) — possible pledge cascade risk")
    elif pr == "elevated":
        risks.append(f"Elevated short interest ({si:.1f}%) — monitor promoter pledge")

    # Dilution
    if row.get("dilution_flag"):
        sg = row.get("shares_growth")
        risks.append(f"Share dilution detected ({sg:+.1f}% YoY) — review capital usage")

    # ── One-line summary ──────────────────────────────────────
    score = row.get("final_score", 0)
    top_reason = why[0] if why else "Passed all filters with balanced scores"
    top_risk   = risks[0] if risks else "No significant risks identified"

    summary = (
        f"Score {score:.1f}/100 — "
        f"Primary driver: {top_reason}. "
        f"Main risk: {top_risk}."
    )

    return {
        "why_picked":      why if why else ["Balanced across all scoring dimensions"],
        "score_breakdown": score_bd,
        "adjustments":     adjs if adjs else ["No score adjustments applied"],
        "risks":           risks if risks else ["No significant risks identified"],
        "summary":         summary,
    }

PORTFOLIO_BETA_BALANCED    = 1.0
PORTFOLIO_BETA_AGGRESSIVE  = 1.3
PORTFOLIO_BETA_OVERHEATED  = 1.6
BUCKET_BETA_WARNING        = 1.8
STRESS_SCENARIO_PCT        = 15.0   # Nifty -15% stress test

def assess_portfolio_volatility(portfolio: dict) -> dict:
    """
    Calculate portfolio-level beta and volatility metrics
    after all stock picks are finalised.

    Uses beta values already stored in each stock's row data.
    No extra API calls.

    Returns:
      weighted_beta      : allocation-weighted portfolio beta
      beta_label         : conservative | balanced | aggressive | overheated
      est_max_drawdown   : estimated max loss in Nifty -15% scenario
      bucket_betas       : {bucket_key: avg_beta}
      warnings           : list of plain English warnings
      health_summary     : one-line health verdict
    """
    result = {
        "weighted_beta":    None,
        "beta_label":       "unknown",
        "est_max_drawdown": None,
        "bucket_betas":     {},
        "warnings":         [],
        "health_summary":   "",
    }

    total_allocation = 0.0
    weighted_beta    = 0.0
    all_betas        = []

    # Bucket-level median beta — used as fallback when a stock has no beta data
    BUCKET_DEFAULT_BETA = {
        "BFSI_IT":        0.85,
        "DEFENCE_INFRA":  0.80,
        "GREEN_ENERGY_EV":0.75,
        "FMCG_PHARMA":    0.50,
    }

    for bucket_key, bucket in portfolio.items():
        bucket_betas = []

        for s in bucket.get("stocks", []):
            beta      = s.get("beta")
            alloc_inr = s.get("allocation_inr", 0)

            # Substitute NaN/None beta with bucket default to prevent NaN propagation
            import math
            if beta is None or (isinstance(beta, float) and math.isnan(beta)):
                beta = BUCKET_DEFAULT_BETA.get(bucket_key, 0.80)
                s["beta"] = beta  # patch in-place so display also shows a value

            if alloc_inr > 0:
                weighted_beta    += beta * alloc_inr
                total_allocation += alloc_inr
                all_betas.append(beta)
                bucket_betas.append(beta)

        if bucket_betas:
            result["bucket_betas"][bucket_key] = round(
                sum(bucket_betas) / len(bucket_betas), 2
            )

    # ── Portfolio-level beta ──────────────────────────────────
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
                f"Consider swapping one high-beta stock for a defensive pick."
            )

        # ── Stress test ───────────────────────────────────────
        est_drawdown = round(port_beta * STRESS_SCENARIO_PCT, 1)
        result["est_max_drawdown"] = est_drawdown

        if est_drawdown > 25:
            result["warnings"].append(
                f"Stress test: Nifty -15% → portfolio could fall -{est_drawdown}%. "
                f"Ensure stop-losses are set on Kite."
            )

    # ── Bucket-level beta warnings ────────────────────────────
    for bucket_key, avg_beta in result["bucket_betas"].items():
        if avg_beta > BUCKET_BETA_WARNING:
            label = BUCKETS[bucket_key]["label"]
            result["warnings"].append(
                f"{label} bucket beta {avg_beta:.2f} is high (>{BUCKET_BETA_WARNING}). "
                f"Consider a lower-beta swap within this bucket."
            )

    # ── Health summary ────────────────────────────────────────
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

def print_portfolio_report(portfolio: dict, macro: dict = None, vol: dict = None):
    print("\n" + "="*60)
    print("  📊 FINAL PORTFOLIO — TOP PICKS THIS MONTH")
    print("="*60)

    # Macro signal summary (2.4)
    if macro:
        print(f"\n  🛢️  MACRO SIGNAL")
        print(f"  {macro['crude']['notes']}")
        print(f"  Signal: {macro['crude'].get('signal_key','neutral').replace('_',' ').title()}")
        if macro.get("fiidii") and not macro["fiidii"].get("error"):
            print(f"  📊 {macro['fiidii']['notes']}")

        # LLM verdict summary (4.5)
        verdict = macro.get("llm_verdict")
        if verdict:
            print(f"\n  🤖 LLM MACRO VERDICT")
            overall = verdict.get("overall_market","")
            if overall:
                print(f"  {overall}")
            emoji_map = {"Positive":"🟢","Cautious":"🟠","Neutral":"⚪","Negative":"🔴"}
            for bk, label in [
                ("BFSI_IT","BFSI+IT"),
                ("DEFENCE_INFRA","Defence"),
                ("GREEN_ENERGY_EV","GreenEnergy"),
                ("FMCG_PHARMA","FMCG/Pharma"),
            ]:
                bv = verdict.get(bk, {})
                v  = bv.get("verdict","Neutral")
                print(
                    f"  {label:<14} {emoji_map.get(v,'⚪')} {v:<10} "
                    f"| {bv.get('action','')[:55]}"
                )

    # Portfolio volatility summary (3.4)
    if vol:
        print(f"\n  📊 PORTFOLIO HEALTH")
        print(f"  {vol['health_summary']}")
        if vol["bucket_betas"]:
            print(f"  Bucket Betas:")
            beta_emoji_map = {
                (0, 1.0):   "🟢",
                (1.0, 1.3): "🟡",
                (1.3, 1.8): "🟠",
                (1.8, 99):  "🔴",
            }
            for bk, bv in vol["bucket_betas"].items():
                emoji = next((e for (lo, hi), e in beta_emoji_map.items() if lo <= bv < hi), "⚪")
                label = BUCKETS.get(bk, {}).get("label", bk)
                print(f"    {label:<35} Beta: {bv:.2f} {emoji}")
        for w in vol.get("warnings", []):
            print(f"  ⚠️  {w}")

    total_invested = 0

    for bucket_key, bucket in portfolio.items():
        base = bucket.get("base_allocation", "N/A")
        adj  = bucket.get("macro_adjustment", "N/A")
        print(f"\n{bucket['label']}")
        print(f"  Allocation: {base}% → {adj}% (macro adjusted)  |  ₹{bucket['total_allocation']:,.0f} total  |  ₹{bucket['per_stock_allocation']:,.0f}/stock")
        print("-" * 58)

        for s in bucket["stocks"]:
            print(f"  {s['ticker']:<20} Score: {s['final_score']:>5.1f}/100")
            print(f"    Name:        {s['name']}")
            print(f"    Price:       ₹{s['price']:>10,.2f}")
            print(f"    Allocation:  ₹{s['allocation_inr']:>10,.0f}  (~{s['approx_shares']} shares)")
            print(f"    PE:          {s['pe_ratio']:<8}  PEG: {s['peg_ratio']:<8}  PB: {s['pb_ratio']}")
            print(f"    ROE:         {s['roe_pct']}%")
            print(f"    Rev Growth:  {s['rev_growth_pct']}%    D/E: {s['debt_equity']}")
            print(f"    Momentum:    1M: {s['momentum_1m']:+.1f}%   3M: {s['momentum_3m']:+.1f}%")
            # Liquidity block
            print(f"    ── Liquidity ─────────────────────────────────")
            print(f"    Avg Daily Vol: {s['adv_30d']:>12,.0f} shares/day")
            print(f"    Avg Daily Val: ₹{s['adtv_cr']:>9.1f} Cr/day")
            # ATR Stop-Loss block
            atr_label = f"₹{s['atr_14day']}" if s['atr_14day'] else "N/A"
            src_label  = "" if s['atr_source'] == "ATR" else " ⚠️ fallback"
            print(f"    ── Kite GTT Stop-Loss Setup ──────────────────")
            print(f"    ATR (14-day):       {atr_label}{src_label}")
            print(f"    ATR Multiplier:     {s['atr_multiplier']}x")
            print(f"    ➡️  Set GTT at:     ₹{s['stop_loss_price']:,.2f}  ({s['stop_loss_pct']}% below buy)")
            print(f"    📈 Trail by:        ₹{s['trailing_stop_dist']:,.2f} below each new high")
            # Earnings Freshness block (2.2)
            trend_emoji = {"improving": "📈", "stable": "➡️", "deteriorating": "⚠️", "unknown": "❓"}.get(s['earnings_trend'], "❓")
            print(f"    ── Earnings Health ───────────────────────────")
            print(f"    Last Result:   {s['last_reported_date']}  ({s['data_age_days']} days ago)")
            print(f"    Trend:         {trend_emoji} {s['earnings_trend'].title()}")
            if s['earnings_miss']:
                print(f"    Surprise:      ⚠️  Missed estimates last quarter")
            if s['freshness_penalty'] > 0:
                print(f"    Score Penalty: -{s['freshness_penalty']} pts applied")
            print(f"    Notes:         {s['earnings_notes']}")
            # Margin Health block (2.3)
            margin_emoji = {
                "expanding":         "📈",
                "stable":            "➡️",
                "compressing":       "⚠️",
                "severe_compression":"🔴",
                "unknown":           "❓",
            }.get(s.get("margin_trend", "unknown"), "❓")
            div = s.get("divergence")
            div_str = f"{div:+.1f}%" if div is not None else "N/A"
            adj = s.get("net_adjustment", 0)
            adj_str = f"{adj:+d} pts" if adj != 0 else "0 pts"
            print(f"    ── Margin Health ─────────────────────────────")
            print(f"    Margin Trend:  {margin_emoji} {s.get('margin_trend','unknown').replace('_',' ').title()}")
            print(f"    Rev/Profit Gap:{div_str}   Score Adj: {adj_str}")
            print(f"    Notes:         {s.get('margin_notes','')}")
            # Promoter block (3.1)
            ins  = s.get("insider_pct")
            inst = s.get("institutional_pct")
            ins_str  = f"{ins:.1f}%"  if ins  is not None else "N/A"
            inst_str = f"{inst:.1f}%" if inst is not None else "N/A"
            adj  = s.get("net_promoter_adj", 0)
            adj_str = f"{adj:+d} pts" if adj != 0 else "0 pts"
            print(f"    ── Promoter & Institutional ──────────────────")
            print(f"    Promoter:      {ins_str}  ({s.get('promoter_signal','unknown').title()})")
            print(f"    Institutional: {inst_str}  ({s.get('institution_signal','unknown').title()})")
            print(f"    Score Adj:     {adj_str}")
            print(f"    Notes:         {s.get('promoter_notes','')}")
            # Institutional Trend block (3.2)
            trend_emoji = {
                "accumulating":    "📈",
                "stable":          "➡️",
                "distributing":    "⚠️",
                "exiting_fast":    "🔴",
                "well_covered":    "✅",
                "moderate_coverage":"➡️",
                "low_coverage":    "⚠️",
                "unknown":         "❓",
            }.get(s.get("inst_trend", "unknown"), "❓")
            chg = s.get("inst_change_pp")
            chg_str = f"{chg:+.1f}pp QoQ" if chg is not None else "No prior data"
            holders = s.get("holder_count")
            holders_str = f"{holders} holders" if holders else "N/A"
            inst_adj = s.get("net_inst_adj", 0)
            inst_adj_str = f"{inst_adj:+d} pts" if inst_adj != 0 else "0 pts"
            print(f"    ── Institutional Trend ───────────────────────")
            print(f"    Trend:         {trend_emoji} {s.get('inst_trend','unknown').replace('_',' ').title()}")
            print(f"    Change:        {chg_str}   Holders: {holders_str}")
            print(f"    Score Adj:     {inst_adj_str}")
            print(f"    Notes:         {s.get('inst_trend_notes','')}")
            # Circuit Risk block (3.3)
            risk_emoji = {
                "low":      "🟢",
                "moderate": "🟡",
                "elevated": "🟠",
                "high":     "🔴",
                "extreme":  "🔴",
            }.get(s.get("circuit_risk", "low"), "🟡")
            c_pen = s.get("circuit_penalty", 0)
            c_pen_str = f"-{c_pen} pts" if c_pen > 0 else "0 pts"
            print(f"    ── Circuit Risk ──────────────────────────────")
            print(f"    Risk Level:    {risk_emoji} {s.get('circuit_risk','low').title()}")
            print(f"    Penalty:       {c_pen_str}")
            print(f"    Notes:         {s.get('circuit_notes','')}")
            # ── Pledge / Dilution (5.5) ───────────────────────
            pledge_emoji = {
                "low":      "🟢",
                "elevated": "🟠",
                "high":     "🔴",
            }.get(s.get("pledge_risk","low"), "🟢")
            si   = s.get("short_interest")
            sg   = s.get("shares_growth")
            si_str = f"{si:.1f}% of float" if si is not None else "N/A"
            sg_str = f"{sg:+.1f}% YoY" if sg is not None else "N/A"
            padj = s.get("net_pledge_adj", 0)
            padj_str = f"{padj:+d} pts" if padj else "0 pts"
            dil_str = "⚠️ YES" if s.get("dilution_flag") else "No"
            print(f"    ── Pledge & Dilution ─────────────────────────")
            print(f"    Pledge Risk:   {pledge_emoji} {s.get('pledge_risk','low').title()}")
            print(f"    Short Interest:{si_str}   Shares Growth: {sg_str}")
            print(f"    Dilution:      {dil_str}   Score Adj: {padj_str}")
            print(f"    Notes:         {s.get('pledge_notes','')[:100]}")
            # ── Audit Trail (5.2) ─────────────────────────────
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
            total_invested += s["allocation_inr"]

    print("="*60)
    print(f"  TOTAL DEPLOYED: ₹{total_invested:,.0f}")
    print(f"  REMAINING:      ₹{BUDGET - total_invested:,.0f} (keep as cash buffer)")
    print("="*60)


def print_bucket_full_ranking(all_results: dict):
    """Print full ranked list for each bucket (for reference)."""
    print("\n" + "="*60)
    print("  📋 FULL BUCKET RANKINGS (for reference)")
    print("="*60)

    display_cols = [
        "ticker", "final_score", "peg_raw", "pe_raw", "roe_raw",
        "revenue_growth_raw", "debt_raw", "momentum_1m", "momentum_3m",
        "adv_30d", "adtv_cr"
    ]
    col_rename = {
        "ticker": "Ticker", "final_score": "Score",
        "peg_raw": "PEG", "pe_raw": "PE", "roe_raw": "ROE%",
        "revenue_growth_raw": "RevGrowth%",
        "debt_raw": "D/E", "momentum_1m": "Mom1M%", "momentum_3m": "Mom3M%",
        "adv_30d": "ADV(shares)", "adtv_cr": "ADTV(₹Cr)"
    }

    for bucket_key, df in all_results.items():
        if df.empty:
            continue
        label = BUCKETS[bucket_key]["label"]
        print(f"\n{label}")
        cols = [c for c in display_cols if c in df.columns]
        show = df[cols].rename(columns=col_rename).round(2)
        show.index = range(1, len(show) + 1)
        print(show.to_string())


def save_results(portfolio: dict, all_results: dict):
    """Save portfolio to JSON, CSV, and POST to API for dashboard."""
    import os as _os
    import urllib.request as _urllib
    import urllib.error as _urlerr

    timestamp = datetime.now().strftime("%Y%m")
    data_dir = _os.getenv("DATA_DIR", ".")
    _os.makedirs(data_dir, exist_ok=True)

    # Save portfolio JSON locally
    portfolio_path = _os.path.join(data_dir, f"portfolio_{timestamp}.json")
    with open(portfolio_path, "w") as f:
        json.dump(portfolio, f, indent=2, default=str)
    print(f"\n  ✅ Portfolio saved: {portfolio_path}")

    # Save full rankings CSV per bucket
    for bucket_key, df in all_results.items():
        if not df.empty:
            path = _os.path.join(data_dir, f"ranking_{bucket_key}_{timestamp}.csv")
            df.to_csv(path, index=False)
    print(f"  ✅ Full rankings saved as CSV files")

    # POST portfolio to API so dashboard can read it immediately
    api_url = _os.getenv("API_URL", "https://web-production-2d832.up.railway.app")
    upload_url = f"{api_url}/portfolio/upload"
    try:
        payload = json.dumps(portfolio, default=str).encode("utf-8")
        req = _urllib.Request(
            upload_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _urllib.urlopen(req, timeout=15) as resp:
            body = resp.read().decode()
            print(f"  ✅ Portfolio POSTed to API: {body}")
    except _urlerr.URLError as e:
        print(f"  ⚠️  Could not POST to API (non-fatal): {e}")
    except Exception as e:
        print(f"  ⚠️  API upload error (non-fatal): {e}")

    return portfolio_path


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    portfolio, all_results, macro, vol = build_portfolio(BUDGET)
    print_portfolio_report(portfolio, macro, vol)
    print_bucket_full_ranking(all_results)
    save_results(portfolio, all_results)
