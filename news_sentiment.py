"""
News Sentiment — Financial RSS Feed Analyser (4.3)
====================================================
Fetches headlines from 4 Indian financial RSS feeds,
classifies them using the same keyword engine as policy_scraper.py,
and saves sentiment signals to news_signals.json.

Scheduled on Railway: every weekday at 8:00 AM IST

Key differences from policy_scraper.py:
  - 2-day window (vs 14 days for policy)
  - Smaller multipliers (max ±4% vs ±6%)
  - Requires 3+ headline matches before applying adjustment
  - Faster — RSS is lightweight vs full page scraping

Usage:
    python news_sentiment.py           # run scan
    python news_sentiment.py --status  # show current signals
    python news_sentiment.py --test    # scan without saving
"""

import json
import os
import re
import time
import argparse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

import requests

# Reuse keyword engine from policy_scraper
from policy_scraper import (
    BUCKET_KEYWORDS,
    score_release,
    HEADLINE_WEIGHT,
)

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

IST              = ZoneInfo("Asia/Kolkata")
NEWS_SIGNALS_FILE = os.path.join(os.path.dirname(__file__), "news_signals.json")
SCAN_DAYS        = 2      # only last 2 days — news is short-lived
MIN_MATCHES      = 3      # minimum headline matches before applying adjustment
MAX_ITEMS        = 50     # max items per feed

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

# RSS Feed URLs
RSS_FEEDS = {
    "Economic Times Markets": (
        "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"
    ),
    "Moneycontrol":           "https://www.moneycontrol.com/rss/MCtopnews.xml",
    "Business Standard":      "https://www.business-standard.com/rss/markets-106.rss",
    "LiveMint":               "https://www.livemint.com/rss/markets",
}

# Smaller multipliers than policy scraper — news is noisier
NEWS_MULTIPLIERS = {
    "positive":     1.04,
    "mild_positive":1.02,
    "neutral":      1.00,
    "cautious":     0.98,
    "negative":     0.96,
}


# ─────────────────────────────────────────────
# RSS PARSER
# ─────────────────────────────────────────────

