#!/usr/bin/env python3
"""
ai_redteam.py — Pre-trade AI red-team for swing candidates.

The scanner scores candidates on numbers alone. This reads recent NEWS on
each candidate and gives a qualitative veto/confidence flag — catching the
landmines pure quant misses (governance events, regulatory action, promoter
pledge/exit, block deals, auditor/management resignations, fraud probes).

For every candidate in /swing/candidates it:
  1. gathers recent headlines mentioning that specific company (word-boundary
     matched against the RSS feed the sentiment pipeline already fetches),
  2. asks Claude Haiku for a flag — CLEAR / CAUTION / AVOID — with reasons,
  3. attaches a `redteam` block to the candidate and re-uploads, so the
     dashboard can show it on the card.
Candidates with no relevant news get a neutral "no news" verdict (absence of
red flags is not a green light — the score still governs).

ADVISORY ONLY — it never removes candidates or places/blocks orders. It just
surfaces what the numbers can't see so you decide before queueing. Any AVOID
also fires a Telegram so you see it even without opening the dashboard.

Suggested cron (VPS, ~15 min after the nightly scan; scan runs 11:00 PM IST):
  15 18 * * 0-5 /home/ubuntu/kite/run_ai_redteam.sh >> /home/ubuntu/kite/ai_redteam.log 2>&1
  (18:15 UTC = 11:45 PM IST)

Usage:
  python ai_redteam.py            # analyse candidates, re-upload, telegram
  python ai_redteam.py --dry-run  # print only, no upload/telegram
"""

import os
import re
import json
import argparse
import urllib.request as _req

from ai_common import call_claude_json, MODEL_FAST, AI_ENABLED

API_URL      = os.getenv("API_URL", "https://web-production-50eee.up.railway.app").rstrip("/")
UPLOAD_TOKEN = os.getenv("UPLOAD_TOKEN", "")
TELEGRAM_BOT = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT= os.getenv("TELEGRAM_CHAT_ID", "")

MAX_HEADLINES = 12   # per stock, most recent first
NAME_STOPWORDS = {"ltd", "limited", "the", "india", "industries", "corporation",
                  "company", "co", "&", "and", "enterprises"}

