# -*- coding: utf-8 -*-
"""
Monthly Earnings Sentiment — NSE Fundamental Analyzer
======================================================
Two-stage monthly analysis for the Indian monthly screener:

Stage 1 — Sector structural outlook (30-day RSS window)
  Same 20 NSE sectors, same keyword system as swing scanner,
  but wider window and structural signal thresholds.
  Output feeds screener.py's sector gate.

Stage 2 — Ticker earnings quality (company-specific RSS scoring)
  Matches company names in the same RSS feeds, scores
  earnings-specific headlines per ticker.
  + Optional Claude LLM deep-analysis for shortlist tickers
    (requires ANTHROPIC_API_KEY env var).

Output: /data/monthly_earnings_sentiment.json
  sector_signals:  {sector: {signal, score, matches, reason}}
  ticker_signals:  {ticker: {signal, score, confidence, reasoning,
                             revenue_trend, margin_trend}}

POST to /signals/upload → type="monthly_earnings_sentiment"
Also writes sector_signals using swing_news_sentiment format so
screener.py can read it with its existing fetch_sentiment_signals().

Schedule: 0 2 1 * *  (2:00 AM UTC = 7:30 AM IST, 1st of each month)
          Run before screener.py (which runs ~3rd of month).

Usage:
  python monthly_earnings_sentiment.py                     # full scan
  python monthly_earnings_sentiment.py --tickers TCS.NS INFY.NS
  python monthly_earnings_sentiment.py --status            # show latest
  python monthly_earnings_sentiment.py --test              # skip save
"""

import json

# Sent with every API POST; uploads are rejected when the server has
# UPLOAD_TOKEN set and this env var is missing or wrong.
import os as _os_tok
_UPLOAD_AUTH = {"X-Upload-Token": _os_tok.environ["UPLOAD_TOKEN"]} if _os_tok.getenv("UPLOAD_TOKEN") else {}

import os
import re
import time
import argparse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from typing import Optional

import requests

# Reuse sector keyword definitions from swing scanner — no duplication
from swing_news_sentiment import SECTOR_KEYWORDS, SIGNAL_EMOJI

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

IST              = ZoneInfo("Asia/Kolkata")
DATA_DIR         = os.getenv("DATA_DIR", "/data")
API_URL          = os.getenv("API_URL", "https://web-production-50eee.up.railway.app")
ANTHROPIC_API    = "https://api.anthropic.com/v1/messages"
ANTHROPIC_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
LLM_MODEL        = "claude-haiku-4-5-20251001"   # cost-efficient; 1 call/month
LLM_ENABLED      = bool(ANTHROPIC_KEY)

OUTPUT_FILE      = os.path.join(DATA_DIR, "monthly_earnings_sentiment.json")

SCAN_DAYS        = 30    # wider window vs swing scanner's 7 days
MIN_MATCHES      = 3     # slightly stricter — more data, want real signal
MAX_ITEMS        = 80    # per feed
MAX_LLM_TICKERS  = 30   # cap on tickers sent to Claude

HEADLINE_WEIGHT  = 2.0
BODY_WEIGHT      = 1.0
PHRASE_BONUS     = 0.5