def _parse_rss_date(date_str: str) -> date | None:
    """
    Parse RSS date formats:
      RFC 2822: "Thu, 16 Apr 2026 10:30:00 +0530"
      ISO 8601: "2026-04-16T10:30:00+05:30"
    """
    if not date_str:
        return None

    formats = [
        "%a, %d %b %Y %H:%M:%S %z",    # RFC 2822
        "%a, %d %b %Y %H:%M:%S %Z",    # RFC 2822 with timezone name
        "%Y-%m-%dT%H:%M:%S%z",         # ISO 8601
        "%Y-%m-%d %H:%M:%S",           # Simple datetime
        "%d %b %Y",                     # 16 Apr 2026
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str.strip(), fmt).date()
        except ValueError:
            continue

    # Try stripping timezone offset and retrying
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
    Returns list of {title, description, date, source} dicts.
    """
    items   = []
    cutoff  = date.today() - timedelta(days=SCAN_DAYS)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()

        # Parse XML
        root = ET.fromstring(resp.content)

        # Handle both RSS 2.0 and Atom formats
        # RSS 2.0: <channel><item>...
        # Atom: <feed><entry>...
        ns = {
            "atom":    "http://www.w3.org/2005/Atom",
            "content": "http://purl.org/rss/1.0/modules/content/",
        }

        # Try RSS 2.0 first
        entries = root.findall(".//item")
        if not entries:
            # Try Atom
            entries = root.findall(".//atom:entry", ns) or root.findall(".//entry")

        for entry in entries[:MAX_ITEMS]:
            # Title
            title_el = entry.find("title")
            if title_el is None:
                title_el = entry.find("atom:title", ns)
            title = (title_el.text or "").strip() if title_el is not None else ""

            # Clean CDATA and HTML tags from title
            title = re.sub(r'<[^>]+>', '', title).strip()

            if not title or len(title) < 10:
                continue

            # Description / summary
            desc_el = (
                entry.find("description") or
                entry.find("atom:summary", ns) or
                entry.find("summary")
            )
            desc = (desc_el.text or "").strip() if desc_el is not None else ""
            desc = re.sub(r'<[^>]+>', '', desc).strip()[:300]

            # Date
            date_el = (
                entry.find("pubDate") or
                entry.find("published") or
                entry.find("atom:published", ns) or
                entry.find("updated")
            )
            pub_date = None
            if date_el is not None and date_el.text:
                pub_date = _parse_rss_date(date_el.text)

            # Skip if too old
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
    """Fetch all RSS feeds and return combined item list."""
    all_items = []
    for source, url in RSS_FEEDS.items():
        items = fetch_rss_feed(source, url)
        all_items.extend(items)
        time.sleep(0.5)
    return all_items


# ─────────────────────────────────────────────
# SENTIMENT AGGREGATOR
# ─────────────────────────────────────────────

def aggregate_news_sentiment(items: list) -> dict:
    """
    Aggregate keyword scores from news items.
    Applies MIN_MATCHES threshold — signal stays neutral
    if fewer than 3 headlines match for a bucket.

    Returns {bucket_key: {score, signal, reason, matches}}
    """
    bucket_totals  = {k: 0.0 for k in BUCKET_KEYWORDS}
    bucket_reasons = {k: [] for k in BUCKET_KEYWORDS}
    bucket_counts  = {k: 0  for k in BUCKET_KEYWORDS}

    for item in items:
        scores = score_release(item["title"], item.get("body", ""))
        for bucket, score in scores.items():
            if abs(score) > 0:
                bucket_totals[bucket]  += score
                bucket_counts[bucket] += 1
                # Only store headline-level matches as reasons
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

        # Apply minimum matches threshold
        if matches < MIN_MATCHES:
            signal = "neutral"
            reason = (
                f"Only {matches} headline match(es) — "
                f"below minimum {MIN_MATCHES} threshold. No adjustment."
            )
        else:
            # Signal thresholds (tighter than policy scraper — news is noisier)
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

            top = bucket_reasons[bucket][:3]
            reason = "; ".join(top) if top else "Matched but no headline-level signals."

        result[bucket] = {
            "score":   total,
            "signal":  signal,
            "reason":  reason,
            "matches": matches,
        }

    return result


# ─────────────────────────────────────────────
# SIGNAL PERSISTENCE
# ─────────────────────────────────────────────

def save_news_signals(signals: dict, items: list):
    output = {
        "generated_at":   datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"),
        "scan_days":      SCAN_DAYS,
        "total_headlines":len(items),
        "min_matches":    MIN_MATCHES,
        "signals":        signals,
        "sample_headlines": [
            {"title": i["title"], "date": i["date"], "source": i["source"]}
            for i in items[:15]
        ],
    }
    with open(NEWS_SIGNALS_FILE, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n  ✅ News signals saved to {NEWS_SIGNALS_FILE}")


def load_news_signals() -> dict | None:
    if not os.path.exists(NEWS_SIGNALS_FILE):
        return None
    try:
        with open(NEWS_SIGNALS_FILE) as f:
            return json.load(f)
    except Exception:
        return None


# ─────────────────────────────────────────────
# MAIN SCAN
# ─────────────────────────────────────────────

def run_news_scan(test_mode: bool = False):
    """Fetch RSS feeds, classify, save signals."""
    print(f"\n{'='*55}")
    print(f"  📰 NEWS SENTIMENT — RSS SCAN")
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

    # Print results
    signal_emoji = {
        "positive":     "🟢",
        "mild_positive":"🟡",
        "neutral":      "⚪",
        "cautious":     "🟠",
        "negative":     "🔴",
    }
    print(f"\n  📊 NEWS SENTIMENT SIGNALS:")
    for bucket, sig in signals.items():
        emoji = signal_emoji.get(sig["signal"], "⚪")
        print(f"\n  {bucket}")
        print(f"    Signal:  {emoji} {sig['signal'].replace('_',' ').title()}  "
              f"(score: {sig['score']:+.1f}, {sig['matches']} matches)")
        if sig["matches"] >= MIN_MATCHES and "↑" in sig["reason"] or "↓" in sig["reason"]:
            for line in sig["reason"].split(";")[:2]:
                if line.strip():
                    print(f"    {line.strip()[:80]}")

    if not test_mode:
        save_news_signals(signals, items)

    return signals


# ─────────────────────────────────────────────
# STATUS
# ─────────────────────────────────────────────

def show_status():
    data = load_news_signals()
    if not data:
        print("  No news signals. Run: python news_sentiment.py")
        return

    signal_emoji = {
        "positive":"🟢", "mild_positive":"🟡",
        "neutral":"⚪",  "cautious":"🟠", "negative":"🔴",
    }
    print(f"\n{'='*55}")
    print(f"  📰 CURRENT NEWS SENTIMENT")
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
    parser = argparse.ArgumentParser(description="Financial news RSS sentiment scanner")
    parser.add_argument("--status", action="store_true", help="Show current signals")
    parser.add_argument("--test",   action="store_true", help="Scan without saving")
    args = parser.parse_args()

    if args.status:
        show_status()
    else:
        run_news_scan(test_mode=args.test)
