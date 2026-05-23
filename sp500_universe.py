"""
S&P 500 Universe — Fetcher & Bucket Mapper
============================================
Fetches the live S&P 500 constituent list from a public GitHub CSV
and maps each stock to one of the 4 strategy buckets based on
GICS (Global Industry Classification Standard) sector.

No hardcoded tickers. Universe refreshes every 7 days from cache.
Exact equivalent of nse_universe.py for the US market.

Columns from GitHub CSV:
  Symbol | Security | GICS Sector | GICS Sub-Industry | ...
"""

import requests
import pandas as pd
import os
import json
import time
from datetime import datetime
from typing import Optional

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

GITHUB_CSV_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
CACHE_PATH    = "/tmp/sp500_cache_v2.csv"
CACHE_MAX_AGE = 7  # days — refresh universe weekly

# ── GICS Sector → Bucket Mapping ─────────────
# Maps S&P 500 GICS sector labels to our 4 bucket keys.
# Exact equivalent of SECTOR_BUCKET_MAP in nse_universe.py
SECTOR_BUCKET_MAP = {
    # TECH — full sectors that are entirely tech
    "Information Technology":   "TECH",

    # Communication Services — mostly tech (Google, Meta, Netflix)
    # but excludes telecom/broadcasting via sub-industry override below
    "Communication Services":   "TECH",

    # Consumer Discretionary intentionally NOT mapped here —
    # it's too broad (TJX, MCD, GM alongside AMZN).
    # Tech-adjacent ones are caught via SUBINDUSTRY_BUCKET_OVERRIDE below.

    # DEFENSIVE — genuinely defensive sectors only
    "Health Care":              "DEFENSIVE_DIV",
    "Consumer Staples":         "DEFENSIVE_DIV",
    "Financials":               "DEFENSIVE_DIV",
    "Utilities":                "DEFENSIVE_DIV",

    # Excluded entirely — too cyclical/volatile:
    # Consumer Discretionary, Materials, Energy, Industrials, Real Estate
}

# Sub-industry overrides — take priority over SECTOR_BUCKET_MAP
SUBINDUSTRY_BUCKET_OVERRIDE = {
    # Semiconductors + chips + memory + equipment → TECH
    "Semiconductors":                               "TECH",
    "Semiconductor Equipment":                      "TECH",
    "Semiconductors & Semiconductor Equipment":     "TECH",
    "Electronic Equipment & Instruments":           "TECH",
    "Electronic Manufacturing Services":            "TECH",
    "Technology Hardware, Storage & Peripherals":   "TECH",
    "Computer Hardware":                            "TECH",
    "Data Processing & Outsourced Services":        "TECH",
    "IT Consulting & Other Services":               "TECH",
    "Internet Services & Infrastructure":           "TECH",
    "Application Software":                         "TECH",
    "Systems Software":                             "TECH",

    # Tech-adjacent Consumer Discretionary → TECH
    "Broadline Retail":                             "TECH",   # AMZN, EBAY
    "Specialized Consumer Services":                "TECH",   # DASH

    # Exclude speculative Health Care sub-industries from DEFENSIVE_DIV
    # Biotechs, drug discovery, life sciences tools = not defensive
    "Biotechnology":                                None,
    "Life Sciences Tools & Services":              None,
    "Health Care Technology":                      None,
    "Wireless Telecommunication Services":          None,
    "Cable & Satellite":                            None,
    "Broadcasting":                                 None,
    "Publishing":                                   None,
}

# ── Fundamental Filters Per Bucket ───────────
# Exact mirror of BUCKET_FILTERS in nse_universe.py
# Thresholds adapted for US market characteristics:
#   - US tech PEs are naturally higher (growth premium)
#   - US debt levels reported differently (no crore conversion)
#   - Market cap in USD millions not INR crore
# ── Insider/Institution Thresholds ───────────
# Exported so screener_us.py can import them
INSIDER_HIGH      = 10.0
INSIDER_NORMAL    = 3.0
INSIDER_LOW       = 1.0
INSTITUTION_HIGH  = 70.0
INSTITUTION_NORMAL= 40.0

BUCKET_FILTERS = {
    "TECH": {
        "min_market_cap_usd_m":  2_000,
        "max_pe":                80,
        "max_pb":                20.0,
        "max_52w_proximity":     0.92,
        "min_roe":               None,       # many growth cos reinvest
        "min_revenue_growth":    8,
        "max_debt_equity":       400,
        "max_peg":               4.0,
        "min_profit_growth":     None,
        "max_price_usd":         200,
    },
    "DEFENSIVE_DIV": {
        "min_market_cap_usd_m":  20_000,    # $20B+ only — large stable companies
        "max_pe":                30,
        "max_pb":                8.0,
        "max_52w_proximity":     0.90,
        "min_roe":               12,
        "min_revenue_growth":    3,
        "max_debt_equity":       250,
        "max_peg":               2.5,
        "min_profit_growth":     3,
        "max_price_usd":         200,
        "min_profit_margin":     0.05,      # must have 5%+ net margin (excludes loss-makers)
        "max_beta":              1.2,       # low volatility — true defensive characteristic
    },
}


