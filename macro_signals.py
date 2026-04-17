"""
Macro Signals Module
=====================
Fetches macro indicators and returns bucket-level allocation
adjustments for the screener.

Currently implements:
  2.4 — Crude Oil (Brent) price tracking
  2.5 — USD/INR currency tracking      (stub, built next)
  2.6 — Global market correlation       (stub, built next)

All signals are combined into a single macro_score per bucket
which adjusts the allocation_pct before screening begins.

Data source: Yahoo Finance (free, no API key)
  Brent Crude : BZ=F
  USD/INR     : INR=X
  S&P 500     : ^GSPC
  Nasdaq      : ^IXIC
"""

import yfinance as yf
import pandas as pd
import time
from datetime import datetime
from typing import Optional

# ─────────────────────────────────────────────
# CRUDE OIL CONFIG (2.4)
# ─────────────────────────────────────────────

CRUDE_TICKER = "BZ=F"       # Brent Crude futures on Yahoo Finance

# Price level thresholds (USD/barrel)
CRUDE_LOW      = 70.0
CRUDE_NEUTRAL  = 85.0
CRUDE_ELEVATED = 100.0

# 30-day trend thresholds (%)
CRUDE_RISING_FAST  =  15.0
CRUDE_FALLING_FAST = -15.0

# Bucket allocation multipliers based on crude signal
# Format: {level: {bucket_key: multiplier}}
# multiplier > 1.0 = increase allocation
# multiplier < 1.0 = decrease allocation
CRUDE_ALLOCATION_ADJUSTMENTS = {
    # Crude LOW + Falling — best macro environment for India
    "low_falling": {
        "BFSI_IT":         1.10,   # rate cuts likely → banks benefit
        "DEFENCE_INFRA":   1.05,   # input costs down
        "GREEN_ENERGY_EV": 0.95,   # less urgency for alternatives
        "FMCG_PHARMA":     1.10,   # raw material cost relief
    },
    # Crude LOW + Stable
    "low_stable": {
        "BFSI_IT":         1.05,
        "DEFENCE_INFRA":   1.05,
        "GREEN_ENERGY_EV": 1.00,
        "FMCG_PHARMA":     1.05,
    },
    # Crude NEUTRAL — baseline, no adjustment
    "neutral": {
        "BFSI_IT":         1.00,
        "DEFENCE_INFRA":   1.00,
        "GREEN_ENERGY_EV": 1.00,
        "FMCG_PHARMA":     1.00,
    },
    # Crude ELEVATED + Stable
    "elevated_stable": {
        "BFSI_IT":         0.95,   # inflation risk
        "DEFENCE_INFRA":   0.95,   # input cost pressure
        "GREEN_ENERGY_EV": 1.08,   # renewables more attractive
        "FMCG_PHARMA":     0.95,   # raw material pressure
    },
    # Crude ELEVATED + Rising — most stressed environment
    "elevated_rising": {
        "BFSI_IT":         0.90,   # rate hike risk → margin compression
        "DEFENCE_INFRA":   0.95,
        "GREEN_ENERGY_EV": 1.10,   # strong tailwind for alternatives
        "FMCG_PHARMA":     0.90,   # severe raw material pressure
    },
    # Crude HIGH (>$100) — emergency adjustment
    "high_rising": {
        "BFSI_IT":         0.85,
        "DEFENCE_INFRA":   0.90,
        "GREEN_ENERGY_EV": 1.15,
        "FMCG_PHARMA":     0.85,
    },
}


# ─────────────────────────────────────────────
# CRUDE OIL FETCHER (2.4)
# ─────────────────────────────────────────────

