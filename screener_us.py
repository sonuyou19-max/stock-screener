"""
US Tech/Semiconductor Monthly Screener
=======================================
Universe : S&P 500 "Information Technology" sector + semiconductor
           sub-industries from other sectors (no buckets).

Pipeline :
  1. Build universe — S&P 500 tech/semi stocks
  2. Screen fundamentals, earnings, margins, insider/inst, circuit risk
  3. Score by percentile (5 dimensions, unified weights)
  4. Pick top 7 with correlation filter
  5. Run rebalancer on live holdings
  6. Generate advisory → POST to /us/advisory/upload

Schedule : monthly (2nd of month)
"""

import yfinance as yf
import os as _os_tok

_UPLOAD_AUTH = {"X-Upload-Token": _os_tok.environ["UPLOAD_TOKEN"]} if _os_tok.getenv("UPLOAD_TOKEN") else {}

import pandas as pd
import numpy as np
import json
import time
import logging
import warnings
import os
import urllib.request as _ur
from datetime import datetime

from sp500_universe import fetch_sp500
from sp500_universe import (
    INSIDER_HIGH, INSIDER_LOW, INSIDER_NORMAL,
    INSTITUTION_HIGH, INSTITUTION_NORMAL,
)

warnings.filterwarnings("ignore")

# Observability: surface pipeline dropouts instead of silently swallowing them.
# WARNING level keeps the screening loop from flooding logs while still flagging
# stocks that drop out due to data/parse failures.
logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
logger = logging.getLogger("screener_us")

# ──────────────────────────────────────────────────────────────────────
# UNIVERSE DEFINITION — no buckets
# ──────────────────────────────────────────────────────────────────────

BUDGET   = 1_000
MAX_PICKS = 7

# GICS sectors included
TECH_SECTORS = {"Information Technology"}

# Sub-industry overrides — include even when parent sector isn't IT
TECH_SUBINDUSTRIES = {
    "Semiconductors",
    "Semiconductor Equipment",
    "Semiconductors & Semiconductor Equipment",
    "Electronic Equipment & Instruments",
    "Electronic Manufacturing Services",
    "Technology Hardware, Storage & Peripherals",
    "Computer Hardware",
    "Data Processing & Outsourced Services",
    "IT Consulting & Other Services",
    "Internet Services & Infrastructure",
    "Application Software",
    "Systems Software",
}

# Sub-industries to exclude even when parent sector is IT
EXCLUDE_SUBINDUSTRIES = {
    "Wireless Telecommunication Services",
    "Cable & Satellite",
    "Broadcasting",
    "Publishing",
}

# ──────────────────────────────────────────────────────────────────────
# SCORING + FILTER CONFIG
# ──────────────────────────────────────────────────────────────────────

SCORING_WEIGHTS = {
    "peg_score":            0.15,
    "roe_score":            0.20,
    "revenue_growth_score": 0.35,
    "debt_score":           0.10,
    "momentum_score":       0.20,
}

FUNDAMENTAL_FILTERS = {
    "min_market_cap_usd_m": 2_000,   # $2B+ only
    "max_pe":               100,     # growth premium headroom
    "min_revenue_growth":   5.0,     # 5%+ YoY revenue growth
    "max_debt_equity":      500,     # semi capex-heavy → higher D/E ok
}

MIN_ADV        = 500_000   # 30-day average daily volume
MIN_ADTV_USD_M = 10.0      # $10M+ average daily trading value

ATR_MULTIPLIER  = 2.5
ATR_PERIOD      = 14
ATR_TRAIL_MULT  = 1.5

# Rebalancer: percentile thresholds among all screened stocks
REBALANCER_EXIT_PERCENTILE = 40   # < 40th percentile → EXIT
REBALANCER_TRIM_PERCENTILE = 55   # 40–55th percentile → TRIM


# ──────────────────────────────────────────────────────────────────────
# UNIVERSE BUILDER
# ──────────────────────────────────────────────────────────────────────

def get_tech_universe():
    sp500_df = fetch_sp500()
    tickers = []
    for _, row in sp500_df.iterrows():
        sector = str(row.get("GICS Sector", ""))
        subind = str(row.get("GICS Sub-Industry", ""))
        if subind in EXCLUDE_SUBINDUSTRIES:
            continue
        if sector in TECH_SECTORS or subind in TECH_SUBINDUSTRIES:
            tickers.append(str(row["Symbol"]).replace(".", "-"))
    return sorted(set(tickers))


def get_subindustry_map() -> dict:
    """{ticker: GICS Sub-Industry} from the cached S&P 500 CSV."""
    try:
        sp500_df = fetch_sp500()
        return {str(r["Symbol"]).replace(".", "-"): str(r.get("GICS Sub-Industry", ""))
                for _, r in sp500_df.iterrows()}
    except Exception:
        return {}


# ──────────────────────────────────────────────────────────────────────
# SENTIMENT (news scanner + weekly LLM synthesis → score adjustment)
# ──────────────────────────────────────────────────────────────────────
# The news scanner and LLM synthesiser ran for months producing signals
# nothing consumed. This wires them into scoring the way the India
# screener consumes its sentiment stack. Each stock maps to one of the
# LLM's 4 buckets via GICS sub-industry, so the weekly verdict actually
# differentiates within the tech universe.

SUBIND_TO_LLM_BUCKET = {
    "Semiconductors":                          "SEMICONDUCTORS",
    "Semiconductor Materials & Equipment":     "SEMICONDUCTORS",
    "Systems Software":                        "AI_CLOUD",
    "Internet Services & Infrastructure":      "AI_CLOUD",
    "IT Consulting & Other Services":          "AI_CLOUD",
    "Data Processing & Outsourced Services":   "AI_CLOUD",
    "Interactive Media & Services":            "AI_CLOUD",
}
DEFAULT_LLM_BUCKET = "HIGH_GROWTH_TECH"   # app software, hardware, EMS, ...

# LLM verdict → points (scaled by confidence); news signal → points.
LLM_VERDICT_ADJ   = {"Positive": 4.0, "Neutral": 0.0, "Cautious": -2.0, "Negative": -5.0}
LLM_CONF_SCALE    = {"High": 1.0, "Medium": 0.75, "Low": 0.5}
NEWS_SIGNAL_ADJ   = {"positive": 2.0, "mild_positive": 1.0, "neutral": 0.0,
                     "cautious": -1.0, "negative": -3.0}
LLM_MAX_AGE_DAYS  = 10   # weekly job — anything older is stale
NEWS_MAX_AGE_DAYS = 5    # daily job


def _signal_age_days(payload: dict) -> float | None:
    """Age from a 'generated_at' like '2026-06-30 08:15 IST'."""
    try:
        gen = str(payload.get("generated_at", ""))[:10]
        return (datetime.now() - datetime.strptime(gen, "%Y-%m-%d")).days
    except Exception:
        return None


def fetch_us_sentiment() -> dict:
    """Load LLM synthesis + news signals from DATA_DIR (fallback: the API
    signal store). Stale or missing signals are dropped — a silent feed
    outage must not keep steering scores."""
    dd = os.getenv("DATA_DIR", ".")
    out = {"llm": None, "news": None, "notes": []}

    sources = {}
    for key, fname in [("us_llm_synthesis", "us_llm_synthesis.json"),
                       ("us_news_signals", "us_news_signals.json")]:
        try:
            with open(os.path.join(dd, fname)) as f:
                sources[key] = json.load(f)
        except Exception:
            pass
    missing = [k for k in ("us_llm_synthesis", "us_news_signals") if k not in sources]
    if missing:
        try:
            api = os.getenv("API_URL", "https://web-production-50eee.up.railway.app")
            with _ur.urlopen(f"{api}/signals", timeout=15) as r:
                api_signals = json.loads(r.read())
            for k in missing:
                if isinstance(api_signals.get(k), dict):
                    sources[k] = api_signals[k]
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


