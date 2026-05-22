"""
LLM Synthesiser US — Claude API Macro Analyst
===============================================
Exact mirror of llm_synthesiser.py for the US market.

Takes all US macro signals and asks Claude to reason through
conflicts and produce a plain English verdict per bucket.

Acts like a morning briefing from a US equity research analyst —
synthesising Fed policy, USD strength, global markets, institutional
flows, earnings season, and news into actionable recommendations.

Runs on Railway: Monday 8 AM IST (after news_sentiment_us.py)

Usage:
    python llm_synthesiser_us.py          # run synthesis
    python llm_synthesiser_us.py --status # show latest verdict
    python llm_synthesiser_us.py --test   # run without saving
"""

import json
import os
import argparse
from datetime import datetime, date
from zoneinfo import ZoneInfo

import requests

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

IST            = ZoneInfo("Asia/Kolkata")
SYNTHESIS_FILE = os.path.join(os.path.dirname(__file__), "us_llm_synthesis.json")
ANTHROPIC_API  = "https://api.anthropic.com/v1/messages"
MODEL          = "claude-sonnet-4-20250514"
MAX_TOKENS     = 1000

# US bucket labels — equiv. to India BUCKET_LABELS
BUCKET_LABELS = {
    "AI_CLOUD":         "🤖 AI + Cloud (Large Cap)",
    "SEMICONDUCTORS":   "⚙️  Semiconductors (Mid Cap)",
    "HIGH_GROWTH_TECH": "⚡ High Growth Tech",
    "DEFENSIVE_DIV":    "🌾 Defensive + Dividend",
}


# ─────────────────────────────────────────────
# CONTEXT BUILDER
# ─────────────────────────────────────────────

def build_macro_context(macro: dict) -> str:
    """
    Format all US macro signals into clean text for the LLM prompt.
    Exact mirror of build_macro_context() in llm_synthesiser.py.
    US signals replace India signals:
      Crude oil      → Fed interest rates
      USD/INR        → USD strength (DXY)
      Global markets → S&P 500 / Nasdaq / VIX
      FII/DII flows  → Institutional flows (13F / ETF flows)
      RBI/PIB policy → Fed / SEC / earnings season
      News sentiment → US financial news sentiment
    """
    lines = []

    # 1. Fed Interest Rates (equiv. Crude Oil)
    fed = macro.get("fed", {})
    lines.append(f"FED INTEREST RATES:\n  {fed.get('notes', 'No data')}")
    lines.append(f"  Level: {fed.get('level', 'unknown').title()}")
    lines.append(f"  Trend: {fed.get('trend', 'unknown').title()}")

    # 2. USD Strength / DXY (equiv. USD/INR)
    usd = macro.get("usd_strength", {})
    lines.append(f"\nUSD STRENGTH (DXY):\n  {usd.get('notes', 'No data')}")
    if usd.get("dxy"):
        lines.append(
            f"  DXY: {usd['dxy']} | "
            f"Level: {usd.get('level', '?').title()} | "
            f"Trend: {usd.get('trend', '?').title()}"
        )

    # 3. US Market Conditions (equiv. Global Markets)
    mkt = macro.get("us_market", {})
    lines.append(f"\nUS MARKET CONDITIONS:\n  {mkt.get('notes', 'No data')}")
    if mkt.get("sp500_change") is not None:
        nasdaq_str = (
            f" | Nasdaq: {mkt['nasdaq_change']:+.1f}%"
            if mkt.get("nasdaq_change") is not None else ""
        )
        vix_str = (
            f" | VIX: {mkt['vix']:.1f}"
            if mkt.get("vix") is not None else ""
        )
        lines.append(
            f"  S&P 500 30d: {mkt['sp500_change']:+.1f}%"
            f"{nasdaq_str}{vix_str}"
        )
        if mkt.get("high_volatility"):
            lines.append("  ⚠️ High VIX — elevated market fear")
        if mkt.get("tech_selloff"):
            lines.append("  ⚠️ Tech/Nasdaq selloff detected")

    # 4. Institutional Flows (equiv. FII/DII Flows)
    flows = macro.get("inst_flows", {})
    lines.append(f"\nINSTITUTIONAL FLOWS:\n  {flows.get('notes', 'No data')}")
    if flows.get("etf_flow_bn") is not None:
        lines.append(
            f"  ETF Flows (10d): ${flows['etf_flow_bn']:+.1f}B | "
            f"Signal: {flows.get('signal', 'neutral').title()}"
        )

    # 5. Policy Signals (equiv. RBI/PIB Policy)
    policy = macro.get("policy", {})
    lines.append(f"\nFED + SEC + EARNINGS POLICY:\n  {policy.get('notes', 'No data')}")
    for bucket, sig in policy.get("signals", {}).items():
        if sig not in ("neutral",):
            lines.append(f"  {bucket}: {sig}")

    # 6. News Sentiment (identical concept)
    news = macro.get("news", {})
    lines.append(f"\nUS FINANCIAL NEWS SENTIMENT:\n  {news.get('notes', 'No data')}")
    for bucket, sig in news.get("signals", {}).items():
        if sig not in ("neutral",):
            lines.append(f"  {bucket}: {sig}")

    # Allocation adjustments
    adj = macro.get("adjusted_allocations", {})
    if adj:
        lines.append(f"\nCOMPUTED ALLOCATION ADJUSTMENTS:")
        base = {"AI_CLOUD": 0.30, "SEMICONDUCTORS": 0.25, "HIGH_GROWTH_TECH": 0.25, "DEFENSIVE_DIV": 0.20}
        for b, v in adj.items():
            diff = round((v - base.get(b, v)) * 100, 1)
            lines.append(f"  {b}: {v*100:.1f}% ({diff:+.1f}% vs base)")

    return "\n".join(lines)


