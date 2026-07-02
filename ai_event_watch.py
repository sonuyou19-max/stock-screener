#!/usr/bin/env python3
"""
ai_event_watch.py — per-holding material-event watcher (daily).

Your rebalancers look at holdings monthly; a stock can blow up on day 8.
This watches news on the specific stocks you HOLD (swing + India monthly +
US), every day, and flags material events the price alone won't tell you
about — earnings surprises, downgrades, regulatory/legal action, promoter
pledge/stake sales, management/auditor exits, credit-rating changes, big
block deals — with a severity and a suggested action.

Only NEW headlines are considered (a rolling dedup store means the same
event doesn't re-alert every day), and only holdings with fresh matched
news are sent to Claude, so it's cheap. ADVISORY ONLY — it never trades;
it tells you to go look.

Suggested cron — twice daily on weekdays (before open, after close IST):
  30 3,11 * * 1-5 /home/ubuntu/kite/run_ai_event_watch.sh >> /home/ubuntu/kite/ai_event_watch.log 2>&1

Usage:
  python ai_event_watch.py            # scan holdings, alert on material events
  python ai_event_watch.py --dry-run  # print only, no telegram/dedup write
"""

import os
import json
import argparse

from ai_common import call_claude_json, MODEL_FAST, AI_ENABLED
import ai_portfolio as pf

UPLOAD_TOKEN = os.getenv("UPLOAD_TOKEN", "")
SEEN_FILE    = "ai_event_seen.json"
MIN_NEW_HEADLINES = 1

SYSTEM = (
    "You are a portfolio risk monitor. Given recent news headlines about a "
    "stock the user already OWNS, decide whether anything MATERIAL has "
    "happened that a holder should act on — earnings surprises, analyst "
    "downgrades/upgrades, regulatory or legal action, promoter pledging or "
    "stake sales, management/auditor resignations, credit-rating changes, "
    "large block/bulk deals, M&A, guidance changes, dilution. Routine price "
    "moves, generic market commentary and reiterated ratings are NOT "
    "material. Judge only from the headlines given. Respond with valid JSON only."
)


def assess_holding(name: str, ticker: str, pnl_pct, new_heads: list) -> dict:
    sym = ticker.replace(".NS", "")
    pnl_txt = f"currently {pnl_pct:+.1f}% for the holder" if pnl_pct is not None else ""
    bullets = "\n".join(f"  - {h[:150]}" for h in new_heads)
    prompt = f"""Stock held: {name} ({sym}) {pnl_txt}

New headlines since the last check:
{bullets}

Return JSON:
{{
  "material": true or false,
  "severity": "critical | notable | minor",
  "event_type": "short label, e.g. 'analyst downgrade', 'promoter pledge', 'earnings beat'",
  "summary": "one sentence on what happened",
  "suggested_action": "one concrete sentence: review/exit/hold/trim and why",
  "headline_ref": "the single most important headline verbatim"
}}
If nothing material is in these headlines, return {{"material": false}} and omit the rest."""
    result = call_claude_json(prompt, model=MODEL_FAST, max_tokens=500, system=SYSTEM)
    return result or {"material": False}


SEV_EMOJI = {"critical": "🔴", "notable": "🟠", "minor": "🟡"}


def main(dry_run: bool = False):
    print("=== AI Per-Holding Event Watcher ===")
    if not AI_ENABLED:
        print("  ❌ ANTHROPIC_API_KEY not set — cannot run.")
        return None

    holdings = pf.get_all_holdings()
    if not holdings:
        print("  No live holdings across any book — nothing to watch.")
        return None
    # De-dupe the same ticker held in more than one book
    uniq = {}
    for h in holdings:
        uniq.setdefault(h["ticker"], h)
    holdings = list(uniq.values())
    print(f"  {len(holdings)} unique holding(s): "
          + ", ".join(h["ticker"].replace('.NS', '') for h in holdings))

    news = pf.gather_news()
    seen = pf.load_seen(SEEN_FILE)

    alerts = []
    newly_seen = {}
    for h in holdings:
        ticker, name, market = h["ticker"], h["name"], h["market"]
        matched = pf.match_headlines(name, ticker, news, market)
        new_heads = [hl for hl in matched
                     if pf.headline_key(ticker, hl) not in seen]
        if len(new_heads) < MIN_NEW_HEADLINES:
            continue

        # PnL for context (best-effort from the stored position)
        bp = h.get("buy_price")
        pnl_pct = None
        if bp and h.get("price"):
            try:
                pnl_pct = (float(h["price"]) / float(bp) - 1) * 100
            except Exception:
                pass

        verdict = assess_holding(name, ticker, pnl_pct, new_heads)
        # Mark these headlines seen regardless of verdict (don't re-ask daily)
        for hl in new_heads:
            newly_seen[pf.headline_key(ticker, hl)] = str(__import__("datetime").date.today())

        if verdict.get("material"):
            verdict["ticker"] = ticker
            verdict["name"] = name
            verdict["book"] = h["book"]
            alerts.append(verdict)
            sev = verdict.get("severity", "minor")
            print(f"  {SEV_EMOJI.get(sev,'•')} {ticker.replace('.NS',''):12} "
                  f"{sev.upper():8} {verdict.get('event_type','')} — {verdict.get('summary','')[:70]}")
        else:
            print(f"  ✓ {ticker.replace('.NS',''):12} {len(new_heads)} new headline(s), nothing material")

    if dry_run:
        print(f"\n  [dry-run] {len(alerts)} material event(s); skipping telegram + dedup write")
        return alerts

    # Persist dedup (merge) so we don't re-alert
    seen.update(newly_seen)
    pf.save_seen(SEEN_FILE, seen)

    if alerts:
        alerts.sort(key=lambda a: {"critical": 0, "notable": 1, "minor": 2}
                    .get(a.get("severity"), 3))
        lines = ["🔔 <b>Holdings Event Watch</b>"]
        for a in alerts:
            sev = a.get("severity", "minor")
            lines.append(
                f"\n{SEV_EMOJI.get(sev,'•')} <b>{a['ticker'].replace('.NS','')}</b> "
                f"[{a['book']}] — {a.get('event_type','event')}\n"
                f"{a.get('summary','')}\n"
                f"→ <i>{a.get('suggested_action','')}</i>")
        pf.telegram("\n".join(lines))
        try:
            pf.post_json(f"{pf.API_URL}/signals/upload",
                         {"type": "ai_holdings_events",
                          "payload": {"events": alerts,
                                      "generated_at": str(__import__("datetime").datetime.now())}},
                         token=UPLOAD_TOKEN)
        except Exception as e:
            print(f"  ⚠️  Signal upload failed: {e}")
        print(f"  📨 {len(alerts)} material event(s) alerted.")
    else:
        print("  No material events — no alert.")
    return alerts


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Per-holding material event watcher")
    ap.add_argument("--dry-run", action="store_true", help="print only")
    args = ap.parse_args()
    main(dry_run=args.dry_run)
