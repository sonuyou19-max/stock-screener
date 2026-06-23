# -*- coding: utf-8 -*-
"""
Swing News Sentiment — NSE Sector RSS Scanner
===============================================
Fetches financial RSS headlines and scores sentiment for each of the
20 NSE sectors used in the swing scanner.

Unlike the long-term news_sentiment.py (4 buckets), this service uses
the exact same 20 sector categories as the Nifty 500 classification
so swing_scanner.py can do a direct lookup.

Sentiment scale:
  positive      → +1 signal in swing scanner
  mild_positive → +0.5 signal
  neutral       → no effect
  cautious      → −1 penalty
  negative      → HARD EXCLUDE (stock removed regardless of technicals)

Schedule: 0 17 * * 0-6  (10:30 PM IST = 17:00 UTC, captures full business day; swing-scanner runs 30 min later at 17:30 UTC)

Usage:
  python swing_news_sentiment.py           # run scan
  python swing_news_sentiment.py --status  # show current signals
  python swing_news_sentiment.py --test    # scan without saving
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

IST                      = ZoneInfo("Asia/Kolkata")
DATA_DIR                 = os.getenv("DATA_DIR", "/data")
API_URL                  = os.getenv("API_URL", "https://web-production-50eee.up.railway.app").rstrip("/")
SWING_SENTIMENT_FILE     = os.path.join(DATA_DIR, "swing_news_sentiment.json")
SWING_HISTORY_FILE       = os.path.join(DATA_DIR, "swing_sentiment_history.json")

ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
LLM_MODEL     = "claude-haiku-4-5-20251001"
LLM_ENABLED   = bool(ANTHROPIC_KEY)

SCAN_DAYS           = 1   # only today's headlines — blending with history avoids duplicate scanning
MIN_MATCHES         = 1   # 1-day window yields fewer items so lower threshold
MAX_ITEMS           = 60  # per feed
HISTORY_MAX_DAYS    = 30  # rolling window kept in history file
HISTORY_BLEND_DAYS  = 7   # how many past days to blend with today's raw signal
HISTORY_BLEND_TODAY = 0.40  # weight given to today's raw signal
HISTORY_BLEND_PAST  = 0.60  # weight given to rolling history average

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

# ─────────────────────────────────────────────
# RSS FEEDS
# ─────────────────────────────────────────────

RSS_FEEDS = {
    # Economic Times — primary source (most reliable on Railway)
    "ET Markets":      "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "ET Economy":      "https://economictimes.indiatimes.com/economy/rssfeeds/1373380680.cms",
    "ET Industry":     "https://economictimes.indiatimes.com/industry/rssfeeds/13352306.cms",
    "ET Auto":         "https://economictimes.indiatimes.com/industry/auto/rssfeeds/17820491.cms",
    "ET Energy":       "https://economictimes.indiatimes.com/industry/energy/rssfeeds/13357174.cms",
    "ET Pharma":       "https://economictimes.indiatimes.com/industry/healthcare/biotech/pharmaceuticals/rssfeeds/13358173.cms",
    "ET IT":           "https://economictimes.indiatimes.com/tech/rssfeeds/78570561.cms",
    "ET Wealth":       "https://economictimes.indiatimes.com/wealth/rssfeeds/837555174.cms",
    # LiveMint
    "LiveMint Markets":  "https://www.livemint.com/rss/markets",
    "LiveMint Economy":  "https://www.livemint.com/rss/economy",
    "LiveMint Industry": "https://www.livemint.com/rss/industry",
    # Business Standard — good coverage, less blocking
    "BS Markets":    "https://www.business-standard.com/rss/markets-106.rss",
    "BS Economy":    "https://www.business-standard.com/rss/economy-policy-102.rss",
    "BS Companies":  "https://www.business-standard.com/rss/companies-101.rss",
    # The Hindu Business Line
    "BusinessLine":  "https://www.thehindubusinessline.com/feeder/default.rss",
    "BL Markets":    "https://www.thehindubusinessline.com/markets/feeder/default.rss",
}

# ─────────────────────────────────────────────
# SECTOR KEYWORDS — 20 NSE SECTORS
# ─────────────────────────────────────────────
# Each sector has "positive" and "negative" keyword lists.
# Keyword match in headline = HEADLINE_WEIGHT (2.0)
# Keyword match in body     = BODY_WEIGHT (1.0)
# Multi-word phrase bonus   = +0.5

HEADLINE_WEIGHT = 2.0
BODY_WEIGHT     = 1.0
PHRASE_BONUS    = 0.5

SECTOR_KEYWORDS = {

    "Financial Services": {
        "positive": [
            "rate cut", "repo rate cut", "rbi cuts", "accommodative policy",
            "liquidity infusion", "credit growth", "loan growth",
            "deposit growth", "banking profit", "net interest income",
            "npa declined", "npa reduction", "bad loan recovery",
            "credit offtake", "financial inclusion", "upi transaction",
            "digital banking", "fintech growth", "insurance premium",
            "mutual fund aum", "sip inflows", "market rally",
            "hdfc bank profit", "icici bank profit", "sbi profit",
            "bajaj finance", "stock market gains", "sensex gains",
            "fii inflows", "foreign investment", "rupee strengthens",
            "gdp growth", "economic growth", "rate easing",
        ],
        "negative": [
            "rate hike", "repo rate hike", "rbi raises", "hawkish",
            "withdrawal of accommodation", "liquidity drain",
            "npa rise", "npa increase", "bad loans surge",
            "bank fraud", "rbi penalty", "pca framework",
            "credit crunch", "loan default", "bank collapse",
            "financial crisis", "debt default", "bond yield spike",
            "rupee falls", "inr depreciates", "fii outflows",
            "market crash", "sensex falls", "stock market fall",
            "lending rate hike", "emi increase",
        ],
    },

    "Information Technology": {
        "positive": [
            "it deal", "tech deal", "software contract", "deal win",
            "it order", "digital transformation", "cloud adoption",
            "ai contract", "generative ai", "it exports",
            "software exports", "tech hiring", "attrition falls",
            "tcs revenue", "infosys revenue", "wipro revenue",
            "hcl tech", "tech mahindra", "it sector growth",
            "rupee weakens", "dollar strengthens", "usd inr",
            "us tech spending", "enterprise spending",
            "data centre", "saas deal", "erp deal", "it budget",
            "visa reform", "h1b reform", "us immigration",
            "it margin improvement", "offshore outsourcing",
        ],
        "negative": [
            "it slowdown", "tech slowdown", "it spending cut",
            "deal cancellation", "deal delay", "it layoffs",
            "tech layoffs", "visa restriction", "h1b ban",
            "us recession", "us slowdown", "client budget cut",
            "rupee strengthens", "inr appreciation",
            "margin pressure", "attrition surge",
            "cybersecurity breach", "data breach", "ransomware",
            "it contract loss", "deal ramp down",
        ],
    },

    "Oil Gas And Consumable Fuels": {
        "positive": [
            "oil price falls", "crude falls", "brent falls",
            "crude crash", "oil slumps", "opec cut",
            "fuel price cut", "petrol price cut", "diesel cut",
            "natural gas discovery", "gas production",
            "refinery expansion", "petchem expansion",
            "oil marketing", "reliance refinery",
            "gas pipeline", "city gas distribution",
            "lng import", "domestic oil production",
            "upstream discovery", "field development",
        ],
        "negative": [
            "oil price surge", "crude surge", "brent surge",
            "oil rally", "crude spike", "opec production cut",
            "fuel price hike", "petrol price hike", "diesel hike",
            "windfall tax", "export duty oil",
            "crude supply disruption", "oil embargo",
            "refinery fire", "pipeline disruption",
            "lng shortage", "gas shortage",
            "excise duty hike", "fuel levy",
        ],
    },

    "Fast Moving Consumer Goods": {
        "positive": [
            "rural demand", "rural recovery", "rural income",
            "msp increase", "minimum support price",
            "monsoon forecast", "normal monsoon", "good rainfall",
            "kharif msp", "rabi msp", "farm income",
            "fmcg volume", "consumer demand", "retail sales",
            "fmcg sales growth", "volume growth", "price hike fmcg",
            "direct benefit transfer", "dbt", "pm kisan",
            "food inflation eases", "vegetable prices fall",
            "wage growth", "consumer confidence",
            "hindustan unilever", "nestle india", "dabur", "marico",
            "britannia", "itc fmcg", "colgate", "godrej consumer",
            "premiumisation", "urban consumption",
        ],
        "negative": [
            "rural distress", "consumer slowdown", "fmcg decline",
            "volume decline", "demand slowdown",
            "food inflation", "commodity inflation",
            "palm oil price", "edible oil price surge",
            "drought", "below normal monsoon", "deficit monsoon",
            "input cost pressure", "raw material cost",
            "gst hike fmcg", "excise fmcg",
            "unemployment rise", "wage cut",
        ],
    },

    "Healthcare": {
        "positive": [
            "drug approval", "fda approval", "usfda approval",
            "anda approval", "nda approval", "drug launch",
            "pharma export", "api export", "generics export",
            "healthcare budget", "health outlay",
            "medical device approval", "medical tourism",
            "hospital expansion", "bed capacity",
            "health insurance", "ayushman bharat",
            "sun pharma", "dr reddy", "cipla", "lupin",
            "biocon", "aurobindo", "divi labs",
            "api production", "active pharma ingredient",
            "biosimilar approval", "vaccine approval",
            "clinical trial success", "phase 3 success",
            "import substitution pharma",
        ],
        "negative": [
            "fda warning letter", "fda import alert",
            "drug recall", "pharma penalty", "gmp violation",
            "drug price control", "nppa price cap",
            "price cap pharma", "drug price reduction",
            "fda ban", "us import ban",
            "clinical trial failure", "drug rejection",
            "hospital accident", "medical negligence",
            "healthcare budget cut", "health scheme cut",
            "pharma sector stress",
        ],
    },

    "Automobile and Auto Components": {
        "positive": [
            "auto sales", "vehicle sales", "car sales growth",
            "two wheeler sales", "commercial vehicle",
            "ev sales", "electric vehicle sales", "ev adoption",
            "ev policy", "ev subsidy", "fame scheme",
            "fuel efficiency", "bs6 compliance",
            "auto export", "vehicle export",
            "maruti sales", "mahindra sales", "tata motors",
            "hero motocorp", "bajaj auto", "tvs motor",
            "auto component", "ancillary growth",
            "rural demand auto", "festive season sales",
            "finance availability", "auto loan",
            "production increase", "capacity expansion auto",
        ],
        "negative": [
            "auto sales decline", "vehicle sales fall",
            "ev slowdown", "ev demand falls",
            "ev subsidy cut", "fame cut",
            "fuel price hike", "petrol hike diesel hike",
            "semiconductor shortage", "chip shortage auto",
            "auto component shortage", "supply chain disruption",
            "interest rate impact auto", "emi increase auto",
            "auto loan tightening",
            "import duty auto", "auto recall",
            "commercial vehicle decline",
        ],
    },

    "Capital Goods": {
        "positive": [
            "defence order", "defence contract", "defence procurement",
            "hal order", "bel order", "l&t order", "cgpower order",
            "capital expenditure", "capex", "infrastructure spending",
            "government capex", "pli scheme", "production linked",
            "make in india", "indigenous manufacturing",
            "power equipment", "transformer order",
            "railway equipment", "metro rail",
            "shipbuilding order", "naval vessel",
            "defence export", "defence indigenisation",
            "atmanirbhar", "drdo", "ministry of defence",
            "engineering order", "equipment order",
            "industrial machinery", "plant machinery",
        ],
        "negative": [
            "capex cut", "capex reduction", "budget cut",
            "order cancellation", "project delay",
            "defence budget cut", "import substitution failure",
            "raw material cost capital goods",
            "commodity inflation", "steel price surge",
            "order book concern", "execution delay",
            "competition from imports",
        ],
    },

    "Metals And Mining": {
        "positive": [
            "steel demand", "steel price rise", "metal prices",
            "aluminium price", "copper price rally",
            "china demand", "china recovery", "chinese stimulus",
            "infrastructure demand steel",
            "mining output", "iron ore price",
            "metal rally", "commodity rally",
            "tata steel", "jsw steel", "hindalco",
            "coal india output", "coal production",
            "steel export", "metal export",
            "capacity expansion steel",
            "lme prices", "zinc price", "nickel price",
        ],
        "negative": [
            "steel price fall", "metal prices decline",
            "aluminium price fall", "copper price fall",
            "china slowdown", "china weakness", "china demand falls",
            "dumping steel", "anti-dumping", "cheap imports",
            "mining ban", "mining suspension",
            "iron ore price fall", "coal price fall",
            "commodity crash", "metal crash",
            "steel oversupply", "capacity glut",
            "import surge steel", "import duty cut steel",
        ],
    },

    "Consumer Durables": {
        "positive": [
            "appliance sales", "consumer durable sales",
            "ac sales", "air conditioner demand",
            "refrigerator sales", "washing machine sales",
            "electronic demand", "gadget sales",
            "festive season demand", "diwali sales",
            "urban consumption", "premiumisation",
            "titan jewellery", "havells", "voltas",
            "dixon electronics", "amber enterprises",
            "pli electronics", "pli consumer durables",
            "smartphone sales", "tv sales",
            "real estate driven demand", "housing demand",
            "summer season demand",
        ],
        "negative": [
            "consumer durable decline", "appliance demand falls",
            "festive season disappointment",
            "input cost pressure durables",
            "commodity inflation durables",
            "import competition", "chinese imports durables",
            "rural demand slowdown",
            "interest rate impact consumer",
            "emi cost durables",
        ],
    },

    "Chemicals": {
        "positive": [
            "chemical export", "specialty chemical",
            "agrochemical demand", "pesticide demand",
            "crop protection", "agrochem season",
            "china plus one", "supply chain shift chemicals",
            "chemical plant expansion",
            "petchem margin", "crude derivative",
            "pi industries", "deepak nitrite", "srf",
            "navin fluorine", "aarti industries",
            "pharma chemical", "api chemical",
            "global chemical demand",
            "capacity addition chemicals",
        ],
        "negative": [
            "chemical price fall", "specialty chemical decline",
            "chinese chemical dumping", "cheap imports chemical",
            "agrochemical inventory", "channel inventory",
            "monsoon failure agrochem",
            "crude price surge chemicals",
            "feedstock cost chemicals",
            "chemical accident", "plant shutdown",
            "chemical export ban",
        ],
    },

    "Construction Materials": {
        "positive": [
            "cement demand", "cement volume", "cement dispatch",
            "housing demand", "real estate boom",
            "infrastructure investment", "road construction",
            "smart city project", "affordable housing",
            "pm awas yojana", "pradhan mantri awas",
            "government housing scheme",
            "ultratech cement", "ambuja cement", "acc cement",
            "shree cement", "dalmia cement",
            "cement price hike", "cement price increase",
            "construction activity", "building material demand",
            "tile demand", "sanitaryware demand",
        ],
        "negative": [
            "cement demand falls", "cement volume decline",
            "housing slowdown", "real estate slowdown",
            "construction slowdown",
            "cement price cut", "cement price war",
            "input cost cement", "energy cost cement",
            "coal price cement", "pet coke price",
            "competition cement", "overcapacity cement",
            "monsoon construction halt",
        ],
    },

    "Power": {
        "positive": [
            "renewable energy", "solar energy", "wind energy",
            "power capacity addition", "green energy",
            "ntpc capacity", "adani green", "tata power",
            "renewable target", "solar target", "wind target",
            "power demand growth", "electricity consumption",
            "grid expansion", "transmission line",
            "green hydrogen", "battery storage",
            "ireda", "nhpc", "sjvn",
            "power ppa", "power purchase agreement",
            "energy transition", "clean energy",
            "solar park", "offshore wind",
            "power generation record", "peak demand record",
        ],
        "negative": [
            "power deficit", "electricity shortage",
            "coal shortage power", "fuel supply disruption",
            "grid failure", "blackout",
            "renewable curtailment", "solar curtailment",
            "power tariff cut", "tariff reduction",
            "electricity price fall",
            "transmission constraint",
            "power sector npa", "power sector stress",
            "renewable subsidy cut", "solar subsidy cut",
        ],
    },

    "Telecommunication": {
        "positive": [
            "telecom subscriber growth", "5g rollout", "5g expansion",
            "5g spectrum", "spectrum auction", "data consumption",
            "arpu increase", "tariff hike telecom",
            "airtel subscriber", "jio subscriber",
            "broadband growth", "fiber rollout",
            "telecom capex", "network expansion",
            "digital india", "rural connectivity",
            "vi revival", "vodafone recovery",
            "tower addition", "site rollout",
            "government 5g", "bsnl revival",
        ],
        "negative": [
            "tariff war", "price war telecom",
            "arpu falls", "subscriber churn",
            "telecom debt", "spectrum cost",
            "agr dues", "adjusted gross revenue",
            "telecom financial stress",
            "vi crisis", "vodafone idea crisis",
            "tower sharing pressure",
            "regulatory risk telecom",
            "broadband competition",
        ],
    },

    "Consumer Services": {
        "positive": [
            "quick commerce growth", "food delivery growth",
            "zomato orders", "swiggy orders",
            "ecommerce growth", "online retail",
            "travel demand", "hotel occupancy",
            "tourism growth", "indigo passenger",
            "aviation growth", "air traffic",
            "retail sales growth", "mall footfall",
            "multiplex recovery", "pvr inox",
            "hospitality sector", "indhotel",
            "consumer confidence", "discretionary spend",
            "eating out", "entertainment spend",
        ],
        "negative": [
            "quick commerce slowdown", "food delivery decline",
            "ecommerce slowdown",
            "aviation fuel cost", "atf price",
            "travel demand falls", "hotel occupancy falls",
            "consumer slowdown services",
            "multiplex decline", "ott competition",
            "luxury slowdown", "premium slowdown",
            "services inflation",
        ],
    },

    "Services And Logistics": {
        "positive": [
            "logistics growth", "supply chain improvement",
            "freight growth", "container volume",
            "port traffic", "adani ports",
            "cargo growth", "warehousing demand",
            "export growth", "import growth",
            "trade volume", "shipping demand",
            "cold chain expansion", "last mile delivery",
            "e-commerce logistics", "3pl growth",
            "concor volume", "railway freight",
            "express delivery", "dhl india", "bluedart",
        ],
        "negative": [
            "freight rate fall", "shipping rate decline",
            "logistics slowdown", "cargo decline",
            "port congestion", "supply chain disruption",
            "export decline", "trade deficit",
            "customs clearance delay",
            "trucking slowdown", "fuel cost logistics",
        ],
    },

    "Realty": {
        "positive": [
            "housing sales", "home sales", "property sales",
            "real estate growth", "housing demand",
            "residential launches", "new project launch",
            "home loan growth", "mortgage growth",
            "property price increase", "real estate rally",
            "dlf sales", "godrej properties", "oberoi realty",
            "prestige estates", "lodha sales",
            "affordable housing scheme", "pm awas yojana",
            "reit income", "commercial real estate",
            "office leasing", "it park demand",
            "retail mall expansion", "data centre realty",
        ],
        "negative": [
            "housing slowdown", "property sales decline",
            "real estate stress", "inventory pile up",
            "unsold inventory", "construction delay",
            "home loan rate hike", "mortgage rate hike",
            "real estate developer default",
            "rera violation", "project stalled",
            "property price fall",
            "commercial real estate vacancy",
        ],
    },

    "Diversified And Infrastructure": {
        "positive": [
            "infrastructure capex", "government spending",
            "national infrastructure pipeline",
            "road construction", "highway award",
            "nhai award", "bridge project",
            "airport expansion", "port development",
            "railway expansion", "metro rail",
            "urban infrastructure", "smart city",
            "gmr airports", "irb infrastructure",
            "engineering procurement construction",
            "epc order", "epc contract",
            "water infrastructure", "irrigation project",
            "power transmission", "grid infrastructure",
        ],
        "negative": [
            "infra capex cut", "infrastructure delay",
            "project cancellation infra",
            "epc order loss", "contract termination",
            "land acquisition delay",
            "fiscal consolidation spending cut",
            "nhai slowdown", "highway delay",
            "urban project stalled",
        ],
    },

    "Textiles And Apparel": {
        "positive": [
            "textile export", "garment export", "apparel export",
            "cotton price falls", "yarn price falls",
            "textile demand", "apparel demand",
            "us textile demand", "eu garment demand",
            "china plus one textile", "supply chain shift apparel",
            "pli textile", "technical textile",
            "man made fibre", "synthetic textile",
            "festive season apparel", "wedding season",
            "page industries", "kpr mill",
        ],
        "negative": [
            "cotton price surge", "yarn cost surge",
            "textile export falls", "garment export decline",
            "us import tariff textile",
            "chinese competition textile",
            "polyester price", "fibre cost",
            "textile demand slowdown",
            "apparel inventory buildup",
        ],
    },

    "Media And Entertainment": {
        "positive": [
            "ott subscription growth", "streaming growth",
            "digital advertising", "digital ad revenue",
            "content deal", "film release", "box office",
            "tv advertising", "advertising growth",
            "ipl viewership", "sports broadcast",
            "zee entertainment", "sun tv",
            "pvr inox ticket", "multiplex recovery",
            "saregama music", "music streaming",
            "print media revival", "newspaper ad revenue",
        ],
        "negative": [
            "advertising slowdown", "ad revenue decline",
            "ott competition", "subscription churn",
            "box office flop", "multiplex decline",
            "digital ad slowdown", "facebook ad",
            "media sector stress",
            "content cost surge", "licensing cost",
            "piracy impact", "regulatory risk media",
        ],
    },

    "Paper And Forest Products": {
        "positive": [
            "paper demand", "paper price increase",
            "packaging demand", "corrugated box demand",
            "ecommerce packaging",
            "print media demand",
            "tissue paper demand",
            "wood pulp price falls", "pulp cost falls",
            "paper export", "newsprint demand",
            "jk paper", "west coast paper",
            "capacity utilisation paper",
        ],
        "negative": [
            "paper price fall", "paper demand decline",
            "digital media impact", "paperless trend",
            "wood pulp cost surge", "pulp price rise",
            "import competition paper",
            "paper oversupply",
            "newsprint demand falls",
        ],
    },
}


# ─────────────────────────────────────────────
# RSS PARSER  (reused from news_sentiment.py)
# ─────────────────────────────────────────────

def _parse_rss_date(date_str: str):
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
            # Use explicit None checks (not truthiness) to avoid deprecation warning
            title_el = entry.find("title")
            if title_el is None:
                title_el = entry.find("atom:title", ns)
            title = re.sub(r'<[^>]+>', '', (title_el.text or "") if title_el is not None else "").strip()
            if not title or len(title) < 10:
                continue

            desc_el = entry.find("description")
            if desc_el is None: desc_el = entry.find("atom:summary", ns)
            if desc_el is None: desc_el = entry.find("summary")
            desc = re.sub(r'<[^>]+>', '', (desc_el.text or "") if desc_el is not None else "").strip()[:300]

            date_el = entry.find("pubDate")
            if date_el is None: date_el = entry.find("published")
            if date_el is None: date_el = entry.find("atom:published", ns)
            if date_el is None: date_el = entry.find("updated")

            pub_date = None
            if date_el is not None and date_el.text:
                pub_date = _parse_rss_date(date_el.text)

            # Accept items with unparseable dates — don't silently drop them
            if pub_date is not None and pub_date < cutoff:
                continue

            items.append({
                "title":  title,
                "body":   desc,
                "date":   str(pub_date or date.today()),
                "source": source_name,
            })

        print(f"    📰 {source_name:<22}: {len(items)} items")

    except ET.ParseError as e:
        print(f"    ⚠️  {source_name}: XML error — {e}")
    except Exception as e:
        print(f"    ⚠️  {source_name}: fetch failed — {e}")

    return items


def fetch_all_feeds() -> list:
    all_items = []
    for source, url in RSS_FEEDS.items():
        items = fetch_rss_feed(source, url)
        all_items.extend(items)
        time.sleep(0.5)
    return all_items


# ─────────────────────────────────────────────
# LLM SECTOR SCORER
# ─────────────────────────────────────────────

def _get_sector_headlines(items: list) -> dict:
    """Filter up to 15 relevant headlines per sector using keyword presence."""
    result = {s: [] for s in SECTOR_KEYWORDS}
    for item in items:
        text = (item["title"] + " " + item.get("body", "")).lower()
        for sector, kw in SECTOR_KEYWORDS.items():
            if len(result[sector]) >= 15:
                continue
            all_kw = kw.get("positive", []) + kw.get("negative", [])
            if any(k in text for k in all_kw):
                result[sector].append(item["title"])
    return result


def llm_score_sectors(items: list) -> dict | None:
    """
    Call Claude Haiku with sector-relevant headlines to get near-term verdicts.
    Returns {sector: {signal, score, matches, reason}} matching aggregate_sentiment
    format, or None if unavailable/failed (caller falls back to keyword scoring).
    """
    sector_headlines = _get_sector_headlines(items)

    sectors_block = []
    for sector, headlines in sector_headlines.items():
        bullets = "\n".join(f"  - {h[:100]}" for h in headlines) if headlines else "  (no relevant headlines found)"
        sectors_block.append(f"{sector}:\n{bullets}")

    sector_template = "\n".join(
        f'  "{s}": {{"signal": "neutral", "reason": "..."}}'
        for s in SECTOR_KEYWORDS
    )

    prompt = f"""You are a senior equity analyst for an Indian equity portfolio. Analyse today's financial news headlines and rate the near-term outlook for each of the 20 NSE sectors for swing trading (1–4 week horizon).

