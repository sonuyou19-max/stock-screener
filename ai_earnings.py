#!/usr/bin/env python3
"""
ai_earnings.py — earnings-results thesis interpreter.

When a holding reports quarterly results, this reads the actual numbers
(revenue / net income / margins with YoY + QoQ deltas from yfinance) plus
any results-related headlines, and asks Claude the one question the raw
screener can't: "given why this stock was bought, did the result support
the thesis or undermine it?" — in plain English, with a hold/trim/exit lean.

Each quarter's result is interpreted once per holding (deduped by report
date). ADVISORY ONLY — it never trades.

Suggested cron — daily after close (16:00 IST) during earnings season, but
safe to run year-round (it no-ops when nobody has reported):
  0 11 * * 1-5 /home/ubuntu/kite/run_ai_earnings.sh >> /home/ubuntu/kite/ai_earnings.log 2>&1

Usage:
  python ai_earnings.py            # interpret any holding that just reported
  python ai_earnings.py --dry-run  # print only
  python ai_earnings.py --ticker RELIANCE.NS   # force one ticker (ignores dedup)
"""

import os
import math
import argparse
from datetime import datetime

import yfinance as yf

from ai_common import call_claude_json, MODEL_REASONING, AI_ENABLED
import ai_portfolio as pf

UPLOAD_TOKEN = os.getenv("UPLOAD_TOKEN", "")
SEEN_FILE    = "ai_earnings_seen.json"
RECENT_DAYS  = 21   # a report is "fresh" if filed within this many days

SYSTEM = (
    "You are an equity analyst helping an investor decide whether a just-"
    "reported quarterly result supports staying in a position. You read the "
    "numbers and the headlines and give a direct, plain-English verdict tied "
    "to whether the original reason for owning the stock still holds. You are "
    "balanced — neither a permabull nor an alarmist — and you flag when the "
    "data is too thin to judge. Respond with valid JSON only."
)


def _series(df, labels):
    for lab in labels:
        if lab in df.index:
            s = df.loc[lab].dropna()
            if len(s):
                return s
    return None


def _pct(new, old):
    try:
        if old and not math.isnan(old) and old != 0:
            return round((new - old) / abs(old) * 100, 1)
    except Exception:
        pass
    return None


def extract_results(ticker: str) -> dict | None:
    """Latest quarter's revenue / net income / margins with YoY & QoQ deltas.
    Returns None if no usable quarterly data."""
    try:
        q = yf.Ticker(ticker).quarterly_financials
    except Exception as e:
        print(f"  ⚠️  {ticker}: financials fetch failed: {e}")
        return None
    if q is None or q.empty or len(q.columns) < 1:
        return None

    report_date = q.columns[0]
    report_date = report_date.date() if hasattr(report_date, "date") else report_date
    age = (datetime.now().date() - report_date).days

    rev = _series(q, ["Total Revenue", "Operating Revenue"])
    ni  = _series(q, ["Net Income", "Net Income Common Stockholders"])
    gp  = _series(q, ["Gross Profit"])

    def val(s, i):
        try:
            return float(s.iloc[i])
        except Exception:
            return None

    out = {
        "report_date": str(report_date),
        "age_days": age,
        "revenue": val(rev, 0) if rev is not None else None,
        "net_income": val(ni, 0) if ni is not None else None,
    }
    if rev is not None and len(rev) >= 2:
        out["revenue_qoq_pct"] = _pct(val(rev, 0), val(rev, 1))
    if rev is not None and len(rev) >= 5:
        out["revenue_yoy_pct"] = _pct(val(rev, 0), val(rev, 4))
    if ni is not None and len(ni) >= 2:
        out["net_income_qoq_pct"] = _pct(val(ni, 0), val(ni, 1))
    if ni is not None and len(ni) >= 5:
        out["net_income_yoy_pct"] = _pct(val(ni, 0), val(ni, 4))
    if gp is not None and rev is not None and val(rev, 0):
        out["gross_margin_pct"] = round(val(gp, 0) / val(rev, 0) * 100, 1)
    if ni is not None and rev is not None and val(rev, 0):
        out["net_margin_pct"] = round(val(ni, 0) / val(rev, 0) * 100, 1)
    return out


def interpret(holding: dict, results: dict, headlines: list) -> dict:
    name, ticker = holding["name"], holding["ticker"]
    sym = ticker.replace(".NS", "")
    thesis_bits = []
    if holding.get("score"):
        thesis_bits.append(f"scanner score {holding['score']}")
    if holding.get("conviction"):
        thesis_bits.append(f"{holding['conviction']} conviction")
    if holding.get("sector"):
        thesis_bits.append(f"sector {holding['sector']}")
    bp, cur = holding.get("buy_price"), holding.get("price")
    pnl = f"{(float(cur)/float(bp)-1)*100:+.1f}%" if bp and cur else "unknown"
    thesis = ", ".join(thesis_bits) or "no recorded rationale"
    heads = "\n".join(f"  - {h[:150]}" for h in headlines) or "  (no results headlines found)"
    cur_txt = "{" + ", ".join(f'"{k}": {v}' for k, v in results.items()) + "}"

    prompt = f"""Holding: {name} ({sym})
Why it was bought: {thesis}
Current position P&L: {pnl}

Latest reported quarter (from filings):
{cur_txt}

Results-related headlines:
{heads}

Question: did this result support the reason for owning the stock, or
undermine it? Return JSON:
{{
  "verdict": "thesis_intact | mixed | thesis_weakening | thesis_broken | insufficient_data",
  "confidence": "High | Medium | Low",
  "summary": "2-3 sentences a holder can act on, citing the specific numbers",
  "positives": ["short bullet(s), or empty"],
  "concerns": ["short bullet(s), or empty"],
  "lean": "hold | trim | exit | add — one word",
  "lean_reason": "one sentence"
}}
Base the verdict on the numbers above; if they are too sparse to judge,
say insufficient_data rather than guessing."""
    return call_claude_json(prompt, model=MODEL_REASONING, max_tokens=900, system=SYSTEM)


