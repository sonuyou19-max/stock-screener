#!/usr/bin/env python3
"""
Automated Kite Connect daily token refresh.
Simulates TOTP login headlessly — no browser needed.

Kite access tokens expire EVERY day (including weekends), so run this 7
days a week — otherwise the token is dead on Saturday/Sunday and any
weekend dashboard scan fails. Use '* * *' (every day), not '* * 1-5':
  15 3 * * * /home/ubuntu/kite/venv/bin/python /home/ubuntu/kite/token_refresh.py >> /home/ubuntu/kite/refresh.log 2>&1
"""
import os
import re
import logging
import pyotp
import requests
from urllib.parse import urljoin, urlparse, parse_qs
from dotenv import load_dotenv
from kiteconnect import KiteConnect

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

API_KEY     = os.environ["KITE_API_KEY"]
API_SECRET  = os.environ["KITE_API_SECRET"]
USER_ID     = os.environ["KITE_USER_ID"]
PASSWORD    = os.environ["KITE_PASSWORD"]
TOTP_SECRET = os.environ["KITE_TOTP_SECRET"]
TOKEN_FILE  = os.path.join(BASE_DIR, "access_token.txt")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def _capture_request_token(s: requests.Session, start_url: str, max_hops: int = 12) -> str:
    """Walk the OAuth redirect chain manually and return the request_token
    WITHOUT ever fetching our registered redirect URL (the Railway
    /kite/callback). The request_token is single-use: if we let requests
    auto-follow into the callback, the callback exchanges (consumes) it before
    our own generate_session can — which is why Step 4 always failed with
    "Token is invalid or has expired". Here we read the token straight off the
    redirect Location and never touch the callback."""
    url, last = start_url, None
    for _ in range(max_hops):
        r = s.get(url, allow_redirects=False)
        last = r
        loc = r.headers.get("Location", "")
        m = re.search(r"[?&]request_token=([^&\s]+)", loc or "")
        if m:                                  # token rides on the redirect target
            return m.group(1)
        if r.is_redirect and loc:
            url = urljoin(url, loc)
            continue
        break                                  # landed on a real page (e.g. consent)

    # Consent/authorize page → POST the form, then read the token off the
    # resulting redirect (again without following it into the callback).
    final_url = last.url if last is not None else url
    if last is not None and "connect/authorize" in final_url and "request_token" not in final_url:
        log.info("  → Consent page detected, submitting authorization …")
        form_data = {k: v[0] for k, v in parse_qs(urlparse(final_url).query,
                                                  keep_blank_values=True).items()}
        r = s.post("https://kite.zerodha.com/connect/authorize",
                   data=form_data, allow_redirects=False)
        loc = r.headers.get("Location", "")
        m = (re.search(r"[?&]request_token=([^&\s]+)", loc or "")
             or re.search(r"request_token=([^&\"'\s]+)", r.text))
        if m:
            return m.group(1)

    m = re.search(r"request_token=([^&\"'\s]+)", final_url)
    if not m and last is not None:
        m = re.search(r"request_token=([^&\"'\s]+)", last.text)
    if not m:
        raise RuntimeError(
            f"request_token not found.\nLast URL: {final_url}\n"
            f"Body (first 500 chars): {last.text[:500] if last is not None else ''}"
        )
    return m.group(1)


def _validate_saved_token(kite: KiteConnect):
    """Return the saved access_token if it actually works, else None. Used as a
    fallback so we never report a false failure if some other path (e.g. the
    Railway callback) already refreshed the token."""
    try:
        with open(TOKEN_FILE) as f:
            tok = f.read().strip()
        if not tok:
            return None
        kite.set_access_token(tok)
        kite.profile()        # raises if the token is invalid/expired
        return tok
    except Exception:
        return None


def refresh() -> str:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
    })

    # Step 1: Password login
    log.info("Step 1: Password login …")
    r = s.post(
        "https://kite.zerodha.com/api/login",
        data={"user_id": USER_ID, "password": PASSWORD},
    )
    r.raise_for_status()
    body = r.json()
    if body.get("status") != "success":
        raise RuntimeError(f"Login failed: {body}")
    request_id = body["data"]["request_id"]
    log.info("  → request_id: %s", request_id)

    # Step 2: TOTP 2FA
    log.info("Step 2: TOTP 2FA …")
    totp_code = pyotp.TOTP(TOTP_SECRET).now()
    r = s.post(
        "https://kite.zerodha.com/api/twofa",
        data={
            "user_id":      USER_ID,
            "request_id":   request_id,
            "twofa_value":  totp_code,
            "twofa_type":   "totp",
            "skip_session": "",
        },
    )
    r.raise_for_status()
    body = r.json()
    if body.get("status") != "success":
        raise RuntimeError(f"2FA failed: {body}")
    log.info("  → 2FA OK")

    # Step 3: Hit Kite Connect login — session cookies auto-authenticate — and
    # capture the request_token from the redirect WITHOUT fetching the Railway
    # callback (which would consume the single-use token first).
    log.info("Step 3: Connect login …")
    connect_url = f"https://kite.trade/connect/login?api_key={API_KEY}&v=3"
    request_token = _capture_request_token(s, connect_url)
    log.info("  → request_token: %s…", request_token[:8])

    # Step 4: Exchange for access_token locally. The token is un-consumed now,
    # so this succeeds on its own — no dependence on the Railway callback.
    log.info("Step 4: Generating session …")
    kite = KiteConnect(api_key=API_KEY)
    try:
        session_data = kite.generate_session(request_token, api_secret=API_SECRET)
        access_token = session_data["access_token"]
        user_id      = session_data.get("user_id", "?")
    except Exception as exc:
        # Safety net: if anything still consumed the token first, accept a
        # freshly-saved token if it actually works, rather than failing loudly.
        log.warning("  generate_session failed (%s) — checking saved token …", exc)
        access_token = _validate_saved_token(kite)
        if not access_token:
            raise
        user_id = "?"
        log.info("  → saved token is valid; using it")

    with open(TOKEN_FILE, "w") as f:
        f.write(access_token)
    os.chmod(TOKEN_FILE, 0o600)

    log.info("✅ Access token saved: %s… (user: %s)", access_token[:8], user_id)
    return access_token


if __name__ == "__main__":
    try:
        refresh()
    except Exception as exc:
        log.error("❌ Token refresh failed: %s", exc)
        raise SystemExit(1)
