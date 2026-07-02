"""
ai_portfolio.py — shared helpers for the holdings-aware AI features
(ai_event_watch.py, ai_earnings.py).

Pulls together the three live portfolios (swing, India monthly, US),
gathers news from the RSS pipelines that already exist, matches headlines
to a specific holding by word-boundary, and provides a rolling dedup store
so the same news doesn't re-alert every day.

All read-only / advisory — nothing here trades.
"""

import os
import re
import json
import urllib.request as _req
from datetime import date, datetime, timedelta

API_URL  = os.getenv("API_URL", "https://web-production-50eee.up.railway.app").rstrip("/")
DATA_DIR = os.getenv("DATA_DIR", ".")

NAME_STOPWORDS = {"ltd", "limited", "the", "india", "industries", "corporation",
                  "company", "co", "and", "enterprises", "inc", "corp", "plc",
                  "holdings", "group", "international"}


# ─────────────────────────────────────────────
# HTTP
# ─────────────────────────────────────────────

def get_json(url):
    r = _req.Request(url, headers={"Accept": "application/json"})
    with _req.urlopen(r, timeout=25) as resp:
        return json.loads(resp.read())


def post_json(url, payload, token=None):
    body = json.dumps(payload, default=str).encode()
    h = {"Content-Type": "application/json"}
    if token:
        h["X-Upload-Token"] = token
    r = _req.Request(url, data=body, headers=h, method="POST")
    with _req.urlopen(r, timeout=25) as resp:
        return json.loads(resp.read())


def telegram(msg: str):
    bot, chat = os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
    if not bot or not chat:
        print("  ⚠️  Telegram not configured — skipping alert.")
        return
    try:
        post_json(f"https://api.telegram.org/bot{bot}/sendMessage",
                  {"chat_id": chat, "text": msg, "parse_mode": "HTML"})
    except Exception as e:
        print(f"  ⚠️  Telegram failed: {e}")


# ─────────────────────────────────────────────
# HOLDINGS (across all three portfolios)
# ─────────────────────────────────────────────

def _flatten_buckets(raw) -> list:
    out = []
    if isinstance(raw, dict):
        for b in raw.values():
            if isinstance(b, dict):
                out.extend(b.get("stocks") or [])
    elif isinstance(raw, list):
        out = raw
    return out


def get_all_holdings() -> list:
    """Every live position across swing / India monthly / US, normalised to
    {ticker, name, market, buy_price, buy_date, shares, ...original}."""
    holdings = []
    sources = [
        ("/swing/live",         "swing", "IN"),
        ("/portfolio/live",     "india", "IN"),
        ("/us/portfolio/live",  "us",    "US"),
    ]
    for ep, book, market in sources:
        try:
            raw = get_json(f"{API_URL}{ep}")
        except Exception as e:
            print(f"  ⚠️  Could not fetch {ep}: {e}")
            continue
        for s in _flatten_buckets(raw):
            if not s.get("ticker"):
                continue
            holdings.append({
                **s,
                "book":       book,
                "market":     market,
                "ticker":     s["ticker"],
                "name":       s.get("name", s["ticker"]),
                "buy_price":  s.get("buy_price") or s.get("price"),
                "buy_date":   s.get("buy_date") or s.get("entry_date"),
                "shares":     s.get("shares") or s.get("approx_shares"),
            })
    return holdings


# ─────────────────────────────────────────────
# NEWS (reuse the existing RSS pipelines)
# ─────────────────────────────────────────────

def gather_news() -> dict:
    """{"IN": [items], "US": [items]} from the sentiment pipelines. Each item
    is {title, body, ...}. Failures degrade to an empty list for that market."""
    news = {"IN": [], "US": []}
    try:
        from swing_news_sentiment import fetch_all_feeds as _in_feeds
        news["IN"] = _in_feeds()
        print(f"  📰 IN news: {len(news['IN'])} items")
    except Exception as e:
        print(f"  ⚠️  IN news fetch failed: {e}")
    try:
        from news_sentiment_us import fetch_all_feeds as _us_feeds
        news["US"] = _us_feeds()
        print(f"  📰 US news: {len(news['US'])} items")
    except Exception as e:
        print(f"  ⚠️  US news fetch failed: {e}")
    return news


def search_terms(name: str, ticker: str) -> list:
    """Distinctive lowercase words/phrases for headline matching."""
    base = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    words = [w for w in base.split() if len(w) > 3 and w not in NAME_STOPWORDS]
    terms = []
    if words:
        terms.append(" ".join(words[:2]))
        terms += [w for w in words if len(w) >= 5][:3]
    return list(dict.fromkeys(terms))


def match_headlines(name: str, ticker: str, news: dict, market: str,
                    limit: int = 15) -> list:
    """Headlines (title strings) mentioning this holding, word-boundary matched."""
    terms = search_terms(name, ticker)
    if not terms:
        return []
    patterns = [re.compile(r"\b" + re.escape(t) + r"\b") for t in terms]
    items = news.get(market, [])
    hits = []
    for it in items:
        text = (it.get("title", "") + " " + it.get("body", "")).lower()
        if any(p.search(text) for p in patterns):
            title = it.get("title", "").strip()
            if title and title not in hits:
                hits.append(title)
    return hits[:limit]


# ─────────────────────────────────────────────
# ROLLING DEDUP STORE
# ─────────────────────────────────────────────

def load_seen(fname: str, keep_days: int = 21) -> dict:
    """Load a {key: iso_date} store, pruning entries older than keep_days."""
    path = os.path.join(DATA_DIR, fname)
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return {}
    cutoff = (date.today() - timedelta(days=keep_days)).isoformat()
    return {k: v for k, v in data.items() if str(v) >= cutoff}


def save_seen(fname: str, seen: dict):
    path = os.path.join(DATA_DIR, fname)
    try:
        with open(path, "w") as f:
            json.dump(seen, f)
    except Exception as e:
        print(f"  ⚠️  Could not persist dedup store {fname}: {e}")


def headline_key(ticker: str, headline: str) -> str:
    norm = re.sub(r"\s+", " ", headline.lower()).strip()[:80]
    return f"{ticker}::{norm}"
