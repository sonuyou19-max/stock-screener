"""
XIRR Tracker — Portfolio Return vs Nifty 50 Benchmark (5.3)
=============================================================
Calculates your actual annualised return (XIRR) using
portfolio JSON files as the source of truth.

Zero manual transaction logging required by default.
Optionally record actual partial sells for higher accuracy.

How it works:
  1. Reads all portfolio_YYYYMM.json files — extracts buy dates + prices
  2. Fetches current live prices for all holdings
  3. Calculates XIRR treating each pick as a full buy on pick date
  4. Compares against Nifty 50 XIRR for the same period
  5. Shows stock-level P&L and portfolio alpha

Usage:
    python xirr_tracker.py                    # full performance report
    python xirr_tracker.py --sell TATAPOWER 3000  # record partial exit
    python xirr_tracker.py --report           # same as default
    python xirr_tracker.py --status           # quick summary only
"""

import json
import os
import glob
import argparse
import time
from datetime import datetime, date, timedelta
from typing import Optional

import yfinance as yf

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

# DATA_DIR first — that's where screener.py actually saves portfolio_*.json
# on Railway (/data); the old list never included it, so the tracker found
# nothing when run there.
PORTFOLIO_DIRS    = [os.getenv("DATA_DIR", "/data"), "./outputs",
                     "/mnt/user-data/outputs", "."]
SELLS_FILE        = os.path.join(os.path.dirname(__file__), "manual_sells.json")
NIFTY50_TICKER    = "^NSEI"
XIRR_TOLERANCE    = 1e-6
XIRR_MAX_ITER     = 1000


# ─────────────────────────────────────────────
# XIRR SOLVER
# ─────────────────────────────────────────────

def xirr(cashflows: list[tuple[date, float]], guess: float = 0.1) -> Optional[float]:
    """
    Calculate XIRR for a series of (date, amount) cashflows.

    Convention:
      Negative amount = cash outflow (buy)
      Positive amount = cash inflow (sell or current value)

    Returns annualised rate as a decimal (e.g. 0.284 = 28.4%)
    or None if no solution found.
    """
    if not cashflows or len(cashflows) < 2:
        return None

    # Anchor to first cashflow date
    dates   = [cf[0] for cf in cashflows]
    amounts = [cf[1] for cf in cashflows]
    t0      = dates[0]

    def npv(rate: float) -> float:
        total = 0.0
        for i, (d, amt) in enumerate(cashflows):
            days = (d - t0).days
            years = days / 365.25
            total += amt / ((1 + rate) ** years)
        return total

    def npv_deriv(rate: float) -> float:
        total = 0.0
        for i, (d, amt) in enumerate(cashflows):
            days = (d - t0).days
            years = days / 365.25
            if years == 0:
                continue
            total += -years * amt / ((1 + rate) ** (years + 1))
        return total

    # Newton-Raphson solver
    rate = guess
    for _ in range(XIRR_MAX_ITER):
        try:
            npv_val  = npv(rate)
            npv_d    = npv_deriv(rate)
            if npv_d == 0:
                break
            new_rate = rate - npv_val / npv_d
            if abs(new_rate - rate) < XIRR_TOLERANCE:
                return round(new_rate, 6)
            rate = new_rate
            # Clamp to prevent divergence
            rate = max(-0.99, min(rate, 100.0))
        except (OverflowError, ZeroDivisionError):
            break

    return None


# ─────────────────────────────────────────────
# PORTFOLIO JSON LOADER
# ─────────────────────────────────────────────

def find_portfolio_files() -> list[str]:
    """Find all portfolio_YYYYMM.json files, sorted oldest first."""
    files = []
    for d in PORTFOLIO_DIRS:
        files.extend(glob.glob(os.path.join(d, "portfolio_*.json")))
    return sorted(set(files))