def fetch_crude_signal() -> dict:
    """
    Fetch Brent Crude price and calculate level + trend signal.

    Returns:
      price         : current Brent price in USD/barrel
      price_30d_ago : price 30 trading days ago
      change_30d    : % change over 30 days
      level         : "low" | "neutral" | "elevated" | "high"
      trend         : "falling_fast" | "stable" | "rising_fast"
      signal_key    : combined key for CRUDE_ALLOCATION_ADJUSTMENTS lookup
      adjustments   : {bucket_key: multiplier} dict
      notes         : human-readable summary
    """
    result = {
        "price":         None,
        "price_30d_ago": None,
        "change_30d":    None,
        "level":         "neutral",
        "trend":         "stable",
        "signal_key":    "neutral",
        "adjustments":   {k: 1.0 for k in ["BFSI_IT", "DEFENCE_INFRA", "GREEN_ENERGY_EV", "FMCG_PHARMA"]},
        "notes":         "",
        "error":         False,
    }

    try:
        crude = yf.Ticker(CRUDE_TICKER)
        hist  = crude.history(period="45d")

        if hist.empty or len(hist) < 5:
            result["notes"] = "⚠️  Could not fetch crude oil data — using neutral allocation."
            result["error"] = True
            return result

        current_price  = round(float(hist["Close"].iloc[-1]), 2)
        price_30d_ago  = round(float(hist["Close"].iloc[-30]) if len(hist) >= 30 else float(hist["Close"].iloc[0]), 2)
        change_30d     = round((current_price / price_30d_ago - 1) * 100, 1)

        result["price"]         = current_price
        result["price_30d_ago"] = price_30d_ago
        result["change_30d"]    = change_30d

        # ── Determine Level ───────────────────────────────────
        if current_price < CRUDE_LOW:
            level = "low"
        elif current_price < CRUDE_NEUTRAL:
            level = "neutral"
        elif current_price < CRUDE_ELEVATED:
            level = "elevated"
        else:
            level = "high"

        result["level"] = level

        # ── Determine Trend ───────────────────────────────────
        if change_30d > CRUDE_RISING_FAST:
            trend = "rising_fast"
        elif change_30d < CRUDE_FALLING_FAST:
            trend = "falling_fast"
        else:
            trend = "stable"

        result["trend"] = trend

        # ── Determine Signal Key ──────────────────────────────
        if level == "high":
            signal_key = "high_rising"
        elif level == "elevated" and trend == "rising_fast":
            signal_key = "elevated_rising"
        elif level == "elevated":
            signal_key = "elevated_stable"
        elif level == "low" and trend == "falling_fast":
            signal_key = "low_falling"
        elif level == "low":
            signal_key = "low_stable"
        else:
            signal_key = "neutral"

        result["signal_key"]  = signal_key
        result["adjustments"] = CRUDE_ALLOCATION_ADJUSTMENTS.get(signal_key, result["adjustments"])

        # ── Build Notes ───────────────────────────────────────
        level_emoji = {"low": "🟢", "neutral": "🟡", "elevated": "🟠", "high": "🔴"}.get(level, "⚪")
        trend_arrow = {"rising_fast": "📈", "stable": "➡️", "falling_fast": "📉"}.get(trend, "➡️")
        result["notes"] = (
            f"Brent Crude: ${current_price}/bbl  "
            f"{trend_arrow} {change_30d:+.1f}% (30d)  "
            f"{level_emoji} Level: {level.title()}"
        )

    except Exception as e:
        result["notes"] = f"⚠️  Crude oil fetch failed: {e} — neutral allocation used."
        result["error"] = True

    return result


# ─────────────────────────────────────────────
# USD/INR SIGNAL (2.5)
# ─────────────────────────────────────────────

USDINR_TICKER = "INR=X"   # USD/INR on Yahoo Finance

# Rate level thresholds (INR per 1 USD)
USDINR_STRONG   = 82.0    # below = strong rupee
USDINR_NEUTRAL  = 86.0    # 82–86 = neutral band
USDINR_WEAK     = 90.0    # 86–90 = weak rupee
                           # above 90 = very weak / stress

# 30-day change thresholds (positive = rupee weakening)
USDINR_DEPRECIATION = 2.0   # > +2% = rupee weakening meaningfully
USDINR_APPRECIATION = -2.0  # < -2% = rupee strengthening meaningfully

# Volatility threshold — 30-day std dev of daily returns
USDINR_HIGH_VOL = 0.5       # > 0.5% daily std = high uncertainty

# Allocation multipliers per scenario
USDINR_ADJUSTMENTS = {
    # Strong rupee + appreciating — FII inflows, cheap imports
    "strong_appreciating": {
        "BFSI_IT":         1.08,   # FII inflows → banks benefit
        "DEFENCE_INFRA":   1.05,   # cheap imported equipment
        "GREEN_ENERGY_EV": 1.08,   # solar panels / EV parts cheaper
        "FMCG_PHARMA":     1.05,   # raw material import cost relief
    },
    # Strong rupee + stable
    "strong_stable": {
        "BFSI_IT":         1.05,
        "DEFENCE_INFRA":   1.03,
        "GREEN_ENERGY_EV": 1.05,
        "FMCG_PHARMA":     1.03,
    },
    # Neutral — no adjustment
    "neutral": {
        "BFSI_IT":         1.00,
        "DEFENCE_INFRA":   1.00,
        "GREEN_ENERGY_EV": 1.00,
        "FMCG_PHARMA":     1.00,
    },
    # Weak rupee + stable — IT earns more in INR, imports costlier
    "weak_stable": {
        "BFSI_IT":         1.03,   # IT export earnings up in INR
        "DEFENCE_INFRA":   0.97,   # import-heavy infra costs up
        "GREEN_ENERGY_EV": 0.95,   # solar panel imports costlier
        "FMCG_PHARMA":     0.97,   # raw material imports costlier
    },
    # Weak rupee + depreciating fast — FII outflows, stress
    "weak_depreciating": {
        "BFSI_IT":         0.95,   # FII outflows hit banks; IT partially offset
        "DEFENCE_INFRA":   0.93,   # input cost pressure
        "GREEN_ENERGY_EV": 0.90,   # import costs spike
        "FMCG_PHARMA":     0.93,   # raw material pressure
    },
    # Very weak rupee (>90) — macro stress, risk-off
    "very_weak": {
        "BFSI_IT":         0.90,
        "DEFENCE_INFRA":   0.90,
        "GREEN_ENERGY_EV": 0.85,
        "FMCG_PHARMA":     0.90,
    },
    # High volatility override — uncertainty discount across all
    "high_volatility": {
        "BFSI_IT":         0.95,
        "DEFENCE_INFRA":   0.95,
        "GREEN_ENERGY_EV": 0.95,
        "FMCG_PHARMA":     0.95,
    },
}


