"""
US Stock Screener — Monthly
============================
Screens S&P 500 universe across 4 growth-focused buckets.
Posts results to /us/portfolio/picks and seeds /us/portfolio/live
on first run.

Buckets:
  AI_CLOUD         30%  — AI infrastructure, cloud platforms
  SEMICONDUCTORS   25%  — Chip designers, manufacturers
  HIGH_GROWTH_TECH 25%  — Software, e-commerce, fintech
  DEFENSIVE_DIV    20%  — Healthcare, consumer staples, financials

Schedule: 30 2 3 * *  (3rd of month, 02:30 UTC = 8:00 AM IST)
"""

import json
import os
import math
import time
from datetime import datetime, date
from zoneinfo import ZoneInfo

import yfinance as yf

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

API_URL  = os.getenv("API_URL", "https://web-production-2d832.up.railway.app")
DATA_DIR = os.getenv("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)

TOTAL_CAPITAL_USD = 1000.0

BUCKETS = {
    "AI_CLOUD": {
        "label":       "High Conviction — AI + Cloud",
        "allocation_pct": 30.0,
        "atr_multiplier": 2.5,
        "trail_multiplier": 1.5,
        "max_stocks":  2,
        "thresholds": {
            "min_rev_growth": 10.0,
            "max_pe":         80.0,   # growth premium allowed
            "max_pb":         20.0,
            "min_roe":        None,   # many reinvest heavily
            "max_de":         3.0,
            "max_beta":       1.5,
        },
        "universe": [
            "NVDA", "MSFT", "GOOGL", "META", "AMZN",
            "CRM",  "PLTR", "NOW",   "SNOW", "MDB",
            "DDOG", "NET",  "AI",    "ORCL", "IBM",
        ],
    },
    "SEMICONDUCTORS": {
        "label":       "Growth — Semiconductors",
        "allocation_pct": 25.0,
        "atr_multiplier": 3.0,
        "trail_multiplier": 1.5,
        "max_stocks":  2,
        "thresholds": {
            "min_rev_growth": 8.0,
            "max_pe":         50.0,
            "max_pb":         15.0,
            "min_roe":        10.0,
            "max_de":         2.0,
            "max_beta":       1.6,
        },
        "universe": [
            "AMD",  "AVGO", "TSM",  "QCOM", "AMAT",
            "LRCX", "KLAC", "MU",   "MRVL", "TXN",
            "INTC", "NXPI", "ON",   "MPWR", "SWKS",
        ],
    },
    "HIGH_GROWTH_TECH": {
        "label":       "Mid Cap — High Growth Tech",
        "allocation_pct": 25.0,
        "atr_multiplier": 2.5,
        "trail_multiplier": 1.5,
        "max_stocks":  2,
        "thresholds": {
            "min_rev_growth": 12.0,
            "max_pe":         60.0,
            "max_pb":         15.0,
            "min_roe":        None,
            "max_de":         4.0,
            "max_beta":       1.5,
        },
        "universe": [
            "AAPL", "SHOP", "SQ",   "PYPL", "UBER",
            "LYFT", "ABNB", "DASH", "RBLX", "COIN",
            "HOOD", "SOFI", "AFRM", "OPEN", "Z",
            "ADBE", "INTU", "ANSS", "CDNS", "FTNT",
        ],
    },
    "DEFENSIVE_DIV": {
        "label":       "Defensive — Dividend + Stability",
        "allocation_pct": 20.0,
        "atr_multiplier": 2.0,
        "trail_multiplier": 1.5,
        "max_stocks":  1,
        "thresholds": {
            "min_rev_growth": 3.0,
            "max_pe":         30.0,
            "max_pb":         8.0,
            "min_roe":        12.0,
            "max_de":         2.5,
            "max_beta":       1.0,
        },
        "universe": [
            "JNJ",  "PG",   "KO",   "PEP",  "MCD",
            "V",    "MA",   "JPM",  "BAC",  "WMT",
            "COST", "HD",   "LOW",  "UNH",  "CVS",
            "ABT",  "MDT",  "TMO",  "DHR",  "BRK-B",
        ],
    },
}

# ─────────────────────────────────────────────
# SCORING
# ─────────────────────────────────────────────

def score_stock(info: dict, thr: dict) -> float:
    """Score 0–100 based on fundamentals and momentum."""
    score = 0.0
    reasons = []

    def safe(key):
        v = info.get(key)
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return None
        return float(v)

    rev  = safe("revenueGrowth")   # decimal e.g. 0.22 = 22%
    pe   = safe("trailingPE") or safe("forwardPE")
    pb   = safe("priceToBook")
    roe  = safe("returnOnEquity")  # decimal
    de   = safe("debtToEquity")
    beta = safe("beta")
    m1   = safe("52WeekChange")    # proxy for momentum
    peg  = safe("pegRatio")

    # Revenue growth (0–25 pts)
    if rev is not None:
        rev_pct = rev * 100
        if rev_pct >= thr["min_rev_growth"]:
            score += min(25, 10 + (rev_pct - thr["min_rev_growth"]) * 0.5)
            reasons.append(f"Revenue growth {rev_pct:.1f}% ✅")
        else:
            reasons.append(f"Revenue growth {rev_pct:.1f}% below threshold ❌")

    # PE ratio (0–20 pts)
    if pe is not None and pe > 0:
        if pe <= thr["max_pe"]:
            score += max(0, 20 - (pe / thr["max_pe"]) * 10)
            reasons.append(f"PE {pe:.1f}x ✅")
        else:
            score -= 10
            reasons.append(f"PE {pe:.1f}x above limit ❌")

    # PB ratio (0–10 pts)
    if pb is not None and pb > 0:
        if pb <= thr["max_pb"]:
            score += max(0, 10 - (pb / thr["max_pb"]) * 5)

    # ROE (0–15 pts)
    if roe is not None and thr.get("min_roe"):
        roe_pct = roe * 100
        if roe_pct >= thr["min_roe"]:
            score += min(15, 8 + (roe_pct - thr["min_roe"]) * 0.3)
            reasons.append(f"ROE {roe_pct:.1f}% ✅")
        else:
            score -= 5
            reasons.append(f"ROE {roe_pct:.1f}% below threshold ❌")

    # Debt/Equity (0–10 pts)
    if de is not None:
        de_norm = de / 100 if de > 10 else de  # yfinance sometimes gives %, sometimes ratio
        if de_norm <= thr["max_de"]:
            score += max(0, 10 - (de_norm / thr["max_de"]) * 5)

    # Beta filter — hard penalty if too volatile
    if beta is not None:
        if beta > thr["max_beta"]:
            score -= 15
            reasons.append(f"Beta {beta:.2f} too high ❌")
        elif beta < 0.5:
            score += 5  # low-beta bonus for defensive bucket
        else:
            reasons.append(f"Beta {beta:.2f} ✅")

    # Momentum — 52W change (0–20 pts)
    if m1 is not None:
        m1_pct = m1 * 100
        if m1_pct > 0:
            score += min(20, 10 + m1_pct * 0.1)
            reasons.append(f"52W momentum +{m1_pct:.1f}% ✅")
        else:
            score += max(-10, m1_pct * 0.1)
            reasons.append(f"52W momentum {m1_pct:.1f}% ❌")

    return max(0.0, min(100.0, round(score, 1))), reasons


def compute_atr(ticker: str, period: int = 14) -> float:
    """Compute 14-day ATR for stop-loss calculation."""
    try:
        hist = yf.Ticker(ticker).history(period="30d")
        if len(hist) < 2:
            return 0.0
        highs  = hist["High"].values
        lows   = hist["Low"].values
        closes = hist["Close"].values
        trs = []
        for i in range(1, len(hist)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i-1]),
                abs(lows[i]  - closes[i-1]),
            )
            trs.append(tr)
        return round(sum(trs[-period:]) / min(period, len(trs)), 4)
    except Exception:
        return 0.0


