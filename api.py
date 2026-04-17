"""
API Server — Serves portfolio data to the Netlify dashboard
============================================================
A lightweight Flask API that exposes portfolio JSON files
and signal data so the static Netlify dashboard can read them.

Deploy this as a 7th Railway service (always-on, not a cron job).
Railway will give it a public HTTPS URL automatically.

Routes:
  GET /portfolio/latest  → latest portfolio_YYYYMM.json
  GET /fiidii            → fiidii_history.json
  GET /signals           → policy + news + llm signals combined
  GET /health            → health check

Usage:
  python api.py
"""

from flask import Flask, jsonify
import glob
import json
import os

app = Flask(__name__)

DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(__file__))


def _load_json(path: str):
    """Load a JSON file safely. Returns None if missing."""
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _find_latest_portfolio() -> str | None:
    """Find the most recent portfolio JSON file."""
    patterns = [
        os.path.join(DATA_DIR, "portfolio_*.json"),
        os.path.join(os.path.dirname(__file__), "portfolio_*.json"),
        "portfolio_*.json",
    ]
    files = []
    for p in patterns:
        files.extend(glob.glob(p))
    if not files:
        return None
    return sorted(set(files))[-1]


@app.after_request
def add_cors(response):
    """Allow Netlify dashboard to call this API."""
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


@app.route("/portfolio/latest")
def latest_portfolio():
    path = _find_latest_portfolio()
    if not path:
        return jsonify({"error": "No portfolio found. Run screener.py first."}), 404
    data = _load_json(path)
    if not data:
        return jsonify({"error": "Portfolio file is empty or corrupt."}), 500
    return jsonify(data)


@app.route("/fiidii")
def fiidii():
    path = os.path.join(DATA_DIR, "fiidii_history.json")
    data = _load_json(path) or []
    return jsonify(data)


@app.route("/signals")
def signals():
    result = {}
    for name in ["policy_signals", "news_signals", "llm_synthesis"]:
        path = os.path.join(DATA_DIR, f"{name}.json")
        data = _load_json(path)
        if data:
            result[name] = data
    return jsonify(result)


@app.route("/health")
def health():
    portfolio_path = _find_latest_portfolio()
    return jsonify({
        "status":    "ok",
        "portfolio": os.path.basename(portfolio_path) if portfolio_path else None,
        "data_dir":  DATA_DIR,
    })


@app.route("/")
def index():
    return jsonify({
        "name":      "Indian Stock Screener API",
        "endpoints": ["/portfolio/latest", "/fiidii", "/signals", "/health"],
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
