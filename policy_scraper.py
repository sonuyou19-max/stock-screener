"""
Policy Scraper — RBI + PIB Press Release Monitor (4.2)
========================================================
Scrapes RBI and PIB press releases weekly, classifies them
using keyword matching, and saves signals to policy_signals.json.

Scheduled on Railway: every Monday at 7:00 AM IST

Usage:
    python policy_scraper.py          # run full scan
    python policy_scraper.py --status # show current signals
    python policy_scraper.py --test   # test scraping without saving
"""

import json
import os
import re
import time
import argparse
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

IST             = ZoneInfo("Asia/Kolkata")
SIGNALS_FILE    = os.path.join(os.path.dirname(__file__), "policy_signals.json")
SCAN_DAYS       = 14    # look back 14 days for releases
MAX_RELEASES    = 30    # max releases to process per source

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


# ─────────────────────────────────────────────
# KEYWORD CLASSIFICATION ENGINE
# ─────────────────────────────────────────────

# Each bucket has positive and negative keywords.
# Multi-word phrases are matched as substrings (case-insensitive).
# Phrases listed first have higher priority.

BUCKET_KEYWORDS = {
    "BFSI_IT": {
        "positive": [
            # RBI rate / liquidity signals
            "rate cut", "repo rate cut", "reduces repo", "rate reduction",
            "accommodative", "liquidity infusion", "credit growth",
            "npa reduction", "npa declined", "bad loan recovery",
            "digital banking", "upi", "fintech", "insurance fdi",
            # IT sector
            "it exports", "software exports", "digital india",
            "semiconductor", "electronics manufacturing",
        ],
        "negative": [
            "rate hike", "repo rate hike", "raises repo", "tightening",
            "withdrawal of accommodation", "hawkish",
            "npa increase", "npa rise", "bad loans surge",
            "bank fraud", "penalty on bank", "rbi penalty",
            "it slowdown", "visa restriction", "h1b",
            "cybersecurity breach", "data breach",
        ],
        "neutral_override": [
            # If these appear near negative words, suppress false positives
            "rate manipulation", "rate rigging",
        ],
    },

    "DEFENCE_INFRA": {
        "positive": [
            "defence procurement", "defence capex", "defence outlay",
            "defence budget increase", "make in india defence",
            "atmanirbhar defence", "indigenous defence",
            "hal order", "bel order", "drdo", "defence export",
            "infrastructure outlay", "pli scheme", "capital expenditure",
            "road construction", "railway capex", "smart city",
            "port development", "airport development",
            "construction boost", "cement demand",
        ],
        "negative": [
            "defence budget cut", "defence cut", "capex reduction",
            "infrastructure delay", "project cancellation",
            "import defence", "defence import increase",
            "fiscal consolidation", "spending cut",
            "infra project stalled",
        ],
        "neutral_override": [],
    },

    "GREEN_ENERGY_EV": {
        "positive": [
            "solar target", "renewable target", "green energy",
            "mnre", "solar capacity", "wind energy",
            "ev policy", "electric vehicle", "ev subsidy",
            "green hydrogen", "battery storage",
            "clean energy", "net zero", "renewable purchase",
            "solar park", "offshore wind",
            "pm kusum", "pm surya ghar", "rooftop solar",
            "ireda", "ntpc green", "adani green",
        ],
        "negative": [
            "solar duty", "import duty solar", "bcd on solar",
            "solar panel duty", "import duty on modules",
            "renewable delay", "green energy delay",
            "ev subsidy cut", "fame scheme cut",
            "grid curtailment", "power sector stress",
            "coal import", "fossil fuel expansion",
        ],
        "neutral_override": [],
    },

    "FMCG_PHARMA": {
        "positive": [
            "msp increase", "rural income", "rural demand",
            "minimum support price", "kharif msp", "rabi msp",
            "monsoon forecast", "normal monsoon", "good rainfall",
            "healthcare budget", "health outlay",
            "pharma export", "drug approval", "fda approval",
            "api production", "medical device",
            "fmcg volume", "consumer demand",
            "direct benefit transfer", "dbt",
        ],
        "negative": [
            "drug price control", "price cap pharma",
            "fda import alert", "fda warning letter",
            "drug recall", "pharma penalty",
            "food inflation", "commodity inflation",
            "palm oil duty", "edible oil price",
            "drought", "below normal monsoon", "deficit monsoon",
            "rural distress", "consumer slowdown",
        ],
        "neutral_override": [],
    },
}