def fetch_sp500() -> pd.DataFrame:
    """
    Fetch S&P 500 constituents.
    Sources in order:
      1. Local cache (7-day)
      2. GitHub CSV (datasets/s-and-p-500-companies)
    """
    # ── Check cache ───────────────────────────────────────────
    if os.path.exists(CACHE_PATH):
        age_days = (datetime.now() - datetime.fromtimestamp(
            os.path.getmtime(CACHE_PATH)
        )).days
        if age_days < CACHE_MAX_AGE:
            try:
                df = pd.read_csv(CACHE_PATH)
                print(f"  📋 S&P 500 universe loaded from cache ({len(df)} stocks, {age_days}d old)")
                return df
            except Exception:
                pass

    def _standardise(df):
        """Standardise column names to Symbol/Security/GICS Sector/GICS Sub-Industry."""
        df.columns = [c.strip() for c in df.columns]
        rename_map = {}
        for col in df.columns:
            cl = col.lower()
            if "symbol" in cl or "ticker" in cl:
                rename_map[col] = "Symbol"
            elif "security" in cl or "company" in cl or ("name" in cl and "symbol" not in cl):
                rename_map[col] = "Security"
            elif ("gics sector" == cl) or ("gics" in cl and "sector" in cl and "sub" not in cl):
                rename_map[col] = "GICS Sector"
            elif "sub-industry" in cl or "subindustry" in cl:
                rename_map[col] = "GICS Sub-Industry"
        df = df.rename(columns=rename_map)
        for col in ["Symbol", "Security", "GICS Sector"]:
            if col not in df.columns:
                df[col] = "Unknown"
        if "GICS Sub-Industry" not in df.columns:
            df["GICS Sub-Industry"] = ""
        df["Symbol"] = df["Symbol"].str.strip().str.replace(".", "-", regex=False)
        return df

    # ── Fetch from GitHub CSV (reliable, no auth needed) ──────
    print("  🌐 Fetching S&P 500 universe from GitHub CSV...")
    try:
        import requests as _req
        from io import StringIO as _SIO
        url = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
        resp = _req.get(url, timeout=20)
        resp.raise_for_status()
        df = pd.read_csv(_SIO(resp.text))
        df = _standardise(df)
        df.to_csv(CACHE_PATH, index=False)
        print(f"  ✅ S&P 500 universe fetched: {len(df)} stocks")
        return df
    except Exception as e:
        print(f"  ⚠️  GitHub CSV fetch failed: {e}")

    # ── All sources failed ────────────────────────────────────
    print("  ❌ All S&P 500 sources failed. Returning empty DataFrame.")
    return pd.DataFrame(columns=["Symbol", "Security", "GICS Sector", "GICS Sub-Industry"])


def map_to_buckets(df: pd.DataFrame) -> dict:
    """
    Map S&P 500 stocks to our 4 buckets using GICS sector/sub-industry.
    Exact equivalent of map_to_buckets() in nse_universe.py.

    Returns {bucket_key: [list of ticker strings]}
    """
    buckets = {
        "TECH":          [],
        "DEFENSIVE_DIV": [],
    }

    unmapped = []

    for _, row in df.iterrows():
        symbol      = str(row.get("Symbol", "")).strip()
        sector      = str(row.get("GICS Sector", "")).strip()
        sub_industry= str(row.get("GICS Sub-Industry", "")).strip()

        if not symbol:
            continue

        # ── Sub-industry override first — EXACT match, case-insensitive ──
        # None = explicitly excluded from all buckets
        bucket = "UNSET"
        sub_lower = sub_industry.lower()
        for key, bk in SUBINDUSTRY_BUCKET_OVERRIDE.items():
            if key.lower() == sub_lower:
                bucket = bk  # could be None (explicit exclude) or a bucket key
                break

        # ── Sector-level mapping if no sub-industry override ─────────────
        if bucket == "UNSET":
            bucket = SECTOR_BUCKET_MAP.get(sector)  # None if not mapped

        # ── Add to bucket (skip if None = excluded) ───────────────────────
        if bucket and bucket in buckets:
            buckets[bucket].append(symbol)
        elif bucket is None:
            pass  # explicitly excluded
        else:
            unmapped.append(f"{symbol} ({sector} / {sub_industry})")

    # Print summary
    print(f"\n  📊 Bucket mapping summary:")
    for bk, tickers in buckets.items():
        print(f"    {bk:<20} {len(tickers):>3} stocks")
    if unmapped:
        print(f"    Unmapped: {len(unmapped)} stocks → {unmapped[:5]}{'...' if len(unmapped)>5 else ''}")

    return buckets