def sentiment_adjustment(subindustry: str, sentiment: dict) -> tuple[float, str]:
    """(points, notes) for one stock from its LLM bucket verdict + the
    bucket-level news signal. Bounded to roughly ±7 by construction."""
    bucket = SUBIND_TO_LLM_BUCKET.get(subindustry, DEFAULT_LLM_BUCKET)
    adj, notes = 0.0, []

    verdicts = sentiment.get("llm") or {}
    bv = verdicts.get(bucket)
    if isinstance(bv, dict) and bv.get("verdict") in LLM_VERDICT_ADJ:
        pts = LLM_VERDICT_ADJ[bv["verdict"]] * LLM_CONF_SCALE.get(bv.get("confidence"), 0.75)
        if pts:
            adj += pts
            notes.append(f"LLM {bucket}: {bv['verdict']} ({pts:+.1f})")

    news = sentiment.get("news") or {}
    news_bucket = "DEFENSIVE_DIV" if bucket == "DEFENSIVE_DIV" else "TECH"
    ns = (news.get(news_bucket) or {}).get("signal")
    if ns in NEWS_SIGNAL_ADJ and NEWS_SIGNAL_ADJ[ns]:
        adj += NEWS_SIGNAL_ADJ[ns]
        notes.append(f"news {news_bucket}: {ns} ({NEWS_SIGNAL_ADJ[ns]:+.1f})")

    return round(adj, 2), "; ".join(notes)


# ──────────────────────────────────────────────────────────────────────
# LIVE HOLDINGS FETCHER
# ──────────────────────────────────────────────────────────────────────

def fetch_live_holdings():
    api = os.getenv("API_URL", "https://web-production-50eee.up.railway.app")
    try:
        with _ur.urlopen(f"{api}/us/portfolio/live", timeout=15) as r:
            portfolio = json.loads(r.read())
        stocks = []
        for bucket in portfolio.values():
            if isinstance(bucket, dict):
                stocks.extend(bucket.get("stocks", []))
        return stocks
    except Exception as e:
        print(f"  ⚠️  Could not fetch live holdings: {e}")
        return []


# ──────────────────────────────────────────────────────────────────────
# PREV INSTITUTIONAL (for trend delta)
# ──────────────────────────────────────────────────────────────────────

def _load_prev_institutional():
    import glob
    patterns = [
        os.path.join(os.getenv("DATA_DIR", "."), "us_portfolio_*.json"),
        "/data/us_portfolio_*.json",
    ]
    files = []
    for pat in patterns:
        files.extend(glob.glob(pat))
    if not files:
        return {}
    try:
        with open(sorted(files)[-1]) as f:
            portfolio = json.load(f)
        result = {}
        for bucket in portfolio.values():
            for stock in (bucket.get("stocks", []) if isinstance(bucket, dict) else []):
                t = stock.get("ticker"); pct = stock.get("institutional_pct")
                if t and pct is not None:
                    result[t] = pct
        return result
    except Exception:
        return {}


# ──────────────────────────────────────────────────────────────────────
# ATR / STOP-LOSS
# ──────────────────────────────────────────────────────────────────────

def calculate_atr(ticker, period=ATR_PERIOD):
    try:
        hist = yf.Ticker(ticker).history(period=f"{period + 10}d")
        if hist.empty or len(hist) < period + 1:
            return None
        hist = hist.tail(period + 1).copy()
        hist["prev_close"] = hist["Close"].shift(1)
        hist["tr1"] = hist["High"] - hist["Low"]
        hist["tr2"] = (hist["High"] - hist["prev_close"]).abs()
        hist["tr3"] = (hist["Low"]  - hist["prev_close"]).abs()
        hist["true_range"] = hist[["tr1","tr2","tr3"]].max(axis=1)
        return round(float(hist["true_range"].iloc[1:].mean()), 4)
    except Exception as e:
        logger.warning(f"calculate_atr failed for {ticker}: {e}")
        return None

def compute_atr_stops(ticker, buy_price):
    atr = calculate_atr(ticker)
    if atr and atr > 0:
        sl    = round(buy_price - ATR_MULTIPLIER * atr, 2)
        trail = round(ATR_TRAIL_MULT * atr, 2)
        sl_pct = round((buy_price - sl) / buy_price * 100, 2)
        src = "ATR"
    else:
        fp    = 0.12   # 12% fallback for tech
        sl    = round(buy_price * (1 - fp), 2)
        trail = round(buy_price * 0.05, 2)
        sl_pct = round(fp * 100, 2)
        atr   = None
        src   = "FALLBACK_FIXED_PCT"
    return {
        "atr_14day": atr, "atr_multiplier": ATR_MULTIPLIER,
        "stop_loss_price": sl, "stop_loss_pct": sl_pct,
        "trailing_stop_dist": trail, "atr_source": src,
    }


# ──────────────────────────────────────────────────────────────────────
# DATA FETCHER
# ──────────────────────────────────────────────────────────────────────

