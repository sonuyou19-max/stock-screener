#!/usr/bin/env python3
"""
ai_attribution.py — AI Attribution Analyst (monthly).

swing_attribution.py aggregates closed trades into tables (win rate / avg
R by score band, sector, signal, exit reason). This takes that structured
summary plus the trade-level snapshots and asks Claude to REASON over it:
which signals actually predict winners, whether each scanner weight is
earning its keep, and specific, bounded weight-change proposals.

This is the reasoning half of the feedback loop. It is ADVISORY ONLY —
it never edits SIGNAL_WEIGHTS or places orders. Output goes to Telegram
and the API signal store (type=ai_attribution) for dashboard display; you
apply any weight change by hand after reading the rationale.

Honesty guardrails:
  - Below MIN_TRADES closed trades it refuses to draw conclusions.
  - The prompt forces a confidence + explicit sample-size caveat and caps
    proposed weight moves so one noisy month can't swing the model.

Suggested cron (1st of month, 7:00 PM IST = 13:30 UTC, after the day's
data is in):
  30 13 1 * * /home/ubuntu/kite/run_ai_attribution.sh >> /home/ubuntu/kite/ai_attribution.log 2>&1

Usage:
  python ai_attribution.py            # analyse, telegram, upload
  python ai_attribution.py --dry-run  # print only
"""

import os
import json
import argparse
import urllib.request as _req

from ai_common import call_claude_json, MODEL_REASONING, AI_ENABLED
from swing_attribution import build_report, format_report

API_URL      = os.getenv("API_URL", "https://web-production-50eee.up.railway.app").rstrip("/")
UPLOAD_TOKEN = os.getenv("UPLOAD_TOKEN", "")
TELEGRAM_BOT = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT= os.getenv("TELEGRAM_CHAT_ID", "")

MIN_TRADES     = 15    # below this, don't ask the model to conclude
MAX_WEIGHT_MOVE = 0.05 # cap any single proposed weight change (informational)

# Current scanner weights — kept in sync with swing_scanner.SIGNAL_WEIGHTS.
# Passed to the model as the baseline it is critiquing.
CURRENT_WEIGHTS = {
    "momentum": 0.20, "volume": 0.20, "macd": 0.15, "sentiment": 0.15,
    "rsi": 0.10, "bollinger": 0.10, "fii": 0.10,
}

SYSTEM = (
    "You are a quantitative trading analyst reviewing a swing-trading "
    "strategy's realised results. You reason carefully from evidence, "
    "distinguish signal from noise, and are conservative: you would rather "
    "say 'insufficient data' than over-fit to a small sample. You never "
    "recommend leverage or changes that aren't supported by the numbers in "
    "front of you. Respond with valid JSON only — no markdown, no preamble."
)


def _get(url):
    r = _req.Request(url, headers={"Accept": "application/json"})
    with _req.urlopen(r, timeout=25) as resp:
        return json.loads(resp.read())


def _post(url, payload, headers=None):
    body = json.dumps(payload, default=str).encode()
    h = {"Content-Type": "application/json", **(headers or {})}
    r = _req.Request(url, data=body, headers=h, method="POST")
    with _req.urlopen(r, timeout=25) as resp:
        return json.loads(resp.read())