def load_all_positions(portfolio_files: list[str]) -> list[dict]:
    """
    Extract all stock positions from portfolio JSONs.

    For stocks appearing in multiple months:
      - Use the EARLIEST buy_date (when first picked)
      - Use the price from that first appearance

    Returns list of position dicts.
    """
    seen      = {}   # ticker → earliest position dict
    for fpath in portfolio_files:
        try:
            with open(fpath) as f:
                portfolio = json.load(f)

            for bucket_key, bucket in portfolio.items():
                if not isinstance(bucket, dict):
                    continue
                for s in bucket.get("stocks", []):
                    ticker    = s.get("ticker")
                    buy_date  = s.get("buy_date")
                    buy_price = s.get("price")
                    shares    = s.get("approx_shares", 0)
                    alloc     = s.get("allocation_inr", 0)
                    name      = s.get("name", ticker)

                    if not ticker or not buy_price:
                        continue

                    # Parse buy date — fall back to filename date
                    if buy_date:
                        try:
                            parsed_date = datetime.strptime(
                                buy_date, "%Y-%m-%d"
                            ).date()
                        except ValueError:
                            parsed_date = _date_from_filename(fpath)
                    else:
                        parsed_date = _date_from_filename(fpath)

                    if ticker not in seen:
                        seen[ticker] = {
                            "ticker":     ticker,
                            "name":       name,
                            "buy_date":   parsed_date,
                            "buy_price":  buy_price,
                            "shares":     shares,
                            "alloc_inr":  alloc,
                            "bucket":     bucket.get("label", bucket_key),
                        }
                    # Keep earliest date
                    elif parsed_date < seen[ticker]["buy_date"]:
                        seen[ticker]["buy_date"]  = parsed_date
                        seen[ticker]["buy_price"] = buy_price
                        seen[ticker]["shares"]    = shares
                        seen[ticker]["alloc_inr"] = alloc

        except Exception as e:
            print(f"  ⚠️  Could not load {fpath}: {e}")

    return list(seen.values())


def _date_from_filename(fpath: str) -> date:
    """Extract date from portfolio_YYYYMM.json filename."""
    basename = os.path.basename(fpath)
    try:
        yyyymm = basename.replace("portfolio_", "").replace(".json", "")
        return datetime.strptime(yyyymm + "01", "%Y%m%d").date()
    except ValueError:
        return date.today()


# ─────────────────────────────────────────────
# LIVE PRICE FETCHER
# ─────────────────────────────────────────────

def fetch_live_prices(tickers: list[str]) -> dict[str, Optional[float]]:
    """Fetch current prices for all tickers. Returns {ticker: price}."""
    prices = {}
    for ticker in tickers:
        try:
            hist = yf.Ticker(ticker).history(period="2d")
            prices[ticker] = round(float(hist["Close"].iloc[-1]), 2) \
                             if not hist.empty else None
        except Exception:
            prices[ticker] = None
        time.sleep(0.25)
    return prices


def fetch_nifty50_price(target_date: date) -> Optional[float]:
    """Fetch Nifty 50 closing price on or near a specific date."""
    try:
        # Fetch a window around the target date
        start = target_date - timedelta(days=7)
        end   = target_date + timedelta(days=3)
        hist  = yf.Ticker(NIFTY50_TICKER).history(
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
        )
        if hist.empty:
            return None
        # Get closest available price to target date
        hist.index = hist.index.date
        # Find closest date on or before target
        available = [d for d in hist.index if d <= target_date]
        if not available:
            available = list(hist.index)
        closest = max(available)
        return round(float(hist.loc[closest, "Close"]), 2)
    except Exception:
        return None


# ─────────────────────────────────────────────
# MANUAL SELLS (OPTIONAL)
# ─────────────────────────────────────────────

