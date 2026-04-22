# “””
NSE Universe — Nifty 500 Fetcher & Bucket Mapper

Fetches the live Nifty 500 constituent list from niftyindices.com
and maps each stock to one of the 4 strategy buckets based on
NSE’s Industry classification.

No login required. No hardcoded tickers.
Universe refreshes every time the screener runs.

Columns in ind_nifty500list.csv:
Company Name | Industry | Symbol | Series | ISIN Code
“””

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

NIFTY500_URL  = “https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv”
CACHE_PATH    = “/tmp/nifty500_cache.csv”
CACHE_MAX_AGE = 7  # days — refresh universe weekly

# ── Sector → Bucket Mapping ───────────────────

# Maps NSE Industry labels to our 4 bucket keys.

# NSE uses these exact strings in their CSV.

SECTOR_BUCKET_MAP = {
# BFSI + IT
“Financial Services”:               “BFSI_IT”,
“Information Technology”:           “BFSI_IT”,
“IT”:                               “BFSI_IT”,

```
# Defence + Infra
"Capital Goods":                    "DEFENCE_INFRA",
"Construction":                     "DEFENCE_INFRA",
"Construction Materials":           "DEFENCE_INFRA",
"Industrial Manufacturing":         "DEFENCE_INFRA",
"Defence":                          "DEFENCE_INFRA",
"Aerospace & Defence":              "DEFENCE_INFRA",

# Green Energy + EV
"Power":                            "GREEN_ENERGY_EV",
"Automobile and Auto Components":   "GREEN_ENERGY_EV",
"Automobiles":                      "GREEN_ENERGY_EV",
"Consumer Durables":                "GREEN_ENERGY_EV",
"Electrical Equipment":             "GREEN_ENERGY_EV",

# FMCG + Pharma
"Fast Moving Consumer Goods":       "FMCG_PHARMA",
"FMCG":                             "FMCG_PHARMA",
"Pharmaceuticals & Biotechnology":  "FMCG_PHARMA",
"Pharmaceuticals":                  "FMCG_PHARMA",
"Healthcare Services":              "FMCG_PHARMA",
"Healthcare":                       "FMCG_PHARMA",
```

}

# ── Fundamental Filters Per Bucket ───────────

# Stocks must pass ALL filters to enter scoring.

# These mirror the criteria we agreed on.

BUCKET_FILTERS = {
“BFSI_IT”: {
“min_market_cap_cr”:    20_000,
“max_pe”:               35,
“max_pb”:               5.0,      # 1.4: PB ceiling
“max_52w_proximity”:    0.90,     # 1.4: exclude if >90% of 52w high
“min_roe”:              15,
“min_revenue_growth”:   10,
“max_debt_equity”:      2.0,
“max_peg”:              2.5,
“min_profit_growth”:    8,
},
“DEFENCE_INFRA”: {
“min_market_cap_cr”:    5_000,
“max_market_cap_cr”:    40_000,
“max_pe”:               50,
“max_pb”:               8.0,      # 1.4
“max_52w_proximity”:    0.90,     # 1.4
“min_roe”:              12,
“min_revenue_growth”:   15,
“max_debt_equity”:      1.5,
“max_peg”:              3.0,
“min_profit_growth”:    12,
},
“GREEN_ENERGY_EV”: {
“min_market_cap_cr”:    2_000,
“max_pe”:               80,
“max_pb”:               10.0,     # 1.4
“max_52w_proximity”:    0.92,     # 1.4: slightly relaxed — sector is volatile
“min_revenue_growth”:   20,
“max_debt_equity”:      6.0,      # Raised from 3.0: solar developers use project-level
# debt in ring-fenced SPVs — Yahoo Finance reports
# consolidated D/E which inflates the parent metric
“max_peg”:              4.0,
“min_profit_growth”:    10,
},
“FMCG_PHARMA”: {
“min_market_cap_cr”:    10_000,
“max_pe”:               65,       # Revised: FMCG/Pharma 5yr avg PE is 52x; 65 allows premium without buying junk
“max_pb”:               12.0,     # Unchanged: FMCG brands justify high PB
“max_52w_proximity”:    0.90,     # Unchanged: don’t buy near peaks
“min_roe”:              18,       # Revised from 20: still screens weak businesses; 20 was too tight
“min_revenue_growth”:   8,        # Unchanged: only growing businesses
“max_debt_equity”:      1.5,      # Revised from 0.5: pharma D/E 0.04–0.58 operationally; 1.5 excludes truly leveraged
“max_peg”:              3.0,      # Unchanged
“min_profit_growth”:    10,       # Unchanged: profit discipline non-negotiable
},
}

