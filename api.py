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
_portfolio_cache: dict = {}
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
    """fii-collector POSTs its history here after every run."""
    global _fiidii_cache
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({"error": "Empty payload"}), 400
        _fiidii_cache = data
        # Persist to disk best-effort
        path = os.path.join(DATA_DIR, "fiidii_history.json")
        _save_json(path, data)
        print(f"✅ FII/DII data received: {len(data)} records")
        return jsonify({"status": "ok", "records": len(data)}), 200
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
