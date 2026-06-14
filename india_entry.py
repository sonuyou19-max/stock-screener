#!/usr/bin/env python3
"""
india_entry.py — 9:15 AM IST cron: place orders for queued India monthly stocks.

BUY entries:
  - Fetch live price from VPS /get-quote
  - If live > limit_price (3% above scan close) → skip (gap-up)
  - If live ≤ optimal_entry (20-DMA) → MARKET order
  - Else → LIMIT order at optimal_entry
  - Telegram summary

SELL entries (rebalancer-driven):
  - Always MARKET order at open
  - Quantity set when user queued (trim % applied to held shares)

Set DRY_RUN=true in .env to simulate without placing real orders.
"""

import os
import json
import time
import urllib.request as _req
import urllib.error

RAILWAY_URL     = os.environ["API_URL"]
UPLOAD_TOKEN    = os.environ["UPLOAD_TOKEN"]
VPS_URL         = os.environ.get("ORACLE_VPS_URL", "http://localhost:5001")
EXECUTOR_SECRET = os.environ["EXECUTOR_SECRET"]
TELEGRAM_BOT    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT   = os.environ.get("TELEGRAM_CHAT_ID", "")
DRY_RUN         = os.environ.get("DRY_RUN", "false").lower() in ("true", "1", "yes")

RAILWAY_HEADERS = {"X-Upload-Token": UPLOAD_TOKEN, "Content-Type": "application/json"}
VPS_HEADERS     = {"X-Executor-Secret": EXECUTOR_SECRET}


def _get(url, headers=None):
    r = _req.Request(url, headers={**(headers or {}), "Accept": "application/json"})
    with _req.urlopen(r, timeout=12) as resp:
        return json.loads(resp.read())


def _post(url, payload, headers=None):
    body = json.dumps(payload).encode()
    h = {"Content-Type": "application/json", **(headers or {})}
    r = _req.Request(url, data=body, headers=h, method="POST")
    with _req.urlopen(r, timeout=15) as resp:
        return json.loads(resp.read())