# ─────────────────────────────────────────────

# CACHE HELPERS

# ─────────────────────────────────────────────

def _cache_is_fresh() -> bool:
“”“Return True if cached CSV exists and is less than CACHE_MAX_AGE days old.”””
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
“””
Download Nifty 500 constituent list from niftyindices.com.
Falls back to cached version if download fails.

```
Returns DataFrame with columns:
  company_name | industry | symbol | nse_ticker
"""
# Use cache if fresh
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

    # Normalise column names
    df.columns = [c.strip() for c in df.columns]

    # Expected: Company Name, Industry, Symbol, Series, ISIN Code
    df = df.rename(columns={
        "Company Name": "company_name",
        "Industry":     "industry",
        "Symbol":       "symbol",
        "Series":       "series",
        "ISIN Code":    "isin",
    })

    # Keep only equity series
    if "series" in df.columns:
        df = df[df["series"] == "EQ"].copy()

    # Add yfinance-compatible ticker (append .NS)
    df["nse_ticker"] = df["symbol"].str.strip() + ".NS"
    df["industry"]   = df["industry"].str.strip()

    df = df[["company_name", "industry", "symbol", "nse_ticker"]].copy()
    df = df.dropna(subset=["symbol", "industry"])

    print(f"  ✅ Fetched {len(df)} stocks from Nifty 500")
    _save_cache(df)
    return df

except Exception as e:
    print(f"  ⚠️  Failed to fetch Nifty 500: {e}")
    print(f"  ⚠️  Attempting to use cached version...")

    cached = _load_cache()
    if cached is not None:
        print(f"  ✅ Loaded {len(cached)} stocks from cache (stale)")
        return cached

    print(f"  ❌ No cache available. Returning empty universe.")
    return pd.DataFrame(columns=["company_name", "industry", "symbol", "nse_ticker"])
```

# ─────────────────────────────────────────────

# BUCKET MAPPER

# ─────────────────────────────────────────────

def map_to_buckets(df: pd.DataFrame) -> dict[str, list[str]]:
“””
Map Nifty 500 stocks to buckets based on Industry.
Returns {bucket_key: [list of nse_tickers]}
“””
buckets: dict[str, list[str]] = {
“BFSI_IT”:        [],
“DEFENCE_INFRA”:  [],
“GREEN_ENERGY_EV”:[],
“FMCG_PHARMA”:    [],
}

```
unmapped = []

for _, row in df.iterrows():
    industry = row["industry"]
    ticker   = row["nse_ticker"]
    bucket   = SECTOR_BUCKET_MAP.get(industry)

    if bucket:
        buckets[bucket].append(ticker)
    else:
        unmapped.append(industry)

# Summary
print(f"\n  📊 Bucket Universe Sizes:")
for key, tickers in buckets.items():
    print(f"    {key:<20} {len(tickers):>3} stocks")

unique_unmapped = set(unmapped)
if unique_unmapped:
    print(f"\n  ℹ️  Unmapped industries ({len(unique_unmapped)}):")
    for ind in sorted(unique_unmapped):
        print(f"    - {ind}")

return buckets
```

# ─────────────────────────────────────────────

# FILTER CHECKER (pre-scoring gate)

# ─────────────────────────────────────────────

def passes_fundamental_filters(data: dict, bucket_key: str) -> tuple[bool, str]:
“””
Check if a stock’s yfinance data passes the bucket’s
fundamental filters. Returns (True, “”) or (False, reason).

