"""
Policy Scraper — RBI + PIB Press Release Monitor (5.0)
========================================================
Scrapes RBI and PIB press releases weekly, classifies them
across 20 NSE sectors using LLM (Claude Haiku) as the primary
classifier with keyword matching as fallback, and saves signals
to policy_signals.json.

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

# Each sector has positive and negative keyword lists.
# Multi-word phrases are matched as substrings (case-insensitive).
# Phrases listed first have higher priority.

SECTOR_POLICY_KEYWORDS = {
    "Financial Services": {
        "positive": [
            "repo rate cut", "rate cut", "reduces repo", "rate reduction",
            "accommodative", "liquidity infusion", "credit growth",
            "crr cut", "crr reduction", "npa reduction", "npa declined",
            "bad loan recovery", "bank recapitalisation", "bank recap",
            "digital banking", "upi", "fintech", "insurance fdi",
            "rupee strengthens", "inr strengthens", "rupee stable",
            "credit offtake", "loan growth", "deposit growth",
            "rbi policy", "monetary policy", "rbi circular",
            "bank profit", "banking sector", "credit offtake",
            "payment system", "neft", "rtgs", "imps",
            "microfinance", "priority sector lending", "financial inclusion",
        ],
        "negative": [
            "repo rate hike", "rate hike", "raises repo", "tightening",
            "withdrawal of accommodation", "hawkish",
            "crr hike", "crr increase",
            "npa increase", "npa rise", "bad loans surge",
            "bank fraud", "penalty on bank", "rbi penalty",
            "banking crisis", "bank collapse", "credit crunch",
            "rupee falls", "rupee weakens", "inr depreciates",
            "pca framework", "prompt corrective action",
        ],
    },

    "Information Technology": {
        "positive": [
            "it exports", "software exports", "digital india",
            "pli electronics", "pli semiconductor", "semiconductor",
            "electronics manufacturing", "sez notification",
            "it hiring", "tech deal", "software deal", "it order",
            "data centre policy", "cloud policy", "ai policy",
            "national data governance", "digital public infrastructure",
            "meity", "it ministry", "chips mission",
            "export incentive it", "seis scheme",
        ],
        "negative": [
            "data localisation", "data localisation mandate",
            "h1b visa", "h1b restriction", "visa restriction",
            "it slowdown", "tech slowdown",
            "cybersecurity regulation", "data privacy penalty",
            "it layoffs", "tech layoffs",
            "digital services tax", "equalisation levy hike",
        ],
    },

    "Oil Gas And Consumable Fuels": {
        "positive": [
            "crude import duty cut", "fuel subsidy", "lpg subsidy",
            "oil price falls", "crude falls", "brent falls",
            "gas price revision upward", "upstream exploration",
            "new oil block", "oalp round", "dsf round",
            "petroleum export", "lng terminal",
            "windfall tax removal", "windfall tax abolished",
            "city gas distribution", "cng png expansion",
        ],
        "negative": [
            "windfall tax", "windfall tax imposed",
            "crude import duty hike", "crude surcharge",
            "oil price surge", "crude surge", "brent surge",
            "lpg price hike", "fuel price hike",
            "gas price cut", "gas price reduction",
            "upstream policy uncertainty", "moratorium exploration",
            "cess on crude", "additional cess petroleum",
        ],
    },

    "Fast Moving Consumer Goods": {
        "positive": [
            "msp increase", "minimum support price", "kharif msp", "rabi msp",
            "rural income", "rural demand", "rural recovery",
            "monsoon forecast", "normal monsoon", "good rainfall",
            "direct benefit transfer", "dbt", "pm kisan",
            "gst reduction food", "gst exemption food",
            "food inflation eases", "vegetable prices fall",
            "consumer demand", "fmcg volume", "wage growth",
            "rural employment", "mgnrega allocation",
        ],
        "negative": [
            "drought", "below normal monsoon", "deficit monsoon",
            "food inflation", "commodity inflation",
            "palm oil duty", "edible oil price rise",
            "rural distress", "consumer slowdown",
            "fmcg volume decline", "input cost pressure",
            "gst hike food", "gst imposed food",
            "raw material cost", "inflation rural",
        ],
    },

    "Healthcare": {
        "positive": [
            "drug approval", "cdsco approval", "fda approval",
            "api pli", "pli pharma", "pharmaceutical pli",
            "healthcare budget", "health outlay", "health capex",
            "jan aushadhi", "generic drug", "pharma export",
            "medical device approval", "medical device pli",
            "nppa price revision upward", "drug price increase allowed",
            "ayushman bharat", "health insurance",
            "bulk drug park", "medical college",
        ],
        "negative": [
            "drug price control", "dpco", "nppa price cap", "price cap pharma",
            "fda import alert", "fda warning letter",
            "drug recall", "pharma penalty",
            "quality control order pharma", "export ban pharma",
            "raw material shortage api", "china api dependency",
            "healthcare budget cut", "health outlay reduced",
        ],
    },

    "Automobile and Auto Components": {
        "positive": [
            "fame subsidy", "fame scheme", "ev subsidy",
            "ev policy", "electric vehicle policy", "pli auto",
            "pli automobile", "scrappage policy", "vehicle scrappage",
            "auto pli", "auto component pli",
            "ev charging infrastructure", "charging station policy",
            "green mobility", "hybrid vehicle incentive",
            "auto sales growth", "passenger vehicle demand",
            "commercial vehicle demand",
        ],
        "negative": [
            "fame subsidy cut", "ev subsidy cut", "fame scheme cut",
            "bs emission norms tightened", "bs7", "emission norms",
            "auto sector slowdown", "vehicle sales decline",
            "chip shortage", "semiconductor shortage auto",
            "import duty auto parts hike", "auto import duty",
            "fuel economy norms", "cafe norms stricter",
        ],
    },

    "Capital Goods": {
        "positive": [
            "infrastructure capex", "capital expenditure", "capex boost",
            "pli capital goods", "pli machinery",
            "defence procurement", "defence capex", "defence outlay",
            "make in india", "atmanirbhar", "indigenous manufacturing",
            "nip national infrastructure pipeline", "pm gati shakti",
            "railway capex", "road construction", "port development",
            "airport development", "smart city",
            "government spending infra", "infra outlay",
            "hal order", "bel order", "drdo",
        ],
        "negative": [
            "capex reduction", "infrastructure delay",
            "project cancellation", "fiscal consolidation",
            "spending cut", "infra project stalled",
            "defence budget cut", "defence cut",
            "import capital goods hike", "tariff capital goods",
            "construction slowdown",
        ],
    },

    "Metals And Mining": {
        "positive": [
            "steel import duty", "anti-dumping steel", "anti-dumping metals",
            "mining policy liberalised", "mining auction",
            "coal block auction", "coal allocation",
            "iron ore export duty removed", "mineral royalty reduced",
            "infrastructure demand metals", "construction demand steel",
            "steel export incentive", "metal export promotion",
            "domestic steel demand", "aluminium demand",
        ],
        "negative": [
            "steel export duty", "iron ore export duty",
            "mining moratorium", "mining ban",
            "royalty increase", "mineral royalty hike",
            "coal import duty", "coke import duty",
            "anti-dumping revoked", "steel import surge",
            "construction slowdown metals", "infra slowdown metals",
            "windfall tax metals", "excess profit levy",
        ],
    },

    "Consumer Durables": {
        "positive": [
            "electronics pli", "pli electronics",
            "anti-dumping duty imports electronics",
            "anti-dumping on imports consumer",
            "bis certification relaxed", "import substitution electronics",
            "energy efficiency incentive", "star rating subsidy",
            "consumer durables demand", "real estate demand durables",
            "rural electrification", "household income rise",
            "festive demand", "urban consumption",
        ],
        "negative": [
            "anti-dumping revoked electronics", "import duty cut electronics",
            "bis norms tightened", "quality control order strict",
            "energy efficiency mandate cost", "bcd reduced electronics",
            "consumer durables slowdown", "demand slowdown durables",
            "input cost rise electronics", "commodity inflation durables",
        ],
    },

    "Chemicals": {
        "positive": [
            "chemical pli", "pli chemicals", "specialty chemical",
            "anti-dumping china chemicals", "anti-dumping duty chemicals",
            "agrochemical export", "pesticide export",
            "petrochemical capacity", "downstream petrochemical",
            "chemical park", "plastic park",
            "fertiliser subsidy", "fertiliser availability",
            "chemical import substitution",
        ],
        "negative": [
            "anti-dumping revoked chemicals", "chemical import surge",
            "petrochemical import duty cut",
            "agrochemical ban", "pesticide ban",
            "environmental norms chemical", "pollution penalty chemical",
            "fertiliser subsidy cut", "fertiliser price hike",
            "raw material chemical import duty hike",
        ],
    },

    "Construction Materials": {
        "positive": [
            "pmay", "pradhan mantri awas yojana",
            "infrastructure demand cement", "cement demand infrastructure",
            "housing project", "affordable housing",
            "road construction cement", "rera reform",
            "sand policy liberalised", "construction activity",
            "smart city cement", "metro rail construction",
            "government housing scheme", "urban housing",
        ],
        "negative": [
            "cement import", "cement import duty cut",
            "sand mining ban", "sand policy strict",
            "rera penalty", "housing project delay",
            "construction slowdown", "real estate slowdown",
            "input cost cement", "energy cost cement",
            "fly ash norms", "environmental norms construction",
        ],
    },

    "Power": {
        "positive": [
            "renewable target", "solar target", "wind target",
            "rpo renewable purchase obligation",
            "green hydrogen", "green hydrogen mission",
            "solar tariff competitive", "solar park",
            "coal supply power", "coal linkage",
            "electricity amendment", "power sector reform",
            "transmission expansion", "power grid investment",
            "pm kusum", "pm surya ghar", "rooftop solar",
            "battery storage policy", "pumped hydro",
            "nuclear power", "clean energy",
        ],
        "negative": [
            "coal shortage power", "coal supply disruption",
            "rpo target missed", "renewable curtailment",
            "grid curtailment", "power sector stress",
            "electricity tariff cap", "discom losses",
            "coal import duty hike power",
            "renewable project delay", "solar duty hike",
            "basic customs duty solar", "bcd solar modules",
            "power subsidy burden", "tariff revision delayed",
        ],
    },

    "Telecommunication": {
        "positive": [
            "spectrum auction", "5g rollout", "5g spectrum",
            "agr relief", "agr dues moratorium",
            "telecom pli", "pli telecom", "telecom equipment pli",
            "bharatnet", "broadband rural", "digital connectivity",
            "fdi telecom", "telecom fdi increase",
            "telecom reform", "iuc interconnect",
            "satellite broadband policy", "4g 5g expansion",
        ],
        "negative": [
            "agr dues", "agr liability",
            "spectrum fee hike", "licence fee hike",
            "telecom tax hike", "usof levy",
            "import duty telecom equipment",
            "data localisation telecom",
            "ott regulation burden", "internet shutdown",
            "telecom slowdown", "arpu pressure",
        ],
    },

    "Consumer Services": {
        "positive": [
            "gst reduction services", "gst exemption services",
            "tourism policy", "tourism incentive", "travel promotion",
            "urban employment", "urban jobs", "urban income",
            "e-commerce policy favourable", "online retail growth",
            "hospitality demand", "hotel occupancy",
            "consumer confidence", "services sector growth",
            "food delivery regulation light", "qsr expansion",
        ],
        "negative": [
            "gst hike services", "service tax increase",
            "e-commerce regulation strict", "e-commerce compliance",
            "fdi e-commerce restriction", "marketplace rules strict",
            "urban unemployment", "urban job loss",
            "consumer confidence fall", "services slowdown",
            "tourism drop", "hospitality slowdown",
        ],
    },

    "Services And Logistics": {
        "positive": [
            "national logistics policy", "logistics policy",
            "pm gati shakti", "gati shakti",
            "dedicated freight corridor", "freight corridor",
            "warehousing policy", "cold chain",
            "multimodal logistics", "logistics park",
            "trade facilitation", "customs clearance faster",
            "express logistics", "air freight",
            "shipping policy", "coastal shipping",
        ],
        "negative": [
            "logistics cost rise", "freight rate hike",
            "customs delay", "port congestion",
            "trucking strike", "transport disruption",
            "fuel cost logistics", "diesel price hike logistics",
            "warehousing regulation strict",
            "trade barrier", "import restriction",
        ],
    },

    "Realty": {
        "positive": [
            "home loan rate cut", "home loan subsidy",
            "rera reform", "rera amendment favourable",
            "pmay", "pradhan mantri awas yojana",
            "stamp duty reduction", "stamp duty cut",
            "reit policy", "reits notification",
            "credit linked subsidy", "clss scheme",
            "affordable housing", "housing demand",
            "real estate fdi", "realty investment",
            "infrastructure status real estate",
        ],
        "negative": [
            "rera penalty", "rera action developer",
            "home loan rate hike", "mortgage rate hike",
            "stamp duty increase", "property tax hike",
            "housing project delay", "real estate slowdown",
            "npa real estate", "stressed realty",
            "unsold inventory", "demand slowdown realty",
        ],
    },

    "Diversified And Infrastructure": {
        "positive": [
            "infra budget", "infra capex", "budget capex allocation",
            "nip national infrastructure pipeline",
            "highway construction", "nhai",
            "metro rail", "bullet train", "railway expansion",
            "port development", "airport development",
            "power grid", "transmission line",
            "ppp policy", "public private partnership",
            "smart city", "urban infra",
            "rvnl order", "l&t order", "engineers india",
        ],
        "negative": [
            "infra spending cut", "capex cut budget",
            "infra project stalled", "project delay",
            "fiscal deficit concern", "spending restraint",
            "ppp dispute", "arbitration infra",
            "land acquisition delay", "environment clearance delay",
        ],
    },

    "Textiles And Apparel": {
        "positive": [
            "textile pli", "pli textiles", "pli mmt",
            "cotton msp increase", "cotton msp",
            "tufs technology upgradation", "tufs scheme",
            "man-made fibre policy", "mmt policy",
            "textile export promotion", "apparel export",
            "garment export incentive", "rosl scheme",
            "mega textile park", "pm mitra",
            "man made fibre", "technical textile",
        ],
        "negative": [
            "cotton msp burden", "cotton price high",
            "textile import surge", "apparel import",
            "anti-dumping revoked textile",
            "export ban cotton", "cotton export restriction",
            "textile demand slowdown", "apparel demand drop",
            "power cost textile", "labour cost textile",
        ],
    },

    "Media And Entertainment": {
        "positive": [
            "ott policy favourable", "ott regulation light",
            "media fdi increase", "broadcasting fdi",
            "digital media growth", "streaming policy",
            "film incentive", "film production subsidy",
            "content export promotion", "media export",
            "ibc broadcasting liberalised",
        ],
        "negative": [
            "ott regulation strict", "ott content regulation",
            "media fdi cap", "fdi restriction media",
            "broadcasting regulation", "cable tv regulation",
            "content censorship", "content restriction",
            "digital media tax", "advertising tax",
            "media slowdown", "ad revenue decline",
        ],
    },

    "Paper And Forest Products": {
        "positive": [
            "paper import duty", "anti-dumping paper",
            "newsprint import duty hike",
            "forest policy favourable", "agroforestry policy",
            "recycling norms relaxed", "paper recycling incentive",
            "pulp import duty cut", "raw material paper cheaper",
            "plantation policy", "bamboo policy",
        ],
        "negative": [
            "paper import duty cut", "newsprint duty cut",
            "anti-dumping revoked paper",
            "forest clearance strict", "forest conservation act",
            "recycling mandate strict", "extended producer responsibility",
            "pulp import duty hike", "raw material paper costly",
            "paper demand decline", "digital substitution",
        ],
    },
}

# Scoring weights
HEADLINE_WEIGHT = 2.0   # headlines are more important than body text
BODY_WEIGHT     = 1.0
PHRASE_BONUS    = 0.5   # multi-word phrases score slightly higher than single words

# Backward-compatibility alias — news_sentiment.py imports BUCKET_KEYWORDS
BUCKET_KEYWORDS = SECTOR_POLICY_KEYWORDS


# ─────────────────────────────────────────────
# SCORING FUNCTION
# ─────────────────────────────────────────────

def score_release(title: str, body: str = "") -> dict:
    """
    Score a single press release against all sector keywords.

    Returns {sector_key: score} where:
      positive score → good for sector
      negative score → bad for sector
      0 → not relevant
    """
    title_lower = title.lower()
    body_lower  = body.lower()
    scores      = {k: 0.0 for k in SECTOR_POLICY_KEYWORDS}

    for sector, keywords in SECTOR_POLICY_KEYWORDS.items():
        sector_score = 0.0

        # ── Positive keywords ─────────────────────────────────
        for phrase in keywords["positive"]:
            phrase_l    = phrase.lower()
            is_multi    = " " in phrase_l
            word_bonus  = PHRASE_BONUS if is_multi else 0.0

            if phrase_l in title_lower:
                sector_score += HEADLINE_WEIGHT + word_bonus
            elif phrase_l in body_lower:
                sector_score += BODY_WEIGHT + word_bonus

        # ── Negative keywords ─────────────────────────────────
        for phrase in keywords["negative"]:
            phrase_l   = phrase.lower()
            is_multi   = " " in phrase_l
            word_bonus = PHRASE_BONUS if is_multi else 0.0

            if phrase_l in title_lower:
                sector_score -= HEADLINE_WEIGHT + word_bonus
            elif phrase_l in body_lower:
                sector_score -= BODY_WEIGHT + word_bonus

        # ── Neutral override — suppress false positives ───────
        for phrase in keywords.get("neutral_override", []):
            if phrase.lower() in title_lower or phrase.lower() in body_lower:
                sector_score = 0.0  # cancel score if override phrase found
                break

        scores[sector] = round(sector_score, 2)

    return scores


def aggregate_scores(releases: list) -> dict:
    """
    Aggregate scores from multiple releases into a final
    signal per sector.

    Returns:
    {
      sector_key: {
        "score":   float,         # net score across all releases
        "signal":  str,           # positive | mild_positive | neutral | cautious | negative
        "reason":  str,           # top contributing release headline
        "releases_matched": int,  # how many releases were relevant
      }
    }
    """
    sector_totals  = {k: 0.0 for k in SECTOR_POLICY_KEYWORDS}
    sector_reasons = {k: [] for k in SECTOR_POLICY_KEYWORDS}
    sector_counts  = {k: 0  for k in SECTOR_POLICY_KEYWORDS}

    for r in releases:
        scores = score_release(r["title"], r.get("body", ""))
        for sector, score in scores.items():
            if abs(score) > 0:
                sector_totals[sector]  += score
                sector_counts[sector] += 1
                if abs(score) >= HEADLINE_WEIGHT:
                    sector_reasons[sector].append(
                        f"{'↑' if score > 0 else '↓'} {r['title'][:80]}"
                    )

    result = {}
    for sector in SECTOR_POLICY_KEYWORDS:
        total = round(sector_totals[sector], 2)

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

        top_reasons = sector_reasons[sector][:3]  # top 3 relevant releases
        reason = (
            "; ".join(top_reasons)
            if top_reasons
            else "No significant policy events in last 14 days"
        )

        result[sector] = {
            "score":            total,
            "signal":           signal,
            "reason":           reason,
            "releases_matched": sector_counts[sector],
        }

    return result


# ─────────────────────────────────────────────
# LLM CLASSIFICATION
# ─────────────────────────────────────────────

ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
LLM_MODEL     = "claude-haiku-4-5-20251001"


def llm_classify_releases(releases: list) -> dict | None:
    """
    Use Claude Haiku to classify releases across 20 NSE sectors.
    Primary classifier when ANTHROPIC_API_KEY is set.
    Returns {sector: {score, signal, reason}} or None on failure/no key.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key or not releases:
        return None

    # Build numbered release list (cap at 40 for token budget)
    release_lines = []
    for i, r in enumerate(releases[:40], 1):
        src = r.get("ministry") or r.get("source", "")
        release_lines.append(f"{i}. [{src}] {r['title'][:120]}")
    releases_text = "\n".join(release_lines)

    sectors_list = "\n".join(f"- {s}" for s in SECTOR_POLICY_KEYWORDS)

    prompt = f"""You are a senior Indian equity macro analyst. Analyze these RBI and Government of India (PIB) press releases and rate their STOCK MARKET impact on 20 NSE sectors.

Press releases from the last {SCAN_DAYS} days:
{releases_text}

SECTOR MAPPING GUIDE — use this to connect policy to sectors:
- RBI repo rate cut / liquidity easing → Financial Services +, Realty +
- RBI repo rate hike / tightening → Financial Services cautious, Realty cautious
- PLI scheme, production incentive → relevant manufacturing sector +
- Import duty hike on a product → that sector + (protects domestic players)
- Import duty cut → that sector cautious (more competition)
- MSP hike, farm loan waiver → Fast Moving Consumer Goods +, Chemicals (fertiliser) +
- FAME subsidy, EV policy, scrappage → Automobile and Auto Components +
- Renewable energy target, solar/wind capacity → Power +
- Budget capex, infra spend, road/highway/metro/port → Capital Goods +, Diversified And Infrastructure +, Construction Materials +
- Defence procurement, Make in India manufacturing → Capital Goods +
- Telecom spectrum auction, 5G rollout, AGR relief → Telecommunication +
- Healthcare scheme, drug pricing (DPCO) → Healthcare (check direction)
- Crude oil import, refinery policy → Oil Gas And Consumable Fuels (check direction)
- IT export, BPO, digital policy → Information Technology (check direction)
- Housing scheme (PMAY), stamp duty cut → Realty +, Construction Materials +
- Chemical park, petrochemical hub → Chemicals +
- Textile PLI, export incentive → Textiles And Apparel +

Rate EACH of the 20 sectors:
{sectors_list}

Scoring rules:
- score: -10 (very negative) to +10 (very positive)
- Assign a non-zero score whenever a release plausibly affects that sector — even indirectly
- score=0 ONLY when there is genuinely NO connection
- signal: "positive" (score>=4), "mild_positive" (1.5 to 4), "neutral" (-1.5 to 1.5), "cautious" (-4 to -1.5), "negative" (score<=-4)
- reason: cite specific release number(s) like "[3]" that drove the score; write "None" if truly no match

Respond with ONLY valid JSON — no markdown, no explanation:
{{"Financial Services": {{"score": 2.5, "signal": "mild_positive", "reason": "[1] RBI rate cut boosts lending margins"}}, "Information Technology": {{...}}, ...all 20 sectors...}}"""

    try:
        resp = requests.post(
            ANTHROPIC_API,
            headers={
                "Content-Type":      "application/json",
                "x-api-key":         api_key,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model":      LLM_MODEL,
                "max_tokens": 1800,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=40,
        )
        resp.raise_for_status()
        raw = resp.json()["content"][0]["text"].strip()
        # strip markdown fences if present
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
        result = json.loads(raw)
        # Validate — must have at least half the sectors
        if len(result) >= 10:
            print(f"  🤖 LLM classified {len(result)} sectors")
            return result
    except Exception as e:
        print(f"  ⚠️  LLM classification failed: {e} — falling back to keywords")
    return None


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

# Key ministries relevant to our sectors
PIB_MINISTRY_URLS = {
    "Finance":          "https://pib.gov.in/allRel.aspx?relid=&mnid=2",
    "Defence":          "https://pib.gov.in/allRel.aspx?relid=&mnid=7",
    "Renewable Energy": "https://pib.gov.in/allRel.aspx?relid=&mnid=69",
    "Health":           "https://pib.gov.in/allRel.aspx?relid=&mnid=24",
    "Commerce":         "https://pib.gov.in/allRel.aspx?relid=&mnid=6",
    "Industry":         "https://pib.gov.in/allRel.aspx?relid=&mnid=19",
    "Agriculture":      "https://pib.gov.in/allRel.aspx?relid=&mnid=1",
    "Petroleum":        "https://pib.gov.in/allRel.aspx?relid=&mnid=46",
    "Telecom":          "https://pib.gov.in/allRel.aspx?relid=&mnid=64",
    "Road Transport":   "https://pib.gov.in/allRel.aspx?relid=&mnid=56",
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

            # Try selectors from most-specific to broadest.
            # PIB allRel.aspx pages use a table layout on the right pane.
            candidate_items = (
                soup.select(".innner-page-main-about-us-content-right-part td") or
                soup.select(".innner-page-main-about-us-content-right-part li") or
                soup.select(".release-list li") or
                soup.select("ul.list li") or
                soup.select("table td") or
                soup.select("li")
            )

            ministry_count = 0
            sample_titles  = []   # for diagnostic logging

            for item in candidate_items:
                text     = item.get_text(separator=" ", strip=True)
                link_tag = item.find("a")
                title    = link_tag.get_text(strip=True) if link_tag else text[:120]
                url_link = ""

                if link_tag and link_tag.get("href"):
                    href     = link_tag["href"]
                    url_link = href if href.startswith("http") \
                               else f"https://pib.gov.in{href}"

                # Require a real parseable date — skip nav items (they have no dates)
                parsed_date = _extract_date_from_text(text)
                if parsed_date is None:
                    continue
                if parsed_date < cutoff:
                    continue

                if title and len(title) > 20:
                    all_releases.append({
                        "title":    title,
                        "date":     str(parsed_date),
                        "url":      url_link,
                        "source":   "PIB",
                        "ministry": ministry,
                    })
                    ministry_count += 1
                    if len(sample_titles) < 2:
                        sample_titles.append(f'"{title[:70]}"')
                    if ministry_count >= MAX_RELEASES:
                        break

            if sample_titles:
                print(f"    📋 PIB {ministry}: {ministry_count} releases — e.g. {', '.join(sample_titles)}", flush=True)
            else:
                print(f"    📋 PIB {ministry}: 0 releases (no dated items found — page structure may have changed)", flush=True)
            time.sleep(0.5)  # polite delay between ministry pages

        except Exception as e:
            print(f"    ⚠️  PIB {ministry} scrape failed: {e}", flush=True)

    return all_releases

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
    _post_signal_to_api("policy_signals", output)


def _post_signal_to_api(signal_type: str, payload: dict):
    """POST signal data to the web API so the dashboard can read it."""
    import urllib.request as _urllib
    import os as _os
    api_url = _os.getenv("API_URL", "https://web-production-2d832.up.railway.app")
    url = f"{api_url}/signals/upload"
    try:
        import json as _json
        body = _json.dumps({"type": signal_type, "payload": payload}).encode("utf-8")
        req = _urllib.Request(url, data=body,
                              headers={"Content-Type": "application/json"},
                              method="POST")
        with _urllib.urlopen(req, timeout=10) as resp:
            print(f"  ✅ {signal_type} POSTed to API: {resp.read().decode()}")
    except Exception as e:
        print(f"  ⚠️  Could not POST {signal_type} to API (non-fatal): {e}")


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
    3. Classify with LLM (primary) or keyword matching (fallback)
    4. Save signals
    """
    print(f"\n{'='*55}")
    print(f"  📜 POLICY SCRAPER — RBI + PIB SCAN")
    print(f"  {datetime.now(IST).strftime('%d %B %Y, %I:%M %p IST')}")
    print(f"  Scanning last {SCAN_DAYS} days...")
    print(f"{'='*55}\n")

    rbi_releases = scrape_rbi_releases()
    time.sleep(1)
    pib_releases = scrape_pib_releases()
    all_releases = rbi_releases + pib_releases
    total = len(all_releases)

    print(f"\n  Total releases fetched: {total}")
    if total == 0:
        print("  ⚠️  No releases fetched — check scraper connectivity.")
        return None

    # Diagnostic: show first 5 titles so we can verify scraper quality
    print(f"\n  📝 Sample release titles:", flush=True)
    for r in all_releases[:5]:
        src = r.get("ministry") or r.get("source", "")
        print(f"    [{r['date']}] [{src}] {r['title'][:90]}", flush=True)

    # Primary: LLM classification
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    print(f"\n  🔍 Classifying {total} releases ({'LLM + keywords' if api_key else 'keywords only'})...", flush=True)

    llm_signals = llm_classify_releases(all_releases)

    if llm_signals:
        # Convert LLM output to same format as aggregate_scores()
        signals = {}
        for sector in SECTOR_POLICY_KEYWORDS:
            llm = llm_signals.get(sector, {})
            signals[sector] = {
                "score":            round(float(llm.get("score", 0.0)), 2),
                "signal":           llm.get("signal", "neutral"),
                "reason":           llm.get("reason", "LLM classification"),
                "releases_matched": total,
                "source":           "llm",
            }
    else:
        # Fallback: keyword matching
        signals = aggregate_scores(all_releases)
        for s in signals.values():
            s["source"] = "keywords"

    # Print results
    print(f"\n  📊 POLICY SIGNALS ({len(signals)} sectors):")
    signal_emoji = {
        "positive":     "🟢",
        "mild_positive":"🟡",
        "neutral":      "⚪",
        "cautious":     "🟠",
        "negative":     "🔴",
    }
    _no_reason = {"No significant policy events in last 14 days",
                  "LLM classification", "No relevant releases", "None"}
    for sector, sig in signals.items():
        emoji  = signal_emoji.get(sig["signal"], "⚪")
        source = sig.get("source", "keywords")
        matched_str = (f"{sig['releases_matched']} releases"
                       if source == "keywords" else f"LLM [{total} releases scanned]")
        print(f"\n  {sector}", flush=True)
        print(f"    Signal:  {emoji} {sig['signal'].replace('_',' ').title()}  "
              f"(score: {sig['score']:+.1f})  [{source}]", flush=True)
        print(f"    Source:  {matched_str}", flush=True)
        reason = sig.get("reason", "")
        if reason and reason not in _no_reason:
            for line in reason.split(";")[:2]:
                if line.strip():
                    print(f"    {line.strip()}", flush=True)

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

    for sector, sig in data.get("signals", {}).items():
        emoji  = signal_emoji.get(sig["signal"], "⚪")
        source = sig.get("source", "keywords")
        print(f"\n  {sector}")
        print(f"    {emoji} {sig['signal'].replace('_',' ').title()}  "
              f"(score: {sig['score']:+.1f}, {sig['releases_matched']} releases, src: {source})")
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
