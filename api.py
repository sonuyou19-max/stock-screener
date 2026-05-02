"""
API Server — Serves portfolio data to the Netlify dashboard
============================================================
Routes:
  GET  /portfolio/latest  → latest portfolio JSON
  POST /portfolio/upload  → screener posts results here
  GET  /fiidii            → FII/DII history
  GET  /signals           → policy + news + llm signals
  GET  /health            → health check
"""

from flask import Flask, jsonify, request
import glob
import json
import os

app = Flask(__name__)

DATA_DIR = os.getenv("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)

# In-memory cache — survives between requests within same container
_portfolio_cache: dict = {}   # legacy — screener picks (kept for compat)
_live_cache:      dict = {}   # what investor actually holds on Kite
_picks_cache:     dict = {}   # screener recommendations this month
_signals_cache:   dict = {}
_fiidii_cache:    list = []


def _load_json(path: str):
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _sanitise(obj):
    """Recursively replace NaN/Infinity with None so JSON is valid for browsers."""
    import math
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _sanitise(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitise(v) for v in obj]
    return obj


def _save_json(path: str, data):
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        return True
    except Exception as e:
        print(f"⚠️  Could not save {path}: {e}")
        return False


def _find_latest_portfolio():
    patterns = [
        os.path.join(DATA_DIR, "portfolio_*.json"),
        "portfolio_*.json",
    ]
    files = []
    for p in patterns:
        files.extend(glob.glob(p))
    return sorted(set(files))[-1] if files else None


@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


@app.route("/portfolio/latest", methods=["GET", "OPTIONS"])
def latest_portfolio():
    # 1. Try in-memory cache first (most recent run)
    if _portfolio_cache:
        return jsonify(_sanitise(_portfolio_cache))

    # 2. Try reading from disk
    path = _find_latest_portfolio()
    if path:
        data = _load_json(path)
        if data:
            return jsonify(_sanitise(data))

    return jsonify({"error": "No portfolio found. Run screener.py first."}), 404


@app.route("/fiidii/upload", methods=["POST", "OPTIONS"])
def upload_fiidii():
    """fii-collector POSTs its history here after every run.
    Merges incoming data with existing records so history survives API restarts.
    """
    global _fiidii_cache
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        incoming = request.get_json(force=True)
        if not incoming:
            return jsonify({"error": "Empty payload"}), 400

        # Load existing data from disk to merge with
        path = os.path.join(DATA_DIR, "fiidii_history.json")
        existing = []
        if os.path.exists(path):
            try:
                with open(path) as f:
                    existing = json.load(f)
            except Exception:
                existing = []

        # Merge: build dict keyed by date, incoming overwrites existing for same date
        merged_by_date = {r["date"]: r for r in existing}
        for r in incoming:
            merged_by_date[r["date"]] = r

        # Sort descending by date, keep last 90 days
        merged = sorted(merged_by_date.values(), key=lambda r: r["date"], reverse=True)[:90]

        _fiidii_cache = merged
        _save_json(path, merged)
        print(f"✅ FII/DII merged: {len(incoming)} incoming + existing → {len(merged)} total records")
        return jsonify({"status": "ok", "records": len(merged)}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/signals/upload", methods=["POST", "OPTIONS"])
def upload_signals():
    """news-scanner / policy-scraper / llm-synth POST their signals here."""
    global _signals_cache
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({"error": "Empty payload"}), 400
        # data should be like {"type": "news_signals", "payload": {...}}
        sig_type = data.get("type")
        payload  = data.get("payload")
        if not sig_type or payload is None:
            return jsonify({"error": "Need 'type' and 'payload' fields"}), 400
        _signals_cache[sig_type] = payload
        path = os.path.join(DATA_DIR, f"{sig_type}.json")
        _save_json(path, payload)
        print(f"✅ Signal received: {sig_type}")
        return jsonify({"status": "ok", "type": sig_type}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/portfolio/upload", methods=["POST", "OPTIONS"])
def upload_portfolio():
    """Screener POSTs its results here after every run."""
    global _portfolio_cache
    if request.method == "OPTIONS":
        return jsonify({}), 200

    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({"error": "Empty payload"}), 400

        # Cache in memory (sanitised)
        _portfolio_cache = _sanitise(data)

        # Also persist to disk (best-effort)
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m")
        path = os.path.join(DATA_DIR, f"portfolio_{timestamp}.json")
        _save_json(path, data)

        print(f"✅ Portfolio received and cached ({len(str(data))} bytes)")
        return jsonify({"status": "ok", "saved_to": path}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/fiidii", methods=["GET"])
def fiidii():
    global _fiidii_cache
    # 1. Try in-memory cache first (most recent from collector POST)
    if _fiidii_cache:
        return jsonify(_fiidii_cache)
    # 2. Try disk
    for path in [
        os.path.join(DATA_DIR, "fiidii_history.json"),
        "fiidii_history.json",
        os.path.join(os.path.dirname(__file__), "fiidii_history.json"),
    ]:
        data = _load_json(path)
        if data:
            _fiidii_cache = data
            return jsonify(data)
    return jsonify([])


@app.route("/signals", methods=["GET"])
def signals():
    result = {}
    for name in ["policy_signals", "news_signals", "llm_synthesis"]:
        # Try in-memory cache first
        if name in _signals_cache:
            result[name] = _signals_cache[name]
            continue
        # Try disk locations
        for path in [
            os.path.join(DATA_DIR, f"{name}.json"),
            f"{name}.json",
            os.path.join(os.path.dirname(__file__), f"{name}.json"),
        ]:
            data = _load_json(path)
            if data:
                result[name] = data
                break
    return jsonify(result)



# ─────────────────────────────────────────────
# LIVE POSITIONS — what investor holds on Kite
# ─────────────────────────────────────────────

@app.route("/portfolio/live", methods=["GET", "OPTIONS"])
def live_portfolio():
    """Return what the investor actually holds on Kite right now."""
    global _live_cache
    if request.method == "OPTIONS":
        return jsonify({}), 200
    # Try cache
    if _live_cache:
        return jsonify(_sanitise(_live_cache))
    # Try disk
    path = os.path.join(DATA_DIR, "portfolio_live.json")
    data = _load_json(path)
    if data:
        _live_cache = data
        return jsonify(_sanitise(data))
    # Fall back to latest screener picks (first run migration)
    path = _find_latest_portfolio()
    if path:
        data = _load_json(path)
        if data:
            return jsonify(_sanitise(data))
    return jsonify({"error": "No live portfolio found"}), 404


@app.route("/portfolio/live/upload", methods=["POST", "OPTIONS"])
def upload_live_portfolio():
    """
    Update live positions — called when investor buys/sells on Kite.
    Dashboard Mark as Bought/Sold buttons POST here.
    """
    global _live_cache
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({"error": "Empty payload"}), 400
        _live_cache = _sanitise(data)
        path = os.path.join(DATA_DIR, "portfolio_live.json")
        _save_json(path, data)
        print(f"✅ Live portfolio updated — {sum(len(b.get('stocks',[])) for b in data.values())} positions")
        return jsonify({"status": "ok", "saved_to": path}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────
# SCREENER PICKS — monthly recommendations
# ─────────────────────────────────────────────

@app.route("/portfolio/picks", methods=["GET", "OPTIONS"])
def picks_portfolio():
    """Return this month's screener recommendations."""
    global _picks_cache
    if request.method == "OPTIONS":
        return jsonify({}), 200
    if _picks_cache:
        return jsonify(_sanitise(_picks_cache))
    path = os.path.join(DATA_DIR, "portfolio_picks.json")
    data = _load_json(path)
    if data:
        _picks_cache = data
        return jsonify(_sanitise(data))
    return jsonify({"error": "No picks found. Run screener.py first."}), 404


@app.route("/portfolio/picks/upload", methods=["POST", "OPTIONS"])
def upload_picks():
    """Screener POSTs recommendations here — does NOT overwrite live positions."""
    global _picks_cache
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({"error": "Empty payload"}), 400
        _picks_cache = _sanitise(data)
        path = os.path.join(DATA_DIR, "portfolio_picks.json")
        _save_json(path, data)
        print(f"✅ Screener picks saved — {sum(len(b.get('stocks',[])) for b in data.values())} picks")
        return jsonify({"status": "ok", "saved_to": path}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────
# SWAP RECOMMENDATIONS — compare live vs picks
# ─────────────────────────────────────────────

@app.route("/portfolio/swap", methods=["GET", "OPTIONS"])
def swap_recommendations():
    """
    Compare live positions vs screener picks bucket by bucket.
    Returns swap recommendations: which stocks to sell and which to buy.
    Logic:
      - For each bucket, compare live stocks vs pick stocks
      - If screener picked the same stock → KEEP
      - If screener picked a different stock → SWAP recommendation
      - Score difference drives conviction (HIGH/MEDIUM/LOW)
    """
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        # Load both
        live_data  = _live_cache  or _load_json(os.path.join(DATA_DIR, "portfolio_live.json"))
        picks_data = _picks_cache or _load_json(os.path.join(DATA_DIR, "portfolio_picks.json"))

        if not live_data:
            return jsonify({"error": "No live portfolio"}), 404
        if not picks_data:
            return jsonify({"error": "No screener picks"}), 404

        swaps   = []
        keeps   = []

        for bucket_key in set(list(live_data.keys()) + list(picks_data.keys())):
            live_bucket  = live_data.get(bucket_key,  {})
            picks_bucket = picks_data.get(bucket_key, {})

            live_tickers  = {s["ticker"]: s for s in live_bucket.get("stocks",  [])}
            picks_tickers = {s["ticker"]: s for s in picks_bucket.get("stocks", [])}

            bucket_label = live_bucket.get("label") or picks_bucket.get("label") or bucket_key

            # Stocks in live that are also in picks → KEEP
            for ticker, s in live_tickers.items():
                if ticker in picks_tickers:
                    keeps.append({
                        "bucket":       bucket_label,
                        "ticker":       ticker,
                        "name":         s.get("name", ticker),
                        "action":       "KEEP",
                        "reason":       "Screener picked again this month",
                        "live_score":   s.get("final_score"),
                        "pick_score":   picks_tickers[ticker].get("final_score"),
                    })

            # Stocks in picks but NOT in live → potential BUY
            new_picks = [t for t in picks_tickers if t not in live_tickers]
            # Stocks in live but NOT in picks → potential SELL
            dropped   = [t for t in live_tickers if t not in picks_tickers]

            # Match dropped vs new_picks by bucket slot
            for i, drop_ticker in enumerate(dropped):
                drop_stock = live_tickers[drop_ticker]
                if i < len(new_picks):
                    new_ticker = new_picks[i]
                    new_stock  = picks_tickers[new_ticker]
                    score_diff = (new_stock.get("final_score", 50) or 50) - (drop_stock.get("final_score", 50) or 50)
                    conviction = "HIGH" if score_diff > 15 else "MEDIUM" if score_diff > 5 else "LOW"
                    swaps.append({
                        "bucket":           bucket_label,
                        "action":           "SWAP",
                        "conviction":       conviction,
                        "sell_ticker":      drop_ticker,
                        "sell_name":        drop_stock.get("name", drop_ticker),
                        "sell_score":       drop_stock.get("final_score"),
                        "sell_buy_price":   drop_stock.get("price"),
                        "sell_shares":      drop_stock.get("approx_shares"),
                        "sell_stop_loss":   drop_stock.get("stop_loss_price"),
                        "buy_ticker":       new_ticker,
                        "buy_name":         new_stock.get("name", new_ticker),
                        "buy_score":        new_stock.get("final_score"),
                        "buy_price":        new_stock.get("price"),
                        "buy_shares":       new_stock.get("approx_shares"),
                        "buy_stop_loss":    new_stock.get("stop_loss_price"),
                        "score_improvement": round(score_diff, 1),
                        "reason":           (
                            f"{new_stock.get('name', new_ticker)} scores "
                            f"{new_stock.get('final_score','?'):.1f} vs "
                            f"{drop_stock.get('name', drop_ticker)}'s "
                            f"{drop_stock.get('final_score','?'):.1f} "
                            f"(+{score_diff:.1f} pts improvement)"
                        ),
                    })
                else:
                    # Dropped with no replacement — screener found fewer stocks
                    swaps.append({
                        "bucket":       bucket_label,
                        "action":       "HOLD_NO_REPLACEMENT",
                        "conviction":   "LOW",
                        "sell_ticker":  drop_ticker,
                        "sell_name":    drop_stock.get("name", drop_ticker),
                        "reason":       "Screener found no replacement — hold current position",
                    })

            # New picks with no dropped stock — new addition
            for new_ticker in new_picks[len(dropped):]:
                new_stock = picks_tickers[new_ticker]
                swaps.append({
                    "bucket":      bucket_label,
                    "action":      "NEW_BUY",
                    "conviction":  "MEDIUM",
                    "buy_ticker":  new_ticker,
                    "buy_name":    new_stock.get("name", new_ticker),
                    "buy_score":   new_stock.get("final_score"),
                    "buy_price":   new_stock.get("price"),
                    "buy_shares":  new_stock.get("approx_shares"),
                    "buy_stop_loss": new_stock.get("stop_loss_price"),
                    "reason":      "New screener pick — no existing position in this slot",
                })

        from datetime import datetime
        return jsonify({
            "generated":  datetime.now().strftime("%d %B %Y, %H:%M IST"),
            "swaps":      swaps,
            "keeps":      keeps,
            "total_swaps": len([s for s in swaps if s["action"] == "SWAP"]),
            "total_new":   len([s for s in swaps if s["action"] == "NEW_BUY"]),
            "total_keeps": len(keeps),
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/prices", methods=["GET"])
def prices():
    """
    Fetch live prices for all stocks in the current portfolio.
    Called by the dashboard every 5 minutes.
    Returns: { "MAHABANK.NS": {"price": 80.10, "change_pct": 3.6, "ts": "..."}, ... }
    """
    try:
        import yfinance as yf
        import math
        from datetime import datetime

        # Get tickers from current portfolio — try cache first, then disk
        port = _portfolio_cache
        if not port:
            latest = _find_latest_portfolio()
            if latest:
                port = _load_json(latest) or {}
                port = _sanitise(port)

        tickers = []
        for bucket in (port or {}).values():
            for s in bucket.get("stocks", []):
                t = s.get("ticker")
                if t:
                    tickers.append(t)

        if not tickers:
            return jsonify({"error": "No portfolio loaded", "prices": {}})

        result = {}
        ts = datetime.now().strftime("%H:%M")

        # FIX: Use fast_info which carries regularMarketChangePercent —
        # this is the official day-change field Yahoo populates correctly
        # both during AND after market hours (like Groww/Kite).
        # The old batch yf.download(period="2d") approach computed change
        # from two daily closes which both equal today's close after hours
        # → showing 0.00% incorrectly.
        for ticker in tickers:
            try:
                stock  = yf.Ticker(ticker)
                fi     = stock.fast_info

                price  = getattr(fi, "last_price", None)
                prev   = getattr(fi, "previous_close", None)

                # fast_info fallback → info dict
                if price is None or (isinstance(price, float) and math.isnan(float(price))):
                    info  = stock.info
                    price = info.get("regularMarketPrice") or info.get("currentPrice")
                    prev  = info.get("regularMarketPreviousClose") or info.get("previousClose")

                if price is None:
                    continue

                price = round(float(price), 2)
                prev  = round(float(prev),  2) if prev else price
                change_pct = round(((price - prev) / prev) * 100, 2) if prev else 0

                result[ticker] = {"price": price, "change_pct": change_pct, "ts": ts}

            except Exception as e:
                print(f"  ⚠️  Price fetch failed for {ticker}: {e}")

        return jsonify({"prices": result, "ts": ts, "count": len(result)})

    except Exception as e:
        return jsonify({"error": str(e), "prices": {}})


@app.route("/market", methods=["GET"])
def market():
    """
    Fetch 9 live market indicators for the dashboard overview strip.
    Called every 5 minutes. Uses fast_info for correct day-change % at all hours.
    Returns: { "indicators": [...], "ts": "HH:MM" }
    """
    try:
        import yfinance as yf
        import math
        from datetime import datetime

        TICKERS = [
            # (key,           ticker,      label,        type,     unit  )
            ("sp500",         "^GSPC",     "S&P 500",    "index",  ""    ),
            ("sensex",        "^BSESN",    "Sensex",     "index",  ""    ),
            ("nifty50",       "^NSEI",     "Nifty 50",   "index",  ""    ),
            ("niftybank",     "^NSEBANK",  "Nifty Bank", "index",  ""    ),
            ("nifty500",      "^CRSLDX",   "Nifty 500",  "index",  ""    ),
            ("niftypharma",   "NIFTY_PHARMA.NS", "Nifty Pharma", "index", ""),
            ("bitcoin",       "BTC-USD",   "Bitcoin",    "crypto", "$"   ),
            # GOLDBEES.NS: 1 unit ≈ 1/100g gold → price × 100 = ₹/10g (MCX-linked, includes duty+GST)
            # SILVERBEES.NS: 1 unit ≈ 1g silver → price × 1000 = ₹/kg (MCX-linked)
            ("gold",          "GOLDBEES.NS",  "Gold",       "commodity", "₹/10g"),
            ("silver",        "SILVERBEES.NS","Silver",     "commodity", "₹/kg"),
            ("crude",         "BZ=F",         "Brent Crude","commodity", "$/bbl"),
        ]

        # USD/INR still needed for crude
        usd_inr = 84.0  # fallback
        try:
            fx = yf.Ticker("INR=X")
            fx_price = getattr(fx.fast_info, "last_price", None)
            if fx_price and not math.isnan(float(fx_price)):
                usd_inr = float(fx_price)
        except Exception:
            pass

        ts         = datetime.now().strftime("%H:%M")
        indicators = []

        for key, ticker, label, kind, unit in TICKERS:
            try:
                stock = yf.Ticker(ticker)
                fi    = stock.fast_info

                price = getattr(fi, "last_price", None)
                prev  = getattr(fi, "previous_close", None)

                if price is None or (isinstance(price, float) and math.isnan(float(price))):
                    info  = stock.info
                    price = info.get("regularMarketPrice") or info.get("currentPrice")
                    prev  = info.get("regularMarketPreviousClose") or info.get("previousClose")

                if price is None:
                    continue

                price = float(price)
                prev  = float(prev) if prev else price
                change_pct = round(((price - prev) / prev) * 100, 2) if prev else 0

                # Gold/Silver ETFs already in INR — just scale to display units
                disp_price = price
                if key == "gold":
                    # GOLDBEES.NS: 1 unit ≈ 0.01g gold → ×1000 = ₹/10g (MCX-linked)
                    disp_price = round(price * 1000, 0)
                    unit = "₹/10g"
                elif key == "silver":
                    # SILVERBEES.NS: 1 unit ≈ 1g silver → ×1000 = ₹/kg
                    disp_price = round(price * 1000, 0)
                    unit = "₹/kg"
                elif key == "bitcoin":
                    disp_price = round(price, 0)
                elif key in ("sensex", "niftybank", "nifty500", "niftypharma"):
                    disp_price = round(price, 2)
                elif key == "nifty50":
                    disp_price = round(price, 2)
                else:
                    disp_price = round(price, 2)

                indicators.append({
                    "key":        key,
                    "label":      label,
                    "price":      disp_price,
                    "change_pct": change_pct,
                    "unit":       unit,
                    "type":       kind,
                })

            except Exception as e:
                print(f"  ⚠️  Market fetch failed for {ticker}: {e}")

        return jsonify({"indicators": indicators, "ts": ts, "usd_inr": round(usd_inr, 2)})

    except Exception as e:
        return jsonify({"error": str(e), "indicators": []})


@app.route("/health", methods=["GET"])
def health():
    portfolio_path = _find_latest_portfolio()
    return jsonify({
        "status":        "ok",
        "portfolio":     os.path.basename(portfolio_path) if portfolio_path else None,
        "cached":        bool(_portfolio_cache),
        "data_dir":      DATA_DIR,
        "data_dir_exists": os.path.exists(DATA_DIR),
    })


@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "name":      "Indian Stock Screener API",
        "endpoints": [
            "GET  /portfolio/latest",
            "POST /portfolio/upload",
            "GET  /fiidii",
            "GET  /signals",
            "GET  /health",
        ],
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
