#!/usr/bin/env python3
"""
trailing_stop.py — 2:30 PM IST cron: update trailing stop-loss GTTs.

For every filled swing trade and India monthly BUY with trail_atr set:
  - Fetch current live price
  - If live > trail_high: compute new_stop = live - trail_atr (ATR-based distance)
    → cancel old stop GTT, place new one, update trail_high in queue
  - Telegram summary of all adjustments

Runs once daily at 2:30 PM IST (9:00 AM UTC) on weekdays.
"""

import os
import json
import math
import time
import urllib.request as _req
import urllib.error

RAILWAY_URL     = os.environ["API_URL"]
UPLOAD_TOKEN    = os.environ["UPLOAD_TOKEN"]
VPS_URL         = os.environ.get("ORACLE_VPS_URL", "http://localhost:5001")
EXECUTOR_SECRET = os.environ["EXECUTOR_SECRET"]
TELEGRAM_BOT    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT   = os.environ.get("TELEGRAM_CHAT_ID", "")

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


def get_live_price(symbol: str) -> float:
    data = _get(f"{VPS_URL}/get-quote?symbol={symbol}", VPS_HEADERS)
    prices = data.get("prices", data)
    for v in prices.values():
        if isinstance(v, dict):
            return float(v.get("last_price", 0))
    vals = list(prices.values())
    return float(vals[0]) if vals else 0.0


def cancel_gtt(gtt_id) -> bool:
    if not gtt_id:
        return False
    try:
        _post(f"{VPS_URL}/cancel-gtt", {"gtt_id": gtt_id}, VPS_HEADERS)
        print(f"  🗑 Cancelled GTT {gtt_id}")
        return True
    except Exception as e:
        print(f"  ⚠️  GTT cancel failed ({gtt_id}): {e}")
        return False


def place_gtt(symbol: str, trigger_price: float, qty: int, last_price: float) -> str | None:
    """Place a SELL GTT in /place-gtt's actual contract:
    {symbol, trigger_values, last_price, orders}. Kite GTT legs must be
    LIMIT orders — price the limit 0.5% through the trigger (tick-rounded)
    so it fills like a market order once triggered."""
    trigger = round(trigger_price, 2)
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


def update_swing_queue(updates: list):
    _post(f"{RAILWAY_URL}/swing/queue/update", updates, RAILWAY_HEADERS)


def update_india_queue(updates: list):
    _post(f"{RAILWAY_URL}/india/queue/update", updates, RAILWAY_HEADERS)


def process_queue(entries: list, queue_type: str, update_fn) -> list:
    """
    For each filled entry with trail_atr set, check if live price is a new
    high and update the stop GTT accordingly. Returns list of adjustment messages.
    """
    msgs = []

    for entry in entries:
        trail_atr = entry.get("trail_atr")
        if not trail_atr:
            continue

        ticker    = entry.get("ticker", "")
        symbol    = entry.get("nse_symbol") or ticker.replace(".NS", "")
        name      = entry.get("name", symbol)
        atr_dist  = float(trail_atr)

        trail_high = float(entry.get("trail_high") or entry.get("fill_price") or 0)
        stop_qty   = int(entry.get("stop_qty") or entry.get("fill_qty") or entry.get("quantity") or 0)
        gtt_id     = entry.get("gtt_id")

        if trail_high <= 0 or stop_qty <= 0:
            continue

        try:
            live = get_live_price(symbol)
        except Exception as e:
            print(f"  ⚠️  Price fetch failed for {symbol}: {e}")
            msgs.append(f"❌ {symbol}: price fetch failed — {e}")
            continue

        print(f"[{queue_type}] {symbol}: live ₹{live:.2f} | trail_high ₹{trail_high:.2f} | ATR dist ₹{atr_dist:.2f}")

        if live <= trail_high:
            print(f"  → no new high, stop unchanged")
            continue

        new_stop = round(live - atr_dist, 2)
        old_stop = entry.get("tsl_stop") or entry.get("stop_loss", "?")

        print(f"  → new high! stop ₹{old_stop} → ₹{new_stop} (₹{live:.2f} − ₹{atr_dist:.2f} ATR)")

        cancel_gtt(gtt_id)
        new_gtt_id = place_gtt(symbol, new_stop, stop_qty, live)

        update_fn([{
            "ticker":     ticker,
            "trail_high": live,
            "tsl_stop":   new_stop,
            "gtt_id":     new_gtt_id,
        }])

        if new_gtt_id:
            msgs.append(
                f"📈 <b>{name}</b> ({symbol})\n"
                f"   New high ₹{live:.2f} → stop raised to ₹{new_stop:.2f} (−₹{atr_dist:.2f} ATR)\n"
                f"   GTT updated ✅"
            )
        else:
            msgs.append(
                f"⚠️ <b>{name}</b> ({symbol})\n"
                f"   New high ₹{live:.2f} — stop raised to ₹{new_stop:.2f} but GTT placement failed!\n"
                f"   Manually place stop for {stop_qty} shares at ₹{new_stop}"
            )

        time.sleep(0.5)

    return msgs


def main():
    print("=== Trailing Stop Update 2:30 PM ===")

    # Fetch filled swing entries
    try:
        sw_data = _get(f"{RAILWAY_URL}/swing/queue?status=filled", RAILWAY_HEADERS)
        sw_entries = sw_data.get("queue", [])
    except Exception as e:
        print(f"⚠️  Could not fetch swing queue: {e}")
        sw_entries = []

    # Fetch filled India BUY entries
    try:
        ind_data = _get(f"{RAILWAY_URL}/india/queue?status=filled&action=BUY", RAILWAY_HEADERS)
        ind_entries = ind_data.get("queue", [])
    except Exception as e:
        print(f"⚠️  Could not fetch India queue: {e}")
        ind_entries = []

    tsl_sw  = [e for e in sw_entries  if e.get("trail_atr")]
    tsl_ind = [e for e in ind_entries if e.get("trail_atr")]

    if not tsl_sw and not tsl_ind:
        print("No positions with trailing stop enabled — nothing to do.")
        _tg("📊 <b>Trailing Stop 2:30 PM</b>\nNo TSL positions active.")
        return

    msgs = [f"📊 <b>Trailing Stop 2:30 PM</b> ({len(tsl_sw)} swing · {len(tsl_ind)} monthly)"]

    sw_msgs  = process_queue(tsl_sw,  "SWING", update_swing_queue)
    ind_msgs = process_queue(tsl_ind, "INDIA", update_india_queue)

    all_msgs = sw_msgs + ind_msgs
    if all_msgs:
        msgs += all_msgs
    else:
        msgs.append("No new highs — all stops unchanged.")

    _tg("\n".join(msgs))
    print(f"Done. {len(all_msgs)} stop(s) updated.")


if __name__ == "__main__":
    main()
