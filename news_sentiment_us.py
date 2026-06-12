"""
News Sentiment US — Financial RSS Feed Analyser
=================================================
Exact mirror of news_sentiment.py for the US market.

Fetches headlines from US financial RSS feeds,
classifies them using the same keyword engine structure,
and saves sentiment signals to us_news_signals.json.

Scheduled on Railway: every weekday at 8:00 AM IST

Key differences from India version:
  - US RSS feeds (Reuters, CNBC, WSJ, Bloomberg, Yahoo Finance)
  - US bucket keywords (Fed, semiconductors, AI, pharma/healthcare)
  - Same 2-day window, same MIN_MATCHES threshold
  - Same scoring weights and signal thresholds

Usage:
    python news_sentiment_us.py           # run scan
    python news_sentiment_us.py --status  # show current signals
    python news_sentiment_us.py --test    # scan without saving
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

import requests

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

IST               = ZoneInfo("Asia/Kolkata")
NEWS_SIGNALS_FILE = os.path.join(os.getenv("DATA_DIR", "/data"), "us_news_signals.json")
SCAN_DAYS         = 2
MIN_MATCHES       = 2
MAX_ITEMS         = 50

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

# US RSS Feeds (equiv. ET Markets / LiveMint for India)
RSS_FEEDS = {
    "Reuters Business":     "https://feeds.reuters.com/reuters/businessNews",
    "Reuters Technology":   "https://feeds.reuters.com/reuters/technologyNews",
    "CNBC Top News":        "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
    "CNBC Technology":      "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=19854910",
    "Yahoo Finance":        "https://finance.yahoo.com/news/rssindex",
}

# Smaller multipliers than policy — news is noisier (identical to India)
NEWS_MULTIPLIERS = {
    "positive":     1.04,
    "mild_positive":1.02,
    "neutral":      1.00,
    "cautious":     0.98,
    "negative":     0.96,
}

# Scoring weights (identical to India policy_scraper.py)
HEADLINE_WEIGHT = 2.0
BODY_WEIGHT     = 1.0
PHRASE_BONUS    = 0.5

# ─────────────────────────────────────────────
# US BUCKET KEYWORDS
# ─────────────────────────────────────────────
# Exact same structure as BUCKET_KEYWORDS in policy_scraper.py
# Just US-market-specific terms replacing India-specific terms

BUCKET_KEYWORDS = {
    "TECH": {
        "positive": [
            # AI + Cloud
            "ai investment", "artificial intelligence", "generative ai",
            "chatgpt", "llm", "large language model", "ai data center",
            "cloud revenue", "azure growth", "aws revenue", "google cloud",
            "ai chip", "nvidia revenue", "ai demand", "ai adoption",
            "microsoft earnings", "alphabet earnings", "meta revenue",
            "cloud spending", "ai capex", "hyperscaler",
            "ai breakthrough", "model release", "foundation model",
            "data center build", "gpu demand",
            # Semiconductors + chips + memory
            "chip demand", "semiconductor demand", "chip shortage easing",
            "nvidia earnings", "amd revenue", "tsm revenue",
            "broadcom earnings", "qualcomm revenue", "micron earnings",
            "chip act", "chips act", "semiconductor subsidies",
            "fab investment", "chipmaker capex", "hbm demand",
            "semiconductor cycle", "upcycle", "chip recovery",
            "memory demand", "dram prices", "nand recovery",
            "quantum computing", "quantum chip", "quantum breakthrough",
            # Growth tech
            "consumer spending", "e-commerce growth", "software revenue",
            "saas growth", "arr growth", "digital ad revenue",
            "rate cut", "fed rate cut", "rates lower", "dovish fed",
            "tech rally", "nasdaq gains",
        ],
        "negative": [
            "ai regulation", "ai ban", "ai restriction",
            "cloud slowdown", "cloud miss", "aws slowdown",
            "microsoft miss", "alphabet miss", "meta miss",
            "chip glut", "semiconductor oversupply", "chip inventory",
            "wafer price falls", "foundry underutilization",
            "china chip ban", "chip export controls", "entity list",
            "tsmc miss", "nvidia miss", "amd miss",
            "semiconductor downturn", "chip cycle down",
            "rate hike", "fed rate hike", "hawkish fed",
            "tech selloff", "nasdaq selloff", "tech crash",
            "antitrust tech", "big tech regulation",
            "consumer slowdown", "saas churn", "software spending cut",
        ],
        "neutral_override": ["ai ethics", "ai safety research", "chip war diplomacy"],
    },

    "DEFENSIVE_DIV": {
        "positive": [
            "healthcare spending", "drug approval", "fda approval",
            "pharma earnings", "johnson johnson", "unitedhealth",
            "dividend increase", "buyback", "share repurchase",
            "consumer staples", "walmart earnings", "costco earnings",
            "utility earnings", "bank earnings", "jpmorgan earnings",
            "safe haven", "defensive rotation", "risk off",
            "bond yield falls", "rates lower defensive",
            "recession hedge", "defensive outperform",
        ],
        "negative": [
            "drug price cap", "drug pricing reform", "ira drug pricing",
            "fda warning letter", "drug recall", "pharma penalty",
            "food inflation", "commodity inflation",
            "bank failure", "bank stress", "credit crunch",
            "dividend cut", "buyback suspended",
            "consumer staples miss", "walmart miss",
        ],
        "neutral_override": ["drug patent expiry"],
    },
}


# ─────────────────────────────────────────────
# KEYWORD SCORER
# ─────────────────────────────────────────────
# Identical logic to score_release() in policy_scraper.py

def score_release(title: str, body: str = "") -> dict:
    """
    Score a news item across all 4 US buckets.
    Identical logic to score_release() in policy_scraper.py.
    """
    title_lower = title.lower()
    body_lower  = body.lower()
    scores      = {k: 0.0 for k in BUCKET_KEYWORDS}

    for bucket, keywords in BUCKET_KEYWORDS.items():
        bucket_score = 0.0

        # Positive keywords
        for kw in keywords.get("positive", []):
            kw_lower = kw.lower()
            word_bonus = PHRASE_BONUS if len(kw.split()) > 1 else 0.0
            if kw_lower in title_lower:
                bucket_score += HEADLINE_WEIGHT + word_bonus
            elif kw_lower in body_lower:
                bucket_score += BODY_WEIGHT + word_bonus

        # Negative keywords
        for kw in keywords.get("negative", []):
            kw_lower = kw.lower()
            word_bonus = PHRASE_BONUS if len(kw.split()) > 1 else 0.0
            if kw_lower in title_lower:
                bucket_score -= HEADLINE_WEIGHT + word_bonus
            elif kw_lower in body_lower:
                bucket_score -= BODY_WEIGHT + word_bonus

        # Neutral overrides — zero out if these phrases present
        for kw in keywords.get("neutral_override", []):
            if kw.lower() in title_lower or kw.lower() in body_lower:
                bucket_score = 0.0
                break

        scores[bucket] = round(bucket_score, 2)

    return scores


# ─────────────────────────────────────────────
# RSS PARSER (identical to news_sentiment.py)
# ─────────────────────────────────────────────

def _parse_rss_date(date_str: str) -> date | None:
    if not date_str:
        return None
    formats = [
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%d %b %Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str.strip(), fmt).date()
        except ValueError:
            continue
    clean = re.sub(r'\s+[+-]\d{4}$', '', date_str.strip())
    for fmt in formats:
        try:
            return datetime.strptime(clean, fmt).date()
        except ValueError:
            continue
    return None


def fetch_rss_feed(source_name: str, url: str) -> list:
    """
    Fetch and parse a single RSS feed.
    Identical to fetch_rss_feed() in news_sentiment.py.
    """
    items  = []
    cutoff = date.today() - timedelta(days=SCAN_DAYS)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()

        try:
            xml_str = resp.content.decode("utf-8", errors="replace")
        except Exception:
            xml_str = resp.text
        xml_str = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", xml_str)
        try:
            root = ET.fromstring(xml_str.encode("utf-8"))
        except ET.ParseError:
            xml_str = "".join(c for c in xml_str if ord(c) >= 0x20 or c in "\t\n\r")
            root = ET.fromstring(xml_str.encode("utf-8"))

        ns = {
            "atom":    "http://www.w3.org/2005/Atom",
            "content": "http://purl.org/rss/1.0/modules/content/",
        }

        entries = root.findall(".//item")
        if not entries:
            entries = root.findall(".//atom:entry", ns) or root.findall(".//entry")

        for entry in entries[:MAX_ITEMS]:
            title_el = entry.find("title")
            if title_el is None:
                title_el = entry.find("atom:title", ns)
            title = (title_el.text or "").strip() if title_el is not None else ""
            title = re.sub(r'<[^>]+>', '', title).strip()
            if not title or len(title) < 10:
                continue

            desc_el = entry.find("description")
            if desc_el is None:
                desc_el = entry.find("atom:summary", ns)
            if desc_el is None:
                desc_el = entry.find("summary")
            desc = (desc_el.text or "").strip() if desc_el is not None else ""
            desc = re.sub(r'<[^>]+>', '', desc).strip()[:300]

            date_el = entry.find("pubDate")
            if date_el is None:
                date_el = entry.find("published")
            if date_el is None:
                date_el = entry.find("atom:published", ns)
            if date_el is None:
                date_el = entry.find("updated")
            pub_date = None
            if date_el is not None and date_el.text:
                pub_date = _parse_rss_date(date_el.text)

            if pub_date and pub_date < cutoff:
                continue

            items.append({
                "title":  title,
                "body":   desc,
                "date":   str(pub_date or date.today()),
                "source": source_name,
            })

        print(f"    📰 {source_name}: {len(items)} items (last {SCAN_DAYS} days)")

    except ET.ParseError as e:
        print(f"    ⚠️  {source_name}: XML parse error — {e}")
    except Exception as e:
        print(f"    ⚠️  {source_name}: fetch failed — {e}")

    return items


def fetch_all_feeds() -> list:
    """Identical to fetch_all_feeds() in news_sentiment.py."""
    all_items = []
    for source, url in RSS_FEEDS.items():
        items = fetch_rss_feed(source, url)
        all_items.extend(items)
        time.sleep(0.5)
    return all_items


# ─────────────────────────────────────────────
# SENTIMENT AGGREGATOR (identical to news_sentiment.py)
# ─────────────────────────────────────────────

def aggregate_news_sentiment(items: list) -> dict:
    """
    Identical to aggregate_news_sentiment() in news_sentiment.py.
    Same MIN_MATCHES threshold, same signal thresholds.
    """
    bucket_totals  = {k: 0.0 for k in BUCKET_KEYWORDS}
    bucket_reasons = {k: [] for k in BUCKET_KEYWORDS}
    bucket_counts  = {k: 0   for k in BUCKET_KEYWORDS}

    for item in items:
        scores = score_release(item["title"], item.get("body", ""))
        for bucket, score in scores.items():
            if abs(score) > 0:
                bucket_totals[bucket]  += score
                bucket_counts[bucket]  += 1
                if abs(score) >= HEADLINE_WEIGHT:
                    direction = "↑" if score > 0 else "↓"
                    src       = item.get("source", "")
                    bucket_reasons[bucket].append(
                        f"{direction} [{src}] {item['title'][:70]}"
                    )

    result = {}
    for bucket in BUCKET_KEYWORDS:
        total   = round(bucket_totals[bucket], 2)
        matches = bucket_counts[bucket]

        if matches < MIN_MATCHES:
            signal = "neutral"
            reason = (
                f"Only {matches} headline match(es) — "
                f"below minimum {MIN_MATCHES} threshold. No adjustment."
            )
        else:
            if total >= 5.0:
                signal = "positive"
            elif total >= 2.0:
                signal = "mild_positive"
            elif total <= -5.0:
                signal = "negative"
            elif total <= -2.0:
                signal = "cautious"
            else:
                signal = "neutral"

            top    = bucket_reasons[bucket][:3]
            reason = "; ".join(top) if top else "Matched but no headline-level signals."

        result[bucket] = {
            "score":   total,
            "signal":  signal,
            "reason":  reason,
            "matches": matches,
        }

    return result


# ─────────────────────────────────────────────
# SIGNAL PERSISTENCE (identical to news_sentiment.py)
# ─────────────────────────────────────────────

def save_news_signals(signals: dict, items: list):
    output = {
        "generated_at":    datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"),
        "scan_days":       SCAN_DAYS,
        "total_headlines": len(items),
        "min_matches":     MIN_MATCHES,
        "signals":         signals,
        "sample_headlines": [
            {"title": i["title"], "date": i["date"], "source": i["source"]}
            for i in items[:15]
        ],
    }
    with open(NEWS_SIGNALS_FILE, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n  ✅ US News signals saved to {NEWS_SIGNALS_FILE}")
    _post_signal_to_api("us_news_signals", output)


def _post_signal_to_api(signal_type: str, payload: dict):
    """POST to /signals/upload — identical to India version."""
    import urllib.request as _urllib
    api_url = os.getenv("API_URL", "https://web-production-50eee.up.railway.app")
    url = f"{api_url}/signals/upload"
    try:
        body = json.dumps({"type": signal_type, "payload": payload}).encode("utf-8")
        req = _urllib.Request(url, data=body,
                              headers={"Content-Type": "application/json", **_UPLOAD_AUTH},
                              method="POST")
        with _urllib.urlopen(req, timeout=10) as r:
            print(f"  ✅ {signal_type} POSTed to API: {r.read().decode()}")
    except Exception as e:
        print(f"  ⚠️  Could not POST {signal_type} to API (non-fatal): {e}")


def load_news_signals() -> dict | None:
    if not os.path.exists(NEWS_SIGNALS_FILE):
        return None
    try:
        with open(NEWS_SIGNALS_FILE) as f:
            return json.load(f)
    except Exception:
        return None


# ─────────────────────────────────────────────
# MAIN SCAN (identical to news_sentiment.py)
# ─────────────────────────────────────────────

def run_news_scan(test_mode: bool = False):
    """Identical structure to run_news_scan() in news_sentiment.py."""
    print(f"\n{'='*55}")
    print(f"  📰 US NEWS SENTIMENT — RSS SCAN")
    print(f"  {datetime.now(IST).strftime('%d %B %Y, %I:%M %p IST')}")
    print(f"  Window: last {SCAN_DAYS} days | Min matches: {MIN_MATCHES}")
    print(f"{'='*55}\n")

    items = fetch_all_feeds()
    print(f"\n  Total headlines: {len(items)}")

    if not items:
        print("  ⚠️  No headlines fetched — check RSS connectivity.")
        return None

    print(f"\n  🔍 Classifying {len(items)} headlines...")
    signals = aggregate_news_sentiment(items)

    signal_emoji = {
        "positive":     "🟢",
        "mild_positive":"🟡",
        "neutral":      "⚪",
        "cautious":     "🟠",
        "negative":     "🔴",
    }
    print(f"\n  📊 US NEWS SENTIMENT SIGNALS:")
    for bucket, sig in signals.items():
        emoji = signal_emoji.get(sig["signal"], "⚪")
        print(f"\n  {bucket}")
        print(f"    Signal:  {emoji} {sig['signal'].replace('_',' ').title()}  "
              f"(score: {sig['score']:+.1f}, {sig['matches']} matches)")
        if sig["matches"] >= MIN_MATCHES and ("↑" in sig["reason"] or "↓" in sig["reason"]):
            for line in sig["reason"].split(";")[:2]:
                if line.strip():
                    print(f"    {line.strip()[:80]}")

    if not test_mode:
        save_news_signals(signals, items)

    return signals


# ─────────────────────────────────────────────
# STATUS (identical to news_sentiment.py)
# ─────────────────────────────────────────────

def show_status():
    data = load_news_signals()
    if not data:
        print("  No US news signals. Run: python news_sentiment_us.py")
        return

    signal_emoji = {
        "positive":"🟢","mild_positive":"🟡",
        "neutral":"⚪","cautious":"🟠","negative":"🔴",
    }
    print(f"\n{'='*55}")
    print(f"  📰 CURRENT US NEWS SENTIMENT")
    print(f"  Generated: {data.get('generated_at')}")
    print(f"  Headlines scanned: {data.get('total_headlines',0)}")
    print(f"{'='*55}")

    for bucket, sig in data.get("signals", {}).items():
        emoji = signal_emoji.get(sig["signal"], "⚪")
        print(f"\n  {bucket}")
        print(f"    {emoji} {sig['signal'].replace('_',' ').title()}  "
              f"(score: {sig['score']:+.1f}, {sig['matches']} matches)")

    print(f"\n  Sample headlines:")
    for h in data.get("sample_headlines", [])[:6]:
        print(f"  [{h['date']}] [{h['source'][:12]:<12}] {h['title'][:65]}")
    print(f"{'='*55}")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="US financial news RSS sentiment scanner")
    parser.add_argument("--status", action="store_true", help="Show current signals")
    parser.add_argument("--test",   action="store_true", help="Scan without saving")
    args = parser.parse_args()

    if args.status:
        show_status()
    else:
        run_news_scan(test_mode=args.test)