```
Checks (in order):
  - Market cap (min/max)
  - PE ceiling
  - PB ceiling          (1.4)
  - 52w high proximity  (1.4)
  - ROE floor
  - Revenue growth floor
  - Debt/Equity ceiling
  - PEG ceiling
  - Profit growth floor
"""
f = BUCKET_FILTERS.get(bucket_key, {})

mkt_cap_cr = data.get("market_cap_cr", 0) or 0

# ── Market cap ────────────────────────────────────────────
if "min_market_cap_cr" in f and mkt_cap_cr < f["min_market_cap_cr"]:
    return False, f"Mkt cap ₹{mkt_cap_cr:.0f}Cr < min ₹{f['min_market_cap_cr']}Cr"

if "max_market_cap_cr" in f and mkt_cap_cr > f["max_market_cap_cr"]:
    return False, f"Mkt cap ₹{mkt_cap_cr:.0f}Cr > max ₹{f['max_market_cap_cr']}Cr"

# ── PE ceiling ────────────────────────────────────────────
pe = data.get("pe_ratio")
if pe and "max_pe" in f and pe > f["max_pe"]:
    return False, f"PE {pe:.1f} > max {f['max_pe']}"

# ── PB ceiling (1.4) ──────────────────────────────────────
pb = data.get("pb_ratio")
if pb is not None and pb > 0 and "max_pb" in f and pb > f["max_pb"]:
    return False, f"PB {pb:.1f} > max {f['max_pb']} — overvalued on assets"

# ── 52-week high proximity (1.4) ──────────────────────────
# Exclude stocks trading too close to their 52w high —
# buying near peak means limited upside and high reversal risk
price_pos = data.get("price_position_52w")
if price_pos is not None and "max_52w_proximity" in f:
    if price_pos > f["max_52w_proximity"]:
        return False, (
            f"Price at {price_pos*100:.0f}% of 52w high "
            f"> max {f['max_52w_proximity']*100:.0f}% — near peak"
        )

# ── ROE floor ─────────────────────────────────────────────
roe = data.get("roe_pct")
if roe is not None and "min_roe" in f and roe < f["min_roe"]:
    return False, f"ROE {roe:.1f}% < min {f['min_roe']}%"

# ── Revenue growth floor ──────────────────────────────────
rev_g = data.get("revenue_growth_pct")
if rev_g is not None and "min_revenue_growth" in f and rev_g < f["min_revenue_growth"]:
    return False, f"Rev growth {rev_g:.1f}% < min {f['min_revenue_growth']}%"

# ── Debt/Equity ceiling ───────────────────────────────────
de = data.get("debt_to_equity")
if de is not None and "max_debt_equity" in f and de > f["max_debt_equity"]:
    return False, f"D/E {de:.2f} > max {f['max_debt_equity']}"

# ── Data quality gate — growth data required ─────────────
# If NEITHER earningsGrowth NOR revenueGrowth is available from Yahoo Finance,
# we cannot verify the stock's growth trajectory or compute PEG.
# Such stocks are excluded to prevent data-blind picks (e.g. GALLANTT).
# Exception: banks/insurers where revenue_growth may appear as NaN due to
# Yahoo Finance quirks — they are already handled by the ROE/PE filters.
earn_g = data.get("earnings_growth_pct")
rev_g_check = data.get("revenue_growth_pct")
industry_for_gate = (data.get("industry") or "").lower()
is_financial = any(k in industry_for_gate for k in ("bank", "insurance", "financial"))
if not is_financial and earn_g is None and rev_g_check is None:
    return False, "No growth data available — cannot verify fundamentals (earningsGrowth + revenueGrowth both None)"

# ── PEG ceiling ───────────────────────────────────────────
# Primary: use computed PEG (PE / earningsGrowth)
# Fallback: compute PEG from PE / revenueGrowth when earnings data missing
# This prevents stocks with missing earningsGrowth from bypassing the cap
peg = data.get("peg_ratio")
if peg is None and "max_peg" in f:
    pe_val  = data.get("pe_ratio")
    rev_g   = data.get("revenue_growth_pct")
    if pe_val and pe_val > 0 and rev_g and rev_g > 0:
        peg = round(pe_val / rev_g, 2)  # fallback PEG
if peg is not None and peg > 0 and "max_peg" in f and peg > f["max_peg"]:
    return False, f"PEG {peg:.2f} > max {f['max_peg']}"

# ── Profit growth floor ───────────────────────────────────
profit_g = data.get("earnings_growth_pct")
if profit_g is not None and "min_profit_growth" in f and profit_g < f["min_profit_growth"]:
    return False, f"Profit growth {profit_g:.1f}% < min {f['min_profit_growth']}%"

return True, ""
```

# ─────────────────────────────────────────────

# MAIN — for standalone testing

# ─────────────────────────────────────────────

if **name** == “**main**”:
df = fetch_nifty500()
print(df.head(10).to_string())
print(f”\nTotal: {len(df)} stocks”)

```
buckets = map_to_buckets(df)
for k, v in buckets.items():
    print(f"\n{k}: {v[:5]}...")  # show first 5 tickers per bucket
```
