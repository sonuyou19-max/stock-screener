"""
Monthly Portfolio Rebalancer — Tax-Aware (3.6)
===============================================
Compare last month's portfolio with this month's fresh screening.
Tells you exactly what to BUY, SELL, and HOLD — with tax implications
for every SELL recommendation.

Indian Capital Gains Tax:
  STCG (< 12 months held): 20%
  LTCG (≥ 12 months held): 12.5% on gains above ₹1.25 lakh exemption

Usage:
    python rebalancer.py --old portfolio_202504.json --new portfolio_202505.json

Or run without args to auto-detect the two most recent portfolio files.
"""

import json
import os
import glob
import argparse
import yfinance as yf
from datetime import datetime, date


# ─────────────────────────────────────────────
# TAX CONSTANTS (Indian FY 2024-25 onwards)
# ─────────────────────────────────────────────

STCG_RATE           = 0.20      # 20%
LTCG_RATE           = 0.125     # 12.5%
LTCG_EXEMPTION      = 125_000   # ₹1.25 lakh annual exemption
LTCG_DAYS           = 365       # 12 months = 365 days
LTCG_WARNING_DAYS   = 45        # flag if LTCG within 45 days
MIN_SAVING_TO_FLAG  = 500       # only flag if tax saving > ₹500


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def load_portfolio(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def extract_holdings(portfolio: dict) -> dict:
    """
    Return {ticker: stock_dict} from a portfolio dict.
    Preserves all fields including buy_date, price, shares.
    """
    holdings = {}
    for bucket in portfolio.values():
        if not isinstance(bucket, dict):
            continue
        for s in bucket.get("stocks", []):
            ticker = s.get("ticker")
            if ticker:
                holdings[ticker] = s
    return holdings


def get_current_price(ticker: str) -> float | None:
    try:
        hist = yf.Ticker(ticker).history(period="1d")
        if hist.empty:
            return None
        return round(float(hist["Close"].iloc[-1]), 2)
    except Exception:
        return None


# ─────────────────────────────────────────────
# TAX CHECKER (3.6)
# ─────────────────────────────────────────────

def tax_check(
    stock: dict,
    current_price: float | None,
    ltcg_used: float = 0.0,
) -> dict:
    """
    Calculate tax implications for selling a stock.

    Args:
      stock         : stock dict from old portfolio JSON
      current_price : live price fetched from yfinance
      ltcg_used     : LTCG gains already realised this FY (₹)
                      Passed via --ltcg-used flag. Reduces available
                      exemption so tax estimate is accurate, not optimistic.

    Returns dict with:
      days_held          : calendar days since buy_date
      days_to_ltcg       : days remaining until LTCG threshold
      tax_status         : "ltcg" | "stcg_near_ltcg" | "stcg"
      est_profit         : estimated profit in ₹
      stcg_tax           : tax if sold now (STCG)
      ltcg_tax           : tax if held to LTCG threshold
      potential_saving   : STCG - LTCG tax
      ltcg_exemption_remaining : how much of ₹1.25L exemption is left
      recommendation     : "sell_now" | "wait_for_ltcg" | "sell_now_ltcg"
      tax_notes          : plain English explanation
    """
    result = {
        "days_held":               None,
        "days_to_ltcg":            None,
        "tax_status":              "unknown",
        "est_profit":              None,
        "stcg_tax":                None,
        "ltcg_tax":                None,
        "potential_saving":        None,
        "ltcg_exemption_remaining":None,
        "recommendation":          "sell_now",
        "tax_notes":               "",
    }

    # ── Days held ─────────────────────────────────────────────
    buy_date_str = stock.get("buy_date")
    if not buy_date_str:
        result["tax_notes"] = "No buy_date in portfolio — tax calculation unavailable."
        return result

    try:
        buy_date  = datetime.strptime(buy_date_str, "%Y-%m-%d").date()
        days_held = (date.today() - buy_date).days
        days_to_ltcg = max(0, LTCG_DAYS - days_held)
    except ValueError:
        result["tax_notes"] = f"Invalid buy_date format: {buy_date_str}"
        return result

    result["days_held"]    = days_held
    result["days_to_ltcg"] = days_to_ltcg

    # ── Tax status ────────────────────────────────────────────
    if days_held >= LTCG_DAYS:
        result["tax_status"] = "ltcg"
    elif days_to_ltcg <= LTCG_WARNING_DAYS:
        result["tax_status"] = "stcg_near_ltcg"
    else:
        result["tax_status"] = "stcg"

    # ── Profit estimate ───────────────────────────────────────
    buy_price = stock.get("price", 0)
    shares    = stock.get("approx_shares", 0)

    if not current_price or not buy_price or not shares:
        result["tax_notes"] = (
            f"Held {days_held} days — "
            f"{'LTCG ✅' if result['tax_status'] == 'ltcg' else f'STCG ({days_to_ltcg}d to LTCG)'}"
            f" — price data unavailable for tax calculation."
        )
        return result

    est_profit = round((current_price - buy_price) * shares, 0)
    result["est_profit"] = est_profit

    if est_profit <= 0:
        result["tax_notes"] = (
            f"Held {days_held} days | "
            f"{'LTCG ✅' if result['tax_status'] == 'ltcg' else f'STCG ({days_to_ltcg}d to LTCG)'} | "
            f"No profit — no tax impact."
        )
        result["recommendation"] = "sell_now"
        return result

    # ── STCG calculation ──────────────────────────────────────
    stcg_tax = round(est_profit * STCG_RATE, 0)

    # ── LTCG calculation with accurate exemption (Option B) ───
    # Remaining exemption = ₹1.25L minus gains already realised this FY
    # ltcg_used is passed in via --ltcg-used flag (default 0)
    ltcg_exemption_remaining = max(0, LTCG_EXEMPTION - ltcg_used)
    taxable_ltcg = max(0, est_profit - ltcg_exemption_remaining)
    ltcg_tax     = round(taxable_ltcg * LTCG_RATE, 0)

    result["ltcg_exemption_remaining"] = ltcg_exemption_remaining

    potential_saving = round(stcg_tax - ltcg_tax, 0)

    result["stcg_tax"]         = stcg_tax
    result["ltcg_tax"]         = ltcg_tax
    result["potential_saving"] = potential_saving

    # ── Recommendation ────────────────────────────────────────
    if result["tax_status"] == "ltcg":
        result["recommendation"] = "sell_now_ltcg"
        result["tax_notes"] = (
            f"✅ LTCG applies (held {days_held} days). "
            f"Est. profit: ₹{est_profit:,.0f} | "
            f"Tax: ₹{ltcg_tax:,.0f} (12.5% on gains above ₹1.25L exemption)."
        )
    elif result["tax_status"] == "stcg_near_ltcg" and potential_saving >= MIN_SAVING_TO_FLAG:
        result["recommendation"] = "wait_for_ltcg"
        result["tax_notes"] = (
            f"💰 Wait {days_to_ltcg} days for LTCG threshold. "
            f"Est. profit: ₹{est_profit:,.0f} | "
            f"STCG tax now: ₹{stcg_tax:,.0f} vs LTCG later: ₹{ltcg_tax:,.0f}. "
            f"Potential saving: ₹{potential_saving:,.0f}."
        )
    else:
        result["recommendation"] = "sell_now"
        result["tax_notes"] = (
            f"STCG applies (held {days_held} days, {days_to_ltcg} days to LTCG). "
            f"Est. profit: ₹{est_profit:,.0f} | "
            f"STCG tax: ₹{stcg_tax:,.0f}."
            + (f" Saving by waiting: ₹{potential_saving:,.0f} — below ₹{MIN_SAVING_TO_FLAG} threshold."
               if potential_saving > 0 else "")
        )

    return result


# ─────────────────────────────────────────────
# REBALANCER
# ─────────────────────────────────────────────

def rebalance(old_path: str, new_path: str, ltcg_used: float = 0.0):
    old_portfolio = load_portfolio(old_path)
    new_portfolio = load_portfolio(new_path)

    old_holdings = extract_holdings(old_portfolio)
    new_holdings = extract_holdings(new_portfolio)

    old_tickers = set(old_holdings.keys())
    new_tickers = set(new_holdings.keys())

    exits   = old_tickers - new_tickers
    entries = new_tickers - old_tickers
    holds   = old_tickers & new_tickers

    exemption_remaining = max(0, LTCG_EXEMPTION - ltcg_used)

    print("\n" + "="*60)
    print("  📅 MONTHLY REBALANCING REPORT — TAX AWARE")
    print(f"  Old: {os.path.basename(old_path)}")
    print(f"  New: {os.path.basename(new_path)}")
    print(f"  Date: {date.today().strftime('%d %B %Y')}")
    print(f"  LTCG used this FY: ₹{ltcg_used:,.0f}  |  "
          f"Exemption remaining: ₹{exemption_remaining:,.0f}")
    if ltcg_used == 0:
        print("  ℹ️  Pass --ltcg-used <amount> if you've had other LTCG sales this FY")
    print("="*60)

    # ── SELL ──────────────────────────────────────────────────
    print(f"\n🔴 SELL — {len(exits)} stock(s) dropped from portfolio")

    tax_wait_stocks  = []   # stocks where waiting saves tax
    sell_now_stocks  = []   # stocks to sell immediately

    if exits:
        print("  Fetching live prices for tax calculation...")
        for t in sorted(exits):
            s       = old_holdings[t]
            current = get_current_price(t)
            tax     = tax_check(s, current, ltcg_used=ltcg_used)

            print(f"\n  {t:<22} {s['name']}")
            print(f"    Buy Price:  ₹{s['price']:,.2f}  |  Now: ₹{current:,.2f}" if current else
                  f"    Buy Price:  ₹{s['price']:,.2f}  |  Price unavailable")
            print(f"    Score was:  {s.get('final_score', 'N/A')}")

            # Tax block
            if tax["days_held"] is not None:
                status_emoji = "✅" if tax["tax_status"] == "ltcg" else (
                               "⚠️" if tax["tax_status"] == "stcg_near_ltcg" else "📋")
                print(f"    ── Tax Assessment ─────────────────────────")
                print(f"    Held:       {tax['days_held']} days "
                      f"({s.get('buy_date','unknown')} → today)")
                print(f"    Status:     {status_emoji} {tax['tax_status'].replace('_',' ').title()}")
                if tax.get("ltcg_exemption_remaining") is not None:
                    print(f"    LTCG Exempt:₹{tax['ltcg_exemption_remaining']:,.0f} remaining this FY")
                if tax.get("est_profit"):
                    print(f"    Est. Profit:₹{tax['est_profit']:,.0f}")
                if tax.get("stcg_tax") is not None:
                    print(f"    STCG Tax:   ₹{tax['stcg_tax']:,.0f}  (if sold now)")
                if tax.get("ltcg_tax") is not None:
                    print(f"    LTCG Tax:   ₹{tax['ltcg_tax']:,.0f}  (if held {tax['days_to_ltcg']}d more)")
                if tax.get("potential_saving") and tax["potential_saving"] > 0:
                    print(f"    💰 Saving:  ₹{tax['potential_saving']:,.0f} by waiting {tax['days_to_ltcg']} days")
                print(f"    {tax['tax_notes']}")

            # Recommendation
            if tax["recommendation"] == "wait_for_ltcg":
                print(f"    ➡️  RECOMMENDATION: HOLD {tax['days_to_ltcg']} more days for LTCG")
                tax_wait_stocks.append(t)
            else:
                label = "✅ SELL NOW (LTCG applies)" if tax["recommendation"] == "sell_now_ltcg" \
                        else "📋 SELL NOW (STCG applies)"
                print(f"    ➡️  RECOMMENDATION: {label}")
                sell_now_stocks.append(t)
    else:
        print("  None")

    # ── BUY ───────────────────────────────────────────────────
    print(f"\n🟢 BUY — {len(entries)} new stock(s) entering portfolio")
    if entries:
        for t in sorted(entries):
            s = new_holdings[t]
            approx = int(s.get("allocation_inr", 0) // s["price"]) if s["price"] > 0 else 0
            print(f"\n  {t:<22} {s['name']}")
            print(f"    Allocate:  ₹{s.get('allocation_inr',0):,.0f}  |  "
                  f"Score: {s.get('final_score','N/A')}  |  "
                  f"Price: ₹{s['price']:,.2f}")
            print(f"    Buy approx {approx} shares at market open (limit order)")
    else:
        print("  None")

    # ── HOLD ──────────────────────────────────────────────────
    print(f"\n🟡 HOLD — {len(holds)} stock(s) continuing from last month")
    if holds:
        for t in sorted(holds):
            old = old_holdings[t]
            new = new_holdings[t]
            score_delta = new.get("final_score",0) - old.get("final_score",0)
            delta_str   = f"{score_delta:+.1f}"

            # Check if any hold stocks are approaching LTCG
            buy_date_str = old.get("buy_date")
            days_note    = ""
            if buy_date_str:
                try:
                    buy_dt    = datetime.strptime(buy_date_str, "%Y-%m-%d").date()
                    days_held = (date.today() - buy_dt).days
                    days_left = max(0, LTCG_DAYS - days_held)
                    if days_left == 0:
                        days_note = " ✅ LTCG"
                    elif days_left <= LTCG_WARNING_DAYS:
                        days_note = f" ⚠️  LTCG in {days_left}d"
                except ValueError:
                    pass

            print(f"  {t:<22} Score: {old.get('final_score','?')} → "
                  f"{new.get('final_score','?')} ({delta_str}){days_note}")
    else:
        print("  None")

    # ── TAX SUMMARY ───────────────────────────────────────────
    if tax_wait_stocks:
        print(f"\n💰 TAX WAIT LIST — {len(tax_wait_stocks)} stock(s) worth holding for LTCG:")
        for t in tax_wait_stocks:
            print(f"  {t}")
        print("  Note: Keep these positions open. The screener excluded them")
        print("  this month but they may re-enter once LTCG threshold is crossed.")

    # ── CASH FLOW SUMMARY ─────────────────────────────────────
    sell_proceeds = sum(
        old_holdings[t].get("allocation_inr", 0)
        for t in exits if t in sell_now_stocks
    )
    buy_cost = sum(new_holdings[t].get("allocation_inr", 0) for t in entries)
    net_cash = sell_proceeds - buy_cost

    print(f"\n💰 CASH FLOW SUMMARY")
    print(f"  Sell now (₹):   ₹{sell_proceeds:,.0f}  ({len(sell_now_stocks)} stocks)")
    if tax_wait_stocks:
        wait_value = sum(old_holdings[t].get("allocation_inr",0) for t in tax_wait_stocks)
        print(f"  Hold for LTCG:  ₹{wait_value:,.0f}  ({len(tax_wait_stocks)} stocks — don't sell yet)")
    print(f"  Buy cost:       ₹{buy_cost:,.0f}")
    if net_cash >= 0:
        print(f"  Net freed up:   ₹{net_cash:,.0f}  → Deploy to new buys or cash buffer")
    else:
        print(f"  Additional needed: ₹{abs(net_cash):,.0f}  → Use cash buffer")

    print("\n⚠️  Tax estimates are approximate. Consult a CA for exact tax liability.")
    print(f"   LTCG exemption used: ₹{ltcg_used:,.0f} / ₹{LTCG_EXEMPTION:,.0f} this FY.")
    print("   Run with --ltcg-used <amount> to set your actual YTD LTCG gains.")
    print("   Always use actual transaction prices for tax filing.")
    print("   Download exact Tax P&L from Zerodha Console after each FY.")
    print("="*60)


def auto_detect_portfolios(output_dirs: list = None) -> tuple:
    if output_dirs is None:
        output_dirs = ["./outputs", "/mnt/user-data/outputs", "."]

    files = []
    for d in output_dirs:
        files.extend(glob.glob(os.path.join(d, "portfolio_*.json")))

    files = sorted(set(files))
    if len(files) < 2:
        raise FileNotFoundError(
            "Need at least 2 portfolio_YYYYMM.json files. Run screener.py first."
        )
    return files[-2], files[-1]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Tax-aware monthly portfolio rebalancer")
    parser.add_argument("--old",       help="Path to last month's portfolio JSON")
    parser.add_argument("--new",       help="Path to this month's portfolio JSON")
    parser.add_argument(
        "--ltcg-used",
        type=float,
        default=0.0,
        metavar="AMOUNT",
        help=(
            "LTCG gains already realised this financial year in ₹. "
            "Reduces the ₹1.25L exemption accordingly for accurate tax estimates. "
            "Example: --ltcg-used 80000 (if you've already booked ₹80,000 in LTCG gains). "
            "Check Zerodha Console → Tax P&L for your YTD figure. Default: 0"
        ),
    )
    args = parser.parse_args()

    if args.old and args.new:
        old_path, new_path = args.old, args.new
    else:
        print("Auto-detecting two most recent portfolio files...")
        old_path, new_path = auto_detect_portfolios()

    rebalance(old_path, new_path, ltcg_used=args.ltcg_used)
