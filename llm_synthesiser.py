"""
LLM Synthesiser — Claude API Macro Analyst (4.5)
==================================================
Takes all 6 macro signals and asks Claude to reason through
conflicts and produce a plain English verdict per bucket.

Acts like a morning briefing from a research analyst —
synthesising crude, FX, global markets, FII/DII, policy,
and news into a single actionable recommendation per sector.

Runs on Railway: Monday 8 AM IST (after policy_scraper.py)

Usage:
    python llm_synthesiser.py          # run synthesis
    python llm_synthesiser.py --status # show latest verdict
    python llm_synthesiser.py --test   # run without saving
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

IST              = ZoneInfo("Asia/Kolkata")
SYNTHESIS_FILE   = os.path.join(os.path.dirname(__file__), "llm_synthesis.json")
ANTHROPIC_API    = "https://api.anthropic.com/v1/messages"
MODEL            = "claude-sonnet-4-20250514"
MAX_TOKENS       = 2800

BUCKET_LABELS = {
    "Financial Services":             "🏦 Financial Services",
    "Information Technology":         "💻 Information Technology",
    "Oil Gas And Consumable Fuels":   "🛢️  Oil Gas & Fuels",
    "Fast Moving Consumer Goods":     "🛒 FMCG",
    "Healthcare":                     "💊 Healthcare",
    "Automobile and Auto Components": "🚗 Auto & Auto Components",
    "Capital Goods":                  "⚙️  Capital Goods",
    "Metals And Mining":              "⛏️  Metals & Mining",
    "Consumer Durables":              "📺 Consumer Durables",
    "Chemicals":                      "🧪 Chemicals",
    "Construction Materials":         "🏗️  Construction Materials",
    "Power":                          "⚡ Power",
    "Telecommunication":              "📡 Telecommunication",
    "Consumer Services":              "🍽️  Consumer Services",
    "Services And Logistics":         "🚚 Services & Logistics",
    "Realty":                         "🏠 Realty",
    "Diversified And Infrastructure": "🛤️  Diversified & Infrastructure",
    "Textiles And Apparel":           "👗 Textiles & Apparel",
    "Media And Entertainment":        "🎬 Media & Entertainment",
    "Paper And Forest Products":      "📄 Paper & Forest Products",
}


# ─────────────────────────────────────────────
# CONTEXT BUILDER
# ─────────────────────────────────────────────

def build_macro_context(macro: dict) -> str:
    """
    Format all 6 macro signals into clean, concise text
    for the LLM prompt.
    """
    lines = []

    # 1. Crude Oil
    crude = macro.get("crude", {})
    lines.append(f"BRENT CRUDE OIL:\n  {crude.get('notes','No data')}")
    lines.append(f"  Level: {crude.get('level','unknown').title()}")
    lines.append(f"  Trend: {crude.get('trend','unknown').title()}")

    # 2. USD/INR
    usdinr = macro.get("usdinr", {})
    lines.append(f"\nUSD/INR CURRENCY:\n  {usdinr.get('notes','No data')}")
    if usdinr.get("rate"):
        lines.append(
            f"  Rate: ₹{usdinr['rate']} | "
            f"Level: {usdinr.get('level','?').title()} | "
            f"Trend: {usdinr.get('trend','?').title()}"
        )

    # 3. Global Markets
    glb = macro.get("global", {})
    lines.append(f"\nGLOBAL MARKETS:\n  {glb.get('notes','No data')}")
    if glb.get("sp500_change") is not None:
        nasdaq_str = (
            f" | Nasdaq: {glb['nasdaq_change']:+.1f}%"
            if glb.get("nasdaq_change") is not None else ""
        )
        lines.append(
            f"  S&P 500 30d: {glb['sp500_change']:+.1f}%"
            f"  Vol: {glb.get('volatility','?')}%/day"
            f"{nasdaq_str}"
        )
        if glb.get("nasdaq_selloff"):
            lines.append("  ⚠️ Nasdaq tech selloff detected")

    # 4. FII/DII Flows
    fiidii = macro.get("fiidii", {})
    lines.append(f"\nFII/DII FLOWS (10-day rolling):\n  {fiidii.get('notes','No data')}")
    if fiidii.get("fii_10d_cr") is not None:
        lines.append(
            f"  FII: ₹{fiidii['fii_10d_cr']:+,.0f}Cr | "
            f"DII: ₹{fiidii.get('dii_10d_cr',0):+,.0f}Cr"
        )

    # 5. Policy Signals
    policy = macro.get("policy", {})
    lines.append(f"\nRBI + GOVERNMENT POLICY:\n  {policy.get('notes','No data')}")
    for bucket, sig in policy.get("signals", {}).items():
        if sig not in ("neutral",):
            lines.append(f"  {bucket}: {sig}")

    # 6. News Sentiment
    news = macro.get("news", {})
    lines.append(f"\nFINANCIAL NEWS SENTIMENT:\n  {news.get('notes','No data')}")
    for bucket, sig in news.get("signals", {}).items():
        if sig not in ("neutral",):
            lines.append(f"  {bucket}: {sig}")

    # Allocation adjustments computed
    adj = macro.get("adjusted_allocations", {})
    if adj:
        lines.append(f"\nCOMPUTED ALLOCATION ADJUSTMENTS:")
        for b, v in adj.items():
            lines.append(f"  {b}: {v*100:.1f}%")

    return "\n".join(lines)


# ─────────────────────────────────────────────
# LLM CALL
# ─────────────────────────────────────────────

def synthesise_macro_verdict(macro: dict) -> dict | None:
    """
    Call Claude API with all macro context and get structured
    per-bucket verdicts.

    Returns dict with verdicts, or None on failure.
    """
    context = build_macro_context(macro)

    system_prompt = (
        "You are a senior macro analyst for an Indian equity portfolio. "
        "You synthesise multiple macro signals and give structured, "
        "actionable verdicts per sector bucket. "
        "Be direct and concise. Prioritise contradictions — explain which "
        "signal dominates and why. "
        "Always respond with valid JSON only, no markdown, no preamble."
    )

    sectors_list = "\n".join(f"{i+1}. {s}" for i, s in enumerate(BUCKET_LABELS))
    example_sector = list(BUCKET_LABELS.keys())[0]

    user_prompt = f"""Today's macro signals for Indian equity markets:

{context}

The portfolio covers 20 NSE sectors. For each sector, provide a verdict considering ALL signals above — especially when they conflict.

For EACH sector provide:
- verdict: exactly one of: Positive | Cautious | Neutral | Negative
- confidence: exactly one of: High | Medium | Low
- key_driver: one sentence — the main signal driving this verdict
- action: one sentence — what to do with this sector's allocation
- watch_for: one thing to monitor in the next 2-4 weeks

Sectors to analyse:
{sectors_list}

Respond with ONLY a JSON object in this exact format (all 20 sectors + overall_market):
{{
  "{example_sector}": {{
    "verdict": "...",
    "confidence": "...",
    "key_driver": "...",
    "action": "...",
    "watch_for": "..."
  }},
  ... (all 20 sectors) ...,
  "overall_market": "one sentence on overall Indian market outlook"
}}"""

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("  ❌ ANTHROPIC_API_KEY not set — skipping LLM synthesis.")
        return None

    try:
        resp = requests.post(
            ANTHROPIC_API,
            headers={
                "Content-Type":    "application/json",
                "x-api-key":       api_key,
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

        # Extract text from response
        raw_text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                raw_text += block.get("text", "")

        if not raw_text:
            print("  ⚠️  LLM returned empty response")
            return None

        # Strip any accidental markdown fences
        raw_text = raw_text.strip()
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]

        verdict = json.loads(raw_text.strip())
        return verdict

    except json.JSONDecodeError as e:
        print(f"  ⚠️  LLM returned invalid JSON: {e}")
        print(f"  Raw: {raw_text[:200]}")
        return None
    except Exception as e:
        print(f"  ⚠️  LLM API call failed: {e}")
        return None


# ─────────────────────────────────────────────
# PERSISTENCE
# ─────────────────────────────────────────────

def save_synthesis(verdict: dict, macro: dict):
    output = {
        "generated_at": datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"),
        "model":        MODEL,
        "verdict":      verdict,
        "macro_summary": {
            "crude":  macro.get("crude", {}).get("notes", ""),
            "usdinr": macro.get("usdinr", {}).get("notes", ""),
            "global": macro.get("global", {}).get("notes", ""),
            "fiidii": macro.get("fiidii", {}).get("notes", ""),
            "policy": macro.get("policy", {}).get("notes", ""),
            "news":   macro.get("news", {}).get("notes", ""),
        },
    }
    with open(SYNTHESIS_FILE, "w") as f:
        json.dump(output, f, indent=2)
    print(f"  ✅ Synthesis saved to {SYNTHESIS_FILE}")
    _post_signal_to_api("llm_synthesis", output)


def _post_signal_to_api(signal_type: str, payload: dict):
    """POST signal data to the web API so the dashboard can read it."""
    import urllib.request as _urllib
    import os as _os
    api_url = _os.getenv("API_URL", "https://web-production-2d832.up.railway.app")
    url = f"{api_url}/signals/upload"
    try:
        import json as _json
        body = _json.dumps({"type": signal_type, "payload": payload}).encode("utf-8")
        req = _urllib.Request(url, data=body,
                              headers={"Content-Type": "application/json"},
                              method="POST")
        with _urllib.urlopen(req, timeout=10) as resp:
            print(f"  ✅ {signal_type} POSTed to API: {resp.read().decode()}")
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
    """Pretty-print the LLM verdict."""
    verdict_emoji = {
        "Positive": "🟢",
        "Cautious": "🟠",
        "Neutral":  "⚪",
        "Negative": "🔴",
    }
    confidence_emoji = {
        "High":   "●●●",
        "Medium": "●●○",
        "Low":    "●○○",
    }

    print(f"\n{'='*60}")
    print(f"  🤖 LLM MACRO SYNTHESIS")
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

        v_str  = bv.get("verdict", "Neutral")
        c_str  = bv.get("confidence", "Low")
        emoji  = verdict_emoji.get(v_str, "⚪")
        conf   = confidence_emoji.get(c_str, "●○○")

        print(f"\n  {label}")
        print(f"    Verdict:    {emoji} {v_str}  Confidence: {conf} {c_str}")
        print(f"    Key Driver: {bv.get('key_driver','')}")
        print(f"    Action:     {bv.get('action','')}")
        print(f"    Watch for:  {bv.get('watch_for','')}")

    print(f"\n{'='*60}")


# ─────────────────────────────────────────────
# MAIN ORCHESTRATOR
# ─────────────────────────────────────────────

def run_synthesis(macro: dict = None, test_mode: bool = False) -> dict | None:
    """
    Build context from macro signals, call Claude API,
    save and return verdict.

    If macro dict not provided, loads from individual signal files.
    """
    print(f"\n{'='*60}")
    print(f"  🤖 LLM SYNTHESISER — MACRO ANALYSIS")
    print(f"  {datetime.now(IST).strftime('%d %B %Y, %I:%M %p IST')}")
    print(f"{'='*60}\n")

    # If no macro dict passed, build from saved signal files
    if macro is None:
        print("  Loading macro signals from files...")
        from macro_signals import get_macro_adjustments
        base = {
            "BFSI_IT":         0.30,
            "DEFENCE_INFRA":   0.30,
            "GREEN_ENERGY_EV": 0.20,
            "FMCG_PHARMA":     0.20,
        }
        macro = get_macro_adjustments(base)

    print("  📝 Building macro context...")
    context = build_macro_context(macro)
    print(f"  Context length: {len(context)} chars")

    print("\n  🤖 Calling Claude API...")
    verdict = synthesise_macro_verdict(macro)

    if verdict is None:
        print("  ❌ Synthesis failed — no verdict produced.")
        return None

    print_verdict(verdict)

    if not test_mode:
        save_synthesis(verdict, macro)

    return verdict


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LLM macro synthesiser")
    parser.add_argument("--status", action="store_true",
                        help="Show latest saved synthesis")
    parser.add_argument("--test",   action="store_true",
                        help="Run synthesis without saving")
    args = parser.parse_args()

    if args.status:
        data = load_synthesis()
        if not data:
            print("No synthesis found. Run: python llm_synthesiser.py")
        else:
            print_verdict(data["verdict"], data.get("generated_at", ""))
            print("\n  Macro context used:")
            for k, v in data.get("macro_summary", {}).items():
                print(f"    {k}: {v}")
    else:
        run_synthesis(test_mode=args.test)
