#!/usr/bin/env python3
"""
Kite order executor — Flask server running on Oracle VPS (static IP 80.225.201.62).
The static IP is required for SEBI-compliant API order routing.

Start manually:   python kite_executor.py
Start via systemd: see setup instructions below.

Endpoints:
  GET  /health          — liveness check
  POST /exchange-token  — called by Railway /kite/callback to swap request_token → access_token
  POST /place-order     — place a market/limit order on Kite
  POST /cancel-order    — cancel a pending order
  GET  /get-pnl         — positions + holdings + aggregate P&L
  GET  /get-orders      — today's order list
  POST /place-gtt       — place a GTT stop-loss order
"""
import os
import logging
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
        kite = _get_kite()
        order_id = kite.place_order(
            variety=data.get("variety", kite.VARIETY_REGULAR),
            exchange=data.get("exchange", kite.EXCHANGE_NSE),
            tradingsymbol=symbol,
            transaction_type=(
                kite.TRANSACTION_TYPE_BUY
                if data.get("side", "BUY").upper() == "BUY"
                else kite.TRANSACTION_TYPE_SELL
            ),
            quantity=int(data["quantity"]),
            product=data.get("product", kite.PRODUCT_CNC),
            order_type=data.get("order_type", kite.ORDER_TYPE_MARKET),
            price=data.get("price"),
            trigger_price=data.get("trigger_price"),
            tag=(data.get("tag", "eq-advisor") or "eq-advisor")[:20],
        )
        log.info(
            "✅ Order placed: %s  %s %s  qty=%s  order_id=%s",
            data.get("side"), symbol, data.get("order_type", "MARKET"),
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
        gtt_id = kite.place_gtt(
            trigger_type=data.get("trigger_type", kite.GTT_TYPE_SINGLE),
            tradingsymbol=data["symbol"].strip().upper(),
            exchange=data.get("exchange", "NSE"),
            trigger_values=data["trigger_values"],
            last_price=float(data["last_price"]),
            orders=data["orders"],
        )
        log.info("✅ GTT placed: gtt_id=%s  symbol=%s", gtt_id, data["symbol"])
        return jsonify({"gtt_id": gtt_id, "status": "placed"})
    except KiteException as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    log.info("Kite executor starting on 0.0.0.0:5001 …")
    app.run(host="0.0.0.0", port=5001, debug=False)