Signal options (pick exactly one per sector):
- positive: multiple strong positive signals, clear near-term tailwind
- mild_positive: more good news than bad, moderate tailwind
- neutral: mixed or insufficient signals
- cautious: more bad news than good, sector headwinds
- negative: multiple strong negative signals, avoid for swing trades

HEADLINES BY SECTOR (today):
{"=" * 60}
{chr(10).join(sectors_block)}
{"=" * 60}

Return ONLY a JSON object with exactly these 20 sector keys — no markdown, no preamble:
{{
{sector_template}
}}"""

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
                "max_tokens": 1500,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        resp.raise_for_status()
        raw = resp.json()["content"][0]["text"].strip()

        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)

        parsed = json.loads(raw.strip())

        VALID_SIGNALS = {"positive", "mild_positive", "neutral", "cautious", "negative"}
        SIGNAL_SCORES = {"positive": 5.0, "mild_positive": 2.0, "neutral": 0.0, "cautious": -2.0, "negative": -5.0}
        result = {}
        for sector in SECTOR_KEYWORDS:
            entry  = parsed.get(sector, {})
            signal = entry.get("signal", "neutral")
            if signal not in VALID_SIGNALS:
                signal = "neutral"
            result[sector] = {
                "score":   SIGNAL_SCORES[signal],
                "signal":  signal,
                "matches": len(sector_headlines.get(sector, [])),
                "reason":  entry.get("reason", "LLM verdict."),
            }

        n_nonneut = sum(1 for v in result.values() if v["signal"] != "neutral")
        print(f"  ✅ LLM scored 20 sectors ({n_nonneut} non-neutral)")
        return result

    except Exception as e:
        print(f"  ⚠️  LLM sector scoring failed: {e} — falling back to keyword scoring")
        return None


# ─────────────────────────────────────────────
# HISTORY BLENDING
# ─────────────────────────────────────────────

_SIGNAL_TO_INT = {"positive": 2, "mild_positive": 1, "neutral": 0, "cautious": -1, "negative": -2}
_SIGNAL_SCORES = {"positive": 5.0, "mild_positive": 2.0, "neutral": 0.0, "cautious": -2.0, "negative": -5.0}


def _int_to_signal(avg: float) -> str:
    if avg >= 1.5:   return "positive"
    if avg >= 0.5:   return "mild_positive"
    if avg <= -1.5:  return "negative"
    if avg <= -0.5:  return "cautious"
    return "neutral"


def blend_with_history(raw_signals: dict) -> dict:
    """
    Blend today's raw signals (40%) with the rolling history average (60%).
    If no history exists, returns raw_signals unchanged.
    History entries saved before today are used (today not yet appended).
    """
    history = []
    if os.path.exists(SWING_HISTORY_FILE):
        try:
            with open(SWING_HISTORY_FILE) as f:
                history = json.load(f)
        except Exception:
            pass

    today_str = str(date.today())
    past = [h for h in history if h.get("date") != today_str][-HISTORY_BLEND_DAYS:]

    if not past:
        print(f"  ℹ️  No history yet — using today's raw signals as-is")
        return raw_signals

    blended = {}
    for sector in SECTOR_KEYWORDS:
        today_int = _SIGNAL_TO_INT.get(raw_signals.get(sector, {}).get("signal", "neutral"), 0)
        hist_ints = [
            _SIGNAL_TO_INT.get(h.get("signals", {}).get(sector, {}).get("signal", "neutral"), 0)
            for h in past
        ]
        hist_avg = sum(hist_ints) / len(hist_ints)
        blended_val = HISTORY_BLEND_TODAY * today_int + HISTORY_BLEND_PAST * hist_avg
        signal = _int_to_signal(blended_val)
        raw = raw_signals.get(sector, {})
        blended[sector] = {
            "score":   _SIGNAL_SCORES[signal],
            "signal":  signal,
            "matches": raw.get("matches", 0),
            "reason":  (
                f"Blended: today={raw.get('signal','neutral')} "
                f"({HISTORY_BLEND_TODAY:.0%}), "
                f"{len(past)}-day hist avg={hist_avg:+.2f} "
                f"({HISTORY_BLEND_PAST:.0%}) → {signal}. "
                + raw.get("reason", "")
            )[:200],
        }

    n_changed = sum(
        1 for s in SECTOR_KEYWORDS
        if blended[s]["signal"] != raw_signals.get(s, {}).get("signal")
    )
    print(f"  🔀 Blended with {len(past)}-day history — {n_changed} sector(s) changed signal")
    return blended


# ─────────────────────────────────────────────
# KEYWORD SCORER (fallback)
# ─────────────────────────────────────────────

def score_headline(title: str, body: str, sector: str) -> float:
    """
    Score a single headline+body against a sector's keyword lists.
    Returns signed float: positive = bullish, negative = bearish.
    """
    kw   = SECTOR_KEYWORDS.get(sector, {})
    pos  = kw.get("positive", [])
    neg  = kw.get("negative", [])
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


def aggregate_sentiment(items: list) -> dict:
    """
    Score all items against all 20 sectors.
    Returns {sector: {score, signal, matches, reasons}} dict.
    """
    totals  = {s: 0.0 for s in SECTOR_KEYWORDS}
    counts  = {s: 0   for s in SECTOR_KEYWORDS}
    reasons = {s: []  for s in SECTOR_KEYWORDS}

    for item in items:
        title = item["title"]
        body  = item.get("body", "")
        for sector in SECTOR_KEYWORDS:
            s = score_headline(title, body, sector)
            if abs(s) > 0:
                totals[sector]  += s
                counts[sector]  += 1
                if abs(s) >= HEADLINE_WEIGHT:
                    direction = "↑" if s > 0 else "↓"
                    reasons[sector].append(
                        f"{direction} [{item['source']}] {title[:70]}"
                    )

    result = {}
    for sector in SECTOR_KEYWORDS:
        total   = round(totals[sector], 2)
        matches = counts[sector]

        if matches < MIN_MATCHES:
            signal = "neutral"
            reason = f"Only {matches} match(es) — below minimum {MIN_MATCHES}. No signal."
        else:
            if   total >=  5.0: signal = "positive"
            elif total >=  2.0: signal = "mild_positive"
            elif total <= -5.0: signal = "negative"
            elif total <= -2.0: signal = "cautious"
            else:               signal = "neutral"
            top    = reasons[sector][:3]
            reason = "; ".join(top) if top else "Matched — no headline-level signals."

        result[sector] = {
            "score":   total,
            "signal":  signal,
            "matches": matches,
            "reason":  reason,
        }

    return result


# ─────────────────────────────────────────────
# SAVE + POST
# ─────────────────────────────────────────────

def save_signals(signals: dict, items: list):
    import urllib.request as _ur
    os.makedirs(DATA_DIR, exist_ok=True)

    output = {
        "generated_at":    datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"),
        "scan_date":       str(date.today()),
        "scan_days":       SCAN_DAYS,
        "total_headlines": len(items),
        "signals":         signals,
        "sample_headlines": [
            {"title": i["title"], "date": i["date"], "source": i["source"]}
            for i in items[:15]
        ],
    }

    with open(SWING_SENTIMENT_FILE, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n  ✅ Signals saved: {SWING_SENTIMENT_FILE}")

    # POST to API
    upload_url = f"{API_URL}/signals/upload"
    print(f"  📤 POSTing signals to: {upload_url}")
    try:
        payload = json.dumps(
            {"type": "swing_news_sentiment", "payload": output},
            default=str
        ).encode("utf-8")
        req = _ur.Request(
            upload_url,
            data=payload,
            headers={"Content-Type": "application/json", **_UPLOAD_AUTH},
            method="POST"
        )
        with _ur.urlopen(req, timeout=12) as r:
            print(f"  ✅ Signals POSTed to API: {r.read().decode()}")
    except Exception as e:
        print(f"  ⚠️  Could not POST signals to {upload_url} ({e})")


# ─────────────────────────────────────────────
# MAIN SCAN
# ─────────────────────────────────────────────

SIGNAL_EMOJI = {
    "positive":     "🟢",
    "mild_positive":"🟡",
    "neutral":      "⚪",
    "cautious":     "🟠",
    "negative":     "🔴",
}


def run_scan(test_mode: bool = False):
    print(f"\n{'='*60}")
    print(f"  📰 SWING NEWS SENTIMENT — 20 NSE SECTORS")
    print(f"  {datetime.now(IST).strftime('%d %B %Y, %I:%M %p IST')}")
    print(f"  Window: {SCAN_DAYS} days | Min matches: {MIN_MATCHES} | LLM: {'enabled' if LLM_ENABLED else 'disabled'}")
    print(f"{'='*60}\n")

    print("  Fetching RSS feeds...\n")
    items = fetch_all_feeds()
    print(f"\n  Total headlines: {len(items)}")

    if not items:
        print("  ⚠️  No headlines — check RSS connectivity.")
        return None

    print(f"\n  Scoring {len(items)} headlines across 20 sectors...\n")
    raw_signals = None
    if LLM_ENABLED:
        print(f"  🤖 LLM sector analysis (Claude {LLM_MODEL})...")
        raw_signals = llm_score_sectors(items)
    if raw_signals is None:
        if LLM_ENABLED:
            print(f"  ↩️  Falling back to keyword scoring...")
        raw_signals = aggregate_sentiment(items)

    # Append RAW signals to rolling history (before blending)
    if not test_mode:
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            history = []
            if os.path.exists(SWING_HISTORY_FILE):
                with open(SWING_HISTORY_FILE) as f:
                    history = json.load(f)
            today_str = str(date.today())
            history = [h for h in history if h.get("date") != today_str]
            history.append({"date": today_str, "signals": raw_signals})
            history = history[-HISTORY_MAX_DAYS:]
            with open(SWING_HISTORY_FILE, "w") as f:
                json.dump(history, f, indent=2)
            print(f"  ✅ Raw signals saved to history: {len(history)} days in rolling window")
            # Also POST history to Railway API so other services (monthly screener)
            # can access it — volumes are not shared between Railway services
            try:
                import urllib.request as _ur2
                hist_payload = json.dumps(
                    {"type": "swing_sentiment_history", "payload": history},
                    default=str
                ).encode("utf-8")
                hist_req = _ur2.Request(
                    f"{API_URL}/signals/upload",
                    data=hist_payload,
                    headers={"Content-Type": "application/json", **_UPLOAD_AUTH},
                    method="POST"
                )
                with _ur2.urlopen(hist_req, timeout=12) as r:
                    print(f"  ✅ History POSTed to API ({len(history)} days)")
            except Exception as he:
                print(f"  ⚠️  History API POST failed (non-fatal): {he}")
        except Exception as e:
            print(f"  ⚠️  History append failed (non-fatal): {e}")

    # Blend today's raw with rolling history for stability
    print(f"\n  Blending today's signals with {HISTORY_BLEND_DAYS}-day history...")
    signals = blend_with_history(raw_signals)

    # Print results (blended)
    print(f"\n  {'SECTOR':<35} {'SIGNAL':<14} {'SCORE':>6}  {'MATCHES':>7}")
    print(f"  {'─'*35} {'─'*14} {'─'*6}  {'─'*7}")
    for sector, sig in sorted(signals.items()):
        emoji  = SIGNAL_EMOJI.get(sig["signal"], "⚪")
        label  = sig["signal"].replace("_", " ").title()
        print(f"  {sector:<35} {emoji} {label:<12} {sig['score']:>+6.1f}  {sig['matches']:>7}")

    # Show top reasons for non-neutral sectors
    print(f"\n  TOP SIGNALS:")
    for sector, sig in signals.items():
        if sig["signal"] not in ("neutral",) and sig["matches"] >= MIN_MATCHES:
            emoji = SIGNAL_EMOJI.get(sig["signal"], "⚪")
            print(f"\n  {emoji} {sector}")
            for line in sig["reason"].split(";")[:2]:
                if line.strip():
                    print(f"    {line.strip()[:80]}")

    if not test_mode:
        save_signals(signals, items)

    return signals


# ─────────────────────────────────────────────
# STATUS
# ─────────────────────────────────────────────

def show_status():
    if not os.path.exists(SWING_SENTIMENT_FILE):
        print("  No swing sentiment data. Run: python swing_news_sentiment.py")
        return

    with open(SWING_SENTIMENT_FILE) as f:
        data = json.load(f)

    print(f"\n{'='*60}")
    print(f"  📰 SWING SECTOR SENTIMENT")
    print(f"  Generated : {data.get('generated_at')}")
    print(f"  Headlines : {data.get('total_headlines', 0)}")
    print(f"{'='*60}")
    print(f"\n  {'SECTOR':<35} {'SIGNAL':<14} {'SCORE':>6}")
    print(f"  {'─'*35} {'─'*14} {'─'*6}")
    for sector, sig in sorted(data.get("signals", {}).items()):
        emoji = SIGNAL_EMOJI.get(sig["signal"], "⚪")
        label = sig["signal"].replace("_", " ").title()
        print(f"  {sector:<35} {emoji} {label:<12} {sig['score']:>+6.1f}")
    print(f"{'='*60}\n")


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Swing News Sentiment — NSE Sectors")
    parser.add_argument("--status", action="store_true", help="Show latest signals")
    parser.add_argument("--test",   action="store_true", help="Run without saving")
    args = parser.parse_args()

    if args.status:
        show_status()
    else:
        run_scan(test_mode=args.test)