def fetch_usdinr_signal() -> dict:
    """
    Fetch USD/INR exchange rate and calculate level + trend signal.

    Note: Yahoo Finance ticker INR=X gives USD/INR rate
    (how many rupees per 1 dollar).
    Higher number = weaker rupee.

    Returns:
      rate          : current USD/INR rate
      rate_30d_ago  : rate 30 trading days ago
      change_30d    : % change (positive = rupee weakening)
      volatility    : 30-day std dev of daily returns
      level         : "strong" | "neutral" | "weak" | "very_weak"
      trend         : "appreciating" | "stable" | "depreciating"
      high_vol      : True if volatility > threshold
      signal_key    : key for USDINR_ADJUSTMENTS lookup
      adjustments   : {bucket_key: multiplier}
      notes         : human-readable summary
    """
    result = {
        "rate":         None,
        "rate_30d_ago": None,
        "change_30d":   None,
        "volatility":   None,
        "level":        "neutral",
        "trend":        "stable",
        "high_vol":     False,
        "signal_key":   "neutral",
        "adjustments":  {k: 1.0 for k in ["BFSI_IT", "DEFENCE_INFRA", "GREEN_ENERGY_EV", "FMCG_PHARMA"]},
        "notes":        "",
        "error":        False,
    }

    try:
        fx    = yf.Ticker(USDINR_TICKER)
        hist  = fx.history(period="45d")

        if hist.empty or len(hist) < 5:
            result["notes"] = "⚠️  Could not fetch USD/INR data — neutral allocation used."
            result["error"] = True
            return result

        current_rate  = round(float(hist["Close"].iloc[-1]), 2)
        rate_30d_ago  = round(float(hist["Close"].iloc[-30]) if len(hist) >= 30 else float(hist["Close"].iloc[0]), 2)
        change_30d    = round((current_rate / rate_30d_ago - 1) * 100, 2)

        # 30-day volatility (std dev of daily returns)
        daily_returns = hist["Close"].pct_change().dropna()
        volatility    = round(float(daily_returns.std() * 100), 3)  # in %

        result["rate"]         = current_rate
        result["rate_30d_ago"] = rate_30d_ago
        result["change_30d"]   = change_30d
        result["volatility"]   = volatility

        # ── Determine Level ───────────────────────────────────
        if current_rate < USDINR_STRONG:
            level = "strong"
        elif current_rate < USDINR_NEUTRAL:
            level = "neutral"
        elif current_rate < USDINR_WEAK:
            level = "weak"
        else:
            level = "very_weak"

        result["level"] = level

        # ── Determine Trend ───────────────────────────────────
        if change_30d > USDINR_DEPRECIATION:
            trend = "depreciating"
        elif change_30d < USDINR_APPRECIATION:
            trend = "appreciating"
        else:
            trend = "stable"

        result["trend"] = trend

        # ── High Volatility Check ─────────────────────────────
        high_vol = volatility > USDINR_HIGH_VOL
        result["high_vol"] = high_vol

        # ── Determine Signal Key ──────────────────────────────
        if level == "very_weak":
            signal_key = "very_weak"
        elif high_vol:
            signal_key = "high_volatility"
        elif level == "weak" and trend == "depreciating":
            signal_key = "weak_depreciating"
        elif level == "weak":
            signal_key = "weak_stable"
        elif level == "strong" and trend == "appreciating":
            signal_key = "strong_appreciating"
        elif level == "strong":
            signal_key = "strong_stable"
        else:
            signal_key = "neutral"

        result["signal_key"]  = signal_key
        result["adjustments"] = USDINR_ADJUSTMENTS.get(signal_key, result["adjustments"])

        # ── Build Notes ───────────────────────────────────────
        level_emoji = {
            "strong":    "🟢",
            "neutral":   "🟡",
            "weak":      "🟠",
            "very_weak": "🔴",
        }.get(level, "⚪")

        trend_arrow = {
            "appreciating": "📉",   # rupee getting stronger
            "stable":       "➡️",
            "depreciating": "📈",   # rupee getting weaker
        }.get(trend, "➡️")

        vol_str = f"  Vol: {volatility:.3f}%/day {'⚠️' if high_vol else '✅'}"

        result["notes"] = (
            f"USD/INR: ₹{current_rate}  "
            f"{trend_arrow} {change_30d:+.2f}% (30d)  "
            f"{level_emoji} {level.replace('_',' ').title()}"
            f"{vol_str}"
        )

    except Exception as e:
        result["notes"] = f"⚠️  USD/INR fetch failed: {e} — neutral used."
        result["error"] = True

    return result