SYSTEM = (
    "You are a risk analyst doing a final pre-trade check on a stock a "
    "quantitative screener already likes. Your only job is to catch "
    "qualitative red flags the numbers miss — governance issues, regulatory "
    "or legal action, promoter pledging or stake sales, auditor or management "
    "resignations, fraud allegations, credit-rating downgrades, block/bulk "
    "deals, dilution. You judge ONLY from the headlines given; you do not "
    "speculate beyond them. Absence of bad news is CLEAR, not an endorsement. "
    "Respond with valid JSON only."
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
        return
    try:
        _post(f"https://api.telegram.org/bot{TELEGRAM_BOT}/sendMessage",
              {"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "HTML"})
    except Exception as e:
        print(f"  ⚠️  Telegram failed: {e}")


def _search_terms(name: str, ticker: str) -> list:
    """Distinctive words from the company name for headline matching."""
    base = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    words = [w for w in base.split() if len(w) > 3 and w not in NAME_STOPWORDS]
    terms = []
    if words:
        terms.append(" ".join(words[:2]))   # first two significant words as a phrase
        terms += [w for w in words if len(w) >= 5][:3]   # standalone distinctive words
    return list(dict.fromkeys(terms))       # dedupe, keep order


def _match_headlines(terms: list, items: list) -> list:
    if not terms:
        return []
    patterns = [re.compile(r"\b" + re.escape(t) + r"\b") for t in terms]
    hits = []
    for it in items:
        text = (it.get("title", "") + " " + it.get("body", "")).lower()
        if any(p.search(text) for p in patterns):
            hits.append(it.get("title", "").strip())
    return hits[:MAX_HEADLINES]


def redteam_stock(name: str, ticker: str, headlines: list) -> dict:
    sym = ticker.replace(".NS", "")
    if not headlines:
        return {"flag": "NO_NEWS", "confidence": "Low",
                "reasons": [], "summary": "No recent company-specific news found.",
                "headlines_seen": 0}
    bullets = "\n".join(f"  - {h[:140]}" for h in headlines)
    prompt = f"""Company: {name} ({sym})

Recent news headlines mentioning this company:
{bullets}

Assess pre-trade risk from ONLY these headlines. Return JSON:
{{
  "flag": "CLEAR | CAUTION | AVOID",
  "confidence": "High | Medium | Low",
  "reasons": ["specific red flags found, quoting the headline basis; empty if none"],
  "summary": "one sentence a trader can act on"
}}
Guidance: CLEAR = nothing concerning; CAUTION = something to be aware of but
not disqualifying; AVOID = a serious governance/legal/dilution/fraud red flag.
If the headlines are just routine market noise or price commentary, that is CLEAR."""

    result = call_claude_json(prompt, model=MODEL_FAST, max_tokens=600, system=SYSTEM)
    if not result or result.get("flag") not in ("CLEAR", "CAUTION", "AVOID"):
        return {"flag": "ERROR", "confidence": "Low", "reasons": [],
                "summary": "Red-team check unavailable this run.",
                "headlines_seen": len(headlines)}
    result["headlines_seen"] = len(headlines)
    return result


FLAG_EMOJI = {"CLEAR": "🟢", "CAUTION": "🟠", "AVOID": "🔴",
              "NO_NEWS": "⚪", "ERROR": "⚠️"}


def main(dry_run: bool = False):
    print("=== AI Pre-trade Red-team ===")
    if not AI_ENABLED:
        print("  ❌ ANTHROPIC_API_KEY not set — cannot run.")
        return None

    data = _get(f"{API_URL}/swing/candidates")
    candidates = data.get("candidates", [])
    if not candidates:
        print("  No candidates to red-team.")
        return None
    print(f"  {len(candidates)} candidates")

    # Reuse the sentiment pipeline's RSS fetch — same feeds, already deduped.
    try:
        from swing_news_sentiment import fetch_all_feeds
        news_items = fetch_all_feeds()
        print(f"  📰 {len(news_items)} news items in window")
    except Exception as e:
        print(f"  ⚠️  Could not fetch news ({e}) — every candidate will be NO_NEWS.")
        news_items = []

    avoids, cautions = [], []
    for c in candidates:
        name, ticker = c.get("name", ""), c.get("ticker", "")
        terms = _search_terms(name, ticker)
        heads = _match_headlines(terms, news_items)
        verdict = redteam_stock(name, ticker, heads)
        c["redteam"] = verdict
        emoji = FLAG_EMOJI.get(verdict["flag"], "❓")
        print(f"  {emoji} {ticker.replace('.NS',''):14} {verdict['flag']:8} "
              f"({verdict['headlines_seen']} news) — {verdict['summary'][:70]}")
        if verdict["flag"] == "AVOID":
            avoids.append((c, verdict))
        elif verdict["flag"] == "CAUTION":
            cautions.append((c, verdict))

    if dry_run:
        print("\n  [dry-run] skipping re-upload + telegram")
        return candidates

    # Re-upload candidates with the redteam block attached
    try:
        data["candidates"] = candidates
        data["redteam_at"] = data.get("generated_at")
        headers = {"X-Upload-Token": UPLOAD_TOKEN} if UPLOAD_TOKEN else {}
        _post(f"{API_URL}/swing/candidates/upload", data, headers)
        print(f"  ✅ Candidates re-uploaded with red-team verdicts")
    except Exception as e:
        print(f"  ⚠️  Re-upload failed: {e}")

    # Telegram only when there's something to warn about
    if avoids or cautions:
        lines = ["🛡 <b>Pre-trade Red-team</b>"]
        for c, v in avoids:
            lines.append(f"\n🔴 <b>{c['ticker'].replace('.NS','')}</b> — AVOID "
                         f"(score {c.get('score')})\n{v['summary']}")
            for r in (v.get("reasons") or [])[:3]:
                lines.append(f"  • {r}")
        for c, v in cautions:
            lines.append(f"\n🟠 <b>{c['ticker'].replace('.NS','')}</b> — CAUTION: {v['summary']}")
        _tg("\n".join(lines))
        print(f"  📨 Telegram sent ({len(avoids)} avoid, {len(cautions)} caution)")
    else:
        print("  No AVOID/CAUTION flags — no Telegram.")
    return candidates


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Pre-trade AI red-team for swing candidates")
    ap.add_argument("--dry-run", action="store_true", help="print only")
    args = ap.parse_args()
    main(dry_run=args.dry_run)