def fetch_stock_data(stock, ticker):
    try:
        import math as _math
        info  = stock.info
        if not info or info.get("regularMarketPrice") is None:
            return None
        hist = stock.history(period="1y")
        if hist.empty or len(hist) < 20:
            return None

        current_price = None
        try:
            fi = stock.fast_info
            fp = getattr(fi, "last_price", None) or getattr(fi, "regularMarketPrice", None)
            if fp and not _math.isnan(float(fp)) and float(fp) > 0:
                current_price = float(fp)
        except Exception:
            pass
        if not current_price:
            rmp = info.get("regularMarketPrice") or info.get("currentPrice")
            if rmp and not _math.isnan(float(rmp)) and float(rmp) > 0:
                current_price = float(rmp)
        if not current_price:
            cv = hist["Close"].dropna().iloc[-1] if not hist["Close"].dropna().empty else None
            if cv and not _math.isnan(float(cv)) and float(cv) > 0:
                current_price = float(cv)
        if not current_price:
            return None

        adv_30d = hist["Volume"].iloc[-30:].mean() if len(hist) >= 30 else hist["Volume"].mean()
        adtv_usd_m = round((adv_30d * current_price) / 1_000_000, 2)
        if adv_30d < MIN_ADV:
            print(f"    ⛔ {ticker} — ADV {adv_30d:,.0f} < {MIN_ADV:,.0f}")
            return None
        if adtv_usd_m < MIN_ADTV_USD_M:
            print(f"    ⛔ {ticker} — ADTV ${adtv_usd_m:.1f}M < ${MIN_ADTV_USD_M}M")
            return None

        p1m = hist["Close"].iloc[-22]  if len(hist) >= 22  else hist["Close"].iloc[0]
        p3m = hist["Close"].iloc[-66]  if len(hist) >= 66  else hist["Close"].iloc[0]
        p6m = hist["Close"].iloc[-126] if len(hist) >= 126 else hist["Close"].iloc[0]
        mom_1m = (current_price / p1m - 1) * 100
        mom_3m = (current_price / p3m - 1) * 100
        mom_6m = (current_price / p6m - 1) * 100

        closes = hist["Close"].dropna()
        dma200 = float(closes.rolling(200).mean().iloc[-1]) if len(closes) >= 200 else None
        if mom_6m < -25:
            print(f"    ⛔ {ticker} — 6M momentum {mom_6m:.0f}% (falling knife)")
            return None
        if dma200 and current_price < dma200 and mom_3m < 0:
            print(f"    ⛔ {ticker} — below 200-DMA with negative 3M momentum")
            return None

        vol_10d   = hist["Volume"].iloc[-10:].mean()
        vol_30d_v = hist["Volume"].iloc[-30:].mean()
        volume_ratio = vol_10d / vol_30d_v if vol_30d_v > 0 else 1.0
        h52 = info.get("fiftyTwoWeekHigh") or float(closes.max())
        l52 = info.get("fiftyTwoWeekLow")  or float(closes.min())
        price_pos = (current_price - l52) / (h52 - l52) if h52 != l52 else 0.5

        pe       = info.get("trailingPE")
        earn_g_raw = info.get("earningsGrowth")
        roe_raw    = info.get("returnOnEquity")
        rev_g_raw  = info.get("revenueGrowth")
        roe_pct    = round(roe_raw * 100, 2)  if roe_raw  is not None else None
        earn_g_pct = round(earn_g_raw * 100, 2) if earn_g_raw is not None else None
        rev_g_pct  = round(rev_g_raw * 100, 2)  if rev_g_raw  is not None else None
        if pe and pe > 0 and earn_g_pct and earn_g_pct > 0:
            peg = min(round(pe / earn_g_pct, 2), 10.0)
        else:
            peg = None
        mktcap_m = round((info.get("marketCap") or 0) / 1_000_000, 0)

        return {
            "ticker": ticker,
            "name": info.get("longName", ticker),
            "sector": info.get("sector", "Technology"),
            "industry": info.get("industry", "Unknown"),
            "current_price": round(current_price, 2),
            "market_cap_usd_m": mktcap_m,
            "pe_ratio": pe, "forward_pe": info.get("forwardPE"),
            "pb_ratio": info.get("priceToBook"), "peg_ratio": peg,
            "roe_pct": roe_pct, "earnings_growth_pct": earn_g_pct, "revenue_growth_pct": rev_g_pct,
            "roe": roe_raw, "revenue_growth": rev_g_raw, "earnings_growth": earn_g_raw,
            "debt_to_equity": info.get("debtToEquity"),
            "current_ratio": info.get("currentRatio"),
            "profit_margin": info.get("profitMargins"),
            "gross_margin": info.get("grossMargins"),
            "dividend_yield": info.get("dividendYield"),
            "beta": info.get("beta"),
            "momentum_1m": round(mom_1m, 2), "momentum_3m": round(mom_3m, 2), "momentum_6m": round(mom_6m, 2),
            "volume_ratio": round(volume_ratio, 2),
            "price_position_52w": round(price_pos, 2),
            "high_52w": round(h52, 2), "low_52w": round(l52, 2),
            "adv_30d": round(adv_30d, 0), "adtv_usd_m": adtv_usd_m,
            "insider_pct": round(info.get("heldPercentInsiders", 0) * 100, 2) if info.get("heldPercentInsiders") is not None else None,
            "institutional_pct": round(info.get("heldPercentInstitutions", 0) * 100, 2) if info.get("heldPercentInstitutions") is not None else None,
        }
    except Exception as e:
        logger.warning(f"fetch_stock_data failed for {ticker}: {e}")
        return None


# ──────────────────────────────────────────────────────────────────────
# FUNDAMENTAL FILTER
# ──────────────────────────────────────────────────────────────────────

def passes_filters(data):
    mc = data.get("market_cap_usd_m", 0) or 0
    if mc < FUNDAMENTAL_FILTERS["min_market_cap_usd_m"]:
        return False, f"Market cap ${mc:.0f}M < ${FUNDAMENTAL_FILTERS['min_market_cap_usd_m']}M"
    pe = data.get("pe_ratio")
    if pe and pe > FUNDAMENTAL_FILTERS["max_pe"]:
        return False, f"PE {pe:.0f} > {FUNDAMENTAL_FILTERS['max_pe']}"
    rg = data.get("revenue_growth_pct")
    if rg is not None and rg < FUNDAMENTAL_FILTERS["min_revenue_growth"]:
        return False, f"Revenue growth {rg:.1f}% < {FUNDAMENTAL_FILTERS['min_revenue_growth']}%"
    de = data.get("debt_to_equity")
    if de and de > FUNDAMENTAL_FILTERS["max_debt_equity"]:
        return False, f"D/E {de:.0f} > {FUNDAMENTAL_FILTERS['max_debt_equity']}"
    return True, "OK"


# ──────────────────────────────────────────────────────────────────────
# SCORING ENGINE
# ──────────────────────────────────────────────────────────────────────

def score_stock(row, weights):
    scores = {}
    peg = row.get("peg_ratio"); pe = row.get("pe_ratio")
    if peg and peg > 0:
        scores["peg_raw"] = peg; scores["pe_raw"] = pe
    elif pe and pe > 0:
        scores["peg_raw"] = round(pe / 10, 2); scores["pe_raw"] = pe
    else:
        scores["peg_raw"] = None; scores["pe_raw"] = None
    roe = row.get("roe"); scores["roe_raw"] = (roe * 100) if roe is not None else None
    rg  = row.get("revenue_growth"); scores["revenue_growth_raw"] = (rg * 100) if rg is not None else None
    de  = row.get("debt_to_equity"); scores["debt_raw"] = de if de is not None else None
    m1 = row.get("momentum_1m", 0) or 0
    m3 = row.get("momentum_3m", 0) or 0
    vr = row.get("volume_ratio", 1.0) or 1.0
    scores["momentum_raw"] = (0.4 * m1) + (0.4 * m3) + (0.2 * (vr - 1) * 10)
    return scores

def normalise_and_compute_final(df, weights):
    def minmax(series, invert=False):
        clean = series.dropna()
        if clean.empty or clean.max() == clean.min():
            return pd.Series([50.0] * len(series), index=series.index)
        n = series.rank(pct=True) * 100
        return (100 - n if invert else n).fillna(50)
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


# ──────────────────────────────────────────────────────────────────────
# CORRELATION ENGINE
# ──────────────────────────────────────────────────────────────────────

def calculate_correlation_matrix(tickers, period="60d"):
    print(f"    📐 Correlations for {len(tickers)} stocks...")
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
    return pd.DataFrame(price_data).dropna(how="all").pct_change().dropna().corr()

def select_low_correlation_picks(df, n_picks, corr_matrix, max_corr=0.75):
    if corr_matrix.empty:
        print(f"    ⚠️  No correlation data — using top {n_picks} by score")
        return df.head(n_picks)
    def _pick(threshold):
        selected = []
        for _, row in df.iterrows():
            ticker = row["ticker"]
            if len(selected) >= n_picks:
                break
            if not selected:
                selected.append(ticker); continue
            ok = True
            for sel in selected:
                if ticker in corr_matrix.index and sel in corr_matrix.index:
                    if abs(corr_matrix.loc[ticker, sel]) > threshold:
                        print(f"    ↩️  {ticker} skipped — corr with {sel}={corr_matrix.loc[ticker,sel]:.2f}")
                        ok = False; break
            if ok:
                selected.append(ticker)
        return selected
    picks = _pick(max_corr)
    if len(picks) < n_picks:
        picks = _pick(0.85)
    if len(picks) < n_picks:
        picks = df["ticker"].head(n_picks).tolist()
    print(f"    ✅ Selected {len(picks)} diversified picks: {picks}")
    return df[df["ticker"].isin(picks)].copy()


