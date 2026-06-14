#!/usr/bin/env python3
"""
swing_cancel.py — 2:00 PM IST cron: cancel unfilled swing LIMIT orders.

For each "order_placed" queue entry:
  - Check order status in Zerodha via VPS /get-orders
  - If OPEN → cancel it, update queue status to "cancelled"
  - If COMPLETE → mark queue as "filled" (postback may have missed it)
  - Send Telegram summary
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

RAILWAY_HEADERS = {
    "X-Upload-Token": UPLOAD_TOKEN,
    "Content-Type": "application/json",
}
VPS_HEADERS = {"X-Executor-Secret": EXECUTOR_SECRET}


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
        _post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT}/sendMessage",
            {"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "HTML"},
        )
    except Exception as e:
        print(f"Telegram error: {e}")


def get_placed_queue():
    data = _get(f"{RAILWAY_URL}/swing/queue?status=order_placed", RAILWAY_HEADERS)
    return data.get("queue", [])


def get_open_orders() -> dict:
    """Returns {order_id: order_dict} for today's orders."""
    data = _get(f"{VPS_URL}/get-orders", VPS_HEADERS)
    orders = data.get("orders") or (data if isinstance(data, list) else [])
    return {str(o.get("order_id", "")): o for o in orders}


def cancel_order(order_id: str) -> dict:
    return _post(f"{VPS_URL}/cancel-order", {"order_id": order_id}, VPS_HEADERS)


def update_queue(updates: list):
    _post(f"{RAILWAY_URL}/swing/queue/update", updates, RAILWAY_HEADERS)


def main():
    print("=== Swing Cancel 2:00 PM ===")
    queue = get_placed_queue()
    if not queue:
        print("No order_placed entries — nothing to cancel.")
        _tg("⏸ <b>Swing Cancel 2:00 PM</b>\nNo open orders to cancel.")
        return

    try:
        open_orders = get_open_orders()
        print(f"Fetched {len(open_orders)} orders from Zerodha")
    except Exception as e:
        msg = f"❌ <b>Swing Cancel 2:00 PM</b>\nCould not fetch orders: {e}"
        print(f"⚠️  {msg}")
        _tg(msg)
        return

    cancelled, filled, errors = [], [], []
    msgs = [f"⏸ <b>Swing Cancel 2:00 PM</b> ({len(queue)} to check)"]

    for entry in queue:
        ticker   = entry.get("ticker", "")
        symbol   = entry.get("nse_symbol") or ticker.replace(".NS", "")
        order_id = str(entry.get("order_id", ""))

        if not order_id:
            print(f"⚠️  {symbol}: no order_id, skipping")
            continue

        order = open_orders.get(order_id)
        if not order:
            # Not in today's order book — might have been filled earlier
            msg = f"ℹ️ {symbol}: order #{order_id} not found — may have filled already"
            print(msg)
            msgs.append(msg)
            filled.append(symbol)
            continue

        kite_status = str(order.get("status", "")).upper()
        avg_price   = order.get("average_price", 0)

        if kite_status == "COMPLETE":
            msg = f"✅ {symbol}: filled @ ₹{avg_price}"
            print(msg)
            msgs.append(msg)
            filled.append(symbol)
            update_queue([{
                "ticker":     ticker,
                "status":     "filled",
                "fill_price": float(avg_price or 0),
                "order_id":   order_id,
            }])
            continue

        if kite_status in ("CANCELLED", "REJECTED"):
            msg = f"⏭ {symbol}: already {kite_status.lower()}"
            print(msg)
            msgs.append(msg)
            cancelled.append(symbol)
            continue

        if kite_status not in ("OPEN", "TRIGGER PENDING", "AMO REQ RECEIVED"):
            msg = f"ℹ️ {symbol}: status={kite_status}, leaving as-is"
            print(msg)
            msgs.append(msg)
            continue

        try:
            cancel_order(order_id)
            cancelled.append(symbol)
            msg = f"⏸ {symbol}: LIMIT order #{order_id} cancelled"
            print(msg)
            msgs.append(msg)
            update_queue([{
                "ticker":       ticker,
                "status":       "cancelled",
                "cancelled_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }])
        except Exception as e:
            print(f"⚠️  Cancel failed for {symbol} (#{order_id}): {e}")
            errors.append(symbol)
            msgs.append(f"❌ {symbol}: cancel failed — {e}")

        time.sleep(0.3)

    summary = (f"\n⏸ {len(cancelled)} cancelled · "
               f"✅ {len(filled)} filled/not-found · "
               f"❌ {len(errors)} errors")
    msgs.append(summary)
    _tg("\n".join(msgs))
    print(summary)


if __name__ == "__main__":
    main()
