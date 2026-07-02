"""
ai_common.py — thin shared client for Anthropic Messages API calls.

Both AI features (ai_attribution.py, ai_redteam.py) use this instead of
re-implementing the raw-requests pattern that was copy-pasted across
swing_news_sentiment.py / screener.py / llm_synthesiser_us.py.

Design notes:
  - No SDK dependency — plain requests, same as the rest of the repo.
  - call_claude() returns the raw text; call_claude_json() parses a JSON
    object out of it (tolerating markdown fences and preamble).
  - Every call is best-effort: on any failure it returns None (JSON) or
    "" (text) so callers degrade gracefully rather than crash a cron.
  - AI output in this system is ADVISORY ONLY — nothing here places or
    cancels orders.
"""

import os
import re
import json
import time

import requests

ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Match the model tiers already used elsewhere in the repo.
MODEL_REASONING = "claude-sonnet-4-6"          # analysis / synthesis
MODEL_FAST      = "claude-haiku-4-5-20251001"  # cheap per-item classification

AI_ENABLED = bool(ANTHROPIC_KEY)


def call_claude(prompt: str, model: str = MODEL_FAST, max_tokens: int = 1024,
                system: str = None, retries: int = 2, timeout: int = 60) -> str:
    """Single-turn call. Returns response text, or "" on failure."""
    if not ANTHROPIC_KEY:
        print("  ⚠️  ANTHROPIC_API_KEY not set — AI call skipped.")
        return ""
    body = {
        "model":      model,
        "max_tokens": max_tokens,
        "messages":   [{"role": "user", "content": prompt}],
    }
    if system:
        body["system"] = system

    for attempt in range(retries + 1):
        try:
            resp = requests.post(
                ANTHROPIC_API,
                headers={
                    "x-api-key":         ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type":      "application/json",
                },
                json=body,
                timeout=timeout,
            )
            # Retry on transient rate-limit / overload
            if resp.status_code in (429, 500, 529) and attempt < retries:
                wait = 2 ** attempt
                print(f"  ⏳ Claude {resp.status_code} — retrying in {wait}s")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()["content"][0]["text"].strip()
        except Exception as e:
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            print(f"  ⚠️  Claude call failed: {e}")
            return ""
    return ""


def _strip_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
    return raw.strip()


def call_claude_json(prompt: str, model: str = MODEL_FAST, max_tokens: int = 1024,
                     system: str = None, retries: int = 2):
    """Call Claude and parse a JSON object from the reply. Returns the
    parsed object, or None on failure. Tolerates markdown fences and any
    prose around the JSON by extracting the outermost {...}."""
    raw = call_claude(prompt, model=model, max_tokens=max_tokens,
                      system=system, retries=retries)
    if not raw:
        return None
    txt = _strip_fences(raw)
    try:
        return json.loads(txt)
    except Exception:
        # Fall back to the outermost brace-delimited block
        start, end = txt.find("{"), txt.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(txt[start:end + 1])
            except Exception:
                pass
    print(f"  ⚠️  Could not parse JSON from Claude reply: {txt[:120]}...")
    return None