def _tg(msg: str):
    if not TELEGRAM_BOT or not TELEGRAM_CHAT:
        return
    try:
        _post(f"https://api.telegram.org/bot{TELEGRAM_BOT}/sendMessage",
              {"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "HTML"})
    except Exception as e:
        print(f"Telegram error: {e}")


def get_queue(action: str = ""):
    url = f"{RAILWAY_URL}/india/queue?status=queued"
    if action:
        url += f"&action={action}"
    data = _get(url, RAILWAY_HEADERS)
    return data.get("queue", [])


def get_live_price(symbol: str) -> float:
    data = _get(f"{VPS_URL}/get-quote?symbol={symbol}", VPS_HEADERS)
    prices = data.get("prices", data)
    for v in prices.values():
        if isinstance(v, dict):
            return float(v.get("last_price", 0))
    vals = list(prices.values())
    return float(vals[0]) if vals else 0.0


def place_order(symbol: str, qty: int, side: str, order_type: str, price: float = None) -> dict:
    payload = {
        "symbol": symbol, "side": side,
        "quantity": int(qty), "order_type": order_type,
        "product": "CNC", "tag": "ind-auto",
    }
    if price and order_type == "LIMIT":
        payload["price"] = round(price, 2)
    return _post(f"{VPS_URL}/place-order", payload, VPS_HEADERS)


def update_queue(updates: list):
    _post(f"{RAILWAY_URL}/india/queue/update", updates, RAILWAY_HEADERS)


def process_buys(msgs: list) -> tuple:
    buy_queue = get_queue("BUY")
    placed, skipped, errors = [], [], []

    for entry in buy_queue:
        ticker = entry.get("ticker", "")
        symbol = entry.get("nse_symbol") or ticker.replace(".NS", "")
        qty    = int(entry.get("quantity") or 0)
        opt    = float(entry.get("optimal_entry") or 0)
        gate   = float(entry.get("limit_price") or (opt * 1.03 if opt else 0))

        if qty <= 0:
            errors.append(symbol)
            msgs.append(f"❌ {symbol}: qty=0, skipping")
            continue

        try:
            live = get_live_price(symbol)
        except Exception as e:
            errors.append(symbol)
            msgs.append(f"❌ {symbol}: price fetch failed — {e}")
            continue

        print(f"BUY {symbol}: live ₹{live:.2f} | optimal ₹{opt:.2f} | gate ₹{gate:.2f}")

        if gate and live > gate:
            msg = f"⏭ {symbol}: gapped above gate (₹{live:.0f} > ₹{gate:.0f}) — skipped"
            print(msg); skipped.append(symbol); msgs.append(msg)
            update_queue([{"ticker": ticker, "status": "skipped", "skip_price": live}])
            continue

        order_type  = "MARKET" if (opt > 0 and live <= opt) else "LIMIT"
        place_price = None if order_type == "MARKET" else (opt if opt > 0 else live)
        disp        = f"MARKET ~₹{live:.0f}" if order_type == "MARKET" else f"LIMIT ₹{place_price:.0f}"

        if DRY_RUN:
            print(f"  [DRY RUN] would place BUY {order_type} {qty}sh @ {disp}")
            placed.append(symbol)
            msgs.append(f"🧪 BUY {symbol}: would place {qty}sh @ {disp}")
        else:
            try:
                result   = place_order(symbol, qty, "BUY", order_type, place_price)
                order_id = result.get("order_id")
                if not order_id:
                    raise ValueError(f"No order_id: {result}")
                update_queue([{
                    "ticker": ticker, "status": "order_placed",
                    "order_id": str(order_id), "placed_price": live,
                    "placed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }])
                placed.append(symbol)
                msgs.append(f"✅ BUY {symbol}: {qty}sh @ {disp} (#{order_id})")
            except Exception as e:
                errors.append(symbol)
                msgs.append(f"❌ BUY {symbol}: order failed — {e}")

        time.sleep(0.5)

    return placed, skipped, errors


def process_sells(msgs: list) -> tuple:
    sell_queue = get_queue("SELL")
    placed, errors = [], []

    for entry in sell_queue:
        ticker = entry.get("ticker", "")
        symbol = entry.get("nse_symbol") or ticker.replace(".NS", "")
        qty    = int(entry.get("quantity") or 0)
        action = entry.get("rebalancer_action", "SELL")
        pct    = entry.get("trim_pct", 100)

        if qty <= 0:
            errors.append(symbol)
            msgs.append(f"❌ SELL {symbol}: qty=0 — too small to sell ({pct}% of held shares)")
            continue

        if DRY_RUN:
            print(f"  [DRY RUN] would place SELL MARKET {qty}sh {symbol} ({action} {pct}%)")
            placed.append(symbol)
            msgs.append(f"🧪 SELL {symbol}: would sell {qty}sh MARKET ({action} {pct}%)")
        else:
            try:
                result   = place_order(symbol, qty, "SELL", "MARKET")
                order_id = result.get("order_id")
                if not order_id:
                    raise ValueError(f"No order_id: {result}")
                update_queue([{
                    "ticker": ticker, "status": "order_placed",
                    "order_id": str(order_id),
                    "placed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }])
                placed.append(symbol)
                msgs.append(f"✅ SELL {symbol}: {qty}sh MARKET ({action} {pct}%) (#{order_id})")
            except Exception as e:
                errors.append(symbol)
                msgs.append(f"❌ SELL {symbol}: order failed — {e}")

        time.sleep(0.5)

    return placed, errors


def main():
    tag = "[DRY RUN]" if DRY_RUN else "[LIVE]"
    print(f"=== India Monthly Entry 9:15 AM {tag} ===")

    buy_q  = get_queue("BUY")
    sell_q = get_queue("SELL")

    if not buy_q and not sell_q:
        print("No queued India entries.")
        _tg("⏸ <b>India Entry 9:15 AM</b>\nNo entries queued.")
        return

    dry = " 🧪 <i>DRY RUN</i>" if DRY_RUN else ""
    msgs = [f"📈 <b>India Monthly 9:15 AM</b> ({len(buy_q)} buys · {len(sell_q)} sells){dry}"]

    b_placed, b_skipped, b_errors = process_buys(msgs)
    s_placed, s_errors            = process_sells(msgs)

    total_placed  = len(b_placed)  + len(s_placed)
    total_skipped = len(b_skipped)
    total_errors  = len(b_errors)  + len(s_errors)

    summary = (f"\n✅ {total_placed} placed · "
               f"⏭ {total_skipped} skipped · "
               f"❌ {total_errors} errors")
    msgs.append(summary)
    _tg("\n".join(msgs))
    print(summary)


if __name__ == "__main__":
    main()