def load_manual_sells() -> list[dict]:
    """Load manually recorded partial/full sell transactions."""
    if not os.path.exists(SELLS_FILE):
        return []
    try:
        with open(SELLS_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def record_sell(ticker: str, amount_inr: float, sell_date: str = None):
    """
    Record a manual sell transaction.

    Args:
      ticker     : e.g. "TATAPOWER.NS"
      amount_inr : proceeds received in ₹ (positive)
      sell_date  : YYYY-MM-DD, defaults to today
    """
    sells = load_manual_sells()
    entry = {
        "ticker":   ticker.upper(),
        "date":     sell_date or date.today().strftime("%Y-%m-%d"),
        "amount":   round(amount_inr, 2),
        "type":     "partial_sell",
    }
    sells.append(entry)
    with open(SELLS_FILE, "w") as f:
        json.dump(sells, f, indent=2)
    print(f"  ✅ Recorded sell: {ticker} ₹{amount_inr:,.0f} on {entry['date']}")


# ─────────────────────────────────────────────
# CASHFLOW BUILDER
# ─────────────────────────────────────────────

def build_cashflows(
    positions:   list[dict],
    live_prices: dict[str, Optional[float]],
    manual_sells: list[dict],
) -> tuple[list, list]:
    """
    Build two cashflow series:
      1. Your portfolio cashflows
      2. Equivalent Nifty 50 cashflows (same investment dates + amounts)

    Returns (portfolio_cashflows, nifty_cashflows)
    Each is a list of (date, amount) tuples.
    """
    portfolio_cfs = []   # (date, ₹amount) — negative=buy, positive=sell/value
    nifty_cfs     = []   # same dates, same amounts, different exit values
    stock_details = []   # for the detailed report

    # Index manual sells by ticker for quick lookup
    sells_by_ticker = {}
    for s in manual_sells:
        t = s["ticker"]
        if t not in sells_by_ticker:
            sells_by_ticker[t] = []
        sells_by_ticker[t].append(s)

    total_invested   = 0.0
    nifty_entry_info = {}   # ticker → {date, amount, nifty_price_then}

    for pos in positions:
        ticker    = pos["ticker"]
        buy_date  = pos["buy_date"]
        buy_price = pos["buy_price"]
        shares    = pos["shares"] or 0
        alloc     = pos["alloc_inr"] or (buy_price * shares)

        if shares <= 0 or alloc <= 0:
            continue

        # ── Portfolio buy cashflow ────────────────────────────
        portfolio_cfs.append((buy_date, -alloc))   # outflow
        total_invested += alloc

        # ── Record Nifty 50 equivalent buy ────────────────────
        nifty_price_then = fetch_nifty50_price(buy_date)
        time.sleep(0.2)
        nifty_cfs.append((buy_date, -alloc))        # same outflow
        nifty_entry_info[ticker] = {
            "alloc":       alloc,
            "nifty_then":  nifty_price_then,
            "buy_date":    buy_date,
        }

        # ── Manual sells for this ticker ──────────────────────
        for sell in sells_by_ticker.get(ticker, []):
            try:
                sell_date = datetime.strptime(sell["date"], "%Y-%m-%d").date()
                portfolio_cfs.append((sell_date, sell["amount"]))
            except ValueError:
                pass

        # ── Current value (unrealised) ────────────────────────
        current_price = live_prices.get(ticker)
        if current_price:
            current_value = round(current_price * shares, 2)
            gain_pct      = round((current_price / buy_price - 1) * 100, 2)

            # Subtract any already-sold amounts from current value estimate
            sold_amount = sum(
                s["amount"] for s in sells_by_ticker.get(ticker, [])
            )
            # Rough remaining shares estimate
            sold_shares_est = sold_amount / buy_price if buy_price > 0 else 0
            remaining_shares = max(0, shares - sold_shares_est)
            remaining_value  = round(current_price * remaining_shares, 2)

            portfolio_cfs.append((date.today(), remaining_value))

            stock_details.append({
                "ticker":         ticker,
                "name":           pos["name"],
                "bucket":         pos["bucket"],
                "buy_date":       str(buy_date),
                "buy_price":      buy_price,
                "current_price":  current_price,
                "shares":         shares,
                "alloc_inr":      alloc,
                "current_value":  current_value,
                "gain_inr":       round(current_value - alloc, 2),
                "gain_pct":       gain_pct,
                "days_held":      (date.today() - buy_date).days,
            })
        else:
            # Price unavailable — use cost as proxy
            portfolio_cfs.append((date.today(), alloc))
            stock_details.append({
                "ticker":     ticker,
                "name":       pos["name"],
                "bucket":     pos["bucket"],
                "buy_date":   str(buy_date),
                "buy_price":  buy_price,
                "current_price": None,
                "shares":     shares,
                "alloc_inr":  alloc,
                "gain_pct":   None,
                "days_held":  (date.today() - buy_date).days,
            })

    # ── Nifty 50 current value ────────────────────────────────
    nifty_now = fetch_nifty50_price(date.today())
    time.sleep(0.2)

    for ticker, info in nifty_entry_info.items():
        if info["nifty_then"] and nifty_now:
            nifty_return = nifty_now / info["nifty_then"]
            nifty_current_value = round(info["alloc"] * nifty_return, 2)
        else:
            nifty_current_value = info["alloc"]   # flat if no data
        nifty_cfs.append((date.today(), nifty_current_value))

    return portfolio_cfs, nifty_cfs, stock_details


# ─────────────────────────────────────────────
# PERFORMANCE REPORT
# ─────────────────────────────────────────────

def generate_performance_report(quick: bool = False):
    """
    Full XIRR performance report vs Nifty 50.
    """
    print(f"\n{'='*60}")
    print(f"  📈 PORTFOLIO PERFORMANCE — XIRR REPORT")
    print(f"  {date.today().strftime('%d %B %Y')}")
    print(f"{'='*60}\n")

    # Load data
    files = find_portfolio_files()
    if not files:
        print("  ❌ No portfolio files found. Run screener.py first.")
        return

    print(f"  📂 Found {len(files)} portfolio file(s):")
    for f in files:
        print(f"     {os.path.basename(f)}")

    positions = load_all_positions(files)
    if not positions:
        print("  ❌ No positions found in portfolio files.")
        return

    print(f"\n  📊 Loading {len(positions)} positions...")

    # Fetch live prices
    tickers = [p["ticker"] for p in positions]
    print(f"  🌐 Fetching live prices...")
    live_prices = fetch_live_prices(tickers)

    # Load any manual sells
    manual_sells = load_manual_sells()
    if manual_sells:
        print(f"  📋 {len(manual_sells)} manual sell(s) loaded")

    print(f"  📐 Building cashflows + Nifty 50 benchmark...")
    portfolio_cfs, nifty_cfs, stock_details = build_cashflows(
        positions, live_prices, manual_sells
    )

    # Sort cashflows by date
    portfolio_cfs = sorted(portfolio_cfs, key=lambda x: x[0])
    nifty_cfs     = sorted(nifty_cfs,     key=lambda x: x[0])

    # Calculate XIRR
    port_xirr  = xirr(portfolio_cfs)
    nifty_xirr = xirr(nifty_cfs)

    # ── Summary header ────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  PERFORMANCE SUMMARY")
    print(f"{'='*60}")

    total_invested = sum(abs(cf[1]) for d, cf in
                         [(cf[0], cf[1]) for cf in portfolio_cfs if cf[1] < 0])
    total_value    = sum(cf[1] for cf in portfolio_cfs if cf[1] > 0 and cf[0] == date.today())
    total_gain     = total_value - total_invested
    simple_return  = round((total_value / total_invested - 1) * 100, 2) \
                     if total_invested > 0 else 0

    print(f"\n  Total Invested:   ₹{total_invested:>12,.0f}")
    print(f"  Current Value:    ₹{total_value:>12,.0f}")
    print(f"  Total Gain/Loss:  ₹{total_gain:>12,.0f}  ({simple_return:+.2f}%)")

    print(f"\n  {'Metric':<30} {'Your Portfolio':>15} {'Nifty 50':>12}")
    print(f"  {'-'*57}")

    if port_xirr is not None:
        port_str = f"{port_xirr*100:+.2f}% p.a."
    else:
        port_str = "Insufficient data"

    if nifty_xirr is not None:
        nifty_str = f"{nifty_xirr*100:+.2f}% p.a."
    else:
        nifty_str = "Unavailable"

    print(f"  {'XIRR (annualised)':<30} {port_str:>15} {nifty_str:>12}")

    # Alpha
    if port_xirr is not None and nifty_xirr is not None:
        alpha = (port_xirr - nifty_xirr) * 100
        alpha_str = f"{alpha:+.2f}%"
        verdict = (
            "✅ Outperforming index" if alpha > 2
            else "⚠️  Underperforming — consider index fund"
            if alpha < -2 else "≈ Matching index"
        )
        print(f"  {'Alpha (vs Nifty 50)':<30} {alpha_str:>15}")
        print(f"\n  Verdict: {verdict}")

    # Period
    if positions:
        earliest = min(p["buy_date"] for p in positions)
        days_held = (date.today() - earliest).days
        print(f"\n  Tracking period: {earliest} → {date.today()} ({days_held} days)")
        if days_held < 90:
            print(f"  ⚠️  Only {days_held} days of data — XIRR is less reliable under 90 days")

    if quick:
        print(f"\n{'='*60}")
        return

    # ── Stock-level breakdown ─────────────────────────────────
    if stock_details:
        print(f"\n  STOCK-LEVEL P&L")
        print(f"  {'-'*60}")
        print(f"  {'Ticker':<14} {'Bucket':<18} {'Days':>5} {'Buy':>8} {'Now':>8} {'Gain%':>7} {'₹ P&L':>10}")
        print(f"  {'-'*60}")

        # Sort by gain % descending
        sortable = [s for s in stock_details if s.get("gain_pct") is not None]
        unsortable = [s for s in stock_details if s.get("gain_pct") is None]
        sortable.sort(key=lambda x: x["gain_pct"], reverse=True)

        for s in sortable + unsortable:
            gain_str  = f"{s['gain_pct']:+.1f}%" if s.get("gain_pct") is not None else "N/A"
            now_str   = f"₹{s['current_price']:,.0f}" if s.get("current_price") else "N/A"
            gain_inr  = s.get("gain_inr", 0)
            inr_str   = f"₹{gain_inr:+,.0f}" if gain_inr else "N/A"
            bucket_short = s["bucket"][:16] if s.get("bucket") else ""

            # Colour indicator
            if s.get("gain_pct") and s["gain_pct"] >= 20:
                indicator = "🟢"
            elif s.get("gain_pct") and s["gain_pct"] >= 0:
                indicator = "🟡"
            elif s.get("gain_pct") and s["gain_pct"] < 0:
                indicator = "🔴"
            else:
                indicator = "⚪"

            print(
                f"  {s['ticker']:<14} {bucket_short:<18} "
                f"{s['days_held']:>5} ₹{s['buy_price']:>6,.0f} "
                f"{now_str:>8} {indicator}{gain_str:>6} {inr_str:>10}"
            )

    # ── Manual sells note ─────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  ℹ️  XIRR uses screener pick prices as buy prices.")
    print(f"  For exact accuracy, record partial sells:")
    print(f"  python xirr_tracker.py --sell TICKER AMOUNT")
    if manual_sells:
        print(f"\n  Manual sells recorded ({len(manual_sells)}):")
        for s in manual_sells[-5:]:
            print(f"    {s['date']}  {s['ticker']:<20} ₹{s['amount']:,.0f}")
    print(f"{'='*60}")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="XIRR portfolio tracker vs Nifty 50")
    parser.add_argument(
        "--sell", nargs=2, metavar=("TICKER", "AMOUNT"),
        help="Record a sell: --sell TATAPOWER.NS 3000"
    )
    parser.add_argument(
        "--sell-date", metavar="YYYY-MM-DD",
        help="Date for the sell (default: today)"
    )
    parser.add_argument(
        "--report", action="store_true",
        help="Generate full performance report (default)"
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Quick summary only"
    )
    args = parser.parse_args()

    if args.sell:
        ticker, amount = args.sell
        try:
            record_sell(ticker, float(amount), args.sell_date)
        except ValueError:
            print(f"❌ Invalid amount: {amount}")
    elif args.status:
        generate_performance_report(quick=True)
    else:
        generate_performance_report(quick=False)
