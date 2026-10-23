"""Telegram delivery that survives the text it is given.

Every script here builds its message as HTML — <b>, <i> — and then
interpolates values straight in. Telegram parses that strictly, so a
single stray character rejects the whole message with HTTP 400 and the
alert is simply lost. Two ways that happens in practice:

  * Model-written text. The attribution analyst wrote "The <65 score
    band", Telegram read "<6" as the start of a tag, and the analysis
    never arrived on 1 October 2026 — it only reached the dashboard.
  * Ticker symbols. NSE has M&M, J&KBANK, ARE&M, GVT&D. "<b>M&M</b>"
    contains "&M", which is not a valid entity, so an entry or exit
    alert for one of those stocks fails the same way.

esc() is for interpolated values: escape them and the surrounding tags
still work. send() is the backstop — on a parse failure it retries
without any markup, so a malformed message degrades to plain text
instead of disappearing. Losing formatting is a nuisance; losing a
fill notification is not.
"""
import html
import json
import os
import re
import urllib.error
import urllib.request

LIMIT = 4096          # Telegram's hard cap on message length


def esc(value) -> str:
    """Escape an interpolated value so it cannot break the markup."""
    return html.escape(str(value), quote=False)


def _strip_tags(msg: str) -> str:
    """Our own markup out, everything else escaped — a plain-text fallback."""
    bare = re.sub(r"</?(?:b|i|u|s|code|pre|a)(?:\s[^>]*)?>", "", msg)
    return html.unescape(bare)


def _post(token: str, chat: str, text: str, html_mode: bool):
    body = {"chat_id": chat, "text": text[:LIMIT]}
    if html_mode:
        body["parse_mode"] = "HTML"
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=15).close()


def send(msg: str, token: str = "", chat: str = "") -> bool:
    """Send a message, falling back to plain text if the markup is rejected.

    Returns True when Telegram accepted it. Never raises — a failed
    alert must not take down the job that was trying to report.
    """
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat  = chat  or os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        print("  ⚠️  Telegram not configured — skipping alert.")
        return False

    try:
        _post(token, chat, msg, html_mode=True)
        return True
    except urllib.error.HTTPError as e:
        if e.code != 400:
            print(f"  ⚠️  Telegram failed: HTTP {e.code}")
            return False
        # 400 is Telegram refusing to parse the markup. The content is
        # still worth delivering.
        print("  ↩  Telegram rejected the markup — resending as plain text")
    except Exception as e:
        print(f"  ⚠️  Telegram failed: {e}")
        return False

    try:
        _post(token, chat, _strip_tags(msg), html_mode=False)
        return True
    except Exception as e:
        print(f"  ⚠️  Telegram failed even as plain text: {e}")
        return False