# ──────────────────────────────────────────────────────────────────────
# SIGNAL CHECKS (unchanged logic, bucket_key removed)
# ──────────────────────────────────────────────────────────────────────

STALE_DATA_DAYS = 120; DETERIORATION_PCT = 20; MISS_THRESHOLD_PCT = 10

def check_earnings_freshness(ticker):
    result = {"last_reported_date": None, "data_age_days": None, "earnings_trend": "unknown",
              "earnings_miss": False, "freshness_penalty": 0, "exclude": False, "notes": ""}
    try:
        stock = yf.Ticker(ticker)
        q = stock.quarterly_financials
        if q is None or q.empty:
            result["notes"] = "No quarterly financials"; result["freshness_penalty"] = 5; return result
        last_date = q.columns[0]
        if hasattr(last_date, "date"): last_date = last_date.date()
        age = (datetime.now().date() - last_date).days
        result["last_reported_date"] = str(last_date); result["data_age_days"] = age
        if age > STALE_DATA_DAYS:
            result["notes"] += f"Stale data ({age}d). "; result["freshness_penalty"] += 5
        ni = None
        for label in ("Net Income", "Net Income Common Stockholders"):
            if label in q.index: ni = q.loc[label].dropna(); break
        if ni is not None and len(ni) >= 2:
            latest = float(ni.iloc[0])
            if len(ni) >= 5:
                base = float(ni.iloc[4]); lbl = "YoY"
                prior_latest = float(ni.iloc[1]); prior_base = float(ni.iloc[5]) if len(ni) >= 6 else None
            else:
                base = float(ni.iloc[1]); lbl = "QoQ"
                prior_latest = float(ni.iloc[1]); prior_base = float(ni.iloc[2]) if len(ni) >= 3 else None
            chg = (latest - base) / abs(base) * 100 if base != 0 else 0
            if chg < -DETERIORATION_PCT:
                result["notes"] += f"Net income down {abs(chg):.1f}% {lbl}. "
                if prior_base is not None:
                    prev_chg = (prior_latest - prior_base) / abs(prior_base) * 100 if prior_base != 0 else 0
                    if prev_chg < -DETERIORATION_PCT:
                        result["earnings_trend"] = "double_deterioration"; result["exclude"] = True
                        result["freshness_penalty"] += 15; result["notes"] += "⛔ Double deterioration."; return result
                result["earnings_trend"] = "deteriorating"; result["freshness_penalty"] += 10
            elif chg > 5:
                result["earnings_trend"] = "improving"; result["notes"] += f"Net income up {chg:.1f}% {lbl} ✅. "
            else:
                result["earnings_trend"] = "stable"; result["notes"] += f"Net income stable ({chg:+.1f}% {lbl}). "
            if latest < 0: result["freshness_penalty"] += 5; result["notes"] += "⚠️ Latest quarter loss-making. "
        result["freshness_penalty"] = min(result["freshness_penalty"], 20)
        if not result["notes"]: result["notes"] = "Earnings data healthy ✅"
    except Exception as ex:
        result["notes"] = f"Could not fetch: {ex}"; result["freshness_penalty"] = 3
    return result

def check_margin_health(data):
    result = {"divergence": None, "margin_trend": "unknown", "margin_penalty": 0, "margin_bonus": 0, "net_adjustment": 0, "margin_notes": ""}
    rev_g = data.get("revenue_growth_pct"); profit_g = data.get("earnings_growth_pct")
    gross_m = data.get("gross_margin"); profit_m = data.get("profit_margin")
    if rev_g is not None and profit_g is not None:
        div = rev_g - profit_g; result["divergence"] = round(div, 1)
        if div > 30:   result["margin_trend"] = "severe_compression"; result["margin_penalty"] = 10; result["margin_notes"] += f"⛔ Severe compression: rev+{rev_g:.1f}% profit+{profit_g:.1f}%. "
        elif div > 15: result["margin_trend"] = "compressing";        result["margin_penalty"] = 5;  result["margin_notes"] += f"⚠️ Compressing: rev+{rev_g:.1f}% profit+{profit_g:.1f}%. "
        elif div < -5: result["margin_trend"] = "expanding";          result["margin_bonus"] = 5;    result["margin_notes"] += f"✅ Expanding: profit+{profit_g:.1f}% > rev+{rev_g:.1f}%. "
        else:          result["margin_trend"] = "stable";             result["margin_notes"] += f"Rev+{rev_g:.1f}% Profit+{profit_g:.1f}% stable. "
    if gross_m is not None:
        gp = round(gross_m * 100, 1)
        if gp < 10:   result["margin_penalty"] += 5; result["margin_notes"] += f"⚠️ Low gross margin {gp}%. "
        elif gp > 40: result["margin_bonus"] += 3;   result["margin_notes"] += f"✅ Strong gross margin {gp}%. "
    if profit_m is not None:
        pp = round(profit_m * 100, 1)
        if pp < 5:   result["margin_penalty"] += 3; result["margin_notes"] += f"⚠️ Thin net margin {pp}%. "
        elif pp > 20: result["margin_bonus"] += 2;  result["margin_notes"] += f"✅ Strong net margin {pp}%. "
    result["margin_penalty"] = min(result["margin_penalty"], 15); result["margin_bonus"] = min(result["margin_bonus"], 8)
    result["net_adjustment"] = result["margin_bonus"] - result["margin_penalty"]
    if not result["margin_notes"]: result["margin_notes"] = "Insufficient margin data."
    return result

def check_promoter_signal(data):
    result = {"insider_pct": data.get("insider_pct"), "institutional_pct": data.get("institutional_pct"),
              "promoter_signal": "unknown", "institution_signal": "unknown",
              "promoter_bonus": 0, "promoter_penalty": 0, "net_promoter_adj": 0, "promoter_notes": ""}
    insider = data.get("insider_pct"); instit = data.get("institutional_pct")
    ind = (data.get("industry") or "").lower()
    is_inst = any(k in ind for k in ("bank", "insurance", "reit", "utility"))
    if insider is not None:
        if is_inst and insider < INSIDER_LOW:    result["promoter_signal"] = "normal";   result["promoter_notes"] += f"Institutional mgmt ({insider:.1f}%). "
        elif insider >= INSIDER_HIGH:            result["promoter_signal"] = "strong";   result["promoter_bonus"] = 5;  result["promoter_notes"] += f"✅ Strong insider ({insider:.1f}%). "
        elif insider >= INSIDER_NORMAL:          result["promoter_signal"] = "normal";   result["promoter_notes"] += f"Insider normal ({insider:.1f}%). "
        elif insider >= INSIDER_LOW:             result["promoter_signal"] = "low";      result["promoter_penalty"] = 2; result["promoter_notes"] += f"⚠️ Low insider ({insider:.1f}%). "
        else:                                    result["promoter_signal"] = "very_low"; result["promoter_penalty"] = 5; result["promoter_notes"] += f"⛔ Very low insider ({insider:.1f}%). "
    if instit is not None:
        if instit >= INSTITUTION_HIGH:   result["institution_signal"] = "high";   result["promoter_bonus"] += 3;  result["promoter_notes"] += f"✅ High institutional {instit:.1f}%. "
        elif instit >= INSTITUTION_NORMAL: result["institution_signal"] = "normal"; result["promoter_notes"] += f"Inst normal {instit:.1f}%. "
        else:                              result["institution_signal"] = "low";    result["promoter_penalty"] += 3; result["promoter_notes"] += f"⚠️ Low inst {instit:.1f}%. "
    if insider is not None and insider >= INSIDER_HIGH and instit is not None and instit >= INSTITUTION_HIGH:
        result["promoter_bonus"] += 2; result["promoter_notes"] += "🏆 Double conviction. "
    result["promoter_bonus"]  = min(result["promoter_bonus"], 10); result["promoter_penalty"] = min(result["promoter_penalty"], 10)
    result["net_promoter_adj"] = result["promoter_bonus"] - result["promoter_penalty"]
    return result

