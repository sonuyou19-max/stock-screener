#!/usr/bin/env python3
"""
swing_attribution.py — weekly performance attribution for closed swing trades.

Answers "which trades work and why": win rate, average P&L% and average
R-multiple broken down by exit reason, score band, conviction, sector,
sentiment, market regime and per-signal strength. This is the feedback
loop the scanner never had — run it after ~30-50 closed trades before
drawing conclusions, then recalibrate scanner weights with evidence.

Reads /swing/history from the Railway API (trades carry an entry-time
signal snapshot since the Phase-2 change; older trades without one are
included in the overall stats and skipped in snapshot breakdowns).

Outputs: full report to stdout, compact summary to Telegram, and the
report JSON to the API signal store (type=swing_attribution) so the
dashboard can render it later.

Suggested cron (Sunday 6:30 PM IST = 13:00 UTC):
  0 13 * * 0 /home/ubuntu/kite/run_swing_attribution.sh >> /home/ubuntu/kite/attribution.log 2>&1

Usage:
  python swing_attribution.py            # fetch, report, telegram, upload
  python swing_attribution.py --dry-run  # fetch + print only
"""

import os
import json
import argparse
import urllib.request as _req

API_URL       = os.getenv("API_URL", "https://web-production-50eee.up.railway.app").rstrip("/")
UPLOAD_TOKEN  = os.getenv("UPLOAD_TOKEN", "")
TELEGRAM_BOT  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")

MIN_SAMPLE       = 10   # below this, flag every conclusion as low-confidence
MIN_BUCKET       = 3    # hide breakdown rows with fewer trades than this
SCORE_BANDS      = [(0, 65, "<65"), (65, 75, "65-75"), (75, 101, "75+")]
SIGNAL_STRONG_AT = 60.0  # per-signal strength >= this counts as "strong"


def _get(url):
    r = _req.Request(url, headers={"Accept": "application/json"})
    with _req.urlopen(r, timeout=20) as resp:
        return json.loads(resp.read())


def _post(url, payload, headers=None):
    body = json.dumps(payload, default=str).encode()
    h = {"Content-Type": "application/json", **(headers or {})}
    r = _req.Request(url, data=body, headers=h, method="POST")
    with _req.urlopen(r, timeout=20) as resp:
        return json.loads(resp.read())


