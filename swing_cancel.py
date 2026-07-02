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


def place_sell_gtt(symbol: str, trigger, qty: int, last_price: float):
    """SELL GTT in the executor's contract (LIMIT leg 0.5% through the
    trigger, tick-rounded). Returns gtt_id or None."""
    trigger = round(float(trigger), 2)
    limit_price = round(round(trigger * 0.995 / 0.05) * 0.05, 2)
    try:
        res = _post(f"{VPS_URL}/place-gtt", {
            "symbol":         symbol,
            "trigger_values": [trigger],
            "last_price":     float(last_price),
            "orders": [{
                "transaction_type": "SELL",
                "quantity":         int(qty),
                "product":          "CNC",
                "order_type":       "LIMIT",
                "price":            limit_price,
            }],
        }, VPS_HEADERS)
        gtt_id = res.get("gtt_id")
        if not gtt_id:
            print(f"  ⚠️  GTT place failed: {res.get('error', res)}")
        return gtt_id
    except Exception as e:
        print(f"  ⚠️  GTT place failed: {e}")
        return None


def protect_fill(entry: dict, symbol: str, fill_price: float, fill_qty: int) -> str:
    """Backstop for fills the postback never processed — a missed postback
    (COMPLETE order still sitting in the queue at 2 PM) or a PARTIALLY
    filled LIMIT cancelled at 2 PM. Both used to leave real shares with no
    stop, no live record, and a stale queue status. Mirrors api.py's
    fill-postback handling: exit GTTs + queue update + live-position sync.
    Returns a note for the Telegram summary."""
    ticker = entry.get("ticker", "")
    if entry.get("gtt_id"):
        return "GTTs already in place"
    if fill_price <= 0 or fill_qty <= 0:
        return "⚠️ no usable fill price/qty — check position manually on Kite"

    stop    = entry.get("stop_loss")
    target1 = entry.get("target1")
    target2 = entry.get("target2")
    qty_t1  = fill_qty // 2
    qty_t2  = fill_qty - qty_t1

    gtt_id    = place_sell_gtt(symbol, stop, fill_qty, fill_price) if stop else None
    gtt_t1_id = place_sell_gtt(symbol, target1, qty_t1, fill_price) if target1 and qty_t1 else None
    gtt_t2_id = place_sell_gtt(symbol, target2, qty_t2, fill_price) if target2 and qty_t2 else None

    today = time.strftime("%Y-%m-%d")
    update_queue([{
        "ticker":     ticker,
        "status":     "filled",
        "fill_price": fill_price,
        "fill_qty":   fill_qty,
        "fill_date":  today,
        "stop_qty":   fill_qty,
        "trail_high": fill_price,
        "gtt_id":     gtt_id,
        "gtt_t1_id":  gtt_t1_id,
        "gtt_t2_id":  gtt_t2_id,
    }])

    try:
        _post(f"{RAILWAY_URL}/swing/live/upload", {
            "ticker":          ticker,
            "name":            entry.get("name", symbol),
            "buy_price":       fill_price,
            "price":           fill_price,
            "stop_loss":       stop,
            "stop_loss_price": stop,
            "stop_pct":        entry.get("stop_pct"),
            "target1":         target1,
            "target2":         target2,
            "trailing_stop":   entry.get("trail_atr"),
            "rr_ratio":        entry.get("rr_ratio"),
            "conviction":      entry.get("conviction"),
            "score":           entry.get("score"),
            "max_score":       entry.get("max_score", 100),
            "atr":             entry.get("atr"),
            "sector":          entry.get("sector"),
            "sentiment_val":   entry.get("sentiment_val"),
            "sentiment_bucket": entry.get("sentiment_bucket"),
            "entry_type":      entry.get("entry_type"),
            "regime":          entry.get("regime"),
            "entry_date":      today,
            "buy_date":        today,
            "shares":          fill_qty,
            "signals":         entry.get("signals"),
            "order_id":        entry.get("order_id"),
            "source":          "cancel_backstop",
        }, RAILWAY_HEADERS)
    except Exception as e:
        print(f"  ⚠️  Live-position sync failed: {e}")

    if stop and not gtt_id:
        return (f"🚨 stop GTT FAILED — manually place stop for "
                f"{fill_qty}sh at ₹{stop} NOW")
    return f"stop+targets GTTs placed for {fill_qty}sh ✅"


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
            # Missed postback — mark filled AND place the exit GTTs the
            # postback would have (the old path marked filled with no stop)
            fill_qty = int(order.get("filled_quantity") or entry.get("quantity") or 0)
            note = protect_fill(entry, symbol, float(avg_price or 0), fill_qty)
            msg = f"✅ {symbol}: filled @ ₹{avg_price} — {note}"
            print(msg)
            msgs.append(msg)
            filled.append(symbol)
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
            part_qty = int(order.get("filled_quantity") or 0)
            if part_qty > 0:
                # Partially filled — those shares are REAL and held. The old
                # path marked the whole entry "cancelled", leaving them with
                # no stop and invisible to the alerter.
                part_price = float(avg_price or 0) or float(order.get("price") or 0)
                note = protect_fill(entry, symbol, part_price, part_qty)
                filled.append(symbol)
                msg = (f"◗ {symbol}: PARTIAL fill {part_qty}sh @ ₹{part_price} "
                       f"(remainder cancelled) — {note}")
            else:
                cancelled.append(symbol)
                msg = f"⏸ {symbol}: LIMIT order #{order_id} cancelled"
                update_queue([{
                    "ticker":       ticker,
                    "status":       "cancelled",
                    "cancelled_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }])
            print(msg)
            msgs.append(msg)
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
