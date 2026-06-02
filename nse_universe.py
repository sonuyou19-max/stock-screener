"""
NSE Universe — Nifty 500 Fetcher & Sector Mapper
==================================================
Fetches the live Nifty 500 constituent list from niftyindices.com
and maps each stock to one of the 20 NSE sector classifications
used by the swing scanner and monthly screener.

No login required. No hardcoded tickers.
Universe refreshes every time the screener runs.

Columns in ind_nifty500list.csv:
  Company Name | Industry | Symbol | Series | ISIN Code
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

NIFTY500_URL  = "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"
CACHE_PATH    = "/tmp/nifty500_cache.csv"
CACHE_MAX_AGE = 7  # days

# ── Sector Mapping ────────────────────────────
# Maps NSE Industry labels from the CSV to the 20 exact NSE sector names
# used by the swing scanner's sector sentiment signals.
NSE_SECTOR_MAP = {
    # Direct matches (most common in CSV)
    "Financial Services":              "Financial Services",
    "Information Technology":          "Information Technology",
    "Oil Gas And Consumable Fuels":    "Oil Gas And Consumable Fuels",
    "Fast Moving Consumer Goods":      "Fast Moving Consumer Goods",
    "Healthcare":                      "Healthcare",
    "Automobile and Auto Components":  "Automobile and Auto Components",
    "Capital Goods":                   "Capital Goods",
    "Metals And Mining":               "Metals And Mining",
    "Consumer Durables":               "Consumer Durables",
    "Chemicals":                       "Chemicals",
    "Construction Materials":          "Construction Materials",
    "Power":                           "Power",
    "Telecommunication":               "Telecommunication",
    "Consumer Services":               "Consumer Services",
    "Services And Logistics":          "Services And Logistics",
    "Realty":                          "Realty",
    "Diversified And Infrastructure":  "Diversified And Infrastructure",
    "Textiles And Apparel":            "Textiles And Apparel",
    "Media And Entertainment":         "Media And Entertainment",
    "Paper And Forest Products":       "Paper And Forest Products",
    # Aliases / variant names that appear in the CSV
    "IT":                              "Information Technology",
    "FMCG":                            "Fast Moving Consumer Goods",
    "Pharmaceuticals & Biotechnology": "Healthcare",
    "Pharmaceuticals":                 "Healthcare",
    "Healthcare Services":             "Healthcare",
    "Automobiles":                     "Automobile and Auto Components",
    "Industrial Manufacturing":        "Capital Goods",
    "Defence":                         "Capital Goods",
    "Aerospace & Defence":             "Capital Goods",
    "Electrical Equipment":            "Capital Goods",
    "Construction":                    "Construction Materials",
    "Logistics":                       "Services And Logistics",
    "Services":                        "Services And Logistics",
    "Media & Entertainment":           "Media And Entertainment",
    "Textiles":                        "Textiles And Apparel",
    "Textiles & Apparel":              "Textiles And Apparel",
    "Diversified":                     "Diversified And Infrastructure",
    "Infrastructure":                  "Diversified And Infrastructure",
    "Oil, Gas & Consumable Fuels":     "Oil Gas And Consumable Fuels",
    "Gas":                             "Oil Gas And Consumable Fuels",
    "Forest Materials":                "Paper And Forest Products",
}

# ── Universal Fundamental Filters ────────────
# Single set applied to all sectors — scoring differentiates quality.
UNIVERSAL_FILTERS = {
    "min_market_cap_cr":  2_000,   # investable companies only
    "max_pe":             75,      # generous ceiling — covers growth sectors
    "max_pb":             12.0,    # allows brand/premium franchises
    "max_52w_proximity":  0.92,    # avoid buying near 52w peak
    "min_roe":            10,      # minimum capital efficiency bar
    "min_revenue_growth": 5,       # growing businesses only
    "max_debt_equity":    3.0,     # moderate leverage tolerance
    "max_peg":            4.5,     # allows growth premium
    "min_profit_growth":  0,       # excludes loss-making (negative earnings growth)
}

# Expose UNIVERSAL_FILTERS as BUCKET_FILTERS for any legacy callers
BUCKET_FILTERS = {"_universal": UNIVERSAL_FILTERS}

# Holdings thresholds — used by screener for display and scoring
INSIDER_HIGH   = 50.0
INSIDER_NORMAL = 35.0
INSIDER_LOW    = 20.0


# ─────────────────────────────────────────────
# CACHE HELPERS
# ─────────────────────────────────────────────

def _cache_is_fresh() -> bool:
    if not os.path.exists(CACHE_PATH):
        return False
    age_days = (time.time() - os.path.getmtime(CACHE_PATH)) / 86400
    return age_days < CACHE_MAX_AGE


def _load_cache() -> Optional[pd.DataFrame]:
    try:
        return pd.read_csv(CACHE_PATH)
    except Exception:
        return None


def _save_cache(df: pd.DataFrame):
    try:
        df.to_csv(CACHE_PATH, index=False)
    except Exception:
        pass


# ─────────────────────────────────────────────
# NIFTY 500 FETCHER
# ─────────────────────────────────────────────

def fetch_nifty500() -> pd.DataFrame:
    """
    Download Nifty 500 constituent list from niftyindices.com.
    Falls back to cached version if download fails.

    Returns DataFrame with columns:
      company_name | industry | symbol | nse_ticker
    """
    if _cache_is_fresh():
        cached = _load_cache()
        if cached is not None:
            print(f"  📋 Nifty 500 loaded from cache ({len(cached)} stocks)")
            return cached

    print(f"  🌐 Fetching Nifty 500 from niftyindices.com...")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/csv,application/csv,*/*",
        "Referer": "https://www.niftyindices.com/",
    }

    try:
        response = requests.get(NIFTY500_URL, headers=headers, timeout=20)
        response.raise_for_status()

        from io import StringIO
        df = pd.read_csv(StringIO(response.text))
        df.columns = [c.strip() for c in df.columns]

        df = df.rename(columns={
            "Company Name": "company_name",
            "Industry":     "industry",
            "Symbol":       "symbol",
            "Series":       "series",
            "ISIN Code":    "isin",
        })

        if "series" in df.columns:
            df = df[df["series"] == "EQ"].copy()

        df["nse_ticker"] = df["symbol"].str.strip() + ".NS"
        df["industry"]   = df["industry"].str.strip()
        df = df[["company_name", "industry", "symbol", "nse_ticker"]].copy()
        df = df.dropna(subset=["symbol", "industry"])

        print(f"  ✅ Fetched {len(df)} stocks from Nifty 500")
        _save_cache(df)
        return df

    except Exception as e:
        print(f"  ⚠️  Failed to fetch Nifty 500: {e}")
        print(f"  ⚠️  Attempting cached version...")
        cached = _load_cache()
        if cached is not None:
            print(f"  ✅ Loaded {len(cached)} stocks from cache (stale)")
            return cached
        print(f"  ❌ No cache available. Returning empty universe.")
        return pd.DataFrame(columns=["company_name", "industry", "symbol", "nse_ticker"])


# ─────────────────────────────────────────────
# SECTOR MAPPER
# ─────────────────────────────────────────────

def map_to_sectors(df: pd.DataFrame) -> dict[str, list[str]]:
    """
    Map Nifty 500 stocks to the 20 NSE sectors.
    Returns {sector_name: [list of nse_tickers]}
    """
    all_sectors = sorted(set(NSE_SECTOR_MAP.values()))
    sectors: dict[str, list[str]] = {s: [] for s in all_sectors}
    unmapped = []

    for _, row in df.iterrows():
        industry = row["industry"]
        ticker   = row["nse_ticker"]
        sector   = NSE_SECTOR_MAP.get(industry)

        if sector:
            sectors[sector].append(ticker)
        else:
            unmapped.append(industry)

    print(f"\n  📊 Sector Universe Sizes:")
    for sec, tickers in sorted(sectors.items(), key=lambda x: -len(x[1])):
        if tickers:
            print(f"    {sec:<40} {len(tickers):>3} stocks")

    unique_unmapped = set(unmapped)
    if unique_unmapped:
        print(f"\n  ℹ️  Unmapped industries ({len(unique_unmapped)}):")
        for ind in sorted(unique_unmapped):
            print(f"    - {ind}")

    return sectors


# Backward-compat alias — kept for any scripts that still import map_to_buckets
def map_to_buckets(df: pd.DataFrame) -> dict[str, list[str]]:
    return map_to_sectors(df)


# ─────────────────────────────────────────────
# FILTER CHECKER (pre-scoring gate)
# ─────────────────────────────────────────────

def passes_fundamental_filters(data: dict, bucket_key: str = "") -> tuple[bool, str]:
    """
    Check if a stock's yfinance data passes the universal fundamental filters.
    Returns (True, "") or (False, reason).

    bucket_key is accepted for backward compatibility but ignored —
    the same UNIVERSAL_FILTERS apply to all sectors.
    """
    f = UNIVERSAL_FILTERS

    mkt_cap_cr = data.get("market_cap_cr", 0) or 0

    if mkt_cap_cr < f["min_market_cap_cr"]:
        return False, f"Mkt cap ₹{mkt_cap_cr:.0f}Cr < min ₹{f['min_market_cap_cr']}Cr"

    pe = data.get("pe_ratio")
    if pe and pe > f["max_pe"]:
        return False, f"PE {pe:.1f} > max {f['max_pe']}"

    pb = data.get("pb_ratio")
    if pb is not None and pb > 0 and pb > f["max_pb"]:
        return False, f"PB {pb:.1f} > max {f['max_pb']} — overvalued on assets"

    price_pos = data.get("price_position_52w")
    if price_pos is not None and price_pos > f["max_52w_proximity"]:
        return False, (
            f"Price at {price_pos*100:.0f}% of 52w high "
            f"> max {f['max_52w_proximity']*100:.0f}% — near peak"
        )

    roe = data.get("roe_pct")
    if roe is not None and roe < f["min_roe"]:
        return False, f"ROE {roe:.1f}% < min {f['min_roe']}%"

    rev_g = data.get("revenue_growth_pct")
    if rev_g is not None and rev_g < f["min_revenue_growth"]:
        return False, f"Rev growth {rev_g:.1f}% < min {f['min_revenue_growth']}%"

    de = data.get("debt_to_equity")
    if de is not None and de > f["max_debt_equity"]:
        return False, f"D/E {de:.2f} > max {f['max_debt_equity']}"

    # Data quality gate — must have at least one growth metric
    earn_g = data.get("earnings_growth_pct")
    rev_g_check = data.get("revenue_growth_pct")
    industry_for_gate = (data.get("industry") or "").lower()
    is_financial = any(k in industry_for_gate for k in ("bank", "insurance", "financial"))
    if not is_financial and earn_g is None and rev_g_check is None:
        return False, "No growth data available — cannot verify fundamentals"

    # Loss-making gate
    pe_val_check = data.get("pe_ratio")
    earn_g_check = data.get("earnings_growth_pct")
    if pe_val_check is None and earn_g_check is not None and earn_g_check < 0:
        return False, f"Loss-making: no PE and earnings declining ({earn_g_check:.1f}%)"

    # PEG ceiling
    peg = data.get("peg_ratio")
    if peg is None:
        pe_val = data.get("pe_ratio")
        rev_g2 = data.get("revenue_growth_pct")
        if pe_val and pe_val > 0 and rev_g2 and rev_g2 > 0:
            peg = round(pe_val / rev_g2, 2)
    if peg is not None and peg > 0 and peg > f["max_peg"]:
        return False, f"PEG {peg:.2f} > max {f['max_peg']}"

    # Profit growth floor
    profit_g = data.get("earnings_growth_pct")
    if profit_g is not None and profit_g < f["min_profit_growth"]:
        return False, f"Profit growth {profit_g:.1f}% < min {f['min_profit_growth']}%"

    return True, ""


# ─────────────────────────────────────────────
# MAIN — for standalone testing
# ─────────────────────────────────────────────

if __name__ == "__main__":
    df = fetch_nifty500()
    print(df.head(10).to_string())
    print(f"\nTotal: {len(df)} stocks")

    sectors = map_to_sectors(df)
    for k, v in sorted(sectors.items()):
        if v:
            print(f"\n{k}: {v[:3]}...")