# Thresholds — stricter than swing (need stronger evidence for monthly picks)
SECTOR_THRESHOLDS = {
    "positive":      8.0,   # swing uses 5.0
    "mild_positive": 3.0,   # swing uses 2.0
    "cautious":     -3.0,   # swing uses -2.0
    "negative":     -8.0,   # swing uses -5.0
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

# ─────────────────────────────────────────────
# RSS FEEDS — same as swing scanner + earnings-focused additions
# ─────────────────────────────────────────────

RSS_FEEDS = {
    # Economic Times
    "ET Markets":    "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "ET Economy":    "https://economictimes.indiatimes.com/economy/rssfeeds/1373380680.cms",
    "ET Industry":   "https://economictimes.indiatimes.com/industry/rssfeeds/13352306.cms",
    "ET Auto":       "https://economictimes.indiatimes.com/industry/auto/rssfeeds/17820491.cms",
    "ET Energy":     "https://economictimes.indiatimes.com/industry/energy/rssfeeds/13357174.cms",
    "ET Pharma":     "https://economictimes.indiatimes.com/industry/healthcare/biotech/pharmaceuticals/rssfeeds/13358173.cms",
    "ET IT":         "https://economictimes.indiatimes.com/tech/rssfeeds/78570561.cms",
    "ET Companies":  "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms",
    # LiveMint
    "LiveMint Markets":  "https://www.livemint.com/rss/markets",
    "LiveMint Companies":"https://www.livemint.com/rss/companies",
    "LiveMint Economy":  "https://www.livemint.com/rss/economy",
    # Business Standard
    "BS Markets":    "https://www.business-standard.com/rss/markets-106.rss",
    "BS Companies":  "https://www.business-standard.com/rss/companies-101.rss",
    "BS Economy":    "https://www.business-standard.com/rss/economy-policy-102.rss",
    # The Hindu Business Line
    "BusinessLine":  "https://www.thehindubusinessline.com/feeder/default.rss",
    "BL Markets":    "https://www.thehindubusinessline.com/markets/feeder/default.rss",
}

# ─────────────────────────────────────────────
# EARNINGS KEYWORDS — for ticker-level scoring
# ─────────────────────────────────────────────

EARNINGS_POSITIVE = [
    # Estimate beats
    "beats estimates", "beat estimates", "better than expected",
    "above estimates", "above expectations",
    # Profit
    "profit rises", "profit jumps", "profit surges", "profit up",
    "pat rises", "pat jumps", "pat up", "net profit up",
    "eps up", "earnings growth", "earnings beat",
    # Revenue / sales
    "revenue growth", "revenue rises", "revenue up", "revenue jumps",
    "sales growth", "sales up", "sales jump", "sales rise",
    # EBITDA / margins
    "ebitda grows", "ebitda rises", "ebitda up",
    "margin expansion", "margins improve", "margins widen",
    # Guidance
    "raises guidance", "upgrades guidance", "raises outlook",
    # Records
    "record revenue", "record profit", "all-time high",
    "strong quarter", "robust quarter", "healthy numbers",
    # Capital returns
    "dividend declared", "interim dividend", "special dividend",
    "buyback announced", "share buyback",
    # Order / volume
    "order book record", "highest ever", "sequential growth",
    "volume growth", "volumes up", "volumes grew", "market share gain",
    "order inflows", "order wins",
    # Banking / NBFC specific positives
    "npa falls", "npa down", "npa reduced", "asset quality improves",
    "gross npa down", "net npa down",
    "slippages down", "slippages decline", "lower slippages",
    "provisions decline", "lower provisions", "provisions fall",
    "credit cost down", "credit cost falls",
    "pcr rises", "pcr improves", "provision coverage rises",
    "loan growth", "advances growth", "credit growth",
]

EARNINGS_NEGATIVE = [
    # Estimate misses
    "misses estimates", "miss estimates", "below expectations",
    "below estimates", "disappoints",
    # Profit
    "profit falls", "profit declines", "profit drops", "profit down",
    "pat falls", "pat declines", "pat down", "net profit down",
    "eps down", "earnings fall", "earnings decline", "earnings miss",
    # Revenue / sales
    "revenue decline", "revenue falls", "revenue drops",
    "sales decline", "sales fall", "sales drop",
    # EBITDA / margins
    "ebitda falls", "ebitda declines", "ebitda down",
    "margin compression", "margins squeeze", "margins contract",
    # Guidance
    "cuts guidance", "lowers guidance", "revises guidance down",
    "negative outlook", "cautious outlook",
    # Losses
    "loss widened", "loss deepens", "loss reported",
    "disappointing results", "weak quarter", "muted numbers",
    # Operational
    "order cancellation", "capex slashed", "cost overrun",
    "inventory build", "demand slowdown", "volume decline",
    "volumes down", "volumes fell", "demand weak", "demand muted",
    "price cuts", "write-off", "write-down",
    # Management
    "management change", "ceo resign",
    # Banking / NBFC specific negatives
    "npa rises", "npa increased", "npa up", "npa worsens",
    "gross npa rises", "gross npa up", "net npa rises",
    "slippages rise", "slippages increase", "higher slippages", "slippages up",
    "provisions up", "provisions increase", "higher provisions", "provisions rise",
    "credit cost rises", "credit cost up", "credit cost elevated",
    "stressed assets", "stress in loans", "asset quality concern",
    "stressed book", "special mention account",
]

# ─────────────────────────────────────────────
# COMPANY NAME MAP — NSE ticker → search terms for RSS matching
# ─────────────────────────────────────────────
# Curated top-100 Nifty companies; extended dynamically from cache

COMPANY_NAME_MAP: dict[str, list[str]] = {
    "RELIANCE.NS":    ["reliance industries", "reliance jio", "ril"],
    "TCS.NS":         ["tata consultancy", "tcs"],
    "INFY.NS":        ["infosys"],
    "HDFCBANK.NS":    ["hdfc bank"],
    "ICICIBANK.NS":   ["icici bank"],
    "HINDUNILVR.NS":  ["hindustan unilever", "hul"],
    "SBIN.NS":        ["state bank", "sbi"],
    "BAJFINANCE.NS":  ["bajaj finance"],
    "BHARTIARTL.NS":  ["bharti airtel", "airtel"],
    "ITC.NS":         ["itc limited", "itc ltd"],
    "KOTAKBANK.NS":   ["kotak bank", "kotak mahindra bank"],
    "LT.NS":          ["larsen & toubro", "l&t"],
    "HCLTECH.NS":     ["hcl tech", "hcl technologies"],
    "WIPRO.NS":       ["wipro"],
    "AXISBANK.NS":    ["axis bank"],
    "TITAN.NS":       ["titan company", "titan co"],
    "MARUTI.NS":      ["maruti suzuki", "maruti"],
    "SUNPHARMA.NS":   ["sun pharma", "sun pharmaceutical"],
    "ULTRACEMCO.NS":  ["ultratech cement"],
    "BAJAJFINSV.NS":  ["bajaj finserv"],
    "TATAMOTORS.NS":  ["tata motors"],
    "DRREDDY.NS":     ["dr reddy", "dr. reddy"],
    "POWERGRID.NS":   ["power grid", "pgcil"],
    "NTPC.NS":        ["ntpc"],
    "ONGC.NS":        ["ongc", "oil and natural gas"],
    "COALINDIA.NS":   ["coal india"],
    "ADANIPORTS.NS":  ["adani ports"],
    "ADANIENT.NS":    ["adani enterprises"],
    "ADANIGREEN.NS":  ["adani green"],
    "TATASTEEL.NS":   ["tata steel"],
    "JSWSTEEL.NS":    ["jsw steel"],
    "HINDALCO.NS":    ["hindalco"],
    "VEDL.NS":        ["vedanta"],
    "CIPLA.NS":       ["cipla"],
    "LUPIN.NS":       ["lupin"],
    "DIVISLAB.NS":    ["divi's lab", "divis labs", "divi laboratories"],
    "BIOCON.NS":      ["biocon"],
    "AUROPHARMA.NS":  ["aurobindo pharma"],
    "GRASIM.NS":      ["grasim"],
    "TECHM.NS":       ["tech mahindra"],
    "MCDOWELL-N.NS":  ["united spirits", "diageo india"],
    "HAVELLS.NS":     ["havells"],
    "VOLTAS.NS":      ["voltas"],
    "TITAN.NS":       ["titan"],
    "TATACONSUM.NS":  ["tata consumer", "tata consumer products"],
    "NESTLEIND.NS":   ["nestle india"],
    "DABUR.NS":       ["dabur"],
    "MARICO.NS":      ["marico"],
    "GODREJCP.NS":    ["godrej consumer", "gcpl"],
    "BRITANNIA.NS":   ["britannia"],
    "COLPAL.NS":      ["colgate"],
    "PEL.NS":         ["pidilite"],
    "PIDILITIND.NS":  ["pidilite"],
    "BERGEPAINT.NS":  ["berger paints"],
    "ASIANPAINT.NS":  ["asian paints"],
    "DMART.NS":       ["dmart", "avenue supermarts"],
    "TRENT.NS":       ["trent"],
    "NYKAA.NS":       ["nykaa", "fss"],
    "ZOMATO.NS":      ["zomato"],
    "PAYTM.NS":       ["paytm", "one 97"],
    "IRCTC.NS":       ["irctc"],
    "INDIGO.NS":      ["indigo", "interglobe aviation"],
    "PVR.NS":         ["pvr inox", "pvr"],
    "DLF.NS":         ["dlf"],
    "GODREJPROP.NS":  ["godrej properties"],
    "OBEROIRLTY.NS":  ["oberoi realty"],
    "PRESTIGE.NS":    ["prestige estates"],
    "PHOENIXLTD.NS":  ["phoenix mills"],
    "HAL.NS":         ["hal", "hindustan aeronautics"],
    "BEL.NS":         ["bel", "bharat electronics"],
    "BHEL.NS":        ["bhel", "bharat heavy electrical"],
    "ABB.NS":         ["abb india"],
    "SIEMENS.NS":     ["siemens india"],
    "CGPOWER.NS":     ["cg power", "crompton greaves"],
    "TATAPOWER.NS":   ["tata power"],
    "TORNTPOWER.NS":  ["torrent power"],
    "CESC.NS":        ["cesc"],
    "JSW Energy":     ["jsw energy"],
    "PIIND.NS":       ["pi industries"],
    "DEEPAKNI.NS":    ["deepak nitrite"],
    "SRF.NS":         ["srf limited"],
    "NAVINFLUOR.NS":  ["navin fluorine"],
    "AARTIIND.NS":    ["aarti industries"],
    "CONCOR.NS":      ["concor", "container corporation"],
    "BLUEDART.NS":    ["blue dart"],
    "MPHASIS.NS":     ["mphasis"],
    "PERSISTENT.NS":  ["persistent systems"],
    "LTIM.NS":        ["ltimindtree", "lti mindtree"],
    "KPITTECH.NS":    ["kpit technologies"],
    "ZEEL.NS":        ["zee entertainment"],
    "SUNTV.NS":       ["sun tv"],
    "PFC.NS":         ["pfc", "power finance"],
    "RECLTD.NS":      ["rec limited", "rural electrification"],
    "IREDA.NS":       ["ireda"],
    "NHPC.NS":        ["nhpc"],
    "SJVN.NS":        ["sjvn"],
}


def _extend_company_map_from_cache() -> dict[str, list[str]]:
    """Load additional companies from nse_universe cache."""
    try:
        from nse_universe import _load_cache
        df = _load_cache()
        if df is None:
            return {}

        result = {}
        for _, row in df.iterrows():
            ticker = str(row.get("nse_ticker", "")).strip()
            name   = str(row.get("company_name", "")).strip().lower()
            if not ticker or not name or ticker in COMPANY_NAME_MAP:
                continue
            # Only add if company name is distinct enough (avoids noise)
            words = name.split()
            if len(words) >= 2:
                result[ticker] = [name, " ".join(words[:2])]
            elif len(words) == 1 and len(words[0]) >= 6:
                result[ticker] = [name]
        return result
    except Exception:
        return {}


# ─────────────────────────────────────────────
# RSS PARSER  (mirrors swing_news_sentiment.py)
# ─────────────────────────────────────────────

def _parse_rss_date(date_str: str) -> Optional[date]:
    if not date_str:
        return None
    fmts = [
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%d %b %Y",
    ]
    for fmt in fmts:
        try:
            return datetime.strptime(date_str.strip(), fmt).date()
        except ValueError:
            continue
    clean = re.sub(r'\s+[+-]\d{4}$', '', date_str.strip())
    for fmt in fmts:
        try:
            return datetime.strptime(clean, fmt).date()
        except ValueError:
            continue
    return None


def fetch_rss_feed(source_name: str, url: str) -> list[dict]:
    items  = []
    cutoff = date.today() - timedelta(days=SCAN_DAYS)
    ns     = {"atom": "http://www.w3.org/2005/Atom"}

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()

        xml_str = resp.content.decode("utf-8", errors="replace")
        xml_str = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", xml_str)

        try:
            root = ET.fromstring(xml_str.encode("utf-8"))
        except ET.ParseError:
            xml_str = "".join(c for c in xml_str if ord(c) >= 0x20 or c in "\t\n\r")
            root = ET.fromstring(xml_str.encode("utf-8"))

        entries = root.findall(".//item")
        if not entries:
            entries = root.findall(".//atom:entry", ns) or root.findall(".//entry")

        for entry in entries[:MAX_ITEMS]:
            title_el = entry.find("title")
            if title_el is None:
                title_el = entry.find("atom:title", ns)
            title = re.sub(r'<[^>]+>', '', (title_el.text or "") if title_el is not None else "").strip()
            if not title or len(title) < 10:
                continue

            desc_el = entry.find("description")
            if desc_el is None: desc_el = entry.find("atom:summary", ns)
            if desc_el is None: desc_el = entry.find("summary")
            desc = re.sub(r'<[^>]+>', '', (desc_el.text or "") if desc_el is not None else "").strip()[:400]

            date_el = entry.find("pubDate")
            if date_el is None: date_el = entry.find("published")
            if date_el is None: date_el = entry.find("atom:published", ns)
            if date_el is None: date_el = entry.find("updated")
            pub_date = _parse_rss_date(date_el.text) if date_el is not None and date_el.text else None

            if pub_date is not None and pub_date < cutoff:
                continue

            items.append({
                "title":  title,
                "body":   desc,
                "date":   str(pub_date or date.today()),
                "source": source_name,
            })

        print(f"    📰 {source_name:<26}: {len(items)} items")

    except ET.ParseError as e:
        print(f"    ⚠️  {source_name}: XML error — {e}")
    except Exception as e:
        print(f"    ⚠️  {source_name}: fetch failed — {e}")

    return items


def fetch_all_feeds() -> list[dict]:
    all_items = []
    for source, url in RSS_FEEDS.items():
        all_items.extend(fetch_rss_feed(source, url))
        time.sleep(0.5)
    return all_items


# ─────────────────────────────────────────────
# STAGE 1 — SECTOR STRUCTURAL SCORING
# ─────────────────────────────────────────────

def score_headline(title: str, body: str, sector: str) -> float:
    """Score a headline against a sector's keyword lists."""
    kw         = SECTOR_KEYWORDS.get(sector, {})
    pos        = kw.get("positive", [])
    neg        = kw.get("negative", [])
    text_title = title.lower()
    text_body  = body.lower()
    score      = 0.0

    for phrase in pos:
        w = HEADLINE_WEIGHT + (PHRASE_BONUS if " " in phrase else 0)
        if phrase in text_title:
            score += w
        elif phrase in text_body:
            score += BODY_WEIGHT

    for phrase in neg:
        w = HEADLINE_WEIGHT + (PHRASE_BONUS if " " in phrase else 0)
        if phrase in text_title:
            score -= w
        elif phrase in text_body:
            score -= BODY_WEIGHT

    return score


def aggregate_sector_sentiment(items: list[dict]) -> dict:
    """
    Score all items against all 20 sectors.
    Uses stricter thresholds than swing scanner (longer-term picks).
    Returns {sector: {score, signal, matches, reason}}.
    """
    totals  = {s: 0.0 for s in SECTOR_KEYWORDS}
    counts  = {s: 0   for s in SECTOR_KEYWORDS}
    reasons = {s: []  for s in SECTOR_KEYWORDS}

    for item in items:
        for sector in SECTOR_KEYWORDS:
            s = score_headline(item["title"], item.get("body", ""), sector)
            if abs(s) > 0:
                totals[sector] += s
                counts[sector] += 1
                if abs(s) >= HEADLINE_WEIGHT:
                    direction = "↑" if s > 0 else "↓"
                    reasons[sector].append(
                        f"{direction} [{item['source']}] {item['title'][:70]}"
                    )

    result = {}
    for sector in SECTOR_KEYWORDS:
        total   = round(totals[sector], 2)
        matches = counts[sector]

        if matches < MIN_MATCHES:
            signal = "neutral"
            reason = f"Only {matches} match(es) — below minimum {MIN_MATCHES}."
        else:
            t = SECTOR_THRESHOLDS
            if   total >= t["positive"]:      signal = "positive"
            elif total >= t["mild_positive"]: signal = "mild_positive"
            elif total <= t["negative"]:      signal = "negative"
            elif total <= t["cautious"]:      signal = "cautious"
            else:                             signal = "neutral"
            top    = reasons[sector][:3]
            reason = "; ".join(top) if top else "Matched — no strong headline signals."

        result[sector] = {
            "score":   total,
            "signal":  signal,
            "matches": matches,
            "reason":  reason,
        }

    return result


# ─────────────────────────────────────────────
# STAGE 2A — TICKER EARNINGS RSS SCORING
# ─────────────────────────────────────────────

def _score_earnings_headline(title: str, body: str) -> float:
    """Score a headline for earnings quality (positive = good results)."""
    text_title = title.lower()
    text_body  = body.lower()
    score      = 0.0

    for phrase in EARNINGS_POSITIVE:
        w = HEADLINE_WEIGHT + (PHRASE_BONUS if " " in phrase else 0)
        if phrase in text_title:
            score += w
        elif phrase in text_body:
            score += BODY_WEIGHT

    for phrase in EARNINGS_NEGATIVE:
        w = HEADLINE_WEIGHT + (PHRASE_BONUS if " " in phrase else 0)
        if phrase in text_title:
            score -= w
        elif phrase in text_body:
            score -= BODY_WEIGHT

    return score


def score_tickers_from_rss(
    items: list[dict],
    company_map: dict[str, list[str]],
    target_tickers: Optional[list[str]] = None,
) -> dict[str, dict]:
    """
    Match headlines to company names; score earnings quality per ticker.

    company_map: {ticker: [search_term_1, search_term_2, ...]}
    target_tickers: if given, only score these tickers
    Returns {ticker: {rss_score, matches, signal, top_headlines}}
    """
    scope  = set(target_tickers) if target_tickers else set(company_map.keys())
    totals = {t: 0.0 for t in scope}
    counts = {t: 0   for t in scope}
    heads  = {t: []  for t in scope}

    for item in items:
        title_lower = item["title"].lower()
        body_lower  = item.get("body", "").lower()
        combined    = title_lower + " " + body_lower

        for ticker in scope:
            terms = company_map.get(ticker, [])
            if not any(term in combined for term in terms):
                continue

            escore = _score_earnings_headline(item["title"], item.get("body", ""))
            if abs(escore) > 0:
                totals[ticker] += escore
                counts[ticker] += 1
                if abs(escore) >= HEADLINE_WEIGHT and len(heads[ticker]) < 5:
                    heads[ticker].append(item["title"][:100])

    result = {}
    for ticker in scope:
        total   = round(totals[ticker], 2)
        matches = counts[ticker]

        if matches == 0:
            signal = "neutral"
        elif total >= 4.0:
            signal = "positive"
        elif total >= 1.5:
            signal = "mild_positive"
        elif total <= -4.0:
            signal = "negative"
        elif total <= -1.5:
            signal = "cautious"
        else:
            signal = "neutral"

        result[ticker] = {
            "rss_score":      total,
            "rss_matches":    matches,
            "signal":         signal,
            "top_headlines":  heads[ticker],
            # LLM fields filled in Stage 2B; defaults here
            "confidence":     0.5 if matches >= 2 else 0.2,
            "reasoning":      f"RSS-only: {matches} earnings headline(s) found." if matches else "No earnings news found in 30-day window.",
            "revenue_trend":  "unknown",
            "margin_trend":   "unknown",
        }

    return result


# ─────────────────────────────────────────────
# STAGE 2B — LLM EARNINGS ANALYSIS (Claude)
# ─────────────────────────────────────────────

def _build_llm_prompt(ticker_headlines: dict[str, dict]) -> str:
    """Build batch prompt for Claude: all tickers in one call."""
    lines = [
        "You are a fundamental equity analyst assessing earnings quality for NSE-listed stocks.",
        "Analyse each company's recent earnings news (last 30 days) for a 3–6 month investment horizon.",
        "",
        "For each company return a JSON object in the array below.",
        "Signal scale: positive > mild_positive > neutral > cautious > negative",
        "Only give 'positive' if multiple strong signals exist. Default to 'neutral' when uncertain.",
        "",
        "COMPANIES:",
    ]

    for i, (ticker, info) in enumerate(ticker_headlines.items(), 1):
        company = ticker.replace(".NS", "")
        heads   = info.get("top_headlines", [])
        rss_sig = info.get("signal", "neutral")

        lines.append(f"\n{i}. {ticker}")
        lines.append(f"   RSS signal (keyword-based): {rss_sig}")
        if heads:
            lines.append("   Recent earnings headlines:")
            for h in heads[:8]:
                lines.append(f"   - {h}")
        else:
            lines.append("   (no specific earnings headlines found in RSS feeds)")

    lines += [
        "",
        "Return ONLY a JSON array, no other text:",
        '[',
        '  {"ticker":"XXX.NS","signal":"neutral","confidence":0.5,',
        '   "revenue_trend":"unknown","margin_trend":"unknown",',
        '   "reasoning":"2-3 sentences max."},',
        '  ...',
        ']',
    ]
    return "\n".join(lines)


def llm_analyze_tickers(ticker_scores: dict[str, dict]) -> dict[str, dict]:
    """
    Enhance RSS scores with Claude LLM analysis.
    Only called when ANTHROPIC_API_KEY is set.
    Processes all tickers in a single API call.
    """
    if not LLM_ENABLED:
        print("  ℹ️  LLM disabled (no ANTHROPIC_API_KEY) — using RSS scores only")
        return ticker_scores

    # Only send tickers with at least some headlines (or notable RSS signal)
    candidates = {
        t: v for t, v in ticker_scores.items()
        if v.get("rss_matches", 0) > 0 or v.get("signal") != "neutral"
    }
    if not candidates:
        print("  ℹ️  No candidates for LLM analysis (all neutral with 0 headlines)")
        return ticker_scores

    # Cap at MAX_LLM_TICKERS to control cost
    if len(candidates) > MAX_LLM_TICKERS:
        # Prioritise tickers with strong RSS signal (non-neutral first, then by |score|)
        candidates = dict(
            sorted(candidates.items(),
                   key=lambda x: (x[1]["signal"] == "neutral", -abs(x[1]["rss_score"])))
            [:MAX_LLM_TICKERS]
        )

    print(f"  🤖 LLM analyzing {len(candidates)} tickers via Claude ({LLM_MODEL})...")
    prompt = _build_llm_prompt(candidates)

    try:
        resp = requests.post(
            ANTHROPIC_API,
            headers={
                "x-api-key":         ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      LLM_MODEL,
                "max_tokens": 2048,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        resp.raise_for_status()
        raw_text = resp.json()["content"][0]["text"].strip()

        # Extract JSON array from response (handle markdown fences)
        json_match = re.search(r'\[[\s\S]*\]', raw_text)
        if not json_match:
            raise ValueError("No JSON array found in LLM response")

        assessments = json.loads(json_match.group(0))

        # Merge LLM results back into ticker_scores
        llm_by_ticker = {a["ticker"]: a for a in assessments if "ticker" in a}
        updated = dict(ticker_scores)

        for ticker, llm_data in llm_by_ticker.items():
            if ticker not in updated:
                continue
            # Blend: LLM signal takes precedence if confidence >= 0.6;
            # otherwise average RSS signal and LLM signal
            llm_conf = float(llm_data.get("confidence", 0.5))
            rss_sig  = updated[ticker]["signal"]
            llm_sig  = llm_data.get("signal", rss_sig)

            if llm_conf >= 0.6:
                final_signal = llm_sig
            else:
                # Blend: keep RSS if they disagree and LLM is uncertain
                SIGNAL_ORDER = ["negative", "cautious", "neutral", "mild_positive", "positive"]
                rss_rank = SIGNAL_ORDER.index(rss_sig) if rss_sig in SIGNAL_ORDER else 2
                llm_rank = SIGNAL_ORDER.index(llm_sig) if llm_sig in SIGNAL_ORDER else 2
                blended_rank = round((rss_rank + llm_rank) / 2)
                final_signal = SIGNAL_ORDER[blended_rank]

            updated[ticker] = {
                **updated[ticker],
                "signal":        final_signal,
                "confidence":    llm_conf,
                "reasoning":     llm_data.get("reasoning", ""),
                "revenue_trend": llm_data.get("revenue_trend", "unknown"),
                "margin_trend":  llm_data.get("margin_trend", "unknown"),
                "llm_applied":   True,
            }

        n_updated = len(llm_by_ticker)
        print(f"  ✅ LLM updated {n_updated} ticker assessments")
        return updated

    except Exception as e:
        print(f"  ⚠️  LLM analysis failed (non-fatal — using RSS scores): {e}")
        return ticker_scores


# ─────────────────────────────────────────────
# SAVE + POST
# ─────────────────────────────────────────────

def save_signals(sector_signals: dict, ticker_signals: dict, items: list):
    import urllib.request as _ur

    os.makedirs(DATA_DIR, exist_ok=True)

    output = {
        "generated_at":    datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"),
        "scan_date":       str(date.today()),
        "scan_days":       SCAN_DAYS,
        "total_headlines": len(items),
        "sector_signals":  sector_signals,
        "ticker_signals":  ticker_signals,
        "sample_headlines": [
            {"title": i["title"], "date": i["date"], "source": i["source"]}
            for i in items[:15]
        ],
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n  ✅ Signals saved: {OUTPUT_FILE}")

    try:
        payload = json.dumps(
            {"type": "monthly_earnings_sentiment", "payload": output},
            default=str
        ).encode("utf-8")
        req = _ur.Request(
            f"{API_URL}/signals/upload",
            data=payload,
            headers={"Content-Type": "application/json", **_UPLOAD_AUTH},
            method="POST"
        )
        with _ur.urlopen(req, timeout=12) as r:
            print(f"  ✅ Signals POSTed to API: {r.read().decode()}")
    except Exception as e:
        print(f"  ⚠️  POST failed (non-fatal): {e}")


# ─────────────────────────────────────────────
# MAIN SCAN
# ─────────────────────────────────────────────

def run_scan(target_tickers: Optional[list[str]] = None, test_mode: bool = False):
    print(f"\n{'='*60}")
    print(f"  📊 MONTHLY EARNINGS SENTIMENT — NSE FUNDAMENTALS")
    print(f"  {datetime.now(IST).strftime('%d %B %Y, %I:%M %p IST')}")
    print(f"  Window: {SCAN_DAYS} days | LLM: {'enabled' if LLM_ENABLED else 'disabled'}")
    print(f"{'='*60}\n")

    # Build company map (curated + cache extension)
    print("  Building company name map...")
    company_map = {**COMPANY_NAME_MAP, **_extend_company_map_from_cache()}
    print(f"  {len(company_map)} companies mapped\n")

    # Fetch RSS
    print("  Fetching RSS feeds...\n")
    items = fetch_all_feeds()
    print(f"\n  Total headlines: {len(items)}")

    if not items:
        print("  ⚠️  No headlines — check RSS connectivity.")
        return None, None

    # ── Stage 1: Sector structural scoring ───────────────────
    print(f"\n  Stage 1: Scoring {len(items)} headlines across 20 sectors...")
    sector_signals = aggregate_sector_sentiment(items)

    print(f"\n  {'SECTOR':<35} {'SIGNAL':<14} {'SCORE':>6}  {'MATCHES':>7}")
    print(f"  {'─'*35} {'─'*14} {'─'*6}  {'─'*7}")
    for sector, sig in sorted(sector_signals.items()):
        emoji = SIGNAL_EMOJI.get(sig["signal"], "⚪")
        label = sig["signal"].replace("_", " ").title()
        print(f"  {sector:<35} {emoji} {label:<12} {sig['score']:>+6.1f}  {sig['matches']:>7}")

    # ── Stage 2A: Ticker earnings RSS scoring ─────────────────
    scope = target_tickers if target_tickers else list(company_map.keys())
    print(f"\n  Stage 2A: Ticker earnings scoring ({len(scope)} companies)...")
    ticker_signals = score_tickers_from_rss(items, company_map, scope)

    n_nonneut = sum(1 for v in ticker_signals.values() if v["signal"] != "neutral")
    print(f"  {n_nonneut} tickers with non-neutral earnings signal")

    # Print non-neutral tickers
    if n_nonneut:
        print(f"\n  {'TICKER':<20} {'SIGNAL':<14} {'RSS SCORE':>9}  HEADLINES")
        print(f"  {'─'*20} {'─'*14} {'─'*9}  {'─'*30}")
        for ticker, sig in sorted(
            ticker_signals.items(),
            key=lambda x: -abs(x[1]["rss_score"])
        ):
            if sig["signal"] == "neutral":
                continue
            emoji = SIGNAL_EMOJI.get(sig["signal"], "⚪")
            label = sig["signal"].replace("_", " ").title()
            sample = sig["top_headlines"][0][:50] if sig["top_headlines"] else "—"
            print(f"  {ticker:<20} {emoji} {label:<12} {sig['rss_score']:>+8.1f}  {sample}")

    # ── Stage 2B: LLM enhancement ─────────────────────────────
    if LLM_ENABLED:
        print(f"\n  Stage 2B: LLM earnings analysis...")
        ticker_signals = llm_analyze_tickers(ticker_signals)
    else:
        print(f"\n  Stage 2B: LLM skipped (set ANTHROPIC_API_KEY to enable)")

    # Summary of non-neutral signals
    print(f"\n  TOP EARNINGS SIGNALS (non-neutral):")
    sorted_tickers = sorted(
        ((t, v) for t, v in ticker_signals.items() if v["signal"] != "neutral"),
        key=lambda x: -abs(x[1]["rss_score"])
    )
    for ticker, sig in sorted_tickers[:15]:
        emoji = SIGNAL_EMOJI.get(sig["signal"], "⚪")
        conf  = f"({sig['confidence']:.0%})" if sig.get("llm_applied") else "(RSS)"
        print(f"  {emoji} {ticker:<20} {sig['signal']:<14} {conf}")
        if sig.get("reasoning"):
            print(f"      {sig['reasoning'][:80]}")

    if not test_mode:
        save_signals(sector_signals, ticker_signals, items)

    return sector_signals, ticker_signals


# ─────────────────────────────────────────────
# STATUS
# ─────────────────────────────────────────────

def show_status():
    if not os.path.exists(OUTPUT_FILE):
        print("  No monthly earnings data. Run: python monthly_earnings_sentiment.py")
        return

    with open(OUTPUT_FILE) as f:
        data = json.load(f)

    print(f"\n{'='*60}")
    print(f"  📊 MONTHLY EARNINGS SENTIMENT")
    print(f"  Generated : {data.get('generated_at')}")
    print(f"  Headlines : {data.get('total_headlines', 0)}")
    print(f"{'='*60}")

    print(f"\n  SECTOR SIGNALS:")
    print(f"  {'─'*35} {'─'*14} {'─'*6}")
    for sector, sig in sorted(data.get("sector_signals", {}).items()):
        emoji = SIGNAL_EMOJI.get(sig["signal"], "⚪")
        label = sig["signal"].replace("_", " ").title()
        print(f"  {sector:<35} {emoji} {label:<12} {sig['score']:>+6.1f}")

    ticker_sigs = data.get("ticker_signals", {})
    nonneut = [(t, v) for t, v in ticker_sigs.items() if v["signal"] != "neutral"]
    if nonneut:
        print(f"\n  TICKER SIGNALS ({len(nonneut)} non-neutral):")
        for ticker, sig in sorted(nonneut, key=lambda x: -abs(x[1]["rss_score"])):
            emoji = SIGNAL_EMOJI.get(sig["signal"], "⚪")
            label = sig["signal"].replace("_", " ").title()
            conf  = f" ({sig['confidence']:.0%})" if sig.get("llm_applied") else ""
            print(f"  {emoji} {ticker:<20} {label}{conf}")
            if sig.get("reasoning") and sig.get("llm_applied"):
                print(f"      {sig['reasoning'][:80]}")

    print(f"{'='*60}\n")


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Monthly Earnings Sentiment — NSE Fundamental Analyzer"
    )
    parser.add_argument("--status",   action="store_true", help="Show latest signals")
    parser.add_argument("--test",     action="store_true", help="Run without saving")
    parser.add_argument(
        "--tickers", nargs="+", metavar="TICKER",
        help="Restrict ticker analysis to specific tickers (e.g. TCS.NS INFY.NS)"
    )
    args = parser.parse_args()

    if args.status:
        show_status()
    else:
        run_scan(target_tickers=args.tickers, test_mode=args.test)