def check_institutional_trend(ticker, current_inst_pct, prev_inst_pct=None):
    result = {"inst_change_pp": None, "inst_trend": "unknown", "inst_trend_bonus": 0,
              "inst_trend_penalty": 0, "net_inst_adj": 0, "inst_trend_notes": "", "holder_count": None}
    if current_inst_pct is not None and prev_inst_pct is not None:
        chg = round(current_inst_pct - prev_inst_pct, 2); result["inst_change_pp"] = chg
        if chg >= 2.0:   result["inst_trend"] = "accumulating";  result["inst_trend_bonus"] = 5;    result["inst_trend_notes"] += f"✅ Accumulating +{chg:.1f}pp. "
        elif chg <= -5.0: result["inst_trend"] = "exiting_fast"; result["inst_trend_penalty"] = 10; result["inst_trend_notes"] += f"⛔ Fast exit {chg:.1f}pp. "
        elif chg <= -2.0: result["inst_trend"] = "distributing"; result["inst_trend_penalty"] = 5;  result["inst_trend_notes"] += f"⚠️ Distributing {chg:.1f}pp. "
        else:             result["inst_trend"] = "stable";        result["inst_trend_notes"] += f"Stable {chg:+.1f}pp. "
    try:
        holders = yf.Ticker(ticker).institutional_holders
        if holders is not None and not holders.empty:
            hc = len(holders); result["holder_count"] = hc
            if prev_inst_pct is None:
                if hc >= 15:  result["inst_trend"] = "well_covered";      result["inst_trend_bonus"] = 3;    result["inst_trend_notes"] += f"✅ {hc} holders. "
                elif hc >= 5: result["inst_trend"] = "moderate_coverage"; result["inst_trend_notes"] += f"{hc} holders. "
                else:         result["inst_trend"] = "low_coverage";      result["inst_trend_penalty"] = 3;  result["inst_trend_notes"] += f"⚠️ Only {hc} holders. "
    except Exception:
        pass
    result["inst_trend_bonus"]   = min(result["inst_trend_bonus"], 8)
    result["inst_trend_penalty"] = min(result["inst_trend_penalty"], 10)
    result["net_inst_adj"]       = result["inst_trend_bonus"] - result["inst_trend_penalty"]
    if not result["inst_trend_notes"].strip(): result["inst_trend_notes"] = "Inst trend data unavailable."
    return result

def check_circuit_risk(data):
    result = {"circuit_risk": "low", "circuit_penalty": 0, "circuit_exclude": False, "circuit_notes": ""}
    beta = data.get("beta"); adtv = data.get("adtv_usd_m", 0) or 0
    price_pos = data.get("price_position_52w"); cp = data.get("current_price", 0); h52 = data.get("high_52w", cp)
    if beta is not None:
        if beta > 2.5:   result["circuit_penalty"] += 8; result["circuit_risk"] = "extreme";  result["circuit_notes"] += f"⛔ Extreme beta {beta:.1f}. "
        elif beta > 2.0: result["circuit_penalty"] += 5; result["circuit_risk"] = "elevated"; result["circuit_notes"] += f"⚠️ High beta {beta:.1f}. "
        elif beta > 1.5: result["circuit_penalty"] += 2; result["circuit_notes"] += f"Beta {beta:.1f} moderate. "
        else:            result["circuit_notes"] += f"Beta {beta:.1f} acceptable. "
    if price_pos is not None and h52 and cp:
        dd = 1 - (cp / h52)
        if dd > 0.40:   result["circuit_penalty"] += 5; result["circuit_risk"] = "moderate" if result["circuit_risk"] == "low" else result["circuit_risk"]; result["circuit_notes"] += f"⚠️ {dd*100:.0f}% below 52w high. "
        elif dd > 0.25: result["circuit_penalty"] += 2; result["circuit_notes"] += f"{dd*100:.0f}% below 52w high. "
        else:           result["circuit_notes"] += f"Price healthy {dd*100:.0f}% below 52w high. "
    if 0 < adtv < 20.0: result["circuit_penalty"] += 3; result["circuit_risk"] = "moderate" if result["circuit_risk"] == "low" else result["circuit_risk"]; result["circuit_notes"] += f"⚠️ ADTV ${adtv:.1f}M thin. "
    if beta and beta > 2.5 and adtv < 20.0: result["circuit_exclude"] = True; result["circuit_risk"] = "extreme"; result["circuit_notes"] += f"⛔ HARD EXCLUDE beta {beta:.1f} + ADTV ${adtv:.1f}M. "
    result["circuit_penalty"] = min(result["circuit_penalty"], 12)
    if not result["circuit_notes"].strip(): result["circuit_notes"] = "Volatility risk: low ✅"
    return result

def check_pledge_dilution(stock, ticker, data):
    result = {"pledge_risk": "low", "dilution_flag": False, "short_interest": None, "float_ratio": None,
              "shares_growth": None, "pledge_penalty": 0, "dilution_penalty": 0, "net_pledge_adj": 0, "pledge_notes": ""}
    try:
        info = stock.info
        spf = info.get("shortPercentOfFloat")
        if spf is not None:
            sp = round(spf * 100, 2); result["short_interest"] = sp
            if sp >= 5.0:   result["pledge_risk"] = "high";     result["pledge_penalty"] = 8; result["pledge_notes"] += f"⛔ High short interest {sp:.1f}%. "
            elif sp >= 2.0: result["pledge_risk"] = "elevated"; result["pledge_penalty"] = 4; result["pledge_notes"] += f"⚠️ Elevated short {sp:.1f}%. "
            else:           result["pledge_notes"] += f"Short interest low {sp:.1f}%. "
        sf = info.get("floatShares"); so = info.get("sharesOutstanding"); ip = data.get("insider_pct", 0) or 0
        if sf and so and so > 0:
            fr = round(sf / so, 3); result["float_ratio"] = fr
            if fr > 0.65 and ip > 40: result["pledge_penalty"] = max(result["pledge_penalty"], 5); result["pledge_notes"] += f"⚠️ Float ratio {fr:.2f} vs insider {ip:.1f}%. "
        try:
            bs = stock.quarterly_balance_sheet
            if bs is not None and not bs.empty and "Ordinary Shares Number" in bs.index:
                ss = bs.loc["Ordinary Shares Number"].dropna()
                if len(ss) >= 2:
                    lat = float(ss.iloc[0]); yago = float(ss.iloc[-1])
                    if yago > 0:
                        gp = round((lat / yago - 1) * 100, 2); result["shares_growth"] = gp
                        if gp > 5.0:   result["dilution_flag"] = True; result["dilution_penalty"] = 5; result["pledge_notes"] += f"⚠️ Shares grew {gp:.1f}% YoY. "
                        elif gp > 2.0: result["dilution_penalty"] = 2; result["pledge_notes"] += f"Mild dilution {gp:.1f}% YoY. "
                        else:          result["pledge_notes"] += f"Shares stable {gp:+.1f}% YoY. "
        except Exception: result["pledge_notes"] += "Share count history unavailable. "
        if not result["pledge_notes"].strip(): result["pledge_notes"] = "No short interest or dilution concerns. ✅"
    except Exception as e: result["pledge_notes"] = f"Check failed: {e}"
    result["pledge_penalty"]  = min(result["pledge_penalty"], 8); result["dilution_penalty"] = min(result["dilution_penalty"], 5)
    result["net_pledge_adj"]  = -(result["pledge_penalty"] + result["dilution_penalty"])
    return result