def _tg(msg: str):
    if not TELEGRAM_BOT or not TELEGRAM_CHAT:
        print("  ⚠️  Telegram not configured — skipping alert.")
        return
    try:
        _post(f"https://api.telegram.org/bot{TELEGRAM_BOT}/sendMessage",
              {"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "HTML"})
    except Exception as e:
        print(f"  ⚠️  Telegram failed: {e}")


# ─────────────────────────────────────────────
# REPORT BUILDING (pure functions — unit-testable)
# ─────────────────────────────────────────────

def _stats(trades: list) -> dict:
    """Aggregate stats for a set of trades."""
    n = len(trades)
    if n == 0:
        return {"n": 0}
    wins = [t for t in trades if (t.get("realised_pnl_inr") or 0) > 0]
    pnl_total = sum(t.get("realised_pnl_inr") or 0 for t in trades)
    pcts = [t["realised_pnl_pct"] for t in trades if t.get("realised_pnl_pct") is not None]
    rs   = [t["r_multiple"] for t in trades if t.get("r_multiple") is not None]
    return {
        "n":         n,
        "win_rate":  round(len(wins) / n * 100, 1),
        "total_pnl": round(pnl_total, 2),
        "avg_pct":   round(sum(pcts) / len(pcts), 2) if pcts else None,
        "avg_r":     round(sum(rs) / len(rs), 2) if rs else None,
    }


def _group(trades: list, key_fn) -> dict:
    groups: dict = {}
    for t in trades:
        k = key_fn(t)
        if k is None:
            continue
        groups.setdefault(k, []).append(t)
    return {k: _stats(v) for k, v in groups.items() if len(v) >= MIN_BUCKET}


def _score_band(t):
    s = t.get("score")
    if s is None:
        return None
    # Pre-migration trades scored 0-7; snapshot carries max_score to detect
    if (t.get("max_score") or 100) <= 10:
        return None
    for lo, hi, label in SCORE_BANDS:
        if lo <= float(s) < hi:
            return label
    return None


def _signal_breakdown(trades: list) -> dict:
    """Per-signal: win rate when the signal was strong vs weak at entry."""
    out: dict = {}
    with_signals = [t for t in trades if isinstance(t.get("signals"), dict)]
    if not with_signals:
        return out
    names = sorted({k for t in with_signals for k in t["signals"]})
    for name in names:
        strong, weak = [], []
        for t in with_signals:
            sig = t["signals"].get(name)
            if not isinstance(sig, dict) or sig.get("strength") is None:
                continue
            (strong if float(sig["strength"]) >= SIGNAL_STRONG_AT else weak).append(t)
        s_stats, w_stats = _stats(strong), _stats(weak)
        if s_stats["n"] >= MIN_BUCKET or w_stats["n"] >= MIN_BUCKET:
            out[name] = {"strong": s_stats, "weak": w_stats}
    return out


def build_report(trades: list) -> dict:
    return {
        "total":          _stats(trades),
        "by_exit_reason": _group(trades, lambda t: t.get("exit_reason")),
        "by_score_band":  _group(trades, _score_band),
        "by_conviction":  _group(trades, lambda t: t.get("conviction")),
        "by_sector":      _group(trades, lambda t: t.get("sector")),
        "by_sentiment":   _group(trades, lambda t: t.get("sentiment_val")),
        "by_regime":      _group(trades, lambda t: t.get("regime")),
        "by_signal":      _signal_breakdown(trades),
        "low_confidence": len(trades) < MIN_SAMPLE,
    }


def _fmt_row(label: str, s: dict) -> str:
    if not s or not s.get("n"):
        return f"  {label:<22} —"
    parts = [f"n={s['n']:<3}", f"win {s['win_rate']:>5.1f}%"]
    if s.get("avg_pct") is not None:
        parts.append(f"avg {s['avg_pct']:+.2f}%")
    if s.get("avg_r") is not None:
        parts.append(f"avg R {s['avg_r']:+.2f}")
    parts.append(f"P&L ₹{s['total_pnl']:,.0f}")
    return f"  {label:<22} " + "  ".join(parts)


def format_report(rep: dict) -> str:
    lines = ["=" * 64, "  SWING TRADE ATTRIBUTION", "=" * 64]
    t = rep["total"]
    if not t["n"]:
        lines.append("  No closed trades yet — nothing to attribute.")
        return "\n".join(lines)
    lines.append(_fmt_row("OVERALL", t))
    if rep["low_confidence"]:
        lines.append(f"  ⚠️  Only {t['n']} closed trades — treat every split below "
                     f"as anecdote, not signal (need ≥{MIN_SAMPLE}).")
    sections = [
        ("BY EXIT REASON",  "by_exit_reason"),
        ("BY SCORE BAND",   "by_score_band"),
        ("BY CONVICTION",   "by_conviction"),
        ("BY SECTOR",       "by_sector"),
        ("BY SENTIMENT",    "by_sentiment"),
        ("BY REGIME",       "by_regime"),
    ]
    for title, key in sections:
        if rep[key]:
            lines.append(f"\n  ── {title} " + "─" * (48 - len(title)))
            for k in sorted(rep[key], key=lambda x: -rep[key][x]["n"]):
                lines.append(_fmt_row(str(k)[:22], rep[key][k]))
    if rep["by_signal"]:
        lines.append("\n  ── BY SIGNAL (strong ≥%.0f vs weak at entry) " % SIGNAL_STRONG_AT
                     + "─" * 8)
        for name, d in sorted(rep["by_signal"].items()):
            s, w = d["strong"], d["weak"]
            s_txt = f"strong n={s['n']} win {s['win_rate']}%" if s.get("n") else "strong —"
            w_txt = f"weak n={w['n']} win {w['win_rate']}%" if w.get("n") else "weak —"
            lines.append(f"  {name:<14} {s_txt:<28} {w_txt}")
    lines.append("=" * 64)
    return "\n".join(lines)


def format_telegram(rep: dict) -> str:
    t = rep["total"]
    if not t["n"]:
        return "📊 <b>Swing Attribution</b>\nNo closed trades yet."
    lines = [
        "📊 <b>Swing Attribution (weekly)</b>",
        f"Closed: {t['n']} · Win rate: <b>{t['win_rate']}%</b> · "
        f"P&L: <b>₹{t['total_pnl']:,.0f}</b>"
        + (f" · avg R {t['avg_r']:+.2f}" if t.get("avg_r") is not None else ""),
    ]
    if rep["low_confidence"]:
        lines.append(f"⚠️ Small sample (&lt;{MIN_SAMPLE}) — read as anecdote.")
    if rep["by_exit_reason"]:
        lines.append("\n<b>Exits:</b>")
        for k, s in sorted(rep["by_exit_reason"].items(), key=lambda x: -x[1]["n"]):
            lines.append(f"  {k}: {s['n']} trades, win {s['win_rate']}%, ₹{s['total_pnl']:,.0f}")
    if rep["by_score_band"]:
        lines.append("\n<b>Score bands:</b>")
        for k, s in sorted(rep["by_score_band"].items()):
            lines.append(f"  {k}: {s['n']} trades, win {s['win_rate']}%"
                         + (f", avg R {s['avg_r']:+.2f}" if s.get("avg_r") is not None else ""))
    best_sec = max(rep["by_sector"].items(), key=lambda x: x[1]["total_pnl"], default=None) \
        if rep["by_sector"] else None
    worst_sec = min(rep["by_sector"].items(), key=lambda x: x[1]["total_pnl"], default=None) \
        if rep["by_sector"] else None
    if best_sec and worst_sec and best_sec[0] != worst_sec[0]:
        lines.append(f"\n🏆 Best sector: {best_sec[0]} (₹{best_sec[1]['total_pnl']:,.0f})")
        lines.append(f"🔻 Worst sector: {worst_sec[0]} (₹{worst_sec[1]['total_pnl']:,.0f})")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main(dry_run: bool = False):
    print("=== Swing Attribution ===")
    data = _get(f"{API_URL}/swing/history")
    trades = data.get("trades", [])
    print(f"  Closed trades: {len(trades)}")

    rep = build_report(trades)
    print(format_report(rep))

    if dry_run:
        print("  [dry-run] skipping Telegram + upload")
        return rep

    _tg(format_telegram(rep))

    try:
        headers = {"X-Upload-Token": UPLOAD_TOKEN} if UPLOAD_TOKEN else {}
        _post(f"{API_URL}/signals/upload",
              {"type": "swing_attribution", "payload": rep}, headers)
        print("  ✅ Report uploaded to signal store (swing_attribution)")
    except Exception as e:
        print(f"  ⚠️  Upload failed: {e}")
    return rep


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Swing trade attribution report")
    p.add_argument("--dry-run", action="store_true", help="print only, no Telegram/upload")
    args = p.parse_args()
    main(dry_run=args.dry_run)