def _tg(msg: str):
    if not TELEGRAM_BOT or not TELEGRAM_CHAT:
        print("  ⚠️  Telegram not configured — skipping alert.")
        return
    try:
        _post(f"https://api.telegram.org/bot{TELEGRAM_BOT}/sendMessage",
              {"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "HTML"})
    except Exception as e:
        print(f"  ⚠️  Telegram failed: {e}")


def _compact_trades(trades: list) -> list:
    """Trade-level rows the model can pattern-match on, snapshot fields only."""
    out = []
    for t in trades:
        if t.get("score") is None or (t.get("max_score") or 100) <= 10:
            continue  # skip pre-snapshot / old-scale trades
        sigs = t.get("signals") or {}
        strengths = {k: round(v.get("strength"), 0)
                     for k, v in sigs.items()
                     if isinstance(v, dict) and v.get("strength") is not None}
        out.append({
            "score":       t.get("score"),
            "conviction":  t.get("conviction"),
            "sector":      t.get("sector"),
            "sentiment":   t.get("sentiment_val"),
            "regime":      t.get("regime"),
            "exit":        t.get("exit_reason"),
            "pnl_pct":     t.get("realised_pnl_pct"),
            "r_multiple":  t.get("r_multiple"),
            "signal_strengths": strengths,
        })
    return out


def build_prompt(report: dict, trades: list) -> str:
    return f"""Analyse this swing-trading strategy's realised results.

STRATEGY DESIGN
The scanner scores each candidate 0-100 as a weighted blend of signals.
Current signal weights:
{json.dumps(CURRENT_WEIGHTS, indent=2)}
A candidate must clear a composite floor (62 bullish regime / 72 bearish)
to qualify. Exits: Target1 +7% (sell half), Target2 +12% (sell rest),
ATR/structure stop, 10-trading-day time exit.

AGGREGATE RESULTS (buckets under 3 trades are hidden; ignore tiny cells):
{json.dumps(report, indent=2, default=str)}

TRADE-LEVEL DATA ({len(trades)} trades with entry snapshots):
{json.dumps(trades, indent=2, default=str)}

Produce a JSON object with EXACTLY these keys:
{{
  "headline": "one-sentence bottom line on how the strategy is performing",
  "confidence": "High | Medium | Low — based on sample size and consistency",
  "sample_caveat": "one sentence on what this sample can and cannot tell us",
  "key_findings": ["3-5 short evidence-based observations, each citing the numbers"],
  "signal_assessment": {{
     "<signal_name>": "earning_weight | overweighted | underweighted | unclear — one clause why"
  }},
  "weight_proposals": [
     {{"signal": "<name>", "direction": "increase | decrease",
       "suggested_delta": <number, magnitude <= {MAX_WEIGHT_MOVE}>,
       "rationale": "one sentence grounded in the results"}}
  ],
  "what_to_watch": ["1-3 things to confirm as more trades close"],
  "do_nothing_if_unsure": true
}}

Rules:
- If the evidence for a weight change is weak, return an empty
  weight_proposals list and say so — do not invent changes.
- Every proposal's suggested_delta magnitude must be <= {MAX_WEIGHT_MOVE}.
- Ground every finding in the actual numbers above; no generic advice."""


def format_telegram(analysis: dict, n: int) -> str:
    lines = [f"🧠 <b>AI Attribution Analyst</b> ({n} closed trades)",
             f"<i>{analysis.get('headline', '')}</i>",
             f"Confidence: <b>{analysis.get('confidence', '?')}</b> — "
             f"{analysis.get('sample_caveat', '')}"]
    kf = analysis.get("key_findings") or []
    if kf:
        lines.append("\n<b>Findings:</b>")
        lines += [f"  • {f}" for f in kf[:5]]
    props = analysis.get("weight_proposals") or []
    if props:
        lines.append("\n<b>Weight proposals (advisory — apply by hand):</b>")
        for p in props:
            arrow = "▲" if p.get("direction") == "increase" else "▼"
            lines.append(f"  {arrow} {p.get('signal')} "
                         f"{p.get('suggested_delta')} — {p.get('rationale')}")
    else:
        lines.append("\n<b>Weight proposals:</b> none — evidence insufficient to change weights.")
    return "\n".join(lines)


def main(dry_run: bool = False):
    print("=== AI Attribution Analyst ===")
    if not AI_ENABLED:
        print("  ❌ ANTHROPIC_API_KEY not set — cannot run.")
        return None

    data = _get(f"{API_URL}/swing/history")
    all_trades = data.get("trades", [])
    report = build_report(all_trades)
    print(format_report(report))

    snap_trades = _compact_trades(all_trades)
    if len(snap_trades) < MIN_TRADES:
        msg = (f"🧠 <b>AI Attribution Analyst</b>\nOnly {len(snap_trades)} closed "
               f"trades carry entry snapshots (need ≥{MIN_TRADES}). Holding off "
               f"on analysis until the sample is meaningful.")
        print(f"  ⏸  {len(snap_trades)}/{MIN_TRADES} snapshot trades — too few to analyse.")
        if not dry_run:
            _tg(msg)
        return None

    print(f"  🧠 Sending {len(snap_trades)} trades to {MODEL_REASONING}...")
    analysis = call_claude_json(
        build_prompt(report, snap_trades),
        model=MODEL_REASONING, max_tokens=2000, system=SYSTEM,
    )
    if not analysis:
        print("  ❌ No analysis returned.")
        if not dry_run:
            _tg("🧠 <b>AI Attribution Analyst</b>\n⚠️ Analysis failed this run — see logs.")
        return None

    # Clamp any oversized proposal defensively (model is instructed, but trust nothing)
    for p in analysis.get("weight_proposals", []) or []:
        try:
            d = abs(float(p.get("suggested_delta", 0)))
            if d > MAX_WEIGHT_MOVE:
                p["suggested_delta"] = round(
                    MAX_WEIGHT_MOVE * (1 if p.get("direction") == "increase" else -1), 3)
                p["_clamped"] = True
        except Exception:
            p["suggested_delta"] = 0

    print("\n" + json.dumps(analysis, indent=2))
    print(format_telegram(analysis, len(snap_trades)))

    if dry_run:
        print("\n  [dry-run] skipping Telegram + upload")
        return analysis

    _tg(format_telegram(analysis, len(snap_trades)))
    try:
        out = {"generated_for_trades": len(snap_trades),
               "aggregate": report, "analysis": analysis}
        headers = {"X-Upload-Token": UPLOAD_TOKEN} if UPLOAD_TOKEN else {}
        _post(f"{API_URL}/signals/upload",
              {"type": "ai_attribution", "payload": out}, headers)
        print("  ✅ Uploaded to signal store (ai_attribution)")
    except Exception as e:
        print(f"  ⚠️  Upload failed: {e}")
    return analysis


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="AI attribution analyst")
    ap.add_argument("--dry-run", action="store_true", help="print only")
    args = ap.parse_args()
    main(dry_run=args.dry_run)
