"""Report missing configuration instead of dying silently at import.

The cron scripts that place and cancel orders read their config with
os.environ[...] at module level:

    RAILWAY_URL     = os.environ["API_URL"]
    UPLOAD_TOKEN    = os.environ["UPLOAD_TOKEN"]
    EXECUTOR_SECRET = os.environ["EXECUTOR_SECRET"]

A missing name therefore raises KeyError during import — before main(),
before any try/except, and before the script has a chance to tell you.
cron redirects the traceback into a log file nobody reads, so the only
symptom is an alert that never arrives.

That is exactly what happened on 2026-10-23: UPLOAD_TOKEN went missing
from the VPS .env, india_entry died on its second line, and the 9:15
entry run was silent rather than reporting a problem. Had entries been
queued that morning, the orders would simply not have been placed, with
nothing to indicate it.

require_env() turns that into a Telegram message naming the missing
variable. TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are deliberately read
leniently here, so the notification path still works when the strict
config is what is broken.
"""
import json
import os
import sys
import urllib.request


def _notify(text: str) -> bool:
    """Best-effort Telegram. Never raises — the caller is already failing."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat  = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        return False
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=json.dumps({"chat_id": chat, "text": text,
                             "parse_mode": "HTML"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10).close()
        return True
    except Exception:
        return False


def require_env(*names: str):
    """Return the named environment variables, or report and exit.

    Reports EVERY missing name in one message rather than stopping at
    the first, so a single run tells you everything to fix instead of
    making you discover them one cron cycle at a time.

    An empty value counts as missing: a blank UPLOAD_TOKEN fails the
    same way an absent one does, and silently, so it is not worth
    distinguishing.
    """
    missing = [n for n in names if not os.environ.get(n)]
    if not missing:
        return tuple(os.environ[n] for n in names)

    script = os.path.basename(sys.argv[0]) or "a scheduled script"
    plural = "s" if len(missing) > 1 else ""
    text = (f"🚨 <b>{script} could not start</b>\n"
            f"Missing config: <code>{', '.join(missing)}</code>\n\n"
            f"Nothing ran. The variable{plural} must be set in the VPS "
            f"<code>~/kite/.env</code>.")
    sent = _notify(text)

    print(f"FATAL: missing required environment: {', '.join(missing)}",
          file=sys.stderr)
    if not sent:
        print("FATAL: could not send the Telegram alert either — check "
              "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.", file=sys.stderr)
    raise SystemExit(1)
