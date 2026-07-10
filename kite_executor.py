#!/usr/bin/env python3
"""
Kite order executor — Flask server running on Oracle VPS (static IP 80.225.201.62).
The static IP is required for SEBI-compliant API order routing.

Start manually:   python kite_executor.py
Start via systemd: see setup instructions below.

Endpoints:
  GET  /health            — liveness check
  POST /exchange-token    — called by Railway /kite/callback to swap request_token → access_token
  POST /place-order       — place a market/limit order on Kite
  POST /cancel-order      — cancel a pending order
  GET  /get-pnl           — positions + holdings + aggregate P&L
  GET  /get-orders        — today's order list
  POST /place-gtt         — place a GTT stop-loss order
  POST /cancel-gtt        — delete a GTT trigger by id
  GET  /get-quote         — live LTP for one or more NSE symbols
  GET  /get-historical    — daily OHLCV history for an NSE symbol (from Zerodha)
"""
import os
import threading
import logging
from datetime import date, datetime, timedelta
from flask import Flask, request, jsonify
from kiteconnect import KiteConnect
try:
    from kiteconnect import KiteException
except ImportError:
    from kiteconnect.exceptions import KiteException
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

API_KEY          = os.environ["KITE_API_KEY"]
API_SECRET       = os.environ["KITE_API_SECRET"]
TOKEN_FILE       = os.path.join(BASE_DIR, "access_token.txt")
EXECUTOR_SECRET  = os.environ.get("EXECUTOR_SECRET", "")

app = Flask(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
)
log = logging.getLogger(__name__)


def _check_auth():
    """Return a 401 Flask response if the shared secret doesn't match, else None."""
    if not EXECUTOR_SECRET:
        return None
    if request.headers.get("X-Executor-Secret", "") != EXECUTOR_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    return None


def _get_kite() -> KiteConnect:
    if not os.path.exists(TOKEN_FILE) or os.path.getsize(TOKEN_FILE) == 0:
        raise RuntimeError("access_token.txt missing — run token_refresh.py first")
    with open(TOKEN_FILE) as f:
        token = f.read().strip()
    kite = KiteConnect(api_key=API_KEY)
    kite.set_access_token(token)
    return kite


# ── Health ────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    token_ok = os.path.exists(TOKEN_FILE) and os.path.getsize(TOKEN_FILE) > 0
    return jsonify({"status": "ok", "token_ready": token_ok})


# ── Token exchange (called by Railway after OAuth redirect) ───────────────────

@app.route("/exchange-token", methods=["POST"])
def exchange_token():
    """
    Railway's /kite/callback forwards the request_token here.
    We exchange it for an access_token and save it locally.
    """
    err = _check_auth()
    if err:
        return err
    data = request.get_json(force=True) or {}
    request_token = data.get("request_token", "").strip()
    if not request_token:
        return jsonify({"error": "request_token required"}), 400
    try:
        kite = KiteConnect(api_key=API_KEY)
        session_data = kite.generate_session(request_token, api_secret=API_SECRET)
        access_token = session_data["access_token"]
        with open(TOKEN_FILE, "w") as f:
            f.write(access_token)
        os.chmod(TOKEN_FILE, 0o600)
        log.info("✅ Token exchanged via callback for user %s", session_data.get("user_id"))
        return jsonify({"status": "ok", "user_id": session_data.get("user_id")})
    except KiteException as e:
        log.error("Kite error during token exchange: %s", e)
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        log.error("Token exchange failed: %s", e)
        return jsonify({"error": str(e)}), 500


# ── Place order ───────────────────────────────────────────────────────────────