# ─────────────────────────────────────────────
# GLOBAL MARKET SIGNAL (2.6)
# ─────────────────────────────────────────────

SP500_TICKER  = "^GSPC"
NASDAQ_TICKER = "^IXIC"

# 30-day return thresholds (%)
GLOBAL_RISK_ON_STRONG  =  5.0   # S&P up >5% → risk-on
GLOBAL_RISK_ON_MILD    =  2.0
GLOBAL_RISK_OFF_MILD   = -5.0   # S&P down >5% → risk-off
GLOBAL_RISK_OFF_STRONG = -10.0  # S&P down >10% → strong risk-off

# Volatility threshold — daily std dev
GLOBAL_HIGH_VOL = 1.5           # >1.5%/day = elevated fear

# Nasdaq vs S&P divergence threshold
# If Nasdaq underperforms S&P by >5% → tech-specific selloff
NASDAQ_DIVERGENCE = -5.0

# Allocation multipliers per scenario
GLOBAL_ADJUSTMENTS = {
    "risk_on_strong": {
        "BFSI_IT":         1.05,
        "DEFENCE_INFRA":   1.03,
        "GREEN_ENERGY_EV": 1.05,
        "FMCG_PHARMA":     1.00,
    },
    "risk_on_mild": {
        "BFSI_IT":         1.03,
        "DEFENCE_INFRA":   1.02,
        "GREEN_ENERGY_EV": 1.03,
        "FMCG_PHARMA":     1.00,
    },
    "neutral": {
        "BFSI_IT":         1.00,
        "DEFENCE_INFRA":   1.00,
        "GREEN_ENERGY_EV": 1.00,
        "FMCG_PHARMA":     1.00,
    },
    "risk_off_mild": {
        "BFSI_IT":         0.92,
        "DEFENCE_INFRA":   0.95,
        "GREEN_ENERGY_EV": 0.92,
        "FMCG_PHARMA":     0.97,   # defensive — least penalised
    },
    "risk_off_strong": {
        "BFSI_IT":         0.85,
        "DEFENCE_INFRA":   0.90,
        "GREEN_ENERGY_EV": 0.85,
        "FMCG_PHARMA":     0.95,   # defensive holds up best
    },
    "high_volatility": {
        "BFSI_IT":         0.93,
        "DEFENCE_INFRA":   0.95,
        "GREEN_ENERGY_EV": 0.93,
        "FMCG_PHARMA":     0.97,
    },
}

# Additional Nasdaq-specific penalty on BFSI_IT if tech selloff detected
NASDAQ_TECH_PENALTY = 0.95   # extra -5% on BFSI_IT when Nasdaq diverges