# ─────────────────────────────────────────────
# LLM CALL
# ─────────────────────────────────────────────

def synthesise_macro_verdict(macro: dict) -> dict | None:
    """
    Call Claude API with US macro context and get structured
    per-bucket verdicts.
    Exact mirror of synthesise_macro_verdict() in llm_synthesiser.py.
    """
    context = build_macro_context(macro)

    system_prompt = (
        "You are a senior macro analyst for a US equity portfolio. "
        "You synthesise multiple macro signals and give structured, "
        "actionable verdicts per sector bucket. "
        "Be direct and concise. Prioritise contradictions — explain which "
        "signal dominates and why. "
        "Always respond with valid JSON only, no markdown, no preamble."
    )

    user_prompt = f"""Today's macro signals for US equity markets:

{context}

The portfolio has 4 sector buckets. For each bucket, provide a verdict considering ALL signals above — especially when they conflict.

For EACH bucket provide:
- verdict: exactly one of: Positive | Cautious | Neutral | Negative
- confidence: exactly one of: High | Medium | Low
- key_driver: one sentence — the main signal driving this verdict
- action: one sentence — what to do with this bucket's allocation
- watch_for: one thing to monitor in the next 2-4 weeks

Buckets to analyse:
1. AI_CLOUD — AI infrastructure, cloud platforms, hyperscalers (NVDA, MSFT, GOOGL, META)
2. SEMICONDUCTORS — Chip designers and manufacturers (AMD, AVGO, TSM, QCOM)
3. HIGH_GROWTH_TECH — E-commerce, software, fintech, consumer internet
4. DEFENSIVE_DIV — Healthcare, consumer staples, financials, utilities

Respond with ONLY a JSON object in this exact format:
{{
  "AI_CLOUD": {{
    "verdict": "...",
    "confidence": "...",
    "key_driver": "...",
    "action": "...",
    "watch_for": "..."
  }},
  "SEMICONDUCTORS": {{ ... }},
  "HIGH_GROWTH_TECH": {{ ... }},
  "DEFENSIVE_DIV": {{ ... }},
  "overall_market": "one sentence on overall US market outlook"
}}"""

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("  ❌ ANTHROPIC_API_KEY not set — skipping LLM synthesis.")
        return None

    try:
        resp = requests.post(
            ANTHROPIC_API,
            headers={
                "Content-Type":      "application/json",
                "x-api-key":         api_key,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model":      MODEL,
                "max_tokens": MAX_TOKENS,
                "system":     system_prompt,
                "messages":   [{"role": "user", "content": user_prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        raw_text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                raw_text += block.get("text", "")

        if not raw_text:
            print("  ⚠️  LLM returned empty response")
            return None

        raw_text = raw_text.strip()
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]

        return json.loads(raw_text.strip())

    except json.JSONDecodeError as e:
        print(f"  ⚠️  LLM returned invalid JSON: {e}")
        return None
    except Exception as e:
        print(f"  ⚠️  LLM API call failed: {e}")
        return None


# ─────────────────────────────────────────────
# PERSISTENCE
# ─────────────────────────────────────────────

def save_synthesis(verdict: dict, macro: dict):
    """Identical to save_synthesis() in llm_synthesiser.py."""
    output = {
        "generated_at":  datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"),
        "model":         MODEL,
        "verdict":       verdict,
        "macro_summary": {
            "fed":         macro.get("fed", {}).get("notes", ""),
            "usd":         macro.get("usd_strength", {}).get("notes", ""),
            "us_market":   macro.get("us_market", {}).get("notes", ""),
            "inst_flows":  macro.get("inst_flows", {}).get("notes", ""),
            "policy":      macro.get("policy", {}).get("notes", ""),
            "news":        macro.get("news", {}).get("notes", ""),
        },
    }
    with open(SYNTHESIS_FILE, "w") as f:
        json.dump(output, f, indent=2)
    print(f"  ✅ US Synthesis saved to {SYNTHESIS_FILE}")
    _post_signal_to_api("us_llm_synthesis", output)


def _post_signal_to_api(signal_type: str, payload: dict):
    """POST to /signals/upload — identical to India version."""
    import urllib.request as _urllib
    api_url = os.getenv("API_URL", "https://web-production-2d832.up.railway.app")
    url = f"{api_url}/signals/upload"
    try:
        body = json.dumps({"type": signal_type, "payload": payload}).encode("utf-8")
        req = _urllib.Request(url, data=body,
                              headers={"Content-Type": "application/json"},
                              method="POST")
        with _urllib.urlopen(req, timeout=10) as r:
            print(f"  ✅ {signal_type} POSTed to API: {r.read().decode()}")
    except Exception as e:
        print(f"  ⚠️  Could not POST {signal_type} to API (non-fatal): {e}")


def load_synthesis() -> dict | None:
    if not os.path.exists(SYNTHESIS_FILE):
        return None
    try:
        with open(SYNTHESIS_FILE) as f:
            return json.load(f)
    except Exception:
        return None


# ─────────────────────────────────────────────
# DISPLAY
# ─────────────────────────────────────────────

def print_verdict(verdict: dict, generated_at: str = ""):
    """Identical to print_verdict() in llm_synthesiser.py."""
    verdict_emoji    = {"Positive":"🟢","Cautious":"🟠","Neutral":"⚪","Negative":"🔴"}
    confidence_emoji = {"High":"●●●","Medium":"●●○","Low":"●○○"}

    print(f"\n{'='*60}")
    print(f"  🤖 US LLM MACRO SYNTHESIS")
    if generated_at:
        print(f"  Generated: {generated_at}")
    print(f"{'='*60}")

    overall = verdict.get("overall_market", "")
    if overall:
        print(f"\n  📈 Overall: {overall}")

    for bucket_key, label in BUCKET_LABELS.items():
        bv = verdict.get(bucket_key, {})
        if not bv:
            continue
        v_str = bv.get("verdict", "Neutral")
        c_str = bv.get("confidence", "Low")
        print(f"\n  {label}")
        print(f"    Verdict:    {verdict_emoji.get(v_str,'⚪')} {v_str}  Confidence: {confidence_emoji.get(c_str,'●○○')} {c_str}")
        print(f"    Key Driver: {bv.get('key_driver','')}")
        print(f"    Action:     {bv.get('action','')}")
        print(f"    Watch for:  {bv.get('watch_for','')}")

    print(f"\n{'='*60}")


# ─────────────────────────────────────────────
# MACRO SIGNALS BUILDER
# ─────────────────────────────────────────────

def get_us_macro_signals() -> dict:
    """
    Fetch live US macro signals. US equivalent of get_macro_adjustments()
    in macro_signals.py. Returns a dict that build_macro_context() can use.
    """
    import yfinance as yf
    import math

    macro = {
        "fed":              {"notes": "No data", "level": "unknown", "trend": "unknown"},
        "usd_strength":     {"notes": "No data", "level": "unknown", "trend": "unknown"},
        "us_market":        {"notes": "No data"},
        "inst_flows":       {"notes": "No data"},
        "policy":           {"notes": "No US policy signals loaded", "signals": {}},
        "news":             {"notes": "No US news signals loaded", "signals": {}},
        "adjusted_allocations": {
            "AI_CLOUD": 0.30, "SEMICONDUCTORS": 0.25,
            "HIGH_GROWTH_TECH": 0.25, "DEFENSIVE_DIV": 0.20,
        },
    }

    # ── 1. Fed rate proxy: 10-year Treasury yield (^TNX) ─────
    try:
        tnx = yf.Ticker("^TNX")
        hist = tnx.history(period="60d")
        if not hist.empty:
            current_yield = float(hist["Close"].iloc[-1])
            prev_yield    = float(hist["Close"].iloc[0])
            trend = "rising" if current_yield > prev_yield + 0.1 else "falling" if current_yield < prev_yield - 0.1 else "stable"
            level = "high" if current_yield > 4.5 else "elevated" if current_yield > 3.5 else "moderate"
            macro["fed"] = {
                "notes": f"10Y Treasury yield: {current_yield:.2f}% ({trend}) — proxy for Fed rate environment",
                "level": level, "trend": trend, "yield_10y": current_yield,
            }
    except Exception as e:
        print(f"  ⚠️  Fed/TNX fetch failed: {e}")

    # ── 2. USD Strength: DXY index (DX-Y.NYB) ────────────────
    try:
        dxy = yf.Ticker("DX-Y.NYB")
        hist = dxy.history(period="30d")
        if not hist.empty:
            dxy_now  = float(hist["Close"].iloc[-1])
            dxy_prev = float(hist["Close"].iloc[0])
            chg = ((dxy_now - dxy_prev) / dxy_prev) * 100
            trend = "strengthening" if chg > 1.0 else "weakening" if chg < -1.0 else "stable"
            level = "strong" if dxy_now > 104 else "moderate" if dxy_now > 100 else "weak"
            macro["usd_strength"] = {
                "notes": f"DXY: {dxy_now:.1f} ({chg:+.1f}% 30d) — USD is {level} and {trend}",
                "level": level, "trend": trend, "dxy": dxy_now,
            }
    except Exception as e:
        print(f"  ⚠️  DXY fetch failed: {e}")

    # ── 3. US Market: SP500 + Nasdaq + VIX ───────────────────
    try:
        sp = yf.Ticker("^GSPC"); nq = yf.Ticker("^IXIC"); vix = yf.Ticker("^VIX")
        sp_hist  = sp.history(period="60d")
        nq_hist  = nq.history(period="60d")
        vix_hist = vix.history(period="5d")
        sp_chg  = ((float(sp_hist["Close"].iloc[-1]) - float(sp_hist["Close"].iloc[0])) / float(sp_hist["Close"].iloc[0])) * 100 if not sp_hist.empty else None
        nq_chg  = ((float(nq_hist["Close"].iloc[-1]) - float(nq_hist["Close"].iloc[0])) / float(nq_hist["Close"].iloc[0])) * 100 if not nq_hist.empty else None
        vix_now = float(vix_hist["Close"].iloc[-1]) if not vix_hist.empty else None
        macro["us_market"] = {
            "notes": f"S&P500 30d: {sp_chg:+.1f}%, Nasdaq: {nq_chg:+.1f}%, VIX: {vix_now:.1f}" if sp_chg is not None else "Market data unavailable",
            "sp500_change": sp_chg, "nasdaq_change": nq_chg, "vix": vix_now,
            "high_volatility": vix_now and vix_now > 25,
            "tech_selloff": nq_chg and nq_chg < -5,
        }
    except Exception as e:
        print(f"  ⚠️  Market fetch failed: {e}")

    # ── 4. Load news signals if available ────────────────────
    news_file = os.path.join(os.path.dirname(__file__), "us_news_signals.json")
    if os.path.exists(news_file):
        try:
            with open(news_file) as f:
                news_data = json.load(f)
            signals = news_data.get("signals", {})
            macro["news"] = {
                "notes": f"US news sentiment from {news_data.get('total_headlines',0)} headlines",
                "signals": {bk: s.get("signal","neutral") for bk, s in signals.items()},
            }
        except Exception:
            pass

    # ── 5. Simple macro-based allocation adjustments ─────────
    fed_level = macro["fed"].get("level","moderate")
    usd_trend = macro["usd_strength"].get("trend","stable")
    vix_now   = macro["us_market"].get("vix", 18)

    adj = {"AI_CLOUD": 0.30, "SEMICONDUCTORS": 0.25, "HIGH_GROWTH_TECH": 0.25, "DEFENSIVE_DIV": 0.20}

    # High rates → shift toward defensive
    if fed_level == "high":
        adj["DEFENSIVE_DIV"]    = min(0.30, adj["DEFENSIVE_DIV"] + 0.05)
        adj["HIGH_GROWTH_TECH"] = max(0.15, adj["HIGH_GROWTH_TECH"] - 0.05)

    # High VIX → shift defensive
    if vix_now and vix_now > 25:
        adj["DEFENSIVE_DIV"] = min(0.30, adj["DEFENSIVE_DIV"] + 0.05)
        adj["AI_CLOUD"]      = max(0.20, adj["AI_CLOUD"] - 0.05)

    # Strong USD → generally good for US tech (domestic earner)
    if usd_trend == "strengthening":
        adj["AI_CLOUD"] = min(0.35, adj["AI_CLOUD"] + 0.02)

    macro["adjusted_allocations"] = adj
    return macro


# ─────────────────────────────────────────────
# MAIN ORCHESTRATOR
# ─────────────────────────────────────────────

def run_synthesis(macro: dict = None, test_mode: bool = False) -> dict | None:
    """Identical structure to run_synthesis() in llm_synthesiser.py."""
    print(f"\n{'='*60}")
    print(f"  🤖 US LLM SYNTHESISER — MACRO ANALYSIS")
    print(f"  {datetime.now(IST).strftime('%d %B %Y, %I:%M %p IST')}")
    print(f"{'='*60}\n")

    if macro is None:
        print("  Loading US macro signals...")
        macro = get_us_macro_signals()

    print("  📝 Building US macro context...")
    context = build_macro_context(macro)
    print(f"  Context length: {len(context)} chars")

    print("\n  🤖 Calling Claude API...")
    verdict = synthesise_macro_verdict(macro)

    if verdict is None:
        print("  ❌ US Synthesis failed — no verdict produced.")
        return None

    print_verdict(verdict)

    if not test_mode:
        save_synthesis(verdict, macro)

    return verdict


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="US LLM macro synthesiser")
    parser.add_argument("--status", action="store_true", help="Show latest saved synthesis")
    parser.add_argument("--test",   action="store_true", help="Run without saving")
    args = parser.parse_args()

    if args.status:
        data = load_synthesis()
        if not data:
            print("No US synthesis found. Run: python llm_synthesiser_us.py")
        else:
            print_verdict(data["verdict"], data.get("generated_at", ""))
    else:
        run_synthesis(test_mode=args.test)