# Scoring weights
HEADLINE_WEIGHT = 2.0   # headlines are more important than body text
BODY_WEIGHT     = 1.0
PHRASE_BONUS    = 0.5   # multi-word phrases score slightly higher than single words


# ─────────────────────────────────────────────
# SCORING FUNCTION
# ─────────────────────────────────────────────

def score_release(title: str, body: str = "") -> dict:
    """
    Score a single press release against all bucket keywords.

    Returns {bucket_key: score} where:
      positive score → good for bucket
      negative score → bad for bucket
      0 → not relevant
    """
    title_lower = title.lower()
    body_lower  = body.lower()
    scores      = {k: 0.0 for k in BUCKET_KEYWORDS}

    for bucket, keywords in BUCKET_KEYWORDS.items():
        bucket_score = 0.0

        # ── Positive keywords ─────────────────────────────────
        for phrase in keywords["positive"]:
            phrase_l    = phrase.lower()
            is_multi    = " " in phrase_l
            word_bonus  = PHRASE_BONUS if is_multi else 0.0

            if phrase_l in title_lower:
                bucket_score += HEADLINE_WEIGHT + word_bonus
            elif phrase_l in body_lower:
                bucket_score += BODY_WEIGHT + word_bonus

        # ── Negative keywords ─────────────────────────────────
        for phrase in keywords["negative"]:
            phrase_l   = phrase.lower()
            is_multi   = " " in phrase_l
            word_bonus = PHRASE_BONUS if is_multi else 0.0

            if phrase_l in title_lower:
                bucket_score -= HEADLINE_WEIGHT + word_bonus
            elif phrase_l in body_lower:
                bucket_score -= BODY_WEIGHT + word_bonus

        # ── Neutral override — suppress false positives ───────
        for phrase in keywords.get("neutral_override", []):
            if phrase.lower() in title_lower or phrase.lower() in body_lower:
                bucket_score = 0.0  # cancel score if override phrase found
                break

        scores[bucket] = round(bucket_score, 2)

    return scores


def aggregate_scores(releases: list) -> dict:
    """
    Aggregate scores from multiple releases into a final
    signal per bucket.

    Returns:
    {
      bucket_key: {
        "score":   float,         # net score across all releases
        "signal":  str,           # positive | neutral | cautious | negative
        "reason":  str,           # top contributing release headline
        "releases_matched": int,  # how many releases were relevant
      }
    }
    """
    bucket_totals  = {k: 0.0 for k in BUCKET_KEYWORDS}
    bucket_reasons = {k: [] for k in BUCKET_KEYWORDS}
    bucket_counts  = {k: 0  for k in BUCKET_KEYWORDS}

    for r in releases:
        scores = score_release(r["title"], r.get("body", ""))
        for bucket, score in scores.items():
            if abs(score) > 0:
                bucket_totals[bucket]  += score
                bucket_counts[bucket] += 1
                if abs(score) >= HEADLINE_WEIGHT:
                    bucket_reasons[bucket].append(
                        f"{'↑' if score > 0 else '↓'} {r['title'][:80]}"
                    )

    result = {}
    for bucket in BUCKET_KEYWORDS:
        total = round(bucket_totals[bucket], 2)

        # Signal thresholds
        if total >= 4.0:
            signal = "positive"
        elif total >= 1.5:
            signal = "mild_positive"
        elif total <= -4.0:
            signal = "negative"
        elif total <= -1.5:
            signal = "cautious"
        else:
            signal = "neutral"

        top_reasons = bucket_reasons[bucket][:3]  # top 3 relevant releases
        reason = (
            "; ".join(top_reasons)
            if top_reasons
            else "No significant policy events in last 14 days"
        )

        result[bucket] = {
            "score":            total,
            "signal":           signal,
            "reason":           reason,
            "releases_matched": bucket_counts[bucket],
        }

    return result


# ─────────────────────────────────────────────
# RBI SCRAPER
# ─────────────────────────────────────────────

RBI_URL = "https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx"