VERDICT_EMOJI = {"thesis_intact": "🟢", "mixed": "🟠", "thesis_weakening": "🟠",
                 "thesis_broken": "🔴", "insufficient_data": "⚪"}


def main(dry_run: bool = False, force_ticker: str = None):
    print("=== AI Earnings Thesis Interpreter ===")
    if not AI_ENABLED:
        print("  ❌ ANTHROPIC_API_KEY not set — cannot run.")
        return None

    holdings = pf.get_all_holdings()
    if force_ticker:
        holdings = [h for h in holdings if h["ticker"] == force_ticker]
        if not holdings:
            holdings = [{"ticker": force_ticker, "name": force_ticker,
                         "market": "US" if not force_ticker.endswith(".NS") else "IN",
                         "book": "manual"}]
    if not holdings:
        print("  No holdings.")
        return None
    uniq = {}
    for h in holdings:
        uniq.setdefault(h["ticker"], h)
    holdings = list(uniq.values())

    seen = pf.load_seen(SEEN_FILE, keep_days=120)   # a quarter is ~90 days
    news = None
    reports = []

    for h in holdings:
        ticker = h["ticker"]
        results = extract_results(ticker)
        if not results:
            continue
        if results["age_days"] > RECENT_DAYS and not force_ticker:
            continue  # nothing freshly reported

        dedup_key = f"{ticker}::{results['report_date']}"
        if seen.get(dedup_key) and not force_ticker:
            print(f"  ✓ {ticker.replace('.NS',''):12} already interpreted {results['report_date']}")
            continue

        if news is None:
            news = pf.gather_news()
        heads = pf.match_headlines(h["name"], ticker, news, h.get("market", "IN"))
        # keep only results-ish headlines to focus the prompt
        kw = ("result", "profit", "revenue", "earnings", "quarter", "q1", "q2",
              "q3", "q4", "pat", "net income", "margin", "beats", "misses")
        results_heads = [x for x in heads if any(k in x.lower() for k in kw)] or heads[:3]

        print(f"  📊 {ticker.replace('.NS','')}: reported {results['report_date']} "
              f"({results['age_days']}d ago) — interpreting...")
        verdict = interpret(h, results, results_heads)
        if not verdict:
            print(f"     ⚠️  interpretation unavailable")
            continue
        verdict.update({"ticker": ticker, "name": h["name"], "book": h.get("book"),
                        "report_date": results["report_date"], "metrics": results})
        reports.append(verdict)
        seen[dedup_key] = str(datetime.now().date())
        v = verdict.get("verdict", "?")
        print(f"     {VERDICT_EMOJI.get(v,'•')} {v} / lean {verdict.get('lean','?')} — "
              f"{verdict.get('summary','')[:90]}")

    if dry_run:
        print(f"\n  [dry-run] {len(reports)} interpretation(s); skipping telegram + dedup write")
        return reports

    pf.save_seen(SEEN_FILE, seen)
    if reports:
        lines = ["📊 <b>Earnings Thesis Check</b>"]
        for r in reports:
            v = r.get("verdict", "?")
            lines.append(
                f"\n{VERDICT_EMOJI.get(v,'•')} <b>{r['ticker'].replace('.NS','')}</b> "
                f"[{r.get('book')}] — {v.replace('_',' ')} · lean <b>{r.get('lean','?')}</b>\n"
                f"{r.get('summary','')}")
            for c in (r.get("concerns") or [])[:2]:
                lines.append(f"  ⚠ {c}")
        pf.telegram("\n".join(lines))
        try:
            pf.post_json(f"{pf.API_URL}/signals/upload",
                         {"type": "ai_earnings",
                          "payload": {"reports": reports,
                                      "generated_at": str(datetime.now())}},
                         token=UPLOAD_TOKEN)
        except Exception as e:
            print(f"  ⚠️  Signal upload failed: {e}")
        print(f"  📨 {len(reports)} earnings interpretation(s) sent.")
    else:
        print("  No holdings reported recently — nothing to interpret.")
    return reports


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Earnings-results thesis interpreter")
    ap.add_argument("--dry-run", action="store_true", help="print only")
    ap.add_argument("--ticker", help="force interpret one ticker (ignores dedup)")
    args = ap.parse_args()
    main(dry_run=args.dry_run, force_ticker=args.ticker)