def fetch_global_market_signal() -> dict:
    """
    Fetch S&P 500 and Nasdaq 30-day performance and volatility.
    Detects global risk-on / risk-off and tech-specific selloffs.

    Returns:
      sp500_change    : S&P 500 30-day % change
      nasdaq_change   : Nasdaq 30-day % change
      nasdaq_diverge  : Nasdaq underperformance vs S&P 500
      volatility      : 30-day std dev of S&P 500 daily returns
      signal          : risk_on_strong | risk_on_mild | neutral |
                        risk_off_mild | risk_off_strong | high_volatility
      nasdaq_selloff  : True if Nasdaq diverges negatively
      adjustments     : {bucket_key: multiplier}
      notes           : human-readable summary
    """
    result = {
        "sp500_change":   None,
        "nasdaq_change":  None,
        "nasdaq_diverge": None,
        "volatility":     None,
        "signal":         "neutral",
        "nasdaq_selloff": False,
        "adjustments":    {k: 1.0 for k in ["BFSI_IT", "DEFENCE_INFRA", "GREEN_ENERGY_EV", "FMCG_PHARMA"]},
        "notes":          "",
        "error":          False,
    }

    try:
        # ── Fetch S&P 500 ─────────────────────────────────────
        sp500_hist = yf.Ticker(SP500_TICKER).history(period="45d")
        time.sleep(0.3)
        nasdaq_hist = yf.Ticker(NASDAQ_TICKER).history(period="45d")

        if sp500_hist.empty or len(sp500_hist) < 5:
            result["notes"] = "⚠️  Could not fetch S&P 500 data — neutral used."
            result["error"] = True
            return result

        # ── S&P 500 metrics ───────────────────────────────────
        sp500_now     = float(sp500_hist["Close"].iloc[-1])
        sp500_30d     = float(sp500_hist["Close"].iloc[-30] if len(sp500_hist) >= 30 else sp500_hist["Close"].iloc[0])
        sp500_change  = round((sp500_now / sp500_30d - 1) * 100, 2)

        sp500_returns = sp500_hist["Close"].pct_change().dropna()
        volatility    = round(float(sp500_returns.std() * 100), 3)

        result["sp500_change"] = sp500_change
        result["volatility"]   = volatility

        # ── Nasdaq metrics ────────────────────────────────────
        nasdaq_selloff = False
        nasdaq_change  = None
        diverge        = None

        if not nasdaq_hist.empty and len(nasdaq_hist) >= 5:
            nasdaq_now    = float(nasdaq_hist["Close"].iloc[-1])
            nasdaq_30d    = float(nasdaq_hist["Close"].iloc[-30] if len(nasdaq_hist) >= 30 else nasdaq_hist["Close"].iloc[0])
            nasdaq_change = round((nasdaq_now / nasdaq_30d - 1) * 100, 2)
            diverge       = round(nasdaq_change - sp500_change, 2)

            # Nasdaq underperforming S&P by >5% = tech-specific selloff
            if diverge < NASDAQ_DIVERGENCE:
                nasdaq_selloff = True

        result["nasdaq_change"]  = nasdaq_change
        result["nasdaq_diverge"] = diverge
        result["nasdaq_selloff"] = nasdaq_selloff

        # ── Determine Signal ──────────────────────────────────
        high_vol = volatility > GLOBAL_HIGH_VOL

        if high_vol:
            signal = "high_volatility"
        elif sp500_change >= GLOBAL_RISK_ON_STRONG:
            signal = "risk_on_strong"
        elif sp500_change >= GLOBAL_RISK_ON_MILD:
            signal = "risk_on_mild"
        elif sp500_change <= GLOBAL_RISK_OFF_STRONG:
            signal = "risk_off_strong"
        elif sp500_change <= GLOBAL_RISK_OFF_MILD:
            signal = "risk_off_mild"
        else:
            signal = "neutral"

        result["signal"]      = signal
        adjustments           = dict(GLOBAL_ADJUSTMENTS.get(signal, result["adjustments"]))

        # ── Apply Nasdaq tech penalty to BFSI_IT if needed ───
        if nasdaq_selloff:
            adjustments["BFSI_IT"] = round(adjustments["BFSI_IT"] * NASDAQ_TECH_PENALTY, 3)

        result["adjustments"] = adjustments

        # ── Build Notes ───────────────────────────────────────
        signal_emoji = {
            "risk_on_strong":  "🟢",
            "risk_on_mild":    "🟢",
            "neutral":         "🟡",
            "risk_off_mild":   "🟠",
            "risk_off_strong": "🔴",
            "high_volatility": "🔴",
        }.get(signal, "⚪")

        nasdaq_str = ""
        if nasdaq_change is not None:
            nasdaq_str = f"  Nasdaq: {nasdaq_change:+.1f}%"
            if nasdaq_selloff:
                nasdaq_str += " ⚠️ tech selloff"

        result["notes"] = (
            f"S&P 500: {sp500_change:+.1f}% (30d)  "
            f"Vol: {volatility:.2f}%/day  "
            f"{signal_emoji} {signal.replace('_',' ').title()}"
            f"{nasdaq_str}"
        )

    except Exception as e:
        result["notes"] = f"⚠️  Global market fetch failed: {e} — neutral used."
        result["error"] = True

    return result


# ─────────────────────────────────────────────
# FII / DII FLOW TRACKER (4.1)
# ─────────────────────────────────────────────

FIIDII_HISTORY_FILE = os.path.join(os.path.dirname(__file__), "fiidii_history.json")
POLICY_SIGNALS_FILE = os.path.join(os.path.dirname(__file__), "policy_signals.json")
NEWS_SIGNALS_FILE   = os.path.join(os.path.dirname(__file__), "news_signals.json")

import os
import json
import requests as _requests


