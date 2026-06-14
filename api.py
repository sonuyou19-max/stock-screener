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

# ── Write protection ─────────────────────────────────────────────
# Every /upload endpoint is reachable from the public internet and the
# API URL ships inside the public dashboard HTML. When UPLOAD_TOKEN is
# set (Railway env var), all POST */upload* requests must carry a
# matching X-Upload-Token header. Unset = open (backwards compatible).
UPLOAD_TOKEN = os.getenv("UPLOAD_TOKEN", "")

_WRITE_PATH_MARKERS = ("/upload", "/add", "/remove", "/trigger", "/queue")

@app.before_request
def _enforce_upload_token():
    if request.method != "POST" or not any(m in request.path for m in _WRITE_PATH_MARKERS):
        return None
    if not UPLOAD_TOKEN:
        return None
    if request.headers.get("X-Upload-Token", "") != UPLOAD_TOKEN:
        return jsonify({"error": "unauthorized — missing or bad X-Upload-Token"}), 401
    return None


@app.route("/auth/verify", methods=["POST", "OPTIONS"])
def auth_verify():
    """Dashboard PIN check. With UPLOAD_TOKEN set the PIN lives only in
    the server env; the public HTML no longer needs a hardcoded PIN."""
    if request.method == "OPTIONS":
        return jsonify({}), 200
    if not UPLOAD_TOKEN:
        return jsonify({"ok": True, "enforced": False})
    pin = (request.get_json(silent=True) or {}).get("pin", "")
    return jsonify({"ok": pin == UPLOAD_TOKEN, "enforced": True})


