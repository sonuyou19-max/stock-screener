#!/usr/bin/env python3
"""
Automated Kite Connect daily token refresh.
Simulates TOTP login headlessly — no browser needed.

Cron (8:45 AM IST = 3:15 AM UTC, weekdays):
  15 3 * * 1-5 /home/ubuntu/kite/venv/bin/python /home/ubuntu/kite/token_refresh.py >> /home/ubuntu/kite/refresh.log 2>&1
"""
import os
import re
import logging
import pyotp
import requests
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

    # Step 3: Hit Kite Connect login — session cookies auto-authenticate
    log.info("Step 3: Connect login …")
    connect_url = f"https://kite.trade/connect/login?api_key={API_KEY}&v=3"
    r = s.get(connect_url, allow_redirects=True)
    final_url = r.url
    log.info("  → URL after redirect: %s", final_url[:120])

    # If landed on the consent/authorize page, POST to it to programmatically approve
    if "connect/authorize" in final_url and "request_token" not in final_url:
        from urllib.parse import urlparse, parse_qs
        log.info("  → Consent page detected, submitting authorization …")
        parsed  = urlparse(final_url)
        qparams = parse_qs(parsed.query, keep_blank_values=True)
        # Flatten single-value lists and send as form POST
        form_data = {k: v[0] for k, v in qparams.items()}
        r = s.post(
            "https://kite.zerodha.com/connect/authorize",
            data=form_data,
            allow_redirects=True,
        )
        final_url = r.url
        log.info("  → post-authorize URL: %s", final_url[:120])

    m = re.search(r"[?&]request_token=([^&\s]+)", final_url)
    if not m:
        # Fallback: scan response body
        m = re.search(r"request_token=([^&\"'\s]+)", r.text)
    if not m:
        raise RuntimeError(
            f"request_token not found in redirect.\n"
            f"Final URL: {final_url}\n"
            f"Body (first 500 chars): {r.text[:500]}"
        )
    request_token = m.group(1)
    log.info("  → request_token: %s…", request_token[:8])

    # Step 4: Exchange for access_token
    log.info("Step 4: Generating session …")
    kite = KiteConnect(api_key=API_KEY)
    session_data = kite.generate_session(request_token, api_secret=API_SECRET)
    access_token = session_data["access_token"]

    with open(TOKEN_FILE, "w") as f:
        f.write(access_token)
    os.chmod(TOKEN_FILE, 0o600)

    log.info(
        "✅ Access token saved: %s… (user: %s)",
        access_token[:8],
        session_data.get("user_id", "?"),
    )
    return access_token


if __name__ == "__main__":
    try:
        refresh()
    except Exception as exc:
        log.error("❌ Token refresh failed: %s", exc)
        raise SystemExit(1)
