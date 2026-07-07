#!/usr/bin/env python3
"""
swing_entry.py — 9:00 AM IST cron: place orders for queued swing trades.

For each "queued" entry:
  - Fetch live price from VPS /get-quote
  - If live > limit_price  → skip (gap-up, Telegram alert)
  - If live ≤ optimal_entry → MARKET order (already dipped to target)
  - Else between optimal and gate → LIMIT order at optimal_entry
  - POST /swing/queue/update with order_id and new status

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


def get_queue():
    data = _get(f"{RAILWAY_URL}/swing/queue?status=queued", RAILWAY_HEADERS)
    return data.get("queue", [])


def get_live_price(symbol: str) -> float:
    data = _get(f"{VPS_URL}/get-quote?symbol={symbol}", VPS_HEADERS)
    prices = data.get("prices", data)
    for v in prices.values():
        if isinstance(v, dict):
            return float(v.get("last_price", 0))
    vals = list(prices.values())
    return float(vals[0]) if vals else 0.0


def place_order(symbol: str, qty: int, order_type: str, price: float = None) -> dict:
    payload = {
        "symbol": symbol,
        "side": "BUY",
        "quantity": int(qty),
        "order_type": order_type,
        "product": "CNC",
        "tag": "sw-auto",
    }
    if price and order_type == "LIMIT":
        payload["price"] = round(price, 2)
    try:
        return _post(f"{VPS_URL}/place-order", payload, VPS_HEADERS)
    except urllib.error.HTTPError as e:
        # The executor returns the real Zerodha rejection in the JSON body;
        # surface it instead of a bare "400 BAD REQUEST" so the log is useful.
        try:
            detail = json.loads(e.read()).get("error", "")
        except Exception:
            detail = ""
        raise RuntimeError(f"HTTP {e.code} — {detail or e.reason}")


def update_queue(updates: list):
    _post(f"{RAILWAY_URL}/swing/queue/update", updates, RAILWAY_HEADERS)


def main():
    print(f"=== Swing Entry 9:00 AM {'[DRY RUN]' if DRY_RUN else '[LIVE]'} ===")
    try:
        queue = get_queue()
    except Exception as e:
        # A crash here (Railway cold-start, network blip, bad token) used to
        # kill the whole run with NO alert — queued orders silently never
        # placed. Fail loudly so it can never be invisible again.
        print(f"🚨 Could not fetch the swing queue: {e}")
        _tg(f"🚨 <b>Swing Entry 9:00 AM FAILED</b>\nCould not read the queue: {e}\n"
            f"Queued orders were NOT placed — check the VPS / run manually.")
        raise
    if not queue:
        print("No queued swing entries.")
        _tg("⏸ <b>Swing Entry 9:00 AM</b>\nNo entries queued.")
        return

    placed, skipped, errors = [], [], []
    dry_tag = " 🧪 <i>DRY RUN — no real orders</i>" if DRY_RUN else ""
    msgs = [f"🔔 <b>Swing Entry 9:00 AM</b> ({len(queue)} queued){dry_tag}"]

    for entry in queue:
        ticker = entry.get("ticker", "")
        symbol = entry.get("nse_symbol") or ticker.replace(".NS", "").replace(".BO", "")
        qty    = int(entry.get("quantity") or 0)
        opt    = float(entry.get("optimal_entry") or 0)
        gate   = float(entry.get("limit_price") or (opt * 1.02 if opt else 0))

        if qty <= 0:
            print(f"⚠️  {symbol}: qty=0, skipping")
            errors.append(symbol)
            continue

        try:
            live = get_live_price(symbol)
        except Exception as e:
            print(f"⚠️  Price fetch failed for {symbol}: {e}")
            errors.append(symbol)
            msgs.append(f"❌ {symbol}: price fetch failed — {e}")
            continue

        print(f"{symbol}: live ₹{live:.2f} | optimal ₹{opt:.2f} | gate ₹{gate:.2f}")

        if gate and live > gate:
            msg = f"⏭ {symbol}: gapped above gate (live ₹{live:.0f} > gate ₹{gate:.0f}) — skipped"
            print(msg)
            skipped.append(symbol)
            msgs.append(msg)
            update_queue([{"ticker": ticker, "status": "skipped", "skip_price": live}])
            continue

        if opt > 0 and live <= opt:
            order_type = "MARKET"
            place_price = None
            print(f"  → MARKET (live {live:.2f} ≤ optimal {opt:.2f})")
        else:
            order_type = "LIMIT"
            place_price = opt if opt > 0 else live
            print(f"  → LIMIT @ ₹{place_price:.2f}")

        disp = f"₹{place_price:.0f}" if order_type == "LIMIT" else f"MARKET ~₹{live:.0f}"
        if DRY_RUN:
            print(f"  [DRY RUN] would place {order_type} {qty}sh @ {disp}")
            placed.append(symbol)
            msgs.append(f"🧪 {symbol}: would place {order_type} {qty}sh @ {disp}")
        else:
            try:
                result = place_order(symbol, qty, order_type, place_price)
                order_id = result.get("order_id")
                if not order_id:
                    raise ValueError(f"No order_id in response: {result}")
                update_queue([{
                    "ticker":       ticker,
                    "status":       "order_placed",
                    "order_id":     str(order_id),
                    "placed_price": live,
                    "placed_at":    time.strftime("%Y-%m-%dT%H:%M:%S"),
                }])
                placed.append(symbol)
                msgs.append(f"✅ {symbol}: {order_type} {qty}sh @ {disp} (#{order_id})")
            except Exception as e:
                print(f"⚠️  Order failed for {symbol}: {e}")
                errors.append(symbol)
                msgs.append(f"❌ {symbol}: order failed — {e}")

        time.sleep(0.5)

    summary = f"\n✅ {len(placed)} placed · ⏭ {len(skipped)} skipped · ❌ {len(errors)} errors"
    msgs.append(summary)
    _tg("\n".join(msgs))
    print(summary)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Last-resort guard: any uncaught error gets surfaced to Telegram
        # instead of dying quietly in the cron log.
        print(f"🚨 Swing entry crashed: {e}")
        try:
            _tg(f"🚨 <b>Swing Entry 9:00 AM crashed</b>\n{e}\n"
                f"Queued orders may NOT have been placed — check the VPS.")
        except Exception:
            pass
        raise