# ─────────────────────────────────────────────
# SCREENER MAIN
# ─────────────────────────────────────────────

def screen_bucket(bk: str, cfg: dict) -> list:
    """Screen a single bucket and return top N scored stocks."""
    print(f"\n  🔍 Screening {bk} ({len(cfg['universe'])} candidates)...")
    thr       = cfg["thresholds"]
    results   = []

    for ticker in cfg["universe"]:
        try:
            time.sleep(0.3)  # rate limit
            t    = yf.Ticker(ticker)
            info = t.info

            price = info.get("regularMarketPrice") or info.get("currentPrice")
            if not price or price <= 0:
                continue

            score, reasons = score_stock(info, thr)
            if score < 20:
                continue

            atr      = compute_atr(ticker)
            sl_dist  = atr * cfg["atr_multiplier"]
            sl_price = round(price - sl_dist, 2)
            sl_pct   = round((sl_dist / price) * 100, 2)

            results.append({
                "ticker":       ticker,
                "name":         info.get("longName") or info.get("shortName") or ticker,
                "price":        round(float(price), 2),
                "final_score":  score,
                "atr_14day":    atr,
                "atr_multiplier": cfg["atr_multiplier"],
                "stop_loss_price": sl_price,
                "stop_loss_pct":   sl_pct,
                "trailing_stop_dist": round(atr * cfg["trail_multiplier"], 2),
                # Fundamentals
                "pe_ratio":     _safe_round(info.get("trailingPE") or info.get("forwardPE")),
                "pb_ratio":     _safe_round(info.get("priceToBook")),
                "roe_pct":      _safe_pct(info.get("returnOnEquity")),
                "rev_growth_pct": _safe_pct(info.get("revenueGrowth")),
                "debt_equity":  _safe_round(info.get("debtToEquity")),
                "beta":         _safe_round(info.get("beta")),
                "peg_ratio":    _safe_round(info.get("pegRatio")),
                "momentum_52w": _safe_pct(info.get("52WeekChange")),
                "audit_trail":  {"why_picked": reasons[:3]},
                "circuit_risk": "low" if (info.get("beta") or 1) < 1.2 else "moderate",
            })
            print(f"    ✅ {ticker:6s} score={score:.1f}")

        except Exception as e:
            print(f"    ⚠️  {ticker}: {e}")

    results.sort(key=lambda x: x["final_score"], reverse=True)
    return results[:cfg["max_stocks"]]