def fetch_fiidii_signal() -> dict:
    """
    Calculate FII/DII 10-day rolling signal from history file.
    History file is maintained by collector.py (runs daily at 4 PM IST).

    Falls back to neutral if history file missing or insufficient data.
    """
    result = {
        "fii_net_cr":  None,
        "dii_net_cr":  None,
        "fii_10d_cr":  None,
        "dii_10d_cr":  None,
        "fii_signal":  "neutral",
        "combined":    "neutral",
        "adjustments": {k: 1.0 for k in ["BFSI_IT", "DEFENCE_INFRA", "GREEN_ENERGY_EV", "FMCG_PHARMA"]},
        "notes":       "",
        "error":       False,
    }

    try:
        if not os.path.exists(FIIDII_HISTORY_FILE):
            result["notes"] = "⚠️  FII/DII history file missing — run collector.py first."
            result["error"] = True
            return result

        with open(FIIDII_HISTORY_FILE) as f:
            history = json.load(f)

        if not history:
            result["notes"] = "⚠️  FII/DII history empty — neutral used."
            result["error"] = True
            return result

        # Sort by date descending, take last 10 trading days
        history = sorted(history, key=lambda r: r["date"], reverse=True)
        recent  = history[:10]

        fii_today = recent[0]["fii_net_cr"]
        dii_today = recent[0]["dii_net_cr"]
        fii_10d   = round(sum(r["fii_net_cr"] for r in recent), 0)
        dii_10d   = round(sum(r["dii_net_cr"] for r in recent), 0)

        result["fii_net_cr"] = fii_today
        result["dii_net_cr"] = dii_today
        result["fii_10d_cr"] = fii_10d
        result["dii_10d_cr"] = dii_10d

        # ── FII signal ────────────────────────────────────────
        if fii_10d >= FII_STRONG_BUY:
            fii_signal = "strong_buy"
        elif fii_10d >= FII_MILD_BUY:
            fii_signal = "mild_buy"
        elif fii_10d <= FII_STRONG_SELL:
            fii_signal = "strong_sell"
        elif fii_10d <= FII_MILD_SELL:
            fii_signal = "mild_sell"
        else:
            fii_signal = "neutral"

        result["fii_signal"] = fii_signal

        # ── Combined FII + DII signal ─────────────────────────
        dii_buying = dii_10d > 1_000

        if fii_10d >= FII_MILD_BUY and dii_buying:
            combined = "both_buying"
        elif fii_10d >= FII_MILD_BUY:
            combined = "fii_buying"
        elif fii_10d <= FII_STRONG_SELL and not dii_buying:
            combined = "both_selling"
        elif fii_10d <= FII_STRONG_SELL:
            combined = "fii_strong_selling"
        elif fii_10d <= FII_MILD_SELL and dii_buying:
            combined = "fii_selling_dii_buying"
        else:
            combined = "neutral"

        result["combined"]    = combined
        result["adjustments"] = FIIDII_ADJUSTMENTS.get(combined, result["adjustments"])

        # ── Notes ─────────────────────────────────────────────
        fii_emoji = "🟢" if fii_10d > 0 else "🔴"
        dii_emoji = "🟢" if dii_10d > 0 else "🔴"
        days_note = f"({len(recent)} days)"

        result["notes"] = (
            f"FII 10d: {fii_emoji} ₹{fii_10d:+,.0f}Cr  "
            f"DII 10d: {dii_emoji} ₹{dii_10d:+,.0f}Cr  "
            f"{days_note}  → {combined.replace('_',' ').title()}"
        )

    except Exception as e:
        result["notes"] = f"⚠️  FII/DII signal failed: {e} — neutral used."
        result["error"] = True

    return result


# ─────────────────────────────────────────────
# POLICY SIGNAL READER (4.2)
# ─────────────────────────────────────────────

# Map policy signal strings to allocation multipliers
POLICY_MULTIPLIERS = {
    "positive":     1.06,
    "mild_positive":1.03,
    "neutral":      1.00,
    "cautious":     0.95,
    "negative":     0.90,
}

def fetch_policy_signal() -> dict:
    """
    Read policy signals generated by policy_scraper.py.
    Converts per-bucket signal strings to allocation multipliers.

    Returns neutral if policy_signals.json is missing or stale (>8 days old).
    """
    neutral = {k: 1.0 for k in ["BFSI_IT", "DEFENCE_INFRA", "GREEN_ENERGY_EV", "FMCG_PHARMA"]}
    result  = {
        "adjustments": neutral,
        "signals":     {},
        "generated_at":"",
        "notes":       "",
        "error":       False,
    }

    try:
        if not os.path.exists(POLICY_SIGNALS_FILE):
            result["notes"] = "⚠️  Policy signals missing — run policy_scraper.py first."
            result["error"] = True
            return result

        with open(POLICY_SIGNALS_FILE) as f:
            data = json.load(f)

        # Check staleness — policy scan should run weekly
        generated_str = data.get("generated_at", "")
        if generated_str:
            try:
                gen_date = datetime.strptime(generated_str[:10], "%Y-%m-%d").date()
                age_days = (date.today() - gen_date).days
                if age_days > 8:
                    result["notes"] = (
                        f"⚠️  Policy signals are {age_days} days old — "
                        f"run policy_scraper.py to refresh."
                    )
                    # Still use them but flag staleness
            except ValueError:
                age_days = 0
        else:
            age_days = 0

        signals     = data.get("signals", {})
        adjustments = {}
        notes_parts = []

        for bucket in ["BFSI_IT", "DEFENCE_INFRA", "GREEN_ENERGY_EV", "FMCG_PHARMA"]:
            sig_data   = signals.get(bucket, {})
            signal_str = sig_data.get("signal", "neutral")
            multiplier = POLICY_MULTIPLIERS.get(signal_str, 1.0)
            adjustments[bucket] = multiplier
            result["signals"][bucket] = signal_str

            if signal_str not in ("neutral",):
                emoji = {"positive":"🟢","mild_positive":"🟡",
                         "cautious":"🟠","negative":"🔴"}.get(signal_str,"⚪")
                notes_parts.append(f"{bucket}: {emoji}{signal_str}")

        result["adjustments"] = adjustments
        result["generated_at"] = generated_str

        if notes_parts:
            result["notes"] = f"Policy signals: {' | '.join(notes_parts)}"
        else:
            result["notes"] = "Policy signals: all neutral ⚪"

        if age_days > 8:
            result["notes"] += f" (⚠️ {age_days}d old)"

    except Exception as e:
        result["notes"] = f"⚠️  Policy signal read failed: {e} — neutral used."
        result["error"] = True

    return result


