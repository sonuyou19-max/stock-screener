"""
Portfolio Tracker — Stop-Loss & Profit Target Monitor
=======================================================
Delegates all logic to alerter.py.
Runs every 30 minutes during market hours via Railway scheduler.
Deduplication ensures max 1 email per alert per day.

Usage:
    python tracker.py                          # auto-detects latest portfolio
    python tracker.py --portfolio p.json       # specific portfolio
    python tracker.py --no-email               # terminal only
    python tracker.py --test-email             # force email even if no alerts
    python tracker.py --force                  # bypass market hours check
"""

import argparse
from alerter import check_and_alert, find_latest_portfolio


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Portfolio tracker with email alerts")
    parser.add_argument("--portfolio",  help="Path to portfolio JSON (auto-detected if omitted)")
    parser.add_argument("--no-email",   action="store_true", help="Terminal output only")
    parser.add_argument("--test-email", action="store_true", help="Force email even with no alerts")
    parser.add_argument("--force",      action="store_true", help="Bypass market hours check")
    args = parser.parse_args()

    portfolio_path = args.portfolio or find_latest_portfolio()
    if not portfolio_path:
        print("❌ No portfolio file found. Run screener.py first.")
        exit(1)

    check_and_alert(
        portfolio_path,
        send_email = not args.no_email,
        test_mode  = args.test_email,
        force_run  = args.force,
    )