def scrape_rbi_releases() -> list:
    """
    Scrape RBI press releases from the last SCAN_DAYS days.
    Returns list of {title, date, url, source} dicts.
    """
    releases = []
    cutoff   = date.today() - timedelta(days=SCAN_DAYS)

    try:
        resp = requests.get(RBI_URL, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # RBI press releases are in table rows with date + title
        # Try multiple selectors as RBI occasionally redesigns
        rows = (
            soup.select("table.tablebg tr") or
            soup.select(".pressRelease tr") or
            soup.select("table tr")
        )

        for row in rows[:MAX_RELEASES]:
            cells = row.find_all("td")
            if len(cells) < 2:
                continue

            # Try to find date cell and title cell
            date_text  = cells[0].get_text(strip=True)
            title_text = cells[-1].get_text(strip=True)
            link_tag   = cells[-1].find("a")
            url        = ""

            if link_tag and link_tag.get("href"):
                href = link_tag["href"]
                url  = href if href.startswith("http") else f"https://www.rbi.org.in{href}"

            # Parse date
            parsed_date = _parse_date(date_text)
            if not parsed_date:
                continue
            if parsed_date < cutoff:
                break  # sorted newest first — stop when too old

            if title_text and len(title_text) > 10:
                releases.append({
                    "title":  title_text,
                    "date":   str(parsed_date),
                    "url":    url,
                    "source": "RBI",
                })

        print(f"    🏦 RBI: {len(releases)} releases in last {SCAN_DAYS} days")

    except Exception as e:
        print(f"    ⚠️  RBI scrape failed: {e}")

    return releases


# ─────────────────────────────────────────────
# PIB SCRAPER
# ─────────────────────────────────────────────

# Key ministries relevant to our buckets
PIB_MINISTRY_URLS = {
    "Finance":           "https://pib.gov.in/allRel.aspx?relid=&mnid=2",
    "Defence":           "https://pib.gov.in/allRel.aspx?relid=&mnid=7",
    "Renewable Energy":  "https://pib.gov.in/allRel.aspx?relid=&mnid=69",
    "Health":            "https://pib.gov.in/allRel.aspx?relid=&mnid=24",
    "Commerce":          "https://pib.gov.in/allRel.aspx?relid=&mnid=6",
}

def scrape_pib_releases() -> list:
    """
    Scrape PIB press releases from key ministries.
    Returns list of {title, date, url, source, ministry} dicts.
    """
    all_releases = []
    cutoff       = date.today() - timedelta(days=SCAN_DAYS)

    for ministry, url in PIB_MINISTRY_URLS.items():
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            # PIB uses a list of releases with date + title
            items = (
                soup.select(".innner-page-main-about-us-content-right-part li") or
                soup.select(".release-list li") or
                soup.select("ul.list li") or
                soup.select("li")
            )

            ministry_count = 0
            for item in items[:MAX_RELEASES]:
                text     = item.get_text(separator=" ", strip=True)
                link_tag = item.find("a")
                title    = link_tag.get_text(strip=True) if link_tag else text[:100]
                url_link = ""

                if link_tag and link_tag.get("href"):
                    href     = link_tag["href"]
                    url_link = href if href.startswith("http") \
                               else f"https://pib.gov.in{href}"

                # Try to extract date from text (PIB often includes it)
                parsed_date = _extract_date_from_text(text) or date.today()
                if parsed_date < cutoff:
                    continue

                if title and len(title) > 15:
                    all_releases.append({
                        "title":    title,
                        "date":     str(parsed_date),
                        "url":      url_link,
                        "source":   "PIB",
                        "ministry": ministry,
                    })
                    ministry_count += 1

            print(f"    📋 PIB {ministry}: {ministry_count} releases")
            time.sleep(0.5)  # polite delay between ministry pages

        except Exception as e:
            print(f"    ⚠️  PIB {ministry} scrape failed: {e}")

    return all_releases


# ─────────────────────────────────────────────
# DATE PARSERS
# ─────────────────────────────────────────────

def _parse_date(text: str) -> date | None:
    """Try to parse a date string in common formats."""
    formats = [
        "%B %d, %Y",   # April 15, 2026
        "%b %d, %Y",   # Apr 15, 2026
        "%d/%m/%Y",    # 15/04/2026
        "%d-%m-%Y",    # 15-04-2026
        "%d-%b-%Y",    # 15-Apr-2026
        "%Y-%m-%d",    # 2026-04-15
        "%d %B %Y",    # 15 April 2026
        "%d %b %Y",    # 15 Apr 2026
    ]
    text = text.strip()
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _extract_date_from_text(text: str) -> date | None:
    """
    Extract a date from free-form text using regex.
    Handles: '15 Apr 2026', 'April 15, 2026', '15/04/2026'
    """
    # Pattern: day month year
    patterns = [
        r"\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{4})\b",
        r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return _parse_date(m.group(0))
    return None


# ─────────────────────────────────────────────
# SIGNAL PERSISTENCE
# ─────────────────────────────────────────────

def save_signals(signals: dict, releases: list):
    """Save signals and raw releases to policy_signals.json."""
    output = {
        "generated_at": datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"),
        "scan_days":     SCAN_DAYS,
        "total_releases":len(releases),
        "signals":       signals,
        "top_releases":  [
            {"title": r["title"], "date": r["date"],
             "source": r["source"], "ministry": r.get("ministry", "")}
            for r in sorted(releases, key=lambda x: x["date"], reverse=True)[:20]
        ],
    }
    with open(SIGNALS_FILE, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n  ✅ Signals saved to {SIGNALS_FILE}")


def load_signals() -> dict | None:
    """Load previously saved policy signals."""
    if not os.path.exists(SIGNALS_FILE):
        return None
    try:
        with open(SIGNALS_FILE) as f:
            return json.load(f)
    except Exception:
        return None


# ─────────────────────────────────────────────
# MAIN SCAN ORCHESTRATOR
# ─────────────────────────────────────────────

def run_policy_scan(test_mode: bool = False):
    """
    Full policy scan:
    1. Scrape RBI releases
    2. Scrape PIB releases
    3. Classify with keyword matching
    4. Save signals
    """
    print(f"\n{'='*55}")
    print(f"  📜 POLICY SCRAPER — RBI + PIB SCAN")
    print(f"  {datetime.now(IST).strftime('%d %B %Y, %I:%M %p IST')}")
    print(f"  Scanning last {SCAN_DAYS} days...")
    print(f"{'='*55}\n")

    # Scrape
    rbi_releases = scrape_rbi_releases()
    time.sleep(1)
    pib_releases = scrape_pib_releases()

    all_releases  = rbi_releases + pib_releases
    total         = len(all_releases)

    print(f"\n  Total releases fetched: {total}")

    if total == 0:
        print("  ⚠️  No releases fetched — check scraper connectivity.")
        return None

    # Classify
    print(f"\n  🔍 Classifying {total} releases...")
    signals = aggregate_scores(all_releases)

    # Print results
    print(f"\n  📊 POLICY SIGNALS:")
    signal_emoji = {
        "positive":     "🟢",
        "mild_positive":"🟡",
        "neutral":      "⚪",
        "cautious":     "🟠",
        "negative":     "🔴",
    }
    for bucket, sig in signals.items():
        emoji = signal_emoji.get(sig["signal"], "⚪")
        print(f"\n  {bucket}")
        print(f"    Signal:  {emoji} {sig['signal'].replace('_',' ').title()}  (score: {sig['score']:+.1f})")
        print(f"    Matched: {sig['releases_matched']} releases")
        if sig["reason"] != "No significant policy events in last 14 days":
            for line in sig["reason"].split(";")[:2]:
                print(f"    {line.strip()}")

    # Save
    if not test_mode:
        save_signals(signals, all_releases)

    return signals


# ─────────────────────────────────────────────
# STATUS DISPLAY
# ─────────────────────────────────────────────

def show_status():
    """Show current saved policy signals."""
    data = load_signals()
    if not data:
        print("  No policy signals found. Run: python policy_scraper.py")
        return

    print(f"\n{'='*55}")
    print(f"  📜 CURRENT POLICY SIGNALS")
    print(f"  Generated: {data.get('generated_at','unknown')}")
    print(f"  Releases scanned: {data.get('total_releases',0)}")
    print(f"{'='*55}")

    signal_emoji = {
        "positive":     "🟢",
        "mild_positive":"🟡",
        "neutral":      "⚪",
        "cautious":     "🟠",
        "negative":     "🔴",
    }

    for bucket, sig in data.get("signals", {}).items():
        emoji = signal_emoji.get(sig["signal"], "⚪")
        print(f"\n  {bucket}")
        print(f"    {emoji} {sig['signal'].replace('_',' ').title()}  "
              f"(score: {sig['score']:+.1f}, {sig['releases_matched']} releases)")
        if sig.get("reason") and "No significant" not in sig["reason"]:
            for line in sig["reason"].split(";")[:2]:
                if line.strip():
                    print(f"    {line.strip()}")

    print(f"\n  Recent releases:")
    for r in data.get("top_releases", [])[:8]:
        src = r.get("ministry") or r.get("source","")
        print(f"  [{r['date']}] [{src}] {r['title'][:70]}")

    print(f"{'='*55}")


# ─────────────────────────────────────────────
# MAIN ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RBI + PIB policy scraper")
    parser.add_argument(
        "--status", action="store_true",
        help="Show current policy signals without scraping"
    )
    parser.add_argument(
        "--test", action="store_true",
        help="Run scan but don't save results"
    )
    args = parser.parse_args()

    if args.status:
        show_status()
    else:
        run_policy_scan(test_mode=args.test)