def _safe_round(v, decimals=2):
    try:
        if v is None or math.isnan(float(v)):
            return None
        return round(float(v), decimals)
    except Exception:
        return None


def _safe_pct(v, decimals=2):
    """Convert decimal ratio to % e.g. 0.22 → 22.0"""
    try:
        if v is None or math.isnan(float(v)):
            return None
        return round(float(v) * 100, decimals)
    except Exception:
        return None


def run_screener():
    print("\n" + "="*60)
    print("  🇺🇸 US STOCK SCREENER — MONTHLY RUN")
    print(f"  {datetime.now().strftime('%d %B %Y, %I:%M %p')}")
    print("="*60)

    picks    = {}
    all_stocks = []

    for bk, cfg in BUCKETS.items():
        alloc_usd = TOTAL_CAPITAL_USD * cfg["allocation_pct"] / 100
        per_stock = alloc_usd / cfg["max_stocks"]

        top = screen_bucket(bk, cfg)
        stocks = []
        for s in top:
            price  = s["price"]
            shares = max(1, int(per_stock // price))  # whole shares only
            actual = round(shares * price, 2)
            s["approx_shares"]   = shares
            s["allocation_usd"]  = actual
            s["per_stock_alloc"] = round(per_stock, 2)
            s["buy_date"]        = date.today().isoformat()
            stocks.append(s)
            all_stocks.append(s)

        picks[bk] = {
            "label":          cfg["label"],
            "allocation_pct": cfg["allocation_pct"],
            "per_stock_alloc": round(per_stock, 2),
            "stocks":         stocks,
        }
        print(f"  ✅ {bk}: {len(stocks)} stock(s) selected")

    # POST to /us/portfolio/picks
    _post_json(f"{API_URL}/us/portfolio/picks/upload", picks)
    print(f"\n  📤 Picks posted to API ({sum(len(v['stocks']) for v in picks.values())} total stocks)")

    # Seed /us/portfolio/live if empty
    import urllib.request as _ur
    try:
        with _ur.urlopen(f"{API_URL}/us/portfolio/live", timeout=10) as r:
            live = json.loads(r.read())
        has_stocks = any(len(v.get("stocks", [])) > 0 for v in live.values())
        if not has_stocks:
            _post_json(f"{API_URL}/us/portfolio/live/upload", picks)
            print("  🌱 Live portfolio seeded from picks (first run)")
    except Exception as e:
        print(f"  ⚠️  Could not check/seed live portfolio: {e}")

    print("\n  ✅ US Screener complete.")
    return picks


def _post_json(url: str, data: dict):
    import urllib.request as _ur
    payload = json.dumps(data).encode()
    req = _ur.Request(url, data=payload,
                      headers={"Content-Type": "application/json"}, method="POST")
    with _ur.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


if __name__ == "__main__":
    run_screener()