@app.route("/place-order", methods=["POST"])
def place_order():
    """
    Body (JSON):
      symbol       — NSE trading symbol, e.g. "RELIANCE"
      side         — "BUY" or "SELL"
      quantity     — integer
      order_type   — "MARKET" (default) | "LIMIT" | "SL" | "SL-M"
      price        — required for LIMIT orders
      trigger_price — required for SL / SL-M orders
      product      — "CNC" (default, delivery) | "MIS" (intraday) | "NRML"
      exchange     — "NSE" (default) | "BSE"
      tag          — optional label (max 20 chars)
    """
    err = _check_auth()
    if err:
        return err
    data = request.get_json(force=True) or {}
    symbol = data.get("symbol", "").strip().upper()
    if not symbol or not data.get("quantity"):
        return jsonify({"error": "symbol and quantity are required"}), 400
    try:
        kite       = _get_kite()
        order_type = data.get("order_type", "MARKET").upper()
        side       = data.get("side", "BUY").upper()
        exchange   = data.get("exchange", kite.EXCHANGE_NSE)
        price      = data.get("price")

        # Zerodha API rejects bare MARKET orders ("market protection" required).
        # Convert to a LIMIT order priced 0.5% through the market so it fills
        # immediately like a market order would, while satisfying the API.
        if order_type == "MARKET" and not price:
            try:
                ltp_data = kite.ltp(f"{exchange}:{symbol}")
                ltp = ltp_data[f"{exchange}:{symbol}"]["last_price"]
                if side == "BUY":
                    price = round(ltp * 1.005, 2)
                else:
                    price = round(ltp * 0.995, 2)
                order_type = "LIMIT"
                log.info(
                    "🔄 MARKET→LIMIT conversion: %s %s LTP=%.2f limit=%.2f",
                    side, symbol, ltp, price,
                )
            except Exception as ltp_err:
                log.warning("LTP fetch failed (%s) — attempting bare MARKET order", ltp_err)

        # Server-side safety net: snap any LIMIT/SL price to the script's
        # ACTUAL tick size (0.05 for most NSE equities, but 0.10/0.20/0.50
        # for some — e.g. SHRIRAMFIN is 0.10). Zerodha rejects any price
        # that isn't a multiple of the script's tick with a 400.
        _ensure_instruments(kite)          # make sure tick sizes are loaded
        price = _round_to_tick(symbol, price)
        trig  = _round_to_tick(symbol, data.get("trigger_price"))

        order_id = kite.place_order(
            variety=data.get("variety", kite.VARIETY_REGULAR),
            exchange=exchange,
            tradingsymbol=symbol,
            transaction_type=(
                kite.TRANSACTION_TYPE_BUY
                if side == "BUY"
                else kite.TRANSACTION_TYPE_SELL
            ),
            quantity=int(data["quantity"]),
            product=data.get("product", kite.PRODUCT_CNC),
            order_type=order_type,
            price=price,
            trigger_price=trig,
            tag=(data.get("tag", "eq-advisor") or "eq-advisor")[:20],
        )
        log.info(
            "✅ Order placed: %s  %s %s  price=%s  qty=%s  order_id=%s",
            side, symbol, order_type, price,
            data.get("quantity"), order_id,
        )
        return jsonify({"order_id": order_id, "status": "placed"})
    except KiteException as e:
        log.error("Kite error: %s", e)
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        log.error("place_order error: %s", e)
        return jsonify({"error": str(e)}), 500


# ── Cancel order ──────────────────────────────────────────────────────────────

@app.route("/cancel-order", methods=["POST"])
def cancel_order():
    """Body: { order_id, variety (optional) }"""
    err = _check_auth()
    if err:
        return err
    data = request.get_json(force=True) or {}
    order_id = data.get("order_id", "").strip()
    if not order_id:
        return jsonify({"error": "order_id required"}), 400
    try:
        kite = _get_kite()
        kite.cancel_order(
            variety=data.get("variety", kite.VARIETY_REGULAR),
            order_id=order_id,
        )
        log.info("✅ Order cancelled: %s", order_id)
        return jsonify({"status": "cancelled", "order_id": order_id})
    except KiteException as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── P&L ───────────────────────────────────────────────────────────────────────

@app.route("/get-pnl", methods=["GET"])
def get_pnl():
    """Returns positions, holdings, and aggregate P&L summary."""
    err = _check_auth()
    if err:
        return err
    try:
        kite = _get_kite()
        positions = kite.positions()
        holdings  = kite.holdings()

        day_pnl = sum(
            float(p.get("pnl") or 0)
            for p in positions.get("day", [])
        )
        holdings_pnl = sum(
            float(h.get("pnl") or 0)
            for h in holdings
        )

        return jsonify({
            "positions": positions,
            "holdings":  holdings,
            "summary": {
                "day_pnl":      round(day_pnl, 2),
                "holdings_pnl": round(holdings_pnl, 2),
                "total_pnl":    round(day_pnl + holdings_pnl, 2),
            },
        })
    except KiteException as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Orders list ───────────────────────────────────────────────────────────────