# ─────────────────────────────────────────────
# NEWS SENTIMENT SIGNAL READER (4.3)
# ─────────────────────────────────────────────

# Smaller multipliers than policy — news is noisier
NEWS_SIGNAL_MULTIPLIERS = {
    "positive":     1.04,
    "mild_positive":1.02,
    "neutral":      1.00,
    "cautious":     0.98,
    "negative":     0.96,
}

def fetch_news_signal() -> dict:
    """
    Read news sentiment signals from news_signals.json.
    Generated daily by news_sentiment.py.

    Returns neutral if file missing or stale (>2 days old).
    """
    neutral = {k: 1.0 for k in ["BFSI_IT", "DEFENCE_INFRA", "GREEN_ENERGY_EV", "FMCG_PHARMA"]}
    result  = {
        "adjustments": neutral,
        "signals":     {},
        "generated_at":"",
        "notes":       "",
        "error":       False,
    }

    try:
        if not os.path.exists(NEWS_SIGNALS_FILE):
            result["notes"] = "⚠️  News signals missing — run news_sentiment.py first."
            result["error"] = True
            return result

        with open(NEWS_SIGNALS_FILE) as f:
            data = json.load(f)

        # Staleness check — news signals expire after 2 days
        generated_str = data.get("generated_at", "")
        if generated_str:
            try:
                gen_date = datetime.strptime(generated_str[:10], "%Y-%m-%d").date()
                age_days = (date.today() - gen_date).days
                if age_days > 2:
                    result["notes"] = (
                        f"⚠️  News signals {age_days}d old — treating as neutral."
                    )
                    return result   # too stale — return neutral
            except ValueError:
                pass

        signals     = data.get("signals", {})
        adjustments = {}
        notes_parts = []

        for bucket in ["BFSI_IT", "DEFENCE_INFRA", "GREEN_ENERGY_EV", "FMCG_PHARMA"]:
            sig_data   = signals.get(bucket, {})
            signal_str = sig_data.get("signal", "neutral")
            multiplier = NEWS_SIGNAL_MULTIPLIERS.get(signal_str, 1.0)
            adjustments[bucket] = multiplier
            result["signals"][bucket] = signal_str

            if signal_str not in ("neutral",):
                emoji = {
                    "positive":"🟢","mild_positive":"🟡",
                    "cautious":"🟠","negative":"🔴"
                }.get(signal_str,"⚪")
                notes_parts.append(f"{bucket[:8]}: {emoji}")

        result["adjustments"] = adjustments
        result["generated_at"] = generated_str

        headlines_count = data.get("total_headlines", 0)
        result["notes"] = (
            f"News ({headlines_count} headlines): "
            + (", ".join(notes_parts) if notes_parts else "all neutral ⚪")
        )

    except Exception as e:
        result["notes"] = f"⚠️  News signal read failed: {e} — neutral used."
        result["error"] = True

    return result


# ─────────────────────────────────────────────
# COMBINED MACRO SIGNAL (used by screener.py)
# ─────────────────────────────────────────────

