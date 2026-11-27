#!/usr/bin/env python3
"""Paper trading for US monthly momentum (round-2 search winner), pretend money only.

Rule: on the first US trading day of each month, at the close, rank the 200 most-traded US stocks
(60-day $ volume) by their return from 12 months ago to 1 month ago, and hold the top 10 equally
until the next month. Trading 212 FX fee (0.15% each way) charged on turnover. Never NVDA.

Backtest 2017-2026 (methods.py, no hindsight in the universe): about +37%/year simple average,
positive in 2017-2021 and 2022-2026, but only 54% of 31-day periods profitable, worst 31 days -26%,
losing years 2018 and 2022. Survivorship (delisted stocks missing) flatters it.

  python3 autopilot/momentum_paper.py run     # daily after the US close; rebalances on month change
  python3 autopilot/momentum_paper.py stats
Book: trader/momentum_paper_state.json (pretend EUR 1,000; results also shown per EUR 130)
"""
import datetime as dt
import gzip
import json
import os
import sys
import time
import urllib.request
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOOK = os.path.join(ROOT, "trader", "momentum_paper_state.json")
CACHE = os.environ.get("BIG_CACHE", "/tmp/bigcache")
NY = ZoneInfo("America/New_York")
FEE, TOP_LIQ, HOLD_N, CAPITAL = 0.0015, 200, 10, 1000.0
NEVER = {"NVDA"}


def daily(sym, rng="1y"):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval=1d"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=25) as r:
            res = json.load(r)["chart"]["result"][0]
    except Exception:
        return None
    q = res["indicators"]["quote"][0]
    adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose") or q["close"]
    rows = []
    for t, c, a, v in zip(res["timestamp"], q["close"], adj, q["volume"]):
        if c and a:
            rows.append((dt.datetime.fromtimestamp(t, NY).date(), a, c * (v or 0)))
    return rows


def candidates():
    """The ~400 most-traded US stocks from the 10-year cache (refreshed monthly with live data)."""
    us = json.load(open(os.path.join(ROOT, "autopilot", "universe_big_us.json")))
    dv = []
    for s in us:
        p = os.path.join(CACHE, s + ".json.gz")
        if not os.path.exists(p):
            continue
        with gzip.open(p, "rt") as f:
            rows = json.load(f)
        if len(rows) > 260:
            tail = rows[-60:]
            dv.append((sum(r[6] * r[5] for r in tail) / len(tail), s))
    dv.sort(reverse=True)
    return [s for _, s in dv[:400]]


def load():
    try:
        return json.load(open(BOOK))
    except (OSError, ValueError):
        return {"started": dt.date.today().isoformat(), "equity_eur": CAPITAL, "holdings": [], "month": None, "history": []}


def save(b):
    json.dump(b, open(BOOK + ".tmp", "w"), indent=1)
    os.replace(BOOK + ".tmp", BOOK)


def rebalance(book, today):
    data = {}
    for s in candidates():
        if s in NEVER:
            continue
        rows = daily(s)
        time.sleep(0.2)
        if rows and len(rows) >= 240 and rows[-1][0] == today:
            data[s] = rows
    liq = sorted(data, key=lambda s: -sum(r[2] for r in data[s][-60:]) / 60)[:TOP_LIQ]
    score = []
    for s in liq:
        c = [r[1] for r in data[s]]
        if len(c) >= 253:
            score.append((c[-22] / c[-253] - 1, s))
    score.sort(reverse=True)
    picks = [s for _, s in score[:HOLD_N]]
    old = {h["sym"] for h in book["holdings"]}
    turnover = len(set(picks) - old) / HOLD_N
    book["equity_eur"] *= (1 - 2 * FEE * turnover)
    book["holdings"] = [{"sym": s, "entry": data[s][-1][1], "weight": 1 / HOLD_N} for s in picks]
    book["month"] = today.strftime("%Y-%m")
    book["history"].append({"date": today.isoformat(), "event": "rebalance", "picks": picks,
                            "momentum_pct": {s: round(100 * m, 0) for m, s in score[:HOLD_N]},
                            "equity_eur": round(book["equity_eur"], 2)})
    print("PAPER MOMENTUM REBALANCE", today, picks)


def mark(book):
    if not book["holdings"]:
        return None
    val = 0.0
    for h in book["holdings"]:
        rows = daily(h["sym"], "1mo")
        px = rows[-1][1] if rows else h["entry"]
        h["last"] = px
        val += h["weight"] * px / h["entry"]
    return book["equity_eur"] * val


def run():
    book = load()
    now = dt.datetime.now(NY)
    spy = daily("SPY", "5d")
    if not spy or spy[-1][0] != now.date():
        print("US market not open today"); return
    if book["month"] != now.strftime("%Y-%m"):
        if book["holdings"]:
            book["equity_eur"] = mark(book)          # close last month's book at today's close
            book["history"].append({"date": now.date().isoformat(), "event": "month end", "equity_eur": round(book["equity_eur"], 2)})
        rebalance(book, now.date())
    eq = mark(book)
    book["last_mark"] = {"date": now.date().isoformat(), "equity_eur": round(eq, 2) if eq else None}
    save(book)
    stats()


def stats():
    b = load()
    eq = (b.get("last_mark") or {}).get("equity_eur") or b["equity_eur"]
    out = {"started": b["started"], "holdings": [h["sym"] for h in b["holdings"]], "equity_eur_of_1000": eq,
           "return_pct": round(100 * (eq / CAPITAL - 1), 2), "per_eur_130": round(130 * (eq / CAPITAL - 1), 2)}
    print(json.dumps(out))
    return out


if __name__ == "__main__":
    run() if (len(sys.argv) > 1 and sys.argv[1] == "run") else stats()
