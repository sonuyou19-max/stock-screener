"""
US Stock Screener - Monthly
============================
Exact mirror of screener.py for the US market.
Every function, every pipeline step, every adjustment is identical.
Only market-specific values change.

Buckets (equiv. India buckets):
  TECH             80%  — AI · Cloud · Semiconductors · Chips · Quantum
  DEFENSIVE_DIV    20%  — Dividend + Stability

Schedule: 30 2 3 * *
"""

import yfinance as yf

# Sent with every API POST; uploads are rejected when the server has
# UPLOAD_TOKEN set and this env var is missing or wrong.
import os as _os_tok
_UPLOAD_AUTH = {"X-Upload-Token": _os_tok.environ["UPLOAD_TOKEN"]} if _os_tok.getenv("UPLOAD_TOKEN") else {}

import pandas as pd
import numpy as np
import json
import time
import warnings
from datetime import datetime
from typing import Optional

from sp500_universe import (
    fetch_sp500,
    map_to_buckets,
    passes_fundamental_filters,
    INSIDER_HIGH,
    INSIDER_LOW,
    INSIDER_NORMAL,
    INSTITUTION_HIGH,
    INSTITUTION_NORMAL,
)

warnings.filterwarnings("ignore")

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _load_prev_institutional(output_dir="./outputs"):
    import glob, os
    patterns = [
        os.path.join(os.getenv("DATA_DIR", "."), "us_portfolio_*.json"),
        os.path.join(output_dir, "us_portfolio_*.json"),
        "/data/us_portfolio_*.json",
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
                pct = stock.get("institutional_pct")
                if ticker and pct is not None:
                    result[ticker] = pct
        return result
    except Exception:
        return {}

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

BUDGET = 1_000
MONTHLY_REFRESH = True

ATR_MULTIPLIERS = {
    "TECH":          2.5,
    "DEFENSIVE_DIV": 2.0,
}
ATR_PERIOD = 14
ATR_TRAIL_MULT = 1.5

MIN_ADV = {
    "TECH":          500_000,
    "DEFENSIVE_DIV": 200_000,
}
MIN_ADTV_USD_M = 10.0

BUCKETS = {
    "TECH": {
        "label": "🚀 Technology — AI · Cloud · Semiconductors · Chips · Quantum",
        "allocation_pct": 0.80,   # 80% in tech
        "picks": 4,               # up to 4 stocks (~$200 each)
        "scoring_weights": {
            "peg_score":           0.15,
            "roe_score":           0.20,
            "revenue_growth_score":0.35,
            "debt_score":          0.10,
            "momentum_score":      0.20,
        },
    },
    "DEFENSIVE_DIV": {
        "label": "🌾 Defensive — Dividend + Stability",
        "allocation_pct": 0.20,   # 20% defensive
        "picks": 2,               # 2 stocks (~$100 each)
        "scoring_weights": {
            "peg_score":           0.20,
            "roe_score":           0.25,
            "revenue_growth_score":0.15,
            "debt_score":          0.15,
            "momentum_score":      0.25,  # increased — avoid downtrending stocks
        },
    },
}

# ─────────────────────────────────────────────
# ATR CALCULATOR
# ─────────────────────────────────────────────

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
    except Exception:
        return None

def compute_atr_stops(ticker, buy_price, bucket_key):
    FALLBACK_PCT = {
        "TECH":0.12,"DEFENSIVE_DIV":0.10,
    }
    atr = calculate_atr(ticker)
    mult = ATR_MULTIPLIERS.get(bucket_key, 3.0)
    if atr and atr > 0:
        sl = round(buy_price - mult * atr, 2)
        trail = round(ATR_TRAIL_MULT * atr, 2)
        sl_pct = round((buy_price - sl) / buy_price * 100, 2)
        src = "ATR"
    else:
        fp = FALLBACK_PCT.get(bucket_key, 0.15)
        sl = round(buy_price * (1 - fp), 2)
        trail = round(buy_price * 0.05, 2)
        sl_pct = round(fp * 100, 2)
        atr = None
        src = "FALLBACK_FIXED_PCT"
    return {
        "atr_14day": atr, "atr_multiplier": mult,
        "stop_loss_price": sl, "stop_loss_pct": sl_pct,
        "trailing_stop_dist": trail, "atr_source": src,
    }

# ─────────────────────────────────────────────
# DATA FETCHER
# ─────────────────────────────────────────────

def fetch_stock_data(ticker, bucket_key=""):
    try:
        import math as _math
        stock = yf.Ticker(ticker)
        info = stock.info
        if not info or info.get("regularMarketPrice") is None:
            return None
        # 1y history: gives a true 6-month momentum window plus a 200-DMA
        # for the downtrend gate (6mo data could not support either).
        hist = stock.history(period="1y")
        if hist.empty or len(hist) < 20:
            return None

        current_price = None
        try:
            fi = stock.fast_info
            fp = getattr(fi,'last_price',None) or getattr(fi,'regularMarketPrice',None)
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
        min_adv = MIN_ADV.get(bucket_key, 300_000)
        adtv_usd_m = round((adv_30d * current_price) / 1_000_000, 2)
        if adv_30d < min_adv:
            print(f"    ⛔ {ticker} excluded — ADV {adv_30d:,.0f} < min {min_adv:,.0f}")
            return None
        if adtv_usd_m < MIN_ADTV_USD_M:
            print(f"    ⛔ {ticker} excluded — ADTV ${adtv_usd_m:.1f}M < min ${MIN_ADTV_USD_M}M")
            return None

        p1m = hist["Close"].iloc[-22] if len(hist)>=22 else hist["Close"].iloc[0]
        p3m = hist["Close"].iloc[-66] if len(hist)>=66 else hist["Close"].iloc[0]
        p6m = hist["Close"].iloc[-126] if len(hist)>=126 else hist["Close"].iloc[0]
        mom_1m = (current_price/p1m - 1)*100
        mom_3m = (current_price/p3m - 1)*100
        mom_6m = (current_price/p6m - 1)*100

        # ── Downtrend hard gates (mirror of swing scanner fix) ──
        # A monthly pick held 30+ days must not be a falling knife: deep
        # 6-month declines and entries below a falling long-term average
        # were the main source of instant stop-loss hits.
        closes = hist["Close"].dropna()
        dma200 = float(closes.rolling(200).mean().iloc[-1]) if len(closes) >= 200 else None
        if mom_6m < -25:
            print(f"    ⛔ {ticker} excluded — 6M momentum {mom_6m:.0f}% (falling knife)")
            return None
        if dma200 and current_price < dma200 and mom_3m < 0:
            print(f"    ⛔ {ticker} excluded — below 200-DMA with negative 3M momentum (downtrend)")
            return None
        vol_10d = hist["Volume"].iloc[-10:].mean()
        vol_30d_v = hist["Volume"].iloc[-30:].mean()
        volume_ratio = vol_10d/vol_30d_v if vol_30d_v > 0 else 1.0
        # `or` not `.get(default)`: yfinance returns these keys with None
        h52 = info.get("fiftyTwoWeekHigh") or (float(closes.max()) if len(closes) else current_price)
        l52 = info.get("fiftyTwoWeekLow")  or (float(closes.min()) if len(closes) else current_price)
        price_pos = (current_price-l52)/(h52-l52) if h52!=l52 else 0.5

        pe = info.get("trailingPE")
        earn_g_raw = info.get("earningsGrowth")
        roe_raw = info.get("returnOnEquity")
        rev_g_raw = info.get("revenueGrowth")
        roe_pct = round(roe_raw*100,2) if roe_raw is not None else None
        earn_g_pct = round(earn_g_raw*100,2) if earn_g_raw is not None else None
        rev_g_pct = round(rev_g_raw*100,2) if rev_g_raw is not None else None
        if pe and pe>0 and earn_g_pct and earn_g_pct>0:
            peg = min(round(pe/earn_g_pct,2), 10.0)
        else:
            peg = None
        mktcap_m = round((info.get("marketCap") or 0)/1_000_000, 0)

        return {
            "ticker": ticker,
            "name": info.get("longName", ticker),
            "sector": info.get("sector","Unknown"),
            "industry": info.get("industry","Unknown"),
            "current_price": round(current_price,2),
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
            "momentum_1m": round(mom_1m,2), "momentum_3m": round(mom_3m,2), "momentum_6m": round(mom_6m,2),
            "volume_ratio": round(volume_ratio,2),
            "price_position_52w": round(price_pos,2),
            "high_52w": round(h52,2), "low_52w": round(l52,2),
            "adv_30d": round(adv_30d,0), "adtv_usd_m": adtv_usd_m,
            "insider_pct": round(info.get("heldPercentInsiders",0)*100,2) if info.get("heldPercentInsiders") is not None else None,
            "institutional_pct": round(info.get("heldPercentInstitutions",0)*100,2) if info.get("heldPercentInstitutions") is not None else None,
        }
    except Exception:
        return None

# ─────────────────────────────────────────────
# SCORING ENGINE (identical to screener.py)
# ─────────────────────────────────────────────

def score_stock(row, weights):
    scores = {}
    peg = row.get("peg_ratio"); pe = row.get("pe_ratio")
    if peg and peg>0: scores["peg_raw"]=peg; scores["pe_raw"]=pe
    elif pe and pe>0: scores["peg_raw"]=round(pe/10,2); scores["pe_raw"]=pe
    else: scores["peg_raw"]=None; scores["pe_raw"]=None
    roe = row.get("roe"); scores["roe_raw"]=(roe*100) if roe is not None else None
    rg = row.get("revenue_growth"); scores["revenue_growth_raw"]=(rg*100) if rg is not None else None
    de = row.get("debt_to_equity"); scores["debt_raw"]=de if de is not None else None
    m1=row.get("momentum_1m",0) or 0; m3=row.get("momentum_3m",0) or 0; vr=row.get("volume_ratio",1.0) or 1.0
    scores["momentum_raw"]=(0.4*m1)+(0.4*m3)+(0.2*(vr-1)*10)
    return scores

def normalise_and_compute_final(df, weights):
    # Percentile rank instead of min-max: one extreme outlier no longer
    # compresses every other stock's score toward 0 on that dimension.
    def minmax(series, invert=False):
        clean=series.dropna()
        if clean.empty or clean.max()==clean.min(): return pd.Series([50.0]*len(series),index=series.index)
        n=series.rank(pct=True)*100
        return (100-n if invert else n).fillna(50)
    df["peg_score"]            = minmax(df["peg_raw"],invert=True)
    df["roe_score"]            = minmax(df["roe_raw"])
    df["revenue_growth_score"] = minmax(df["revenue_growth_raw"])
    df["debt_score"]           = minmax(df["debt_raw"],invert=True)
    df["momentum_score"]       = minmax(df["momentum_raw"])
    df["final_score"] = (
        df["peg_score"]*weights["peg_score"] + df["roe_score"]*weights["roe_score"] +
        df["revenue_growth_score"]*weights["revenue_growth_score"] +
        df["debt_score"]*weights["debt_score"] + df["momentum_score"]*weights["momentum_score"]
    )
    return df.sort_values("final_score",ascending=False)

# ─────────────────────────────────────────────
# CORRELATION ENGINE (identical to screener.py)
# ─────────────────────────────────────────────

def calculate_correlation_matrix(tickers, period="60d"):
    print(f"    📐 Calculating correlations for {len(tickers)} stocks...")
    price_data = {}
    for ticker in tickers:
        try:
            hist = yf.Ticker(ticker).history(period=period)
            if not hist.empty and len(hist)>=20: price_data[ticker]=hist["Close"]
            time.sleep(0.2)
        except Exception: continue
    if len(price_data)<2: return pd.DataFrame()
    return pd.DataFrame(price_data).dropna(how="all").pct_change().dropna().corr()

def select_low_correlation_picks(df, n_picks, corr_matrix, max_corr=0.75):
    if corr_matrix.empty:
        print(f"    ⚠️  No correlation data — using top-{n_picks} by score")
        return df.head(n_picks)
    def _pick(threshold):
        selected=[]
        for _,row in df.iterrows():
            ticker=row["ticker"]
            if len(selected)>=n_picks: break
            if not selected: selected.append(ticker); continue
            ok=True
            for sel in selected:
                if ticker in corr_matrix.index and sel in corr_matrix.index:
                    if abs(corr_matrix.loc[ticker,sel])>threshold:
                        print(f"    ↩️  {ticker} skipped — corr({ticker},{sel})={corr_matrix.loc[ticker,sel]:.2f}>{threshold}")
                        ok=False; break
            if ok: selected.append(ticker)
        return selected
    picks=_pick(max_corr)
    if len(picks)<n_picks: print(f"    ⚠️  Only {len(picks)} low-corr picks — relaxing to 0.85"); picks=_pick(0.85)
    if len(picks)<n_picks: print(f"    ⚠️  Still short — falling back to top-{n_picks}"); picks=df["ticker"].head(n_picks).tolist()
    print(f"    ✅ Selected {len(picks)} diversified picks: {picks}")
    return df[df["ticker"].isin(picks)].copy()

# ─────────────────────────────────────────────
# EARNINGS FRESHNESS (identical to screener.py)
# ─────────────────────────────────────────────

STALE_DATA_DAYS=120; DETERIORATION_PCT=20; MISS_THRESHOLD_PCT=10

def check_earnings_freshness(ticker):
    result={"last_reported_date":None,"data_age_days":None,"earnings_trend":"unknown",
            "earnings_miss":False,"freshness_penalty":0,"exclude":False,"notes":""}
    try:
        stock=yf.Ticker(ticker)
        q=stock.quarterly_financials
        if q is None or q.empty: result["notes"]="No quarterly financials"; result["freshness_penalty"]=5; return result
        last_date=q.columns[0]
        if hasattr(last_date,"date"): last_date=last_date.date()
        age=(datetime.now().date()-last_date).days
        result["last_reported_date"]=str(last_date); result["data_age_days"]=age
        if age>STALE_DATA_DAYS: result["notes"]+=f"Stale data ({age}d). "; result["freshness_penalty"]+=5
        # Net income QoQ trend from quarterly_financials (already fetched).
        # The old quarterly_earnings "Actual"/"Estimate" check was dead code —
        # that API was removed from yfinance and never carried those columns.
        ni=None
        for label in ("Net Income","Net Income Common Stockholders"):
            if label in q.index: ni=q.loc[label].dropna(); break
        if ni is not None and len(ni)>=2:
            latest=float(ni.iloc[0])
            # Same-quarter YoY when 5 quarters exist — sequential QoQ flags
            # every seasonal business as "deteriorating" each soft quarter.
            if len(ni)>=5:
                base=float(ni.iloc[4]); lbl="YoY"
                prior_latest=float(ni.iloc[1]); prior_base=float(ni.iloc[5]) if len(ni)>=6 else None
            else:
                base=float(ni.iloc[1]); lbl="QoQ"
                prior_latest=float(ni.iloc[1]); prior_base=float(ni.iloc[2]) if len(ni)>=3 else None
            chg=(latest-base)/abs(base)*100 if base!=0 else 0
            if chg<-DETERIORATION_PCT:
                result["notes"]+=f"Net income down {abs(chg):.1f}% {lbl}. "
                if prior_base is not None:
                    prev_chg=(prior_latest-prior_base)/abs(prior_base)*100 if prior_base!=0 else 0
                    if prev_chg<-DETERIORATION_PCT:
                        result["earnings_trend"]="double_deterioration"; result["exclude"]=True
                        result["freshness_penalty"]+=15; result["notes"]+="⛔ Double deterioration."; return result
                result["earnings_trend"]="deteriorating"; result["freshness_penalty"]+=10
            elif chg>5: result["earnings_trend"]="improving"; result["notes"]+=f"Net income up {chg:.1f}% {lbl} ✅. "
            else: result["earnings_trend"]="stable"; result["notes"]+=f"Net income stable ({chg:+.1f}% {lbl}). "
            if latest<0: result["freshness_penalty"]+=5; result["notes"]+="⚠️ Latest quarter loss-making. "
        result["freshness_penalty"]=min(result["freshness_penalty"],20)
        if not result["notes"]: result["notes"]="Earnings data healthy ✅"
    except Exception as ex:
        result["notes"]=f"Could not fetch: {ex}"; result["freshness_penalty"]=3
    return result

# ─────────────────────────────────────────────
# MARGIN HEALTH (identical to screener.py)
# ─────────────────────────────────────────────

def check_margin_health(data):
    result={"divergence":None,"margin_trend":"unknown","margin_penalty":0,"margin_bonus":0,"net_adjustment":0,"margin_notes":""}
    rev_g=data.get("revenue_growth_pct"); profit_g=data.get("earnings_growth_pct")
    gross_m=data.get("gross_margin"); profit_m=data.get("profit_margin")
    if rev_g is not None and profit_g is not None:
        div=rev_g-profit_g; result["divergence"]=round(div,1)
        if div>30: result["margin_trend"]="severe_compression"; result["margin_penalty"]=10; result["margin_notes"]+=f"⛔ Severe compression: rev+{rev_g:.1f}% profit+{profit_g:.1f}%. "
        elif div>15: result["margin_trend"]="compressing"; result["margin_penalty"]=5; result["margin_notes"]+=f"⚠️ Compressing: rev+{rev_g:.1f}% profit+{profit_g:.1f}%. "
        elif div<-5: result["margin_trend"]="expanding"; result["margin_bonus"]=5; result["margin_notes"]+=f"✅ Expanding: profit+{profit_g:.1f}% > rev+{rev_g:.1f}%. "
        else: result["margin_trend"]="stable"; result["margin_notes"]+=f"Rev+{rev_g:.1f}% Profit+{profit_g:.1f}% stable. "
    if gross_m is not None:
        gp=round(gross_m*100,1)
        if gp<10: result["margin_penalty"]+=5; result["margin_notes"]+=f"⚠️ Low gross margin {gp}%. "
        elif gp>40: result["margin_bonus"]+=3; result["margin_notes"]+=f"✅ Strong gross margin {gp}%. "
    if profit_m is not None:
        pp=round(profit_m*100,1)
        if pp<5: result["margin_penalty"]+=3; result["margin_notes"]+=f"⚠️ Thin net margin {pp}%. "
        elif pp>20: result["margin_bonus"]+=2; result["margin_notes"]+=f"✅ Strong net margin {pp}%. "
    result["margin_penalty"]=min(result["margin_penalty"],15); result["margin_bonus"]=min(result["margin_bonus"],8)
    result["net_adjustment"]=result["margin_bonus"]-result["margin_penalty"]
    if not result["margin_notes"]: result["margin_notes"]="Insufficient margin data."
    return result

# ─────────────────────────────────────────────
# INSIDER/INSTITUTIONAL SIGNAL (promoter equiv.)
# ─────────────────────────────────────────────

def check_promoter_signal(data):
    result={"insider_pct":data.get("insider_pct"),"institutional_pct":data.get("institutional_pct"),
            "promoter_signal":"unknown","institution_signal":"unknown",
            "promoter_bonus":0,"promoter_penalty":0,"net_promoter_adj":0,"promoter_notes":""}
    insider=data.get("insider_pct"); instit=data.get("institutional_pct")
    ind=(data.get("industry") or "").lower()
    is_inst=any(k in ind for k in ("bank","insurance","reit","utility"))
    if insider is not None:
        if is_inst and insider<INSIDER_LOW: result["promoter_signal"]="normal"; result["promoter_notes"]+=f"Institutional mgmt — low insider normal ({insider:.1f}%). "
        elif insider>=INSIDER_HIGH: result["promoter_signal"]="strong"; result["promoter_bonus"]=5; result["promoter_notes"]+=f"✅ Strong insider ({insider:.1f}%). "
        elif insider>=INSIDER_NORMAL: result["promoter_signal"]="normal"; result["promoter_notes"]+=f"Insider normal ({insider:.1f}%). "
        elif insider>=INSIDER_LOW: result["promoter_signal"]="low"; result["promoter_penalty"]=2; result["promoter_notes"]+=f"⚠️ Low insider ({insider:.1f}%). "
        else: result["promoter_signal"]="very_low"; result["promoter_penalty"]=5; result["promoter_notes"]+=f"⛔ Very low insider ({insider:.1f}%). "
    if instit is not None:
        if instit>=INSTITUTION_HIGH: result["institution_signal"]="high"; result["promoter_bonus"]+=3; result["promoter_notes"]+=f"✅ High institutional {instit:.1f}%. "
        elif instit>=INSTITUTION_NORMAL: result["institution_signal"]="normal"; result["promoter_notes"]+=f"Inst normal {instit:.1f}%. "
        else: result["institution_signal"]="low"; result["promoter_penalty"]+=3; result["promoter_notes"]+=f"⚠️ Low inst {instit:.1f}%. "
    if insider is not None and insider>=INSIDER_HIGH and instit is not None and instit>=INSTITUTION_HIGH:
        result["promoter_bonus"]+=2; result["promoter_notes"]+="🏆 Double conviction. "
    result["promoter_bonus"]=min(result["promoter_bonus"],10); result["promoter_penalty"]=min(result["promoter_penalty"],10)
    result["net_promoter_adj"]=result["promoter_bonus"]-result["promoter_penalty"]
    return result

# ─────────────────────────────────────────────
# INSTITUTIONAL TREND (identical to screener.py)
# ─────────────────────────────────────────────

def check_institutional_trend(ticker, current_inst_pct, prev_inst_pct=None):
    result={"inst_change_pp":None,"inst_trend":"unknown","inst_trend_bonus":0,
            "inst_trend_penalty":0,"net_inst_adj":0,"inst_trend_notes":"","holder_count":None}
    if current_inst_pct is not None and prev_inst_pct is not None:
        chg=round(current_inst_pct-prev_inst_pct,2); result["inst_change_pp"]=chg
        if chg>=2.0: result["inst_trend"]="accumulating"; result["inst_trend_bonus"]=5; result["inst_trend_notes"]+=f"✅ Accumulating +{chg:.1f}pp. "
        elif chg<=-5.0: result["inst_trend"]="exiting_fast"; result["inst_trend_penalty"]=10; result["inst_trend_notes"]+=f"⛔ Fast exit {chg:.1f}pp. "
        elif chg<=-2.0: result["inst_trend"]="distributing"; result["inst_trend_penalty"]=5; result["inst_trend_notes"]+=f"⚠️ Distributing {chg:.1f}pp. "
        else: result["inst_trend"]="stable"; result["inst_trend_notes"]+=f"Stable {chg:+.1f}pp. "
    try:
        holders=yf.Ticker(ticker).institutional_holders
        if holders is not None and not holders.empty:
            hc=len(holders); result["holder_count"]=hc
            if prev_inst_pct is None:
                if hc>=15: result["inst_trend"]="well_covered"; result["inst_trend_bonus"]=3; result["inst_trend_notes"]+=f"✅ {hc} holders. "
                elif hc>=5: result["inst_trend"]="moderate_coverage"; result["inst_trend_notes"]+=f"{hc} holders. "
                else: result["inst_trend"]="low_coverage"; result["inst_trend_penalty"]=3; result["inst_trend_notes"]+=f"⚠️ Only {hc} holders. "
    except Exception: pass
    result["inst_trend_bonus"]=min(result["inst_trend_bonus"],8); result["inst_trend_penalty"]=min(result["inst_trend_penalty"],10)
    result["net_inst_adj"]=result["inst_trend_bonus"]-result["inst_trend_penalty"]
    if not result["inst_trend_notes"].strip(): result["inst_trend_notes"]="Inst trend data unavailable."
    return result

# ─────────────────────────────────────────────
# CIRCUIT/VOLATILITY RISK (identical to screener.py)
# ─────────────────────────────────────────────

def check_circuit_risk(data):
    result={"circuit_risk":"low","circuit_penalty":0,"circuit_exclude":False,"circuit_notes":""}
    beta=data.get("beta"); adtv=data.get("adtv_usd_m",0) or 0
    price_pos=data.get("price_position_52w"); cp=data.get("current_price",0); h52=data.get("high_52w",cp)
    if beta is not None:
        if beta>2.5: result["circuit_penalty"]+=8; result["circuit_risk"]="extreme"; result["circuit_notes"]+=f"⛔ Extreme beta {beta:.1f}. "
        elif beta>2.0: result["circuit_penalty"]+=5; result["circuit_risk"]="elevated"; result["circuit_notes"]+=f"⚠️ High beta {beta:.1f}. "
        elif beta>1.5: result["circuit_penalty"]+=2; result["circuit_notes"]+=f"Beta {beta:.1f} moderate. "
        else: result["circuit_notes"]+=f"Beta {beta:.1f} acceptable. "
    if price_pos is not None and h52 and cp:
        dd=1-(cp/h52)
        if dd>0.40: result["circuit_penalty"]+=5; result["circuit_risk"]="moderate" if result["circuit_risk"]=="low" else result["circuit_risk"]; result["circuit_notes"]+=f"⚠️ {dd*100:.0f}% below 52w high. "
        elif dd>0.25: result["circuit_penalty"]+=2; result["circuit_notes"]+=f"{dd*100:.0f}% below 52w high. "
        else: result["circuit_notes"]+=f"Price healthy {dd*100:.0f}% below 52w high. "
    if 0<adtv<20.0: result["circuit_penalty"]+=3; result["circuit_risk"]="moderate" if result["circuit_risk"]=="low" else result["circuit_risk"]; result["circuit_notes"]+=f"⚠️ ADTV ${adtv:.1f}M thin. "
    if beta and beta>2.5 and adtv<20.0: result["circuit_exclude"]=True; result["circuit_risk"]="extreme"; result["circuit_notes"]+=f"⛔ HARD EXCLUDE beta {beta:.1f} + ADTV ${adtv:.1f}M. "
    result["circuit_penalty"]=min(result["circuit_penalty"],12)
    if not result["circuit_notes"].strip(): result["circuit_notes"]="Volatility risk: low ✅"
    return result

# ─────────────────────────────────────────────
# SHORT INTEREST / DILUTION (pledge equiv.)
# ─────────────────────────────────────────────

def check_pledge_dilution(ticker, data):
    result={"pledge_risk":"low","dilution_flag":False,"short_interest":None,"float_ratio":None,
            "shares_growth":None,"pledge_penalty":0,"dilution_penalty":0,"net_pledge_adj":0,"pledge_notes":""}
    try:
        stock=yf.Ticker(ticker); info=stock.info
        spf=info.get("shortPercentOfFloat")
        if spf is not None:
            sp=round(spf*100,2); result["short_interest"]=sp
            if sp>=5.0: result["pledge_risk"]="high"; result["pledge_penalty"]=8; result["pledge_notes"]+=f"⛔ High short interest {sp:.1f}%. "
            elif sp>=2.0: result["pledge_risk"]="elevated"; result["pledge_penalty"]=4; result["pledge_notes"]+=f"⚠️ Elevated short {sp:.1f}%. "
            else: result["pledge_notes"]+=f"Short interest low {sp:.1f}%. "
        sf=info.get("floatShares"); so=info.get("sharesOutstanding"); ip=data.get("insider_pct",0) or 0
        if sf and so and so>0:
            fr=round(sf/so,3); result["float_ratio"]=fr
            if fr>0.65 and ip>40: result["pledge_penalty"]=max(result["pledge_penalty"],5); result["pledge_risk"]="elevated" if result["pledge_risk"]=="low" else result["pledge_risk"]; result["pledge_notes"]+=f"⚠️ Float ratio {fr:.2f} vs insider {ip:.1f}%. "
        try:
            bs=stock.quarterly_balance_sheet
            if bs is not None and not bs.empty and "Ordinary Shares Number" in bs.index:
                ss=bs.loc["Ordinary Shares Number"].dropna()
                if len(ss)>=2:
                    lat=float(ss.iloc[0]); yago=float(ss.iloc[-1])
                    if yago>0:
                        gp=round((lat/yago-1)*100,2); result["shares_growth"]=gp
                        if gp>5.0: result["dilution_flag"]=True; result["dilution_penalty"]=5; result["pledge_notes"]+=f"⚠️ Shares grew {gp:.1f}% YoY. "
                        elif gp>2.0: result["dilution_penalty"]=2; result["pledge_notes"]+=f"Mild dilution {gp:.1f}% YoY. "
                        else: result["pledge_notes"]+=f"Shares stable {gp:+.1f}% YoY. "
        except Exception: result["pledge_notes"]+="Share count history unavailable. "
        if not result["pledge_notes"].strip(): result["pledge_notes"]="No short interest or dilution concerns. ✅"
    except Exception as e: result["pledge_notes"]=f"Check failed: {e}"
    result["pledge_penalty"]=min(result["pledge_penalty"],8); result["dilution_penalty"]=min(result["dilution_penalty"],5)
    result["net_pledge_adj"]=-(result["pledge_penalty"]+result["dilution_penalty"])
    return result

# ─────────────────────────────────────────────
# AUDIT TRAIL GENERATOR (identical to screener.py)
# ─────────────────────────────────────────────

def generate_audit_trail(row, bucket_key):
    why=[]; risks=[]; adjs=[]; score_bd={}
    peg=row.get("peg_raw"); roe=row.get("roe_raw"); rev_g=row.get("revenue_growth_raw"); debt=row.get("debt_raw")
    for dim,rv,label in [("peg_score",peg,"PEG"),("roe_score",roe,"ROE"),("revenue_growth_score",rev_g,"Revenue Growth"),("debt_score",debt,"Debt Level"),("momentum_score",row.get("momentum_raw"),"Momentum")]:
        s=row.get(dim)
        if s is not None: score_bd[label]=round(s,0)
    if rev_g is not None:
        if rev_g>=25: why.append(f"Exceptional revenue growth ({rev_g:.1f}% YoY)")
        elif rev_g>=15: why.append(f"Strong revenue growth ({rev_g:.1f}% YoY)")
        elif rev_g>=8: why.append(f"Solid revenue growth ({rev_g:.1f}% YoY)")
    if roe is not None:
        if roe>=25: why.append(f"Excellent ROE ({roe:.1f}%) — highly efficient")
        elif roe>=18: why.append(f"Strong ROE ({roe:.1f}%)")
        elif roe>=12: why.append(f"Acceptable ROE ({roe:.1f}%)")
    if peg is not None:
        if peg<1.0: why.append(f"Undervalued — PEG {peg:.2f}")
        elif peg<2.0: why.append(f"Reasonably valued — PEG {peg:.2f}")
    m1=row.get("momentum_1m",0) or 0; m3=row.get("momentum_3m",0) or 0
    if m1>=10 and m3>=15: why.append(f"Strong momentum +{m1:.1f}% (1M) +{m3:.1f}% (3M)")
    elif m1>=5 or m3>=10: why.append(f"Positive momentum +{m1:.1f}% (1M) +{m3:.1f}% (3M)")
    insider=row.get("insider_pct")
    if insider is not None and insider>=INSIDER_HIGH: why.append(f"Strong insider conviction ({insider:.1f}%)")
    et=row.get("earnings_trend","")
    if et=="improving": why.append("Earnings improving QoQ")
    elif et=="stable": why.append("Earnings stable")
    mt=row.get("margin_trend","")
    if mt=="expanding": why.append("Margins expanding")
    it=row.get("inst_trend","")
    if it=="accumulating": why.append("Institutions accumulating")
    elif it=="well_covered": why.append(f"Well covered by institutions ({row.get('holder_count')} holders)")
    for val,key,label2 in [(row.get("freshness_penalty",0),"freshness_penalty","Earnings freshness"),(row.get("net_adjustment",0),"net_adjustment","Margin health"),(row.get("net_promoter_adj",0),"net_promoter_adj","Insider signal"),(row.get("net_inst_adj",0),"net_inst_adj","Inst trend"),(row.get("circuit_penalty",0),"circuit_penalty","Volatility risk")]:
        if val and val!=0:
            direction="-" if key in ("freshness_penalty","circuit_penalty") else "+"
            adjs.append(f"{label2}: {direction}{abs(val):.0f} pts")
    de_lim={"TECH":400,"DEFENSIVE_DIV":250}
    limit=de_lim.get(bucket_key,300)
    if debt is not None and debt>limit*0.75: risks.append(f"D/E {debt:.0f} elevated (limit {limit})")
    pp=row.get("price_position_52w")
    if pp is not None and pp>0.85: risks.append(f"At {pp*100:.0f}% of 52w high — limited upside")
    if row.get("earnings_miss"): risks.append("Missed estimates last quarter")
    beta=row.get("beta")
    if beta and beta>1.5: risks.append(f"High beta ({beta:.1f})")
    cr=row.get("circuit_risk","low")
    if cr in ("elevated","high","extreme"): risks.append(f"Volatility risk {cr} — set stop-loss on Bolero")
    age=row.get("data_age_days")
    if age and age>120: risks.append(f"Data {age} days old — verify before buying")
    pr=row.get("pledge_risk","low"); si=row.get("short_interest")
    if pr=="high" and si: risks.append(f"High short interest ({si:.1f}%)")
    elif pr=="elevated" and si: risks.append(f"Elevated short interest ({si:.1f}%)")
    if row.get("dilution_flag"): risks.append(f"Share dilution {row.get('shares_growth',0):+.1f}% YoY")
    score=row.get("final_score",0)
    return {
        "why_picked": why if why else ["Balanced scores across all dimensions"],
        "score_breakdown": score_bd,
        "adjustments": adjs if adjs else ["No adjustments applied"],
        "risks": risks if risks else ["No significant risks"],
        "summary": f"Score {score:.1f}/100 — {(why[0] if why else 'Balanced')}. Risk: {(risks[0] if risks else 'None')}.",
    }

# ─────────────────────────────────────────────
# PORTFOLIO VOLATILITY (identical to screener.py)
# ─────────────────────────────────────────────

PORTFOLIO_BETA_BALANCED=1.0; PORTFOLIO_BETA_AGGRESSIVE=1.3; PORTFOLIO_BETA_OVERHEATED=1.6
BUCKET_BETA_WARNING=1.8; STRESS_SCENARIO_PCT=15.0

def assess_portfolio_volatility(portfolio):
    result={"weighted_beta":None,"beta_label":"unknown","est_max_drawdown":None,"bucket_betas":{},"warnings":[],"health_summary":""}
    total_alloc=0.0; wb=0.0
    DEFAULTS={"TECH":1.25,"DEFENSIVE_DIV":0.70}
    for bk,bucket in portfolio.items():
        bbetas=[]
        for s in bucket.get("stocks",[]):
            import math
            beta=s.get("beta"); alloc=s.get("allocation_usd",0)
            if beta is None or (isinstance(beta,float) and math.isnan(beta)): beta=DEFAULTS.get(bk,1.0); s["beta"]=beta
            if alloc>0: wb+=beta*alloc; total_alloc+=alloc; bbetas.append(beta)
        if bbetas: result["bucket_betas"][bk]=round(sum(bbetas)/len(bbetas),2)
    if total_alloc>0:
        pb=round(wb/total_alloc,2); result["weighted_beta"]=pb
        if pb<PORTFOLIO_BETA_BALANCED: result["beta_label"]="conservative"
        elif pb<PORTFOLIO_BETA_AGGRESSIVE: result["beta_label"]="balanced"
        elif pb<PORTFOLIO_BETA_OVERHEATED: result["beta_label"]="aggressive"
        else: result["beta_label"]="overheated"; result["warnings"].append(f"Portfolio beta {pb:.2f} overheated — add defensive stock.")
        ed=round(pb*STRESS_SCENARIO_PCT,1); result["est_max_drawdown"]=ed
        if ed>25: result["warnings"].append(f"Stress test: S&P500 -15% → portfolio -{ed}%. Set stop-losses on Bolero.")
    for bk,ab in result["bucket_betas"].items():
        if ab>BUCKET_BETA_WARNING: result["warnings"].append(f"{BUCKETS[bk]['label']} beta {ab:.2f} high — consider lower-beta swap.")
    em={"conservative":"🟢","balanced":"🟡","aggressive":"🟠","overheated":"🔴","unknown":"⚪"}.get(result["beta_label"],"⚪")
    result["health_summary"]=f"Portfolio Beta: {result['weighted_beta']:.2f} {em} {result['beta_label'].title()} | Stress (S&P500 -15%): -{result['est_max_drawdown']}%" if result["weighted_beta"] else "Beta insufficient."
    return result

# ─────────────────────────────────────────────
# BUCKET SCREENER (identical pipeline to screener.py)
# ─────────────────────────────────────────────

def screen_bucket(bucket_key, bucket_config, bucket_tickers, prev_institutional=None):
    label=bucket_config["label"]
    print(f"\n  Screening {label} ({len(bucket_tickers)} candidates)...")
    records=[]; excl_liq=0; excl_fund=0; excl_earn=0

    for ticker in bucket_tickers:
        data=fetch_stock_data(ticker, bucket_key=bucket_key)
        if data is None: excl_liq+=1; time.sleep(0.3); continue

        passed,reason=passes_fundamental_filters(data, bucket_key)
        if not passed: print(f"    ⛔ {ticker} — {reason}"); excl_fund+=1; time.sleep(0.3); continue

        freshness=check_earnings_freshness(ticker); time.sleep(0.3)
        if freshness["exclude"]: print(f"    ⛔ {ticker} — {freshness['notes'].strip()}"); excl_earn+=1; continue
        for k in ["last_reported_date","data_age_days","earnings_trend","earnings_miss","freshness_penalty","notes"]:
            data[k if k!="notes" else "earnings_notes"]=freshness[k]

        margin=check_margin_health(data)
        for k in ["divergence","margin_trend","margin_penalty","margin_bonus","net_adjustment","margin_notes"]: data[k]=margin[k]

        promoter=check_promoter_signal(data)
        for k in ["promoter_signal","institution_signal","promoter_bonus","promoter_penalty","net_promoter_adj","promoter_notes"]: data[k]=promoter[k]

        prev_pct=(prev_institutional or {}).get(ticker)
        inst=check_institutional_trend(ticker, data.get("institutional_pct"), prev_pct); time.sleep(0.2)
        for k in ["inst_change_pp","inst_trend","net_inst_adj","inst_trend_notes","holder_count"]: data[k]=inst[k]

        circuit=check_circuit_risk(data)
        if circuit["circuit_exclude"]: print(f"    ⛔ {ticker} — {circuit['circuit_notes'].strip()}"); excl_earn+=1; continue
        for k in ["circuit_risk","circuit_penalty","circuit_notes"]: data[k]=circuit[k]

        pledge=check_pledge_dilution(ticker, data); time.sleep(0.3)
        for k in ["pledge_risk","dilution_flag","short_interest","float_ratio","shares_growth","net_pledge_adj","pledge_notes"]: data[k]=pledge[k]

        scores=score_stock(data, bucket_config["scoring_weights"])
        records.append({**data, **scores}); time.sleep(0.3)

    print(f"    📊 {len(records)} passed | {excl_liq} liquidity ⛔ | {excl_fund} fundamentals ⛔ | {excl_earn} earnings ⛔")
    if not records:
        print(f"  ⚠️  No stocks passed for {bucket_key}. Consider relaxing filters in sp500_universe.BUCKET_FILTERS.")
        return pd.DataFrame()

    df=pd.DataFrame(records)
    df=normalise_and_compute_final(df, bucket_config["scoring_weights"])

    # Apply all adjustments (identical order to screener.py)
    for col,sign in [("freshness_penalty",-1),("net_adjustment",1),("net_promoter_adj",1),("net_inst_adj",1),("circuit_penalty",-1),("net_pledge_adj",1)]:
        if col in df.columns:
            df["final_score"]=(df["final_score"]+sign*df[col]).clip(lower=0,upper=100)
    return df.sort_values("final_score",ascending=False)

# ─────────────────────────────────────────────
# PORTFOLIO BUILDER (identical to screener.py)
# ─────────────────────────────────────────────

def build_portfolio(budget=BUDGET):
    portfolio={}; all_results={}
    print("\n"+"="*60)
    print("  🇺🇸 US STOCK SCREENER — MONTHLY RUN")
    print(f"  Date:   {datetime.now().strftime('%d %B %Y')}")
    print(f"  Budget: ${budget:,.0f}")
    print("="*60)

    print("\n  Step 1: Building stock universe from S&P 500...")
    sp500_df=fetch_sp500(); bucket_universe=map_to_buckets(sp500_df)

    prev_inst=_load_prev_institutional()
    print(f"  📂 {'Loaded prior institutional for '+str(len(prev_inst))+' stocks' if prev_inst else 'No prior data — first run'}")

    print("\n  Step 3: Screening each bucket...")
    for bk,cfg in BUCKETS.items():
        tickers=bucket_universe.get(bk,[])
        if not tickers: print(f"  ⚠️  No stocks mapped to {bk}"); continue

        df=screen_bucket(bk, cfg, tickers, prev_inst)
        all_results[bk]=df
        if df.empty: continue

        n=cfg["picks"]; alloc=budget*cfg["allocation_pct"]; per=alloc/n
        scored_tickers=df["ticker"].tolist()
        corr=calculate_correlation_matrix(scored_tickers)
        top=select_low_correlation_picks(df, n, corr, max_corr=0.75)

        # Skip stocks we can't afford with whole shares
        affordable_rows=[]
        for _,row in top.iterrows():
            p=row.get("current_price",0)
            if p>0 and int(per//p)>=1:
                affordable_rows.append(row)
            else:
                print(f"  ⚠️  Skipping {row['ticker']} (${p:,.0f} > ${per:.0f} per-stock budget)")

        # Backfill every dropped slot from the full ranked list — previously a
        # single unaffordable pick simply shrank the bucket and left budget idle.
        if len(affordable_rows)<n:
            picked={r["ticker"] for r in affordable_rows} | set(top["ticker"])
            for _,row in df.iterrows():
                if len(affordable_rows)>=n: break
                t=row["ticker"]; p=row.get("current_price",0)
                if t in picked or p<=0 or int(per//p)<1: continue
                too_corr=False
                if not corr.empty:
                    for r2 in affordable_rows:
                        s=r2["ticker"]
                        if t in corr.index and s in corr.index and abs(corr.loc[t,s])>0.85:
                            too_corr=True; break
                if too_corr: continue
                affordable_rows.append(row); picked.add(t)
                print(f"  ✅ Backfilled slot with {t} (${p:.0f}, score {row['final_score']:.1f})")

        portfolio[bk]={"label":cfg["label"],"allocation_pct":round(cfg["allocation_pct"]*100,1),"total_allocation":alloc,"per_stock_allocation":per,"stocks":[]}

        for row in affordable_rows:
            ticker=row["ticker"]; bp=row["current_price"]
            atr=compute_atr_stops(ticker, bp, bk); time.sleep(0.2)
            shares=int(per//bp) if bp>0 else 0; alloc_usd=round(shares*bp,2)
            portfolio[bk]["stocks"].append({
                "ticker":ticker,"name":row["name"],"price":bp,"final_score":round(row["final_score"],1),
                "pe_ratio":round(row["pe_raw"],1) if row.get("pe_raw") else "N/A",
                "peg_ratio":round(row["peg_raw"],2) if row.get("peg_raw") else "N/A",
                "pb_ratio":round(row.get("pb_ratio"),1) if row.get("pb_ratio") else "N/A",
                "roe_pct":round(row["roe_raw"],1) if row.get("roe_raw") else "N/A",
                "rev_growth_pct":round(row["revenue_growth_raw"],1) if row.get("revenue_growth_raw") else "N/A",
                "debt_equity":round(row["debt_raw"],0) if row.get("debt_raw") else "N/A",
                "momentum_1m":row["momentum_1m"],"momentum_3m":row["momentum_3m"],
                "allocation_usd":alloc_usd,"per_stock_alloc":round(per,2),"approx_shares":shares,
                "adv_30d":int(row.get("adv_30d",0)),"adtv_usd_m":row.get("adtv_usd_m",0),
                "atr_14day":atr["atr_14day"],"atr_multiplier":atr["atr_multiplier"],
                "stop_loss_price":atr["stop_loss_price"],"stop_loss_pct":atr["stop_loss_pct"],
                "trailing_stop_dist":atr["trailing_stop_dist"],"atr_source":atr["atr_source"],
                "corr_checked":True,"max_corr_threshold":0.75,
                "last_reported_date":row.get("last_reported_date","N/A"),"data_age_days":row.get("data_age_days","N/A"),
                "earnings_trend":row.get("earnings_trend","unknown"),"earnings_miss":row.get("earnings_miss",False),
                "freshness_penalty":row.get("freshness_penalty",0),"earnings_notes":row.get("earnings_notes",""),
                "divergence":row.get("divergence"),"margin_trend":row.get("margin_trend","unknown"),
                "margin_penalty":row.get("margin_penalty",0),"margin_bonus":row.get("margin_bonus",0),
                "net_adjustment":row.get("net_adjustment",0),"margin_notes":row.get("margin_notes",""),
                "insider_pct":row.get("insider_pct"),"institutional_pct":row.get("institutional_pct"),
                "promoter_signal":row.get("promoter_signal","unknown"),"institution_signal":row.get("institution_signal","unknown"),
                "net_promoter_adj":row.get("net_promoter_adj",0),"promoter_notes":row.get("promoter_notes",""),
                "inst_change_pp":row.get("inst_change_pp"),"inst_trend":row.get("inst_trend","unknown"),
                "net_inst_adj":row.get("net_inst_adj",0),"inst_trend_notes":row.get("inst_trend_notes",""),
                "holder_count":row.get("holder_count"),
                "circuit_risk":row.get("circuit_risk","low"),"circuit_penalty":row.get("circuit_penalty",0),"circuit_notes":row.get("circuit_notes",""),
                "beta":row.get("beta"),"buy_date":datetime.now().strftime("%Y-%m-%d"),
                "pledge_risk":row.get("pledge_risk","low"),"dilution_flag":row.get("dilution_flag",False),
                "short_interest":row.get("short_interest"),"shares_growth":row.get("shares_growth"),
                "net_pledge_adj":row.get("net_pledge_adj",0),"pledge_notes":row.get("pledge_notes",""),
                "audit_trail":generate_audit_trail(row, bk),
            })

    vol=assess_portfolio_volatility(portfolio)
    print(f"\n  📊 {vol['health_summary']}")
    for w in vol["warnings"]: print(f"  ⚠️  {w}")
    return portfolio, all_results, vol

# ─────────────────────────────────────────────
# SAVE + POST TO API (identical to screener.py)
# ─────────────────────────────────────────────

def save_results(portfolio, all_results):
    import os as _os, urllib.request as _ur
    ts=datetime.now().strftime("%Y%m"); dd=_os.getenv("DATA_DIR",".")
    _os.makedirs(dd,exist_ok=True)
    path=_os.path.join(dd,f"us_portfolio_{ts}.json")
    with open(path,"w") as f: json.dump(portfolio,f,indent=2,default=str)
    print(f"\n  ✅ US Portfolio saved: {path}")
    for bk,df in all_results.items():
        if not df.empty: df.to_csv(_os.path.join(dd,f"us_ranking_{bk}_{ts}.csv"),index=False)
    print(f"  ✅ Full rankings saved as CSV")
    api=_os.getenv("API_URL","https://web-production-2d832.up.railway.app")
    def _post(url,data):
        req=_ur.Request(url,data=data,headers={"Content-Type": "application/json", **_UPLOAD_AUTH},method="POST")
        with _ur.urlopen(req,timeout=15) as r: return r.read().decode()
    payload=json.dumps(portfolio,default=str).encode()
    try: body=_post(f"{api}/us/portfolio/picks/upload",payload); print(f"  ✅ US picks POSTed: {body}")
    except Exception as e: print(f"  ⚠️  Could not POST picks: {e}")
    try:
        with _ur.urlopen(f"{api}/us/portfolio/live",timeout=8) as r: existing=json.loads(r.read())
        has=any(len(v.get("stocks",[]))>0 for v in existing.values() if isinstance(v,dict))
        if not has: body=_post(f"{api}/us/portfolio/live/upload",payload); print(f"  ✅ US live seeded: {body}")
        else: print("  ℹ️  US live portfolio exists — not overwriting")
    except Exception as e: print(f"  ⚠️  Could not seed live: {e}")
    return path

# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    portfolio, all_results, vol = build_portfolio(BUDGET)
    print("\n"+"="*60+"\n  📊 US PORTFOLIO — TOP PICKS\n"+"="*60)
    for bk,bucket in portfolio.items():
        print(f"\n{bucket['label']} — {bucket['allocation_pct']}% (${bucket['total_allocation']:,.0f})")
        for s in bucket["stocks"]:
            print(f"  {s['ticker']:<8} Score:{s['final_score']:>5.1f} | ${s['price']:>10,.2f} | {s['approx_shares']}sh | SL:${s['stop_loss_price']:.2f}")
    save_results(portfolio, all_results)
