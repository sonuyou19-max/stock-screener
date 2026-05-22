"""
S&P 500 Universe — Fetcher & Bucket Mapper
============================================
Fetches the live S&P 500 constituent list from Wikipedia
and maps each stock to one of the 4 strategy buckets based on
GICS (Global Industry Classification Standard) sector.

No hardcoded tickers. Universe refreshes every time screener runs.
Exact equivalent of nse_universe.py for the US market.

Columns from Wikipedia S&P 500 table:
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

SP500_URL     = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
CACHE_PATH    = "/tmp/sp500_cache_v2.csv"
CACHE_MAX_AGE = 7  # days — refresh universe weekly

# ── GICS Sector → Bucket Mapping ─────────────
# Maps S&P 500 GICS sector labels to our 4 bucket keys.
# Exact equivalent of SECTOR_BUCKET_MAP in nse_universe.py
SECTOR_BUCKET_MAP = {
    # AI + Cloud (equivalent of BFSI_IT)
    "Information Technology":           "AI_CLOUD",
    "Communication Services":           "AI_CLOUD",

    # Semiconductors (equivalent of DEFENCE_INFRA)
    # Note: semiconductors are a sub-industry of IT
    # We catch them via GICS Sub-Industry mapping below

    # High Growth Tech (equivalent of GREEN_ENERGY_EV)
    # Consumer Discretionary includes e-commerce, streaming, etc.
    "Consumer Discretionary":           "HIGH_GROWTH_TECH",

    # Defensive + Dividend (equivalent of FMCG_PHARMA)
    "Health Care":                      "DEFENSIVE_DIV",
    "Consumer Staples":                 "DEFENSIVE_DIV",
    "Financials":                       "DEFENSIVE_DIV",
    "Industrials":                      "DEFENSIVE_DIV",
    "Energy":                           "DEFENSIVE_DIV",
    "Utilities":                        "DEFENSIVE_DIV",
    "Real Estate":                      "DEFENSIVE_DIV",
    "Materials":                        "DEFENSIVE_DIV",
}

# ── GICS Sub-Industry overrides ───────────────
# Semiconductors are part of IT sector but belong in their own bucket.
# This mirrors how India's Defence/Infra was carved out of Capital Goods.
SUBINDUSTRY_BUCKET_OVERRIDE = {
    "Semiconductors":                   "SEMICONDUCTORS",
    "Semiconductor Equipment":          "SEMICONDUCTORS",
    "Semiconductors & Semiconductor Equipment": "SEMICONDUCTORS",
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
    "AI_CLOUD": {
        "min_market_cap_usd_m":  10_000,    # $10B minimum (large cap anchor)
        "max_pe":                80,         # Higher than India — US tech commands premium
        "max_pb":                20.0,
        "max_52w_proximity":     0.90,
        "min_roe":               10,         # Many cloud cos reinvest heavily
        "min_revenue_growth":    10,
        "max_debt_equity":       300,        # yfinance reports D/E as % for US (e.g. 150 = 1.5x)
        "max_peg":               3.0,
        "min_profit_growth":     5,          # Relaxed — many AI cos still scaling
    },
    "SEMICONDUCTORS": {
        "min_market_cap_usd_m":  5_000,     # $5B minimum
        "max_market_cap_usd_m":  None,      # No cap — NVDA, AVGO are mega-caps
        "max_pe":                50,
        "max_pb":                15.0,
        "max_52w_proximity":     0.90,
        "min_roe":               12,
        "min_revenue_growth":    8,
        "max_debt_equity":       200,
        "max_peg":               3.0,
        "min_profit_growth":     10,
    },
    "HIGH_GROWTH_TECH": {
        "min_market_cap_usd_m":  2_000,
        "max_pe":                60,
        "max_pb":                15.0,
        "max_52w_proximity":     0.92,
        "min_revenue_growth":    12,
        "max_debt_equity":       400,        # E-commerce/streaming often levered
        "max_peg":               4.0,
        "min_profit_growth":     0,          # Many growth cos barely profitable
    },
    "DEFENSIVE_DIV": {
        "min_market_cap_usd_m":  5_000,
        "max_pe":                30,
        "max_pb":                8.0,
        "max_52w_proximity":     0.90,
        "min_roe":               12,
        "min_revenue_growth":    3,
        "max_debt_equity":       250,
        "max_peg":               2.5,
        "min_profit_growth":     3,
    },
}


def fetch_sp500() -> pd.DataFrame:
    """
    Fetch S&P 500 constituents.
    Tries multiple sources in order:
      1. Local cache (7-day)
      2. Wikipedia via requests with browser User-Agent
      3. Alternative: datahub.io CSV (public domain)
      4. Alternative: GitHub-hosted CSV
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

    # ── Source 1: Wikipedia with browser User-Agent ───────────
    print("  🌐 Fetching S&P 500 universe from Wikipedia...")
    try:
        import requests as _req
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        resp = _req.get(SP500_URL, headers=headers, timeout=20)
        resp.raise_for_status()
        tables = pd.read_html(resp.text, attrs={"id": "constituents"})
        df = _standardise(tables[0])
        df.to_csv(CACHE_PATH, index=False)
        print(f"  ✅ S&P 500 universe fetched from Wikipedia: {len(df)} stocks")
        return df
    except Exception as e:
        print(f"  ⚠️  Wikipedia fetch failed: {e}")

    # ── Source 2: datahub.io public CSV ───────────────────────
    print("  🌐 Trying datahub.io fallback...")
    try:
        import requests as _req
        url = "https://pkgstore.datahub.io/core/s-and-p-500-companies/components_csv/data/components_csv.csv"
        resp = _req.get(url, timeout=20)
        resp.raise_for_status()
        from io import StringIO
        df = pd.read_csv(StringIO(resp.text))
        df = _standardise(df)
        df.to_csv(CACHE_PATH, index=False)
        print(f"  ✅ S&P 500 universe fetched from datahub.io: {len(df)} stocks")
        return df
    except Exception as e:
        print(f"  ⚠️  datahub.io fetch failed: {e}")

    # ── Source 3: GitHub-hosted CSV ───────────────────────────
    print("  🌐 Trying GitHub CSV fallback...")
    try:
        import requests as _req
        url = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
        resp = _req.get(url, timeout=20)
        resp.raise_for_status()
        from io import StringIO
        df = pd.read_csv(StringIO(resp.text))
        df = _standardise(df)
        df.to_csv(CACHE_PATH, index=False)
        print(f"  ✅ S&P 500 universe fetched from GitHub: {len(df)} stocks")
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
        "AI_CLOUD":         [],
        "SEMICONDUCTORS":   [],
        "HIGH_GROWTH_TECH": [],
        "DEFENSIVE_DIV":    [],
    }

    unmapped = []

    for _, row in df.iterrows():
        symbol      = str(row.get("Symbol", "")).strip()
        sector      = str(row.get("GICS Sector", "")).strip()
        sub_industry= str(row.get("GICS Sub-Industry", "")).strip()

        if not symbol:
            continue

        # ── Sub-industry override first (catches semiconductors) ──
        bucket = None
        for key, bk in SUBINDUSTRY_BUCKET_OVERRIDE.items():
            if key.lower() in sub_industry.lower():
                bucket = bk
                break

        # ── Sector-level mapping ──────────────────────────────
        if bucket is None:
            bucket = SECTOR_BUCKET_MAP.get(sector)

        if bucket:
            buckets[bucket].append(symbol)
        else:
            unmapped.append(f"{symbol} ({sector})")

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