@app.route("/get-orders", methods=["GET"])
def get_orders():
    """Today's order book."""
    err = _check_auth()
    if err:
        return err
    try:
        return jsonify({"orders": _get_kite().orders()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── GTT (Good Till Triggered) ─────────────────────────────────────────────────

@app.route("/get-quote", methods=["GET"])
def get_quote():
    """
    Fetch live last-traded price(s) from Zerodha.
    Query param: symbol=RELIANCE  or  symbol=RELIANCE,TCS,INFY
    Exchange defaults to NSE; prefix with BSE: to override.
    Returns: { "RELIANCE": { "last_price": 1452.30, "exchange": "NSE" }, ... }
    """
    err = _check_auth()
    if err:
        return err
    raw = request.args.get("symbol", "").strip().upper()
    if not raw:
        return jsonify({"error": "symbol query param required"}), 400
    try:
        kite = _get_kite()
        symbols = [s.strip() for s in raw.split(",") if s.strip()]
        # kite.ltp accepts "NSE:RELIANCE" format
        keys = []
        for s in symbols:
            keys.append(s if ":" in s else f"NSE:{s}")
        data = kite.ltp(keys)
        result = {}
        for key, val in data.items():
            exchange, sym = key.split(":", 1)
            result[sym] = {
                "last_price": val.get("last_price"),
                "exchange":   exchange,
            }
        return jsonify(result)
    except KiteException as e:
        log.error("Kite quote error: %s", e)
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        log.error("get_quote error: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/place-gtt", methods=["POST"])
def place_gtt():
    """
    Place a GTT stop-loss order (stays on Zerodha servers up to 1 year).
    Body:
      symbol         — NSE trading symbol
      trigger_values — list of trigger prices, e.g. [450.0]
      last_price     — current market price (required by Kite API)
      orders         — list of order dicts, e.g.:
                       [{"transaction_type":"SELL","quantity":10,
                         "product":"CNC","order_type":"LIMIT","price":448.0}]
      trigger_type   — "single" (default) | "two-leg"
      exchange       — "NSE" (default)
    """
    err = _check_auth()
    if err:
        return err
    data = request.get_json(force=True) or {}
    required = ["symbol", "trigger_values", "last_price", "orders"]
    missing = [k for k in required if not data.get(k)]
    if missing:
        return jsonify({"error": f"Missing required fields: {missing}"}), 400
    try:
        kite = _get_kite()
        sym = data["symbol"].strip().upper()
        # Snap trigger(s) and each order's LIMIT price to the script's real
        # tick size, or Zerodha 400s on non-0.05-tick scripts (e.g. 0.10).
        _ensure_instruments(kite)
        triggers = [_round_to_tick(sym, t) for t in data["trigger_values"]]
        orders = []
        for o in data["orders"]:
            o = dict(o)
            if o.get("price") is not None:
                o["price"] = _round_to_tick(sym, o["price"])
            orders.append(o)
        gtt_id = kite.place_gtt(
            trigger_type=data.get("trigger_type", kite.GTT_TYPE_SINGLE),
            tradingsymbol=sym,
            exchange=data.get("exchange", "NSE"),
            trigger_values=triggers,
            last_price=float(data["last_price"]),
            orders=orders,
        )
        log.info("✅ GTT placed: gtt_id=%s  symbol=%s", gtt_id, data["symbol"])
        return jsonify({"gtt_id": gtt_id, "status": "placed"})
    except KiteException as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/cancel-gtt", methods=["POST"])
def cancel_gtt():
    """Delete a GTT trigger by id. Body: {"gtt_id": 123456}"""
    err = _check_auth()
    if err:
        return err
    data = request.get_json(force=True) or {}
    gtt_id = data.get("gtt_id")
    if not gtt_id:
        return jsonify({"error": "gtt_id required"}), 400
    try:
        kite = _get_kite()
        kite.delete_gtt(trigger_id=int(gtt_id))
        log.info("🗑 GTT cancelled: %s", gtt_id)
        return jsonify({"status": "cancelled", "gtt_id": gtt_id})
    except KiteException as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 400
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


# ── Instrument token cache ────────────────────────────────────────────────────
# Tokens are stable (same symbol = same token indefinitely), but new listings
# and delistings mean we refresh once per calendar day on first use.

_inst_lock       = threading.Lock()
_inst_cache: dict = {}      # "RELIANCE" → instrument_token (int)
_tick_cache: dict = {}      # "RELIANCE" → tick_size (float, e.g. 0.05 / 0.10)
_inst_cache_date = None


def _ensure_instruments(kite: KiteConnect):
    global _inst_cache, _tick_cache, _inst_cache_date
    today = date.today()
    with _inst_lock:
        if _inst_cache_date == today and _inst_cache:
            return
        log.info("Refreshing NSE instruments cache…")
        instruments = kite.instruments("NSE")
        _inst_cache = {i["tradingsymbol"]: i["instrument_token"] for i in instruments}
        _tick_cache = {i["tradingsymbol"]: float(i.get("tick_size") or 0.05)
                       for i in instruments}
        _inst_cache_date = today
        log.info("Instrument cache: %d symbols", len(_inst_cache))


def _round_to_tick(symbol: str, price):
    """Snap a price to the symbol's ACTUAL tick size (0.05 for most NSE
    equities, but 0.10 / 0.20 / 0.50 for some). Zerodha rejects any price
    that isn't a multiple of the script's tick. Falls back to 0.05 if the
    tick isn't cached yet."""
    if price is None:
        return None
    try:
        tick = _tick_cache.get((symbol or "").strip().upper(), 0.05) or 0.05
        return round(round(float(price) / tick) * tick, 2)
    except (TypeError, ValueError):
        return price


# ── Historical OHLCV ──────────────────────────────────────────────────────────

@app.route("/get-historical", methods=["GET"])
def get_historical():
    """
    Fetch daily OHLCV for one NSE symbol directly from Zerodha's data feed.
    Query params:
      symbol — NSE trading symbol without exchange suffix, e.g. RELIANCE
      days   — calendar days of history to fetch (default 400 ≈ 1 year of trading days)
    Returns:
      { "symbol": "RELIANCE", "rows": [{"date","open","high","low","close","volume"}, …] }
    """
    err = _check_auth()
    if err:
        return err
    symbol = request.args.get("symbol", "").strip().upper()
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    try:
        days = int(request.args.get("days", 400))
    except ValueError:
        days = 400
    try:
        kite = _get_kite()
        _ensure_instruments(kite)
        token = _inst_cache.get(symbol)
        if not token:
            return jsonify({"error": f"Instrument not found on NSE: {symbol}"}), 404

        to_dt   = datetime.now()
        from_dt = to_dt - timedelta(days=days)
        rows = kite.historical_data(
            instrument_token=token,
            from_date=from_dt,
            to_date=to_dt,
            interval="day",
            continuous=False,
            oi=False,
        )
        # Convert datetime objects to ISO strings so the response is JSON-serialisable
        clean = []
        for r in rows:
            clean.append({
                "date":   r["date"].isoformat() if hasattr(r["date"], "isoformat") else str(r["date"]),
                "open":   r["open"],
                "high":   r["high"],
                "low":    r["low"],
                "close":  r["close"],
                "volume": r["volume"],
            })
        log.info("Historical: %s  %d rows", symbol, len(clean))
        return jsonify({"symbol": symbol, "rows": clean})
    except KiteException as e:
        # Include the exception type so the caller can tell apart an expired
        # token (TokenException) from a missing historical-data subscription
        # (PermissionException) etc. — both otherwise read as a bare 400.
        log.error("Kite historical error for %s: %s: %s", symbol, type(e).__name__, e)
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 400
    except Exception as e:
        log.error("get_historical error for %s: %s", symbol, e)
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


if __name__ == "__main__":
    log.info("Kite executor starting on 0.0.0.0:5001 …")
    app.run(host="0.0.0.0", port=5001, debug=False)