# ──────────────────────────────────────────────────────────────────────
# AUDIT TRAIL
# ──────────────────────────────────────────────────────────────────────

def generate_audit_trail(row):
    why = []; risks = []; adjs = []; score_bd = {}
    peg = row.get("peg_raw"); roe = row.get("roe_raw"); rev_g = row.get("revenue_growth_raw"); debt = row.get("debt_raw")
    for dim, rv, label in [("peg_score", peg, "PEG"), ("roe_score", roe, "ROE"),
                            ("revenue_growth_score", rev_g, "Revenue Growth"),
                            ("debt_score", debt, "Debt Level"), ("momentum_score", row.get("momentum_raw"), "Momentum")]:
        s = row.get(dim)
        if s is not None: score_bd[label] = round(s, 0)
    if rev_g is not None:
        if rev_g >= 25: why.append(f"Exceptional revenue growth ({rev_g:.1f}% YoY)")
        elif rev_g >= 15: why.append(f"Strong revenue growth ({rev_g:.1f}% YoY)")
        elif rev_g >= 8:  why.append(f"Solid revenue growth ({rev_g:.1f}% YoY)")
    if roe is not None:
        if roe >= 25: why.append(f"Excellent ROE ({roe:.1f}%)")
        elif roe >= 18: why.append(f"Strong ROE ({roe:.1f}%)")
        elif roe >= 12: why.append(f"Acceptable ROE ({roe:.1f}%)")
    if peg is not None:
        if peg < 1.0: why.append(f"Undervalued — PEG {peg:.2f}")
        elif peg < 2.0: why.append(f"Reasonably valued — PEG {peg:.2f}")
    m1 = row.get("momentum_1m", 0) or 0; m3 = row.get("momentum_3m", 0) or 0
    if m1 >= 10 and m3 >= 15: why.append(f"Strong momentum +{m1:.1f}% (1M) +{m3:.1f}% (3M)")
    elif m1 >= 5 or m3 >= 10: why.append(f"Positive momentum +{m1:.1f}% (1M) +{m3:.1f}% (3M)")
    insider = row.get("insider_pct")
    if insider is not None and insider >= INSIDER_HIGH: why.append(f"Strong insider conviction ({insider:.1f}%)")
    et = row.get("earnings_trend", "")
    if et == "improving": why.append("Earnings improving")
    elif et == "stable":  why.append("Earnings stable")
    mt = row.get("margin_trend", "")
    if mt == "expanding": why.append("Margins expanding")
    it = row.get("inst_trend", "")
    if it == "accumulating": why.append("Institutions accumulating")
    elif it == "well_covered": why.append(f"Well covered ({row.get('holder_count')} institutional holders)")
    for val, key, label2 in [
        (row.get("freshness_penalty", 0),  "freshness_penalty", "Earnings freshness"),
        (row.get("net_adjustment", 0),     "net_adjustment",    "Margin health"),
        (row.get("net_promoter_adj", 0),   "net_promoter_adj",  "Insider signal"),
        (row.get("net_inst_adj", 0),       "net_inst_adj",      "Inst trend"),
        (row.get("circuit_penalty", 0),    "circuit_penalty",   "Volatility risk"),
    ]:
        if val and val != 0:
            direction = "-" if key in ("freshness_penalty", "circuit_penalty") else "+"
            adjs.append(f"{label2}: {direction}{abs(val):.0f} pts")
    if debt is not None and debt > 375:
        risks.append(f"D/E {debt:.0f} elevated (limit 500)")
    pp = row.get("price_position_52w")
    if pp is not None and pp > 0.85: risks.append(f"At {pp*100:.0f}% of 52w high — limited upside")
    beta = row.get("beta")
    if beta and beta > 1.5: risks.append(f"High beta ({beta:.1f})")
    cr = row.get("circuit_risk", "low")
    if cr in ("elevated", "high", "extreme"): risks.append(f"Volatility risk {cr} — set stop-loss")
    age = row.get("data_age_days")
    if age and age > 120: risks.append(f"Data {age} days old — verify before buying")
    pr = row.get("pledge_risk", "low"); si = row.get("short_interest")
    if pr == "high" and si:     risks.append(f"High short interest ({si:.1f}%)")
    elif pr == "elevated" and si: risks.append(f"Elevated short interest ({si:.1f}%)")
    if row.get("dilution_flag"): risks.append(f"Share dilution {row.get('shares_growth', 0):+.1f}% YoY")
    score = row.get("final_score", 0)
    return {
        "why_picked": why if why else ["Balanced scores across all dimensions"],
        "score_breakdown": score_bd,
        "adjustments": adjs if adjs else ["No adjustments applied"],
        "risks": risks if risks else ["No significant risks identified"],
        "summary": f"Score {score:.1f}/100 — {(why[0] if why else 'Balanced')}. Risk: {(risks[0] if risks else 'None')}.",
    }


# ──────────────────────────────────────────────────────────────────────
# UNIVERSE SCREENER (single pass — no buckets)
# ──────────────────────────────────────────────────────────────────────