DATA_DIR = os.getenv("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)

# In-memory cache — survives between requests within same container
_portfolio_cache: dict = {}   # legacy — screener picks (kept for compat)
_live_cache:      dict = {}   # what investor actually holds on Kite
_picks_cache:     dict = {}   # screener recommendations this month
_advisory_cache:  dict = {}   # monthly Claude portfolio advisory
_signals_cache:   dict = {}
_fiidii_cache:    list = []
_perf_cache:      list = []   # daily performance snapshots
_history_cache:   list = []   # closed/realised trade history

# ── US portfolio caches ──────────────────────────────────────────
_us_live_cache:     dict = {}
_us_picks_cache:    dict = {}
_us_advisory_cache: dict = {}
_us_perf_cache:     list = []
_us_history_cache:  list = []

# ── Swing trading caches ─────────────────────────────────────────
_swing_candidates_cache: dict = {}   # today's scan candidates
_swing_live_cache:       list = []   # open swing positions
_swing_history_cache:    list = []   # closed swing trades

PERF_FILE      = os.path.join(os.getenv("DATA_DIR", "/data"), "performance_history.json")
HISTORY_FILE   = os.path.join(os.getenv("DATA_DIR", "/data"), "trade_history.json")
US_LIVE_FILE     = os.path.join(os.getenv("DATA_DIR", "/data"), "us_portfolio_live.json")
US_PICKS_FILE    = os.path.join(os.getenv("DATA_DIR", "/data"), "us_portfolio_picks.json")
US_ADVISORY_FILE = os.path.join(os.getenv("DATA_DIR", "/data"), "us_monthly_advisory.json")
US_PERF_FILE     = os.path.join(os.getenv("DATA_DIR", "/data"), "us_performance_history.json")
US_HISTORY_FILE  = os.path.join(os.getenv("DATA_DIR", "/data"), "us_trade_history.json")

ADVISORY_FILE         = os.path.join(os.getenv("DATA_DIR", "/data"), "monthly_advisory.json")
REBALANCE_FILE        = os.path.join(os.getenv("DATA_DIR", "/data"), "rebalance_report.json")

_rebalance_cache: dict = {}

SWING_CANDIDATES_FILE = os.path.join(os.getenv("DATA_DIR", "/data"), "swing_candidates.json")
SWING_LIVE_FILE       = os.path.join(os.getenv("DATA_DIR", "/data"), "swing_live.json")
SWING_HISTORY_FILE    = os.path.join(os.getenv("DATA_DIR", "/data"), "swing_history.json")
SWING_QUEUE_FILE      = os.path.join(os.getenv("DATA_DIR", "/data"), "swing_queue.json")
INDIA_QUEUE_FILE      = os.path.join(os.getenv("DATA_DIR", "/data"), "india_queue.json")


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
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Upload-Token"
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
        # Also cache rebalance reports separately for fast retrieval
        if sig_type == "rebalance_report":
            global _rebalance_cache
            _rebalance_cache = payload
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
    for name in ["policy_signals", "news_signals", "llm_synthesis",
                  "us_news_signals", "us_llm_synthesis", "swing_candidates",
                  "swing_news_sentiment", "monthly_earnings_sentiment"]:
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
    if request.method == "OPTIONS":
        return jsonify({}), 200
    # Always read from disk — multi-instance safe
    live = _read_live_ind()
    if live:
        return jsonify(_sanitise(live))
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


def _read_live_ind():
    """Always read from disk — single source of truth, multi-instance safe."""
    path = os.path.join(DATA_DIR, "portfolio_live.json")
    data = _load_json(path)
    result = data if isinstance(data, dict) else {}
    global _live_cache
    _live_cache = result
    return result


def _write_live_ind(data):
    global _live_cache
    sanitised = _sanitise(data)
    path = os.path.join(DATA_DIR, "portfolio_live.json")
    if not _save_json(path, sanitised):
        raise RuntimeError(f"Failed to write India live portfolio to {path}")
    _live_cache = sanitised


@app.route("/portfolio/live/add", methods=["POST", "OPTIONS"])
def add_live_ind():
    """Append a single stock to the India live portfolio. Atomic — server is
    the single source of truth, so the client never overwrites the whole list."""
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        stock = request.get_json(force=True)
        ticker = (stock or {}).get("ticker")
        if not ticker:
            return jsonify({"error": "stock must include a 'ticker'"}), 400
        live = _read_live_ind()
        for b in live.values():
            if any(s.get("ticker") == ticker for s in b.get("stocks", [])):
                return jsonify({"status": "exists", "ticker": ticker}), 200
        live.setdefault("top_picks", {"label": "Monthly Top Picks", "stocks": []})
        live["top_picks"].setdefault("stocks", []).append(stock)
        _write_live_ind(live)
        total = sum(len(b.get("stocks", [])) for b in live.values())
        print(f"✅ India live: added {ticker} ({total} holdings)")
        return jsonify({"status": "ok", "ticker": ticker, "holdings": total})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/portfolio/live/remove", methods=["POST", "OPTIONS"])
def remove_live_ind():
    """Remove a single ticker from the India live portfolio. Atomic."""
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        ticker = (request.get_json(force=True) or {}).get("ticker")
        if not ticker:
            return jsonify({"error": "must include a 'ticker'"}), 400
        live = _read_live_ind()
        for b in live.values():
            if b.get("stocks"):
                b["stocks"] = [s for s in b["stocks"] if s.get("ticker") != ticker]
        _write_live_ind(live)
        total = sum(len(b.get("stocks", [])) for b in live.values())
        print(f"✅ India live: removed {ticker} ({total} holdings)")
        return jsonify({"status": "ok", "ticker": ticker, "holdings": total})
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


@app.route("/portfolio/advisory", methods=["GET", "OPTIONS"])
def get_advisory():
    """Monthly Claude portfolio advisory — generated by screener.py."""
    if request.method == "OPTIONS":
        return jsonify({}), 200
    global _advisory_cache
    if _advisory_cache:
        return jsonify(_sanitise(_advisory_cache))
    data = _load_json(ADVISORY_FILE)
    if data:
        _advisory_cache = data
        return jsonify(_sanitise(data))
    return jsonify({"error": "No advisory yet. Run screener.py first."}), 404


@app.route("/portfolio/advisory/upload", methods=["POST", "OPTIONS"])
def upload_advisory():
    """Screener POSTs monthly advisory here after generation."""
    if request.method == "OPTIONS":
        return jsonify({}), 200
    global _advisory_cache
    try:
        data = request.get_json(force=True)
        _advisory_cache = _sanitise(data)
        _save_json(ADVISORY_FILE, _advisory_cache)
        print(f"✅ Monthly advisory saved — action: {data.get('action','?')}")
        return jsonify({"status": "ok", "action": data.get("action", "?")}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/rebalance/report", methods=["GET", "OPTIONS"])
def get_rebalance_report():
    """Latest rebalancer.py report — HOLD/TRIM/EXIT decisions for live holdings."""
    if request.method == "OPTIONS":
        return jsonify({}), 200
    global _rebalance_cache
    if _rebalance_cache:
        return jsonify(_sanitise(_rebalance_cache))
    data = _load_json(REBALANCE_FILE)
    if data:
        _rebalance_cache = data
        return jsonify(_sanitise(data))
    return jsonify({"error": "No rebalance report yet. Run rebalancer.py first."}), 404


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

        # Get tickers from LIVE portfolio (what user actually holds), not screener picks
        port = _live_cache
        if not port:
            live_path = os.path.join(DATA_DIR, "portfolio_live.json")
            port = _load_json(live_path) or {}
        if not port:
            # Fall back to screener picks only if no live portfolio exists yet
            port = _portfolio_cache
            if not port:
                latest = _find_latest_portfolio()
                if latest:
                    port = _load_json(latest) or {}
                    port = _sanitise(port)

        tickers = []
        seen: set = set()
        for bucket in (port or {}).values():
            for s in bucket.get("stocks", []):
                t = s.get("ticker")
                if t and t not in seen:
                    tickers.append(t)
                    seen.add(t)

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
            ("gold",          "GOLDBEES.NS","Gold",       "commodity", "₹/10g"),
            ("silver",        "SILVERBEES.NS","Silver",   "commodity", "₹/kg"),
            ("crude",         "BZ=F",      "Brent Crude","commodity", "$/bbl"),
            ("usdinr",        "INR=X",     "USD/INR",    "forex",  "₹"   ),
            ("eurinr",        "EURINR=X",  "EUR/INR",    "forex",  "₹"   ),
        ]

        # Fetch USD/INR once for commodity conversion (crude)
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

                # Convert/scale prices to display units
                disp_price = price
                if key == "gold":
                    # GOLDBEES.NS: 1 unit = ~0.01g gold → ×1000 = ₹/10g (MCX-linked)
                    disp_price = round(price * 1000, 0)
                    unit = "₹/10g"
                elif key == "silver":
                    # SILVERBEES.NS: 1 unit = ~1g silver → ×1000 = ₹/kg (MCX-linked)
                    disp_price = round(price * 1000, 0)
                    unit = "₹/kg"
                elif key == "bitcoin":
                    disp_price = round(price, 0)
                elif key in ("usdinr", "eurinr"):
                    disp_price = round(price, 2)
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


@app.route("/portfolio/history", methods=["GET"])
def portfolio_history_get():
    """Returns all closed/realised trade records."""
    global _history_cache
    if not _history_cache:
        loaded = _load_json(HISTORY_FILE)
        _history_cache = loaded if isinstance(loaded, list) else []
    return jsonify({"trades": _sanitise(_history_cache), "count": len(_history_cache)})


@app.route("/portfolio/history/upload", methods=["POST", "OPTIONS"])
def portfolio_history_upload():
    """
    Dashboard POSTs a closed trade record here when a stock is sold.
    Body: single record { ticker, ... } to append
          OR empty list [] to clear all history
    """
    global _history_cache
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        rec = request.get_json(force=True)

        # Support clearing: POST [] wipes history
        if isinstance(rec, list):
            _history_cache = rec  # [] = clear, or full list = replace
            _save_json(HISTORY_FILE, _history_cache)
            return jsonify({"ok": True, "total": len(_history_cache), "action": "replaced"})

        if not rec.get("ticker"):
            return jsonify({"error": "ticker required"}), 400

        if not _history_cache:
            loaded = _load_json(HISTORY_FILE)
            _history_cache = loaded if isinstance(loaded, list) else []

        _history_cache.append(rec)
        _history_cache.sort(key=lambda r: r.get("sell_date", ""), reverse=True)
        _save_json(HISTORY_FILE, _history_cache)
        return jsonify({"ok": True, "total": len(_history_cache)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/performance", methods=["GET"])
def performance_get():
    """
    Returns daily performance history for portfolio vs Nifty50 vs Nifty500.
    Each record: { date, portfolio_pct, nifty50_pct, nifty500_pct }
    """
    global _perf_cache
    if not _perf_cache:
        loaded = _load_json(PERF_FILE)
        _perf_cache = loaded if isinstance(loaded, list) else []
    return jsonify({"history": _sanitise(_perf_cache), "count": len(_perf_cache)})


@app.route("/performance/upload", methods=["POST", "OPTIONS"])
def performance_upload():
    """
    Alerter posts EOD snapshot here after market close.
    Body: { date, portfolio_pct, nifty50_pct, nifty500_pct }
    Upserts by date (one record per calendar day).
    """
    global _perf_cache
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        rec = request.get_json(force=True)
        date_str = rec.get("date")
        if not date_str:
            return jsonify({"error": "date required"}), 400

        if not _perf_cache:
            loaded = _load_json(PERF_FILE)
            _perf_cache = loaded if isinstance(loaded, list) else []

        # Upsert — replace existing record for same date
        _perf_cache = [r for r in _perf_cache if r.get("date") != date_str]
        _perf_cache.append(rec)
        _perf_cache.sort(key=lambda r: r.get("date", ""))

        _save_json(PERF_FILE, _perf_cache)
        return jsonify({"ok": True, "date": date_str, "total": len(_perf_cache)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500



# ════════════════════════════════════════════════════════════════
#  US PORTFOLIO ENDPOINTS
# ════════════════════════════════════════════════════════════════

@app.route("/us/portfolio/live", methods=["GET", "OPTIONS"])
def us_portfolio_live_get():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    return jsonify(_sanitise(_read_live_us()))


@app.route("/us/portfolio/live/upload", methods=["POST", "OPTIONS"])
def us_portfolio_live_upload():
    global _us_live_cache
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        data = request.get_json(force=True)
        _us_live_cache = data
        _save_json(US_LIVE_FILE, data)
        return jsonify({"status": "ok", "buckets": len(data)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _read_live_us():
    """Always read from disk — single source of truth, multi-instance safe."""
    data = _load_json(US_LIVE_FILE)
    result = data if isinstance(data, dict) else {}
    global _us_live_cache
    _us_live_cache = result
    return result


def _write_live_us(data):
    global _us_live_cache
    sanitised = _sanitise(data)
    if not _save_json(US_LIVE_FILE, sanitised):
        raise RuntimeError(f"Failed to write US live portfolio to {US_LIVE_FILE}")
    _us_live_cache = sanitised


@app.route("/us/portfolio/live/add", methods=["POST", "OPTIONS"])
def add_live_us():
    """Append a single stock to the US live portfolio. Atomic — server owns
    the list so concurrent/stale clients can't wipe earlier additions."""
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        stock = request.get_json(force=True)
        ticker = (stock or {}).get("ticker")
        if not ticker:
            return jsonify({"error": "stock must include a 'ticker'"}), 400
        live = _read_live_us()
        for b in live.values():
            if any(s.get("ticker") == ticker for s in b.get("stocks", [])):
                return jsonify({"status": "exists", "ticker": ticker}), 200
        live.setdefault("top_picks", {"label": "Monthly Top Picks", "stocks": []})
        live["top_picks"].setdefault("stocks", []).append(stock)
        _write_live_us(live)
        total = sum(len(b.get("stocks", [])) for b in live.values())
        print(f"✅ US live: added {ticker} ({total} holdings)")
        return jsonify({"status": "ok", "ticker": ticker, "holdings": total})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/us/portfolio/live/remove", methods=["POST", "OPTIONS"])
def remove_live_us():
    """Remove a single ticker from the US live portfolio. Atomic."""
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        ticker = (request.get_json(force=True) or {}).get("ticker")
        if not ticker:
            return jsonify({"error": "must include a 'ticker'"}), 400
        live = _read_live_us()
        for b in live.values():
            if b.get("stocks"):
                b["stocks"] = [s for s in b["stocks"] if s.get("ticker") != ticker]
        _write_live_us(live)
        total = sum(len(b.get("stocks", [])) for b in live.values())
        print(f"✅ US live: removed {ticker} ({total} holdings)")
        return jsonify({"status": "ok", "ticker": ticker, "holdings": total})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/us/portfolio/picks", methods=["GET", "OPTIONS"])
def us_portfolio_picks_get():
    global _us_picks_cache
    if request.method == "OPTIONS":
        return jsonify({}), 200
    if not _us_picks_cache:
        loaded = _load_json(US_PICKS_FILE)
        _us_picks_cache = loaded if isinstance(loaded, dict) else {}
    return jsonify(_sanitise(_us_picks_cache))


@app.route("/us/portfolio/picks/upload", methods=["POST", "OPTIONS"])
def us_portfolio_picks_upload():
    global _us_picks_cache
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        data = request.get_json(force=True)
        _us_picks_cache = data
        _save_json(US_PICKS_FILE, data)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/us/portfolio/swap", methods=["GET", "OPTIONS"])
def us_portfolio_swap():
    """Compare US live vs picks and return SWAP/KEEP/NEW BUY recommendations."""
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        live  = _us_live_cache  or (_load_json(US_LIVE_FILE)  or {})
        picks = _us_picks_cache or (_load_json(US_PICKS_FILE) or {})
        if not live or not picks:
            return jsonify({"swaps": [], "message": "Portfolio or picks not loaded"})

        swaps = []
        for bk, pick_bucket in picks.items():
            live_bucket  = live.get(bk, {})
            live_stocks  = {s["ticker"]: s for s in live_bucket.get("stocks", [])}
            pick_stocks  = {s["ticker"]: s for s in pick_bucket.get("stocks", [])}

            for ticker, ps in pick_stocks.items():
                if ticker not in live_stocks:
                    # Find lowest-scoring live stock in same bucket
                    if live_stocks:
                        worst = min(live_stocks.values(), key=lambda x: x.get("final_score", 0))
                        score_diff = ps.get("final_score", 0) - worst.get("final_score", 0)
                        conviction = "HIGH" if score_diff >= 20 else "MEDIUM" if score_diff >= 10 else "LOW"
                        swaps.append({
                            "action": "SWAP", "bucket": bk,
                            "sell_ticker": worst["ticker"], "sell_name": worst.get("name",""),
                            "sell_score": worst.get("final_score", 0),
                            "buy_ticker": ticker, "buy_name": ps.get("name",""),
                            "buy_score": ps.get("final_score", 0),
                            "score_diff": round(score_diff, 1),
                            "conviction": conviction,
                        })
                    else:
                        swaps.append({
                            "action": "NEW BUY", "bucket": bk,
                            "buy_ticker": ticker, "buy_name": ps.get("name",""),
                            "buy_score": ps.get("final_score", 0),
                            "conviction": "HIGH",
                        })

            for ticker, ls in live_stocks.items():
                if ticker in pick_stocks:
                    swaps.append({
                        "action": "KEEP", "bucket": bk,
                        "ticker": ticker, "name": ls.get("name",""),
                        "score": ls.get("final_score", 0),
                    })

        return jsonify({"swaps": _sanitise(swaps), "count": len(swaps)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/us/advisory", methods=["GET", "OPTIONS"])
def us_advisory_get():
    """Monthly US tech/semi screener advisory — generated by screener_us.py."""
    if request.method == "OPTIONS":
        return jsonify({}), 200
    global _us_advisory_cache
    if _us_advisory_cache:
        return jsonify(_sanitise(_us_advisory_cache))
    data = _load_json(US_ADVISORY_FILE)
    if data:
        _us_advisory_cache = data
        return jsonify(_sanitise(data))
    return jsonify({"error": "No US advisory yet. Run screener_us.py first."}), 404


@app.route("/us/advisory/upload", methods=["POST", "OPTIONS"])
def us_advisory_upload():
    """Screener POSTs monthly US advisory here after generation."""
    if request.method == "OPTIONS":
        return jsonify({}), 200
    global _us_advisory_cache
    try:
        data = request.get_json(force=True)
        _us_advisory_cache = _sanitise(data)
        _save_json(US_ADVISORY_FILE, _us_advisory_cache)
        print(f"✅ US monthly advisory saved — action: {data.get('action','?')}")
        return jsonify({"status": "ok", "action": data.get("action", "?")})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/price", methods=["GET"])
def single_price():
    """Fetch live price for any single ticker. GET /price?ticker=STX"""
    try:
        import yfinance as yf, math
        ticker = request.args.get("ticker", "").strip().upper()
        if not ticker:
            return jsonify({"error": "ticker param required"}), 400
        info = yf.Ticker(ticker).fast_info
        p = getattr(info, "last_price", None) or getattr(info, "regular_market_price", None)
        chg = getattr(info, "regular_market_change_percent", None)
        prev = getattr(info, "previous_close", None)
        if p is None or math.isnan(float(p)):
            return jsonify({"error": f"No price data for {ticker}"}), 404
        if (not chg or math.isnan(float(chg))) and prev:
            chg = (float(p) - float(prev)) / float(prev) * 100
        return jsonify({
            "ticker": ticker,
            "price": round(float(p), 2),
            "change_pct": round(float(chg) if chg and not math.isnan(float(chg)) else 0, 2),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/us/prices", methods=["GET"])
def us_prices():
    """Fetch live USD prices for all US portfolio stocks."""
    try:
        import yfinance as yf
        import math
        from datetime import datetime

        port = _us_live_cache or (_load_json(US_LIVE_FILE) or {})
        tickers = []
        for bucket in port.values():
            for s in bucket.get("stocks", []):
                t = s.get("ticker")
                if t:
                    tickers.append(t)

        if not tickers:
            return jsonify({"error": "No US portfolio loaded", "prices": {}})

        result = {}
        ts = datetime.now().strftime("%H:%M")

        for ticker in tickers:
            try:
                fi    = yf.Ticker(ticker).fast_info
                price = getattr(fi, "last_price",     None)
                prev  = getattr(fi, "previous_close", None)
                if price is None:
                    info  = yf.Ticker(ticker).info
                    price = info.get("regularMarketPrice") or info.get("currentPrice")
                    prev  = info.get("regularMarketPreviousClose") or info.get("previousClose")
                if price is None:
                    continue
                price = round(float(price), 2)
                prev  = round(float(prev), 2) if prev else price
                change_pct = round(((price - prev) / prev) * 100, 2) if prev else 0
                result[ticker] = {"price": price, "change_pct": change_pct, "ts": ts}
            except Exception as e:
                print(f"  ⚠️  US price fetch failed for {ticker}: {e}")

        return jsonify({"prices": result, "ts": ts, "count": len(result)})
    except Exception as e:
        return jsonify({"error": str(e), "prices": {}})


@app.route("/us/performance", methods=["GET"])
def us_performance_get():
    global _us_perf_cache
    if not _us_perf_cache:
        loaded = _load_json(US_PERF_FILE)
        _us_perf_cache = loaded if isinstance(loaded, list) else []
    return jsonify({"history": _sanitise(_us_perf_cache), "count": len(_us_perf_cache)})


@app.route("/us/performance/upload", methods=["POST", "OPTIONS"])
def us_performance_upload():
    global _us_perf_cache
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        rec = request.get_json(force=True)
        date_str = rec.get("date")
        if not date_str:
            return jsonify({"error": "date required"}), 400
        if not _us_perf_cache:
            loaded = _load_json(US_PERF_FILE)
            _us_perf_cache = loaded if isinstance(loaded, list) else []
        _us_perf_cache = [r for r in _us_perf_cache if r.get("date") != date_str]
        _us_perf_cache.append(rec)
        _us_perf_cache.sort(key=lambda r: r.get("date", ""))
        _save_json(US_PERF_FILE, _us_perf_cache)
        return jsonify({"ok": True, "date": date_str, "total": len(_us_perf_cache)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/us/portfolio/history", methods=["GET"])
def us_portfolio_history_get():
    global _us_history_cache
    if not _us_history_cache:
        loaded = _load_json(US_HISTORY_FILE)
        _us_history_cache = loaded if isinstance(loaded, list) else []
    return jsonify({"trades": _sanitise(_us_history_cache), "count": len(_us_history_cache)})


@app.route("/us/portfolio/history/upload", methods=["POST", "OPTIONS"])
def us_portfolio_history_upload():
    global _us_history_cache
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        rec = request.get_json(force=True)

        # Support clearing: POST [] wipes history
        if isinstance(rec, list):
            _us_history_cache = rec
            _save_json(US_HISTORY_FILE, _us_history_cache)
            return jsonify({"ok": True, "total": len(_us_history_cache), "action": "replaced"})

        if not rec.get("ticker"):
            return jsonify({"error": "ticker required"}), 400
        if not _us_history_cache:
            loaded = _load_json(US_HISTORY_FILE)
            _us_history_cache = loaded if isinstance(loaded, list) else []
        _us_history_cache.append(rec)
        _us_history_cache.sort(key=lambda r: r.get("sell_date", ""), reverse=True)
        _save_json(US_HISTORY_FILE, _us_history_cache)
        return jsonify({"ok": True, "total": len(_us_history_cache)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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



# ═══════════════════════════════════════════════════════════
# SWING TRADING ENDPOINTS
# ═══════════════════════════════════════════════════════════

# ── GET /swing/candidates ────────────────────────────────────────
@app.route("/swing/candidates", methods=["GET", "OPTIONS"])
def swing_candidates_get():
    """Today's swing trade candidates from swing_scanner.py."""
    global _swing_candidates_cache
    if request.method == "OPTIONS":
        return jsonify({}), 200
    if not _swing_candidates_cache:
        loaded = _load_json(SWING_CANDIDATES_FILE)
        if loaded:
            _swing_candidates_cache = loaded
    return jsonify(_sanitise(_swing_candidates_cache or {
        "candidates": [],
        "generated_at": None,
        "total_candidates": 0,
    }))


# ── POST /swing/candidates/upload ────────────────────────────────
@app.route("/swing/candidates/upload", methods=["POST", "OPTIONS"])
def swing_candidates_upload():
    """swing_scanner.py posts today's candidates here."""
    global _swing_candidates_cache
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        data = request.get_json(force=True)
        # Accept direct payload or signals-style {type, payload}
        if "type" in data and "payload" in data:
            data = data["payload"]
        _swing_candidates_cache = data
        _save_json(SWING_CANDIDATES_FILE, data)
        count = len(data.get("candidates", []))
        return jsonify({"status": "ok", "candidates": count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── GET /swing/live ──────────────────────────────────────────────
@app.route("/swing/live", methods=["GET", "OPTIONS"])
def swing_live_get():
    """Currently open swing positions (entered on Kite)."""
    global _swing_live_cache
    if request.method == "OPTIONS":
        return jsonify({}), 200
    if not _swing_live_cache:
        loaded = _load_json(SWING_LIVE_FILE)
        _swing_live_cache = loaded if isinstance(loaded, list) else []
    return jsonify(_sanitise(_swing_live_cache))


# ── POST /swing/live/upload ──────────────────────────────────────
@app.route("/swing/live/upload", methods=["POST", "OPTIONS"])
def swing_live_upload():
    """
    Dashboard POSTs when user marks a swing trade as entered.
    Body: single trade record OR full list to replace.
    """
    global _swing_live_cache
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        data = request.get_json(force=True)

        # Full list replace (e.g. [] to clear)
        if isinstance(data, list):
            _swing_live_cache = data
            _save_json(SWING_LIVE_FILE, _swing_live_cache)
            return jsonify({"status": "ok", "positions": len(_swing_live_cache)})

        # Single trade append
        if not data.get("ticker"):
            return jsonify({"error": "ticker required"}), 400
        if not _swing_live_cache:
            loaded = _load_json(SWING_LIVE_FILE)
            _swing_live_cache = loaded if isinstance(loaded, list) else []

        # Update if exists, else append
        existing_idx = next(
            (i for i, p in enumerate(_swing_live_cache)
             if p.get("ticker") == data["ticker"]), None
        )
        if existing_idx is not None:
            _swing_live_cache[existing_idx] = data
        else:
            _swing_live_cache.append(data)

        _save_json(SWING_LIVE_FILE, _swing_live_cache)
        return jsonify({"status": "ok", "positions": len(_swing_live_cache)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── GET /swing/history ───────────────────────────────────────────
@app.route("/swing/history", methods=["GET", "OPTIONS"])
def swing_history_get():
    """All closed swing trades with P&L."""
    global _swing_history_cache
    if request.method == "OPTIONS":
        return jsonify({}), 200
    if not _swing_history_cache:
        loaded = _load_json(SWING_HISTORY_FILE)
        _swing_history_cache = loaded if isinstance(loaded, list) else []
    return jsonify({
        "trades":       _sanitise(_swing_history_cache),
        "total_trades": len(_swing_history_cache),
        "winners":      sum(1 for t in _swing_history_cache if t.get("realised_pnl_inr", 0) > 0),
        "total_pnl":    round(sum(t.get("realised_pnl_inr", 0) for t in _swing_history_cache), 2),
    })


# ── POST /swing/history/upload ───────────────────────────────────
@app.route("/swing/history/upload", methods=["POST", "OPTIONS"])
def swing_history_upload():
    """
    Dashboard POSTs a closed swing trade here.
    Body: single trade record to append, OR [] to clear.
    """
    global _swing_history_cache
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        data = request.get_json(force=True)

        # Clear/replace with list
        if isinstance(data, list):
            _swing_history_cache = data
            _save_json(SWING_HISTORY_FILE, _swing_history_cache)
            return jsonify({"ok": True, "total": len(_swing_history_cache), "action": "replaced"})

        if not data.get("ticker"):
            return jsonify({"error": "ticker required"}), 400
        if not _swing_history_cache:
            loaded = _load_json(SWING_HISTORY_FILE)
            _swing_history_cache = loaded if isinstance(loaded, list) else []

        _swing_history_cache.append(data)
        _swing_history_cache.sort(
            key=lambda r: r.get("exit_date", ""), reverse=True
        )
        _save_json(SWING_HISTORY_FILE, _swing_history_cache)
        return jsonify({"ok": True, "total": len(_swing_history_cache)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── SWING QUEUE — automated 9AM entry ───────────────────────────

_swing_queue: dict = {}  # keyed by ticker, e.g. "RELIANCE.NS"


def _read_queue() -> dict:
    global _swing_queue
    data = _load_json(SWING_QUEUE_FILE)
    _swing_queue = data if isinstance(data, dict) else {}
    return _swing_queue


def _write_queue(q: dict):
    global _swing_queue
    _swing_queue = q
    _save_json(SWING_QUEUE_FILE, q)


@app.route("/swing/queue", methods=["GET", "OPTIONS"])
def swing_queue_get():
    """Return current queue. Optional ?status= filter."""
    if request.method == "OPTIONS":
        return jsonify({}), 200
    q = _read_queue()
    status_filter = request.args.get("status", "")
    if status_filter:
        q = {k: v for k, v in q.items() if v.get("status") == status_filter}
    return jsonify({"queue": list(q.values()), "count": len(q)})


@app.route("/swing/queue", methods=["POST"])
def swing_queue_add():
    """Add or replace a ticker in the queue."""
    try:
        entry = request.get_json(force=True) or {}
        ticker = entry.get("ticker")
        if not ticker:
            return jsonify({"error": "ticker required"}), 400
        from datetime import datetime as _dt
        q = _read_queue()
        entry.setdefault("queued_at", _dt.now().isoformat())
        entry.setdefault("status", "queued")
        q[ticker] = entry
        _write_queue(q)
        print(f"✅ Swing queue: added {ticker} (qty={entry.get('quantity')})")
        return jsonify({"status": "ok", "ticker": ticker, "count": len(q)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/swing/queue/remove", methods=["POST", "OPTIONS"])
def swing_queue_remove():
    """Remove a ticker from the queue."""
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        ticker = (request.get_json(force=True) or {}).get("ticker")
        if not ticker:
            return jsonify({"error": "ticker required"}), 400
        q = _read_queue()
        removed = ticker in q
        q.pop(ticker, None)
        _write_queue(q)
        print(f"✅ Swing queue: removed {ticker}")
        return jsonify({"status": "ok", "removed": removed, "count": len(q)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/swing/queue/update", methods=["POST", "OPTIONS"])
def swing_queue_update():
    """Patch one or more queue entries. Body: [{ticker, ...fields}] or {ticker, ...fields}."""
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        payload = request.get_json(force=True)
        updates = payload if isinstance(payload, list) else [payload]
        q = _read_queue()
        for upd in updates:
            ticker = upd.get("ticker")
            if ticker and ticker in q:
                q[ticker].update({k: v for k, v in upd.items() if k != "ticker"})
                print(f"✅ Swing queue: updated {ticker} → {upd.get('status', '?')}")
        _write_queue(q)
        return jsonify({"status": "ok", "updated": len(updates)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── GET /swing/prices ────────────────────────────────────────────
@app.route("/swing/prices", methods=["GET", "OPTIONS"])
def swing_prices():
    """
    Live prices for all open swing positions.
    Same pattern as /prices but reads from swing/live.
    """
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        global _swing_live_cache
        if not _swing_live_cache:
            loaded = _load_json(SWING_LIVE_FILE)
            _swing_live_cache = loaded if isinstance(loaded, list) else []

        if not _swing_live_cache:
            return jsonify({"prices": {}})

        import yfinance as yf
        import math

        tickers = list({p["ticker"] for p in _swing_live_cache if p.get("ticker")})
        prices  = {}

        for ticker in tickers:
            try:
                fi = yf.Ticker(ticker).fast_info
                price = getattr(fi, "last_price", None) or getattr(fi, "regular_market_price", None)
                prev  = getattr(fi, "previous_close", None)
                chg   = getattr(fi, "regular_market_change_percent", None)
                if price and not math.isnan(float(price)):
                    p = float(price)
                    # Calculate change_pct from previous_close if fast_info chg is 0/None
                    if (not chg or math.isnan(float(chg)) or float(chg) == 0) and prev:
                        chg = (p - float(prev)) / float(prev) * 100
                    prices[ticker] = {
                        "price":      round(p, 2),
                        "change_pct": round(float(chg) if chg and not math.isnan(float(chg)) else 0, 2),
                    }
            except Exception:
                pass

        return jsonify({"prices": _sanitise(prices)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/trigger/llm-synth", methods=["POST", "OPTIONS"])
def trigger_llm_synth():
    """Run LLM macro synthesis from the web service (has outbound internet).
    Protected by X-Upload-Token. Returns the verdict on success."""
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        import importlib, sys
        # Force fresh import in case module was cached without env vars
        if "llm_synthesiser" in sys.modules:
            del sys.modules["llm_synthesiser"]
        from llm_synthesiser import run_synthesis
        verdict = run_synthesis()
        if verdict is None:
            return jsonify({"error": "Synthesis failed — check ANTHROPIC_API_KEY and network"}), 500
        return jsonify({"status": "ok", "verdict": verdict})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ════════════════════════════════════════════════════════════════
#  KITE CONNECT WEBHOOK ENDPOINTS
#  These URLs are registered in Zerodha Kite Connect app settings:
#    Redirect URL: https://<railway-domain>/kite/callback
#    Postback URL: https://<railway-domain>/kite/postback
# ════════════════════════════════════════════════════════════════

ORACLE_VPS_URL   = os.getenv("ORACLE_VPS_URL", "")   # e.g. http://80.225.201.62:5001
EXECUTOR_SECRET  = os.getenv("EXECUTOR_SECRET", "")   # shared secret with Oracle VPS

_VPS_HEADERS = lambda: {"X-Executor-Secret": EXECUTOR_SECRET}


def _vps_post(endpoint: str, payload: dict):
    import urllib.request, urllib.error, json as _json
    if not ORACLE_VPS_URL:
        return {"error": "Oracle VPS not configured"}, 503
    body = _json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{ORACLE_VPS_URL}{endpoint}", data=body,
        headers={"Content-Type": "application/json", **_VPS_HEADERS()},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return _json.loads(resp.read()), resp.status
    except urllib.error.HTTPError as e:
        return _json.loads(e.read()), e.code
    except Exception as e:
        return {"error": str(e)}, 502


def _vps_get(endpoint: str):
    import urllib.request, urllib.error, json as _json
    if not ORACLE_VPS_URL:
        return {"error": "Oracle VPS not configured"}, 503
    req = urllib.request.Request(
        f"{ORACLE_VPS_URL}{endpoint}",
        headers=_VPS_HEADERS(),
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return _json.loads(resp.read()), resp.status
    except urllib.error.HTTPError as e:
        return _json.loads(e.read()), e.code
    except Exception as e:
        return {"error": str(e)}, 502


@app.route("/kite/quote", methods=["GET", "OPTIONS"])
def kite_quote():
    """Proxy live LTP from Zerodha. ?symbol=RELIANCE or ?symbol=RELIANCE,TCS"""
    if request.method == "OPTIONS":
        return jsonify({}), 200
    symbol = request.args.get("symbol", "").strip().upper()
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    result, status = _vps_get(f"/get-quote?symbol={symbol}")
    return jsonify(result), status


@app.route("/kite/historical", methods=["GET", "OPTIONS"])
def kite_historical():
    """Proxy daily OHLCV from Zerodha. ?symbol=RELIANCE&days=400"""
    if request.method == "OPTIONS":
        return jsonify({}), 200
    symbol = request.args.get("symbol", "").strip().upper()
    days   = request.args.get("days", "400")
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    result, status = _vps_get(f"/get-historical?symbol={symbol}&days={days}")
    return jsonify(result), status


@app.route("/kite/place-order", methods=["POST", "OPTIONS"])
def kite_place_order():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    data = request.get_json(force=True) or {}
    result, status = _vps_post("/place-order", data)
    return jsonify(result), status


@app.route("/kite/cancel-order", methods=["POST", "OPTIONS"])
def kite_cancel_order():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    data = request.get_json(force=True) or {}
    result, status = _vps_post("/cancel-order", data)
    return jsonify(result), status


@app.route("/kite/get-pnl", methods=["GET", "OPTIONS"])
def kite_get_pnl():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    result, status = _vps_get("/get-pnl")
    return jsonify(result), status


@app.route("/kite/callback", methods=["GET", "OPTIONS"])
def kite_callback():
    """
    Zerodha redirects here after OAuth login with ?request_token=xxx&status=success.
    Forwards the request_token to the Oracle VPS to exchange for an access_token.
    """
    if request.method == "OPTIONS":
        return jsonify({}), 200

    status        = request.args.get("status", "")
    request_token = request.args.get("request_token", "")
    message       = request.args.get("message", "")

    if status != "success" or not request_token:
        print(f"⚠️  Kite callback error: status={status} message={message}")
        return f"""
        <html><body style="font-family:sans-serif;padding:40px">
        <h2>Kite Login Failed</h2>
        <p>Status: {status}</p>
        <p>Message: {message or 'Unknown error'}</p>
        </body></html>
        """, 400

    print(f"✅ Kite callback: request_token={request_token[:8]}… status={status}")

    # Forward to Oracle VPS to exchange for access_token
    exchange_ok = False
    if ORACLE_VPS_URL:
        try:
            import urllib.request, urllib.error
            import json as _json
            payload = _json.dumps({"request_token": request_token}).encode()
            req = urllib.request.Request(
                f"{ORACLE_VPS_URL}/exchange-token",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Executor-Secret": EXECUTOR_SECRET,
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = _json.loads(resp.read())
                exchange_ok = result.get("status") == "ok"
                print(f"✅ Token exchanged for user: {result.get('user_id')}")
        except Exception as e:
            print(f"⚠️  Could not forward token to Oracle VPS: {e}")

    status_msg = "Access token updated on trading server." if exchange_ok else (
        "Token received. Trading server update skipped (VPS not configured)."
        if not ORACLE_VPS_URL else
        "Warning: token received but trading server exchange failed — check VPS logs."
    )

    return f"""
    <html><body style="font-family:sans-serif;padding:40px;max-width:500px;margin:auto">
    <h2>&#10003; Kite Login Successful</h2>
    <p>{status_msg}</p>
    <p style="color:#888;font-size:13px">You can close this window.</p>
    </body></html>
    """


@app.route("/kite/postback", methods=["POST", "OPTIONS"])
def kite_postback():
    """
    Zerodha POSTs order status updates here (fills, rejections, cancellations).
    On a BUY fill for a queued swing trade: auto-place GTT stop-loss.
    """
    if request.method == "OPTIONS":
        return jsonify({}), 200

    try:
        data      = request.get_json(silent=True) or request.form.to_dict()
        order_id  = str(data.get("order_id", "?"))
        status    = data.get("status", "?")
        symbol    = data.get("tradingsymbol", "?")
        side      = data.get("transaction_type", "?")
        qty       = data.get("quantity", "?")
        avg_price = data.get("average_price", "?")

        print(f"📬 Kite postback: {side} {qty}x{symbol} @ ₹{avg_price} "
              f"→ {status} (order_id={order_id})")

        # Auto GTT stop-loss when a queued swing BUY fills
        if status == "COMPLETE" and side == "BUY" and avg_price:
            try:
                fill_price = float(avg_price)
                fill_qty   = int(data.get("filled_quantity") or qty or 0)
            except (TypeError, ValueError):
                fill_price, fill_qty = 0, 0

            if fill_price > 0 and fill_qty > 0:
                q = _read_queue()
                ticker_ns = f"{symbol}.NS"
                entry = q.get(ticker_ns) or q.get(symbol)
                t_key = ticker_ns if ticker_ns in q else (symbol if symbol in q else None)

                if entry and entry.get("status") in ("queued", "order_placed"):
                    stop_loss = entry.get("stop_loss")
                    if stop_loss:
                        gtt_payload = {
                            "symbol":        symbol,
                            "trigger_price": float(stop_loss),
                            "quantity":      fill_qty,
                            "side":          "SELL",
                            "order_type":    "MARKET",
                            "product":       "CNC",
                        }
                        gtt_result, _ = _vps_post("/place-gtt", gtt_payload)
                        gtt_id = gtt_result.get("gtt_id") or gtt_result.get("trigger_id")
                        print(f"✅ Auto-GTT: {symbol} stop ₹{stop_loss} qty {fill_qty} → gtt_id={gtt_id}")
                        if t_key:
                            q[t_key].update({
                                "status":     "filled",
                                "fill_price": fill_price,
                                "order_id":   order_id,
                                "gtt_id":     gtt_id,
                            })
                            _write_queue(q)
    except Exception as e:
        print(f"⚠️  Kite postback error: {e}")

    return jsonify({"status": "ok"}), 200


# ════════════════════════════════════════════════════════════════
#  INDIA MONTHLY QUEUE — automated buy/sell orders
# ════════════════════════════════════════════════════════════════

_india_queue: dict = {}  # keyed by ticker e.g. "RELIANCE.NS"


def _read_india_queue() -> dict:
    global _india_queue
    data = _load_json(INDIA_QUEUE_FILE)
    _india_queue = data if isinstance(data, dict) else {}
    return _india_queue


def _write_india_queue(q: dict):
    global _india_queue
    _india_queue = q
    _save_json(INDIA_QUEUE_FILE, q)


@app.route("/india/queue", methods=["GET", "OPTIONS"])
def india_queue_get():
    """Return India monthly order queue. Optional ?status= or ?action= filter."""
    if request.method == "OPTIONS":
        return jsonify({}), 200
    q = _read_india_queue()
    status_filter = request.args.get("status", "")
    action_filter = request.args.get("action", "")
    if status_filter:
        q = {k: v for k, v in q.items() if v.get("status") == status_filter}
    if action_filter:
        q = {k: v for k, v in q.items() if v.get("action") == action_filter}
    return jsonify({"queue": list(q.values()), "count": len(q)})


@app.route("/india/queue", methods=["POST"])
def india_queue_add():
    """Add or replace a ticker in the India queue."""
    try:
        entry = request.get_json(force=True) or {}
        ticker = entry.get("ticker")
        if not ticker:
            return jsonify({"error": "ticker required"}), 400
        from datetime import datetime as _dt
        q = _read_india_queue()
        entry.setdefault("queued_at", _dt.now().isoformat())
        entry.setdefault("status", "queued")
        q[ticker] = entry
        _write_india_queue(q)
        print(f"✅ India queue: {entry.get('action','?')} {ticker} qty={entry.get('quantity')}")
        return jsonify({"status": "ok", "ticker": ticker, "count": len(q)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/india/queue/remove", methods=["POST", "OPTIONS"])
def india_queue_remove():
    """Remove a ticker from the India queue."""
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        ticker = (request.get_json(force=True) or {}).get("ticker")
        if not ticker:
            return jsonify({"error": "ticker required"}), 400
        q = _read_india_queue()
        removed = ticker in q
        q.pop(ticker, None)
        _write_india_queue(q)
        print(f"✅ India queue: removed {ticker}")
        return jsonify({"status": "ok", "removed": removed, "count": len(q)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/india/queue/update", methods=["POST", "OPTIONS"])
def india_queue_update():
    """Patch one or more India queue entries. Body: [{ticker, ...fields}] or {ticker, ...fields}."""
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        payload = request.get_json(force=True)
        updates = payload if isinstance(payload, list) else [payload]
        q = _read_india_queue()
        for upd in updates:
            ticker = upd.get("ticker")
            if ticker and ticker in q:
                q[ticker].update({k: v for k, v in upd.items() if k != "ticker"})
                print(f"✅ India queue: updated {ticker} → {upd.get('status', '?')}")
        _write_india_queue(q)
        return jsonify({"status": "ok", "updated": len(updates)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