def get_macro_adjustments(base_allocations: dict) -> dict:
    """
    Fetch all macro signals and combine them into final
    allocation multipliers per bucket.

    When multiple signals are active, multipliers are
    multiplied together (compounded), then normalised
    so total allocation still sums to 100%.

    Args:
        base_allocations: {bucket_key: allocation_pct} e.g.
                          {"BFSI_IT": 0.30, "DEFENCE_INFRA": 0.30, ...}

    Returns:
        {
          "adjusted_allocations": {bucket_key: adjusted_pct},
          "crude":   crude_signal_dict,
          "usdinr":  usdinr_signal_dict,
          "global":  global_signal_dict,
          "summary": human-readable string
        }
    """
    print("\n  📡 Fetching macro signals...")

    # Fetch all signals
    crude      = fetch_crude_signal()
    time.sleep(0.5)
    usdinr     = fetch_usdinr_signal()
    time.sleep(0.5)
    global_mkt = fetch_global_market_signal()
    time.sleep(0.5)
    fiidii     = fetch_fiidii_signal()
    policy     = fetch_policy_signal()
    news       = fetch_news_signal()

    # Print signal summary
    print(f"    🛢️  {crude['notes']}")
    print(f"    💱  {usdinr['notes']}")
    print(f"    🌍  {global_mkt['notes']}")
    print(f"    📊  {fiidii['notes']}")
    print(f"    📜  {policy['notes']}")
    print(f"    📰  {news['notes']}")

    buckets = list(base_allocations.keys())

    # Compound multipliers across all six signals
    combined_multipliers = {}
    for bucket in buckets:
        m  = crude["adjustments"].get(bucket, 1.0)
        m *= usdinr["adjustments"].get(bucket, 1.0)
        m *= global_mkt["adjustments"].get(bucket, 1.0)
        m *= fiidii["adjustments"].get(bucket, 1.0)
        m *= policy["adjustments"].get(bucket, 1.0)
        m *= news["adjustments"].get(bucket, 1.0)
        combined_multipliers[bucket] = m

    # Apply multipliers to base allocations
    raw_adjusted = {
        b: base_allocations[b] * combined_multipliers[b]
        for b in buckets
    }

    # Normalise so total still sums to 1.0
    total = sum(raw_adjusted.values())
    adjusted_allocations = {
        b: round(v / total, 4)
        for b, v in raw_adjusted.items()
    }

    # Print adjustment table
    print(f"\n    📊 Macro Allocation Adjustments:")
    for b in buckets:
        base_pct = base_allocations[b] * 100
        adj_pct  = adjusted_allocations[b] * 100
        diff     = adj_pct - base_pct
        arrow    = "↑" if diff > 0.1 else ("↓" if diff < -0.1 else "→")
        print(
            f"    {b:<20} {base_pct:.0f}% → {adj_pct:.1f}%  "
            f"{arrow} ({diff:+.1f}%)"
        )

    summary = f"Crude: {crude['notes']}"

    macro_result = {
        "adjusted_allocations": adjusted_allocations,
        "crude":                crude,
        "usdinr":               usdinr,
        "global":               global_mkt,
        "fiidii":               fiidii,
        "policy":               policy,
        "news":                 news,
        "summary":              summary,
        "llm_verdict":          None,   # populated below if synthesiser available
    }

    # ── Optional LLM synthesis (4.5) ─────────────────────────
    # Only runs if llm_synthesiser.py is present and API is reachable.
    # Failure is silent — tool works fine without it.
    try:
        from llm_synthesiser import run_synthesis, load_synthesis
        from datetime import date as _date

        # Check if synthesis is fresh (same day)
        existing = load_synthesis()
        if existing:
            gen_str = existing.get("generated_at", "")
            try:
                gen_date = datetime.strptime(gen_str[:10], "%Y-%m-%d").date()
                if gen_date == _date.today():
                    print(f"\n  🤖 LLM synthesis: using today's cached verdict.")
                    macro_result["llm_verdict"] = existing.get("verdict")
                    _print_verdict_summary(existing.get("verdict", {}))
                else:
                    raise ValueError("stale")
            except (ValueError, AttributeError):
                # Stale or missing — run fresh synthesis
                print(f"\n  🤖 Running LLM synthesis...")
                verdict = run_synthesis(macro=macro_result, test_mode=False)
                macro_result["llm_verdict"] = verdict
    except ImportError:
        pass   # llm_synthesiser not available — continue silently
    except Exception as e:
        print(f"\n  ⚠️  LLM synthesis skipped: {e}")

    return macro_result


def _print_verdict_summary(verdict: dict):
    """Print a compact one-line summary of LLM verdicts."""
    if not verdict:
        return
    parts = []
    emoji_map = {"Positive":"🟢","Cautious":"🟠","Neutral":"⚪","Negative":"🔴"}
    for b in ["BFSI_IT","DEFENCE_INFRA","GREEN_ENERGY_EV","FMCG_PHARMA"]:
        v = verdict.get(b, {}).get("verdict", "Neutral")
        parts.append(f"{b[:8]}: {emoji_map.get(v,'⚪')}{v}")
    print(f"    🤖  {' | '.join(parts)}")


# ─────────────────────────────────────────────
# MAIN — standalone test
# ─────────────────────────────────────────────

if __name__ == "__main__":
    base = {
        "BFSI_IT":         0.30,
        "DEFENCE_INFRA":   0.30,
        "GREEN_ENERGY_EV": 0.20,
        "FMCG_PHARMA":     0.20,
    }
    result = get_macro_adjustments(base)
    print("\nFinal adjusted allocations:")
    for k, v in result["adjusted_allocations"].items():
        print(f"  {k}: {v*100:.1f}%")