def passes_fundamental_filters(data: dict, bucket_key: str) -> tuple[bool, str]:
    """
    Check if a stock passes all fundamental filters for its bucket.
    Exact equivalent of passes_fundamental_filters() in nse_universe.py.

    Returns (True, "") if passes, (False, reason) if fails.
    """
    filters = BUCKET_FILTERS.get(bucket_key, {})

    price       = data.get("current_price", 0) or 0
    market_cap  = data.get("market_cap_usd_m", 0) or 0
    pe          = data.get("pe_ratio")
    pb          = data.get("pb_ratio")
    roe         = data.get("roe_pct")           # already in %
    rev_g       = data.get("revenue_growth_pct") # already in %
    de          = data.get("debt_to_equity")
    peg         = data.get("peg_ratio")
    profit_g    = data.get("earnings_growth_pct") # already in %
    price_pos   = data.get("price_position_52w")

    # ── Profit margin filter (excludes loss-makers / biotech) ────
    min_pm = filters.get("min_profit_margin")
    if min_pm is not None:
        pm = data.get("profit_margin")
        if pm is None or pm < min_pm:
            return False, f"Profit margin {(pm or 0)*100:.1f}% < min {min_pm*100:.0f}%"

    # ── Beta filter (ensures true defensive low-volatility) ───────
    max_beta = filters.get("max_beta")
    if max_beta is not None:
        beta = data.get("beta")
        if beta is not None and beta > max_beta:
            return False, f"Beta {beta:.2f} > max {max_beta} (too volatile for defensive)"

    # ── Price cap filter (ensures whole share fits in allocation) ─
    max_price = filters.get("max_price_usd")
    if max_price and data.get("current_price", 0) > max_price:
        return False, f"Price ${data['current_price']:.0f} > max ${max_price} (insufficient for whole share)"

    # ── Market cap filter ─────────────────────────────────────
    min_cap = filters.get("min_market_cap_usd_m", 0)
    if market_cap < min_cap:
        return False, f"Market cap ${market_cap:,.0f}M < min ${min_cap:,.0f}M"

    max_cap = filters.get("max_market_cap_usd_m")
    if max_cap and market_cap > max_cap:
        return False, f"Market cap ${market_cap:,.0f}M > max ${max_cap:,.0f}M"

    # ── PE filter ─────────────────────────────────────────────
    max_pe = filters.get("max_pe")
    if max_pe and pe is not None and pe > 0:
        if pe > max_pe:
            return False, f"PE {pe:.1f}x > max {max_pe}x"

    # ── PB filter ─────────────────────────────────────────────
    max_pb = filters.get("max_pb")
    if max_pb and pb is not None and pb > 0:
        if pb > max_pb:
            return False, f"PB {pb:.1f}x > max {max_pb}x"

    # ── 52-week proximity filter (avoid buying at peak) ───────
    max_prox = filters.get("max_52w_proximity")
    if max_prox and price_pos is not None:
        if price_pos > max_prox:
            return False, f"Price at {price_pos*100:.0f}% of 52w high > max {max_prox*100:.0f}%"

    # ── ROE filter ────────────────────────────────────────────
    min_roe = filters.get("min_roe")
    if min_roe and roe is not None:
        if roe < min_roe:
            return False, f"ROE {roe:.1f}% < min {min_roe}%"

    # ── Revenue growth filter ─────────────────────────────────
    min_rev_g = filters.get("min_revenue_growth")
    if min_rev_g and rev_g is not None:
        if rev_g < min_rev_g:
            return False, f"Revenue growth {rev_g:.1f}% < min {min_rev_g}%"

    # ── Debt/Equity filter ────────────────────────────────────
    # Note: yfinance reports D/E as percentage for US stocks (e.g. 150 = 1.5x)
    max_de = filters.get("max_debt_equity")
    if max_de and de is not None and de > 0:
        if de > max_de:
            return False, f"D/E {de:.0f} > max {max_de}"

    # ── PEG filter ────────────────────────────────────────────
    max_peg = filters.get("max_peg")
    if max_peg and peg is not None and peg > 0:
        if peg > max_peg:
            return False, f"PEG {peg:.2f} > max {max_peg}"

    # ── Profit growth filter ──────────────────────────────────
    min_profit_g = filters.get("min_profit_growth")
    if min_profit_g is not None and profit_g is not None:
        if profit_g < min_profit_g:
            return False, f"Profit growth {profit_g:.1f}% < min {min_profit_g}%"

    return True, ""