def screen_universe(universe_tickers, live_extra_tickers=None):
    all_tickers = list(set(universe_tickers) | set(live_extra_tickers or []))
    print(f"  Screening {len(all_tickers)} candidates ({len(universe_tickers)} universe + {len(set(live_extra_tickers or [])-set(universe_tickers))} live extras)...")
    prev_inst = _load_prev_institutional()
    records = []; excl_liq = excl_fund = excl_earn = 0

    for ticker in all_tickers:
        # Instantiate yf.Ticker once and thread it through the helpers that read
        # .info — yfinance caches .info on the instance, so check_pledge_dilution
        # reuses it instead of firing a 2nd .info network call.
        stock_obj = yf.Ticker(ticker)
        data = fetch_stock_data(stock_obj, ticker)
        if data is None: excl_liq += 1; time.sleep(0.3); continue
        passed, reason = passes_filters(data)
        if not passed: print(f"    ⛔ {ticker} — {reason}"); excl_fund += 1; time.sleep(0.3); continue
        freshness = check_earnings_freshness(ticker); time.sleep(0.3)
        if freshness["exclude"]: print(f"    ⛔ {ticker} — {freshness['notes'].strip()}"); excl_earn += 1; continue
        for k in ["last_reported_date","data_age_days","earnings_trend","earnings_miss","freshness_penalty","notes"]:
            data[k if k != "notes" else "earnings_notes"] = freshness[k]
        margin = check_margin_health(data)
        for k in ["divergence","margin_trend","margin_penalty","margin_bonus","net_adjustment","margin_notes"]: data[k] = margin[k]
        promoter = check_promoter_signal(data)
        for k in ["promoter_signal","institution_signal","promoter_bonus","promoter_penalty","net_promoter_adj","promoter_notes"]: data[k] = promoter[k]
        prev_pct = prev_inst.get(ticker)
        inst = check_institutional_trend(ticker, data.get("institutional_pct"), prev_pct); time.sleep(0.2)
        for k in ["inst_change_pp","inst_trend","net_inst_adj","inst_trend_notes","holder_count"]: data[k] = inst[k]
        circuit = check_circuit_risk(data)
        if circuit["circuit_exclude"]: print(f"    ⛔ {ticker} — {circuit['circuit_notes'].strip()}"); excl_earn += 1; continue
        for k in ["circuit_risk","circuit_penalty","circuit_notes"]: data[k] = circuit[k]
        pledge = check_pledge_dilution(stock_obj, ticker, data); time.sleep(0.3)
        for k in ["pledge_risk","dilution_flag","short_interest","float_ratio","shares_growth","net_pledge_adj","pledge_notes"]: data[k] = pledge[k]
        scores = score_stock(data, SCORING_WEIGHTS)
        records.append({**data, **scores}); time.sleep(0.3)

    print(f"  📊 {len(records)} passed | {excl_liq} liquidity ⛔ | {excl_fund} fundamentals ⛔ | {excl_earn} earnings ⛔")
    if not records: return pd.DataFrame()
    df = pd.DataFrame(records)
    df = normalise_and_compute_final(df, SCORING_WEIGHTS)
    for col, sign in [("freshness_penalty",-1),("net_adjustment",1),("net_promoter_adj",1),("net_inst_adj",1),("circuit_penalty",-1),("net_pledge_adj",1)]:
        if col in df.columns:
            df["final_score"] = (df["final_score"] + sign * df[col]).clip(lower=0, upper=100)

    # Sentiment overlay: weekly LLM bucket verdict + daily news signal
    sentiment = fetch_us_sentiment()
    for note in sentiment["notes"]:
        print(f"    📰 {note}")
    if sentiment["llm"] or sentiment["news"]:
        subind_map = get_subindustry_map()
        adj_notes = df["ticker"].map(
            lambda t: sentiment_adjustment(subind_map.get(t, ""), sentiment))
        df["sentiment_adj"]   = adj_notes.map(lambda x: x[0])
        df["sentiment_notes"] = adj_notes.map(lambda x: x[1])
        df["llm_bucket"]      = df["ticker"].map(
            lambda t: SUBIND_TO_LLM_BUCKET.get(subind_map.get(t, ""), DEFAULT_LLM_BUCKET))
        df["final_score"] = (df["final_score"] + df["sentiment_adj"]).clip(lower=0, upper=100)
        n_adj = int((df["sentiment_adj"] != 0).sum())
        print(f"    📰 Sentiment applied to {n_adj}/{len(df)} stocks "
              f"(range {df['sentiment_adj'].min():+.1f} to {df['sentiment_adj'].max():+.1f})")
    else:
        df["sentiment_adj"], df["sentiment_notes"], df["llm_bucket"] = 0.0, "", ""
        print("    📰 No fresh sentiment signals — scores unadjusted")

    return df.sort_values("final_score", ascending=False)


# ──────────────────────────────────────────────────────────────────────
# REBALANCER
# ──────────────────────────────────────────────────────────────────────

def _holding_crashed(ticker: str):
    """Re-check the price filters for a holding that fell out of the scored
    set. The falling-knife / trend filters silently DROP crashed stocks from
    scoring, which used to earn them a soft 'TRIM — review manually' verdict
    instead of EXIT — the worse the crash, the weaker the signal. Returns a
    reason string if the holding fails those filters, else None."""
    try:
        hist = yf.Ticker(ticker).history(period="1y")
        closes = hist["Close"].dropna()
        if len(closes) < 66:
            return None
        price  = float(closes.iloc[-1])
        mom_3m = (price / float(closes.iloc[-66]) - 1) * 100
        mom_6m = (price / float(closes.iloc[-126 if len(closes) >= 126 else 0]) - 1) * 100
        if mom_6m < -25:
            return f"6M momentum {mom_6m:.0f}% — crashed through the falling-knife filter"
        if len(closes) >= 200:
            dma200 = float(closes.rolling(200).mean().iloc[-1])
            if price < dma200 and mom_3m < 0:
                return f"below 200-DMA with 3M momentum {mom_3m:.0f}% — trend broken"
        return None
    except Exception:
        return None


def run_rebalancer(live_holdings, scored_df, top7_tickers):
    if not live_holdings: return []
    score_map   = {} if scored_df.empty else {r["ticker"]: r["final_score"] for _, r in scored_df.iterrows()}
    all_scores  = scored_df["final_score"].values if not scored_df.empty else np.array([50])
    health = []
    for stock in live_holdings:
        ticker = stock.get("ticker")
        curr_score = score_map.get(ticker)
        if ticker in top7_tickers:
            verdict = "HOLD"; reason = "Still ranks in top 7 — hold"
        elif curr_score is None:
            crash_reason = _holding_crashed(ticker)
            if crash_reason:
                verdict = "EXIT"; reason = f"{crash_reason}. Exit — do not hold an unscreenable position."
            else:
                verdict = "TRIM"; reason = "Not found in current screener universe — review manually"
        else:
            pct = float((curr_score > all_scores).mean() * 100)
            if pct < REBALANCER_EXIT_PERCENTILE:
                verdict = "EXIT"; reason = f"Score {curr_score:.0f} — bottom {100-pct:.0f}th percentile. Weak relative performance."
            elif pct < REBALANCER_TRIM_PERCENTILE:
                verdict = "TRIM"; reason = f"Score {curr_score:.0f} — slipped to {pct:.0f}th percentile. Monitor closely."
            else:
                verdict = "HOLD"; reason = f"Score {curr_score:.0f} — still competitive"
        health.append({"ticker": ticker, "name": stock.get("name", ticker),
                       "rebalancer_verdict": verdict, "reason": reason,
                       "current_score": round(curr_score, 1) if curr_score is not None else None})
    return health


# ──────────────────────────────────────────────────────────────────────
# ADVISORY GENERATOR
# ──────────────────────────────────────────────────────────────────────

def generate_advisory(top7_rows, health, live_holdings):
    live_tickers = {s["ticker"] for s in live_holdings}
    top7_tickers = {r["ticker"] for r in top7_rows}
    exit_stocks  = [h for h in health if h["rebalancer_verdict"] == "EXIT"]
    new_picks    = [r for r in top7_rows if r["ticker"] not in live_tickers]
    n_hold = len([h for h in health if h["rebalancer_verdict"] == "HOLD"])
    n_trim = len([h for h in health if h["rebalancer_verdict"] == "TRIM"])

    if exit_stocks and new_picks:
        action = "EXIT_AND_ADD"; sell_ticker = exit_stocks[0]["ticker"]; buy_ticker = new_picks[0]["ticker"]
    elif exit_stocks:
        action = "EXIT"; sell_ticker = exit_stocks[0]["ticker"]; buy_ticker = None
    elif new_picks and len(live_tickers) < MAX_PICKS:
        action = "ADD"; sell_ticker = None; buy_ticker = new_picks[0]["ticker"]
    else:
        action = "HOLD"; sell_ticker = None; buy_ticker = None

    if action == "HOLD":
        if not live_tickers:
            reasoning = f"Portfolio is empty. Add from this month's top {MAX_PICKS} tech/semiconductor picks below."
        else:
            reasoning = f"All {n_hold} holding{'s' if n_hold!=1 else ''} remain in the top picks. Portfolio is strong — no changes needed this month."
    elif action == "ADD":
        reasoning = f"{len(new_picks)} new high-conviction pick{'s' if len(new_picks)!=1 else ''} identified. {n_hold} current holding{'s' if n_hold!=1 else ''} remain in top {MAX_PICKS}. Add {buy_ticker} to fill an open slot."
    elif action == "EXIT":
        reasoning = f"{exit_stocks[0]['ticker']} has weakened — {exit_stocks[0]['reason']}. Exit to free up capital."
    else:
        reasoning = f"{exit_stocks[0]['ticker']} underperforming ({exit_stocks[0]['reason']}). Rotate into {buy_ticker}, this month's top new pick."
    if n_trim:
        reasoning += f" ({n_trim} holding{'s' if n_trim!=1 else ''} flagged for monitoring — verdicts shown below.)"

    return {
        "action":     action,
        "buy_ticker": buy_ticker,
        "sell_ticker":sell_ticker,
        "reasoning":  reasoning,
        "top_picks": [
            {"ticker": r["ticker"], "name": r.get("name", r["ticker"]),
             "sector": r.get("sector", "Technology"), "score": int(round(r["final_score"]))}
            for r in top7_rows
        ],
        "holdings_health":  health,
        "generated_at":     datetime.now().strftime("%Y-%m-%d"),
        "current_holdings": len(live_tickers),
        "source":           "screener",
    }


# ──────────────────────────────────────────────────────────────────────
# MAIN RUNNER
# ──────────────────────────────────────────────────────────────────────

def run_screener():
    print("\n" + "="*60)
    print("  🇺🇸 US TECH/SEMI SCREENER — MONTHLY RUN")
    print(f"  Date  : {datetime.now().strftime('%d %B %Y')}")
    print(f"  Budget: ${BUDGET:,.0f}  |  Max picks: {MAX_PICKS}")
    print("="*60)

    print("\n  Step 1: Building tech/semi universe from S&P 500...")
    universe_tickers = get_tech_universe()
    print(f"  📋 {len(universe_tickers)} tech/semi stocks in universe")

    print("\n  Step 2: Fetching current live holdings from API...")
    live_holdings = fetch_live_holdings()
    live_tickers  = [s["ticker"] for s in live_holdings]
    print(f"  📂 {len(live_holdings)} live holding(s): {', '.join(live_tickers) or 'none'}")

    print("\n  Step 3: Screening universe (30-60 min)...")
    scored_df = screen_universe(universe_tickers, live_extra_tickers=live_tickers)
    if scored_df.empty:
        print("  ❌ No stocks passed screening. Aborting."); return None, None

    print(f"\n  Step 4: Selecting top {MAX_PICKS} with correlation filter...")
    corr   = calculate_correlation_matrix(scored_df["ticker"].head(40).tolist())
    top_df = select_low_correlation_picks(scored_df, MAX_PICKS, corr, max_corr=0.75)
    top7_rows    = top_df.to_dict("records")
    top7_tickers = {r["ticker"] for r in top7_rows}
    print(f"  ✅ Top {len(top7_rows)}: {', '.join(r['ticker'] for r in top7_rows)}")

    print("\n  Step 5: Running rebalancer on live holdings...")
    health = run_rebalancer(live_holdings, scored_df, top7_tickers)
    for h in health:
        icon = {"HOLD": "✅", "TRIM": "⚠️", "EXIT": "🔴"}.get(h["rebalancer_verdict"], "❓")
        print(f"    {icon} {h['ticker']} → {h['rebalancer_verdict']}: {h['reason']}")

    print("\n  Step 6: Generating advisory...")
    advisory = generate_advisory(top7_rows, health, live_holdings)
    print(f"  📋 Action: {advisory['action']}")

    # Attach full audit trail + stop-loss to each pick
    scored_map = {r["ticker"]: r for _, r in scored_df.iterrows()}
    for row in top7_rows:
        sr = scored_map.get(row["ticker"], row)
        bp = row.get("current_price", 0)
        row["audit_trail"]   = generate_audit_trail(sr)
        row["buy_date"]      = datetime.now().strftime("%Y-%m-%d")
        per_slot             = BUDGET / MAX_PICKS
        row["allocation_usd"]= round(per_slot, 2)
        # US brokers support fractional shares, and at ~$143/slot most quality
        # tech names cost more than one slot. Use fractional shares so allocation
        # is accurate instead of silently flooring to 0 while booking full cash.
        row["approx_shares"] = round(per_slot / bp, 4) if bp > 0 else 0
        atr = compute_atr_stops(row["ticker"], bp)
        row.update(atr)

    return top7_rows, advisory


def post_advisory(top7_rows, advisory):
    api = os.getenv("API_URL", "https://web-production-50eee.up.railway.app")
    dd  = os.getenv("DATA_DIR", ".")
    os.makedirs(dd, exist_ok=True)

    def _post(url, payload):
        req = _ur.Request(url, data=payload,
                          headers={"Content-Type": "application/json", **_UPLOAD_AUTH},
                          method="POST")
        with _ur.urlopen(req, timeout=30) as r:
            return r.read().decode()

    # Save picks to disk
    ts = datetime.now().strftime("%Y%m")
    picks_data = {"top_picks": {"label": "Tech/Semi Monthly Picks", "stocks": top7_rows}}
    picks_path = os.path.join(dd, f"us_portfolio_{ts}.json")
    with open(picks_path, "w") as f:
        json.dump(picks_data, f, indent=2, default=str)
    print(f"\n  ✅ Picks saved: {picks_path}")

    # POST advisory → this is what the dashboard reads
    try:
        body = _post(f"{api}/us/advisory/upload", json.dumps(advisory, default=str).encode())
        print(f"  ✅ Advisory POSTed: {body}")
    except Exception as e:
        print(f"  ⚠️  Could not POST advisory: {e}")

    # POST picks → for individual stock detail cards
    try:
        body = _post(f"{api}/us/portfolio/picks/upload", json.dumps(picks_data, default=str).encode())
        print(f"  ✅ Picks POSTed: {body}")
    except Exception as e:
        print(f"  ⚠️  Could not POST picks: {e}")

    # Seed live portfolio only if it's empty
    try:
        with _ur.urlopen(f"{api}/us/portfolio/live", timeout=10) as r:
            existing = json.loads(r.read())
        has = any(len(v.get("stocks", [])) > 0 for v in existing.values() if isinstance(v, dict))
        if not has:
            body = _post(f"{api}/us/portfolio/live/upload", json.dumps(picks_data, default=str).encode())
            print(f"  ✅ Live portfolio seeded")
        else:
            print(f"  ℹ️  Live portfolio exists — not overwriting")
    except Exception as e:
        print(f"  ⚠️  Could not seed live: {e}")


# ──────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    top7_rows, advisory = run_screener()
    if advisory:
        print("\n" + "="*60)
        print("  📊 US ADVISORY SUMMARY")
        print("="*60)
        print(f"  Action   : {advisory['action']}")
        print(f"  Reasoning: {advisory['reasoning']}")
        print(f"\n  Top {MAX_PICKS} Picks:")
        for i, p in enumerate(advisory["top_picks"], 1):
            row = next((r for r in top7_rows if r["ticker"] == p["ticker"]), {})
            print(f"  {i}. {p['ticker']:<8} Score:{p['score']:>4} | ${row.get('current_price',0):>9,.2f} | {row.get('approx_shares',0)}sh | SL:${row.get('stop_loss_price',0):.2f}")
        print(f"\n  Holdings health:")
        for h in advisory.get("holdings_health", []):
            icon = {"HOLD": "✅", "TRIM": "⚠️", "EXIT": "🔴"}.get(h["rebalancer_verdict"], "❓")
            print(f"  {icon} {h['ticker']}: {h['rebalancer_verdict']} — {h['reason']}")
        post_advisory(top7_rows, advisory)
