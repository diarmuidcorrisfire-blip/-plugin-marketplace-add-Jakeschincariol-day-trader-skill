#!/usr/bin/env python3
"""Backtest autopilot's opening-range breakout on the last ~60 days of Yahoo 5-minute bars.

Same rules as autopilot.py (EU and US universes, 1% chase limit, up on the day, stop at the
range midpoint, risk 0.3-2.5% of price, sizing to 0.75 x EUR 2 and the EUR 130 budget, US FX fee
0.15% each way). Compares variants: target in R, opening-range length, entry deadline, stop rule.
One trade a day per market, the strongest mover first, like the live code.
Usage: python3 autopilot/backtest.py [--out trader/backtest.json]
"""
import datetime as dt
import json
import math
import os
import sys
import time
import urllib.request
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = {}


def bars60(sym):
    if sym in CACHE:
        return CACHE[sym]
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=60d&interval=5m"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=25) as r:
            res = json.load(r)["chart"]["result"][0]
    except Exception:
        CACHE[sym] = None
        return None
    q = res["indicators"]["quote"][0]
    rows = [(t, o, h, l, c, v or 0) for t, o, h, l, c, v in zip(res["timestamp"], q["open"], q["high"], q["low"], q["close"], q["volume"])
            if None not in (o, h, l, c)]
    CACHE[sym] = rows
    time.sleep(0.25)
    return rows


MARKETS = {
    "EU": {"universe": "autopilot/eu_universe.json", "tz": "Europe/Berlin", "open": "09:00", "flat": "17:05",
           "fee": 0.0, "fx": 1.0, "min_tgt": 0.0},
    "US": {"universe": "autopilot/us_universe.json", "tz": "America/New_York", "open": "09:30", "flat": "15:10",
           "fee": 0.0015, "fx": 1.10, "min_tgt": 1.5},
}


def t(s):
    return dt.time.fromisoformat(s)


def by_day(rows, tz):
    days = {}
    for r in rows:
        lt = dt.datetime.fromtimestamp(r[0], tz)
        days.setdefault(lt.date(), []).append((lt,) + r[1:])
    return days


def simulate(mk, data, or_start, or_end, entry_end, target_r, stop_rule, risk_eur=2.0, budget_eur=130.0):
    """data: {sym: {date: [bars]}}. Returns list of trade dicts (one per day max)."""
    tz = ZoneInfo(mk["tz"])
    dates = sorted({d for days in data.values() for d in days})
    trades = []
    for i, d in enumerate(dates[1:], 1):
        cands = []
        for sym, days in data.items():
            v = days.get(d)
            prev_days = [x for x in days if x < d]
            if not v or not prev_days or len(v) < 60:
                continue
            prev = days[max(prev_days)][-1][4]
            orng = [b for b in v if or_start <= b[0].time() < or_end]
            if len(orng) < 3:
                continue
            hi, lo = max(b[2] for b in orng), min(b[3] for b in orng)
            for j, b in enumerate(v):
                if b[0].time() < or_end:
                    continue
                if b[0].time() > entry_end:
                    break
                c = b[4]
                if c > hi:
                    if c <= hi * 1.01 and c > prev and c >= 5:
                        stop = math.floor(((hi + lo) / 2 if stop_rule == "mid" else lo) * 100) / 100
                        if 0.003 * c <= c - stop <= 0.025 * c:
                            cands.append((b[0], 100 * (c / prev - 1), sym, j, c, stop))
                    break                      # only the first close above the range counts
        if not cands:
            continue
        cands.sort(key=lambda x: (x[0], -x[1]))   # earliest signal, then strongest
        when, chg, sym, j, c, stop = cands[0]
        v = data[sym][d]
        entry = c * 1.0002                     # small slippage
        r = entry - stop
        tgt = entry + target_r * r
        if 100 * (tgt / entry - 1) < mk["min_tgt"]:
            continue
        fee, fx = mk["fee"], mk["fx"]
        qty = min(0.75 * risk_eur * fx / (r + fee * (entry + stop)), 0.95 * budget_eur * fx / entry)
        exit_p, why = None, None
        for b in v[j + 1:]:
            if b[3] <= stop:
                exit_p, why = min(stop, b[1]), "stop"; break   # gap through the stop fills at the open
            if b[2] >= tgt:
                exit_p, why = tgt, "target"; break
            if b[0].time() >= t(mk["flat"]):
                exit_p, why = b[4], "close"; break
        if exit_p is None:
            exit_p, why = v[-1][4], "close"
        pnl = ((exit_p - entry) * qty - fee * (entry + exit_p) * qty) / fx
        trades.append({"date": d.isoformat(), "sym": sym, "entry": round(entry, 3), "stop": stop, "exit": round(exit_p, 3),
                       "why": why, "r": round((exit_p - entry) / r, 2), "pnl_eur": round(pnl, 3), "at": when.strftime("%H:%M")})
    return trades


def summary(trades):
    if not trades:
        return {"trades": 0}
    pnl = [x["pnl_eur"] for x in trades]
    wins = [p for p in pnl if p > 0]
    eq = peak = dd = 0.0
    for p in pnl:
        eq += p; peak = max(peak, eq); dd = min(dd, eq - peak)
    return {"trades": len(pnl), "win_rate": round(len(wins) / len(pnl), 3), "total_eur": round(sum(pnl), 2),
            "avg_eur": round(sum(pnl) / len(pnl), 3), "avg_r": round(sum(x["r"] for x in trades) / len(trades), 3),
            "worst_drawdown_eur": round(dd, 2),
            "exits": {k: sum(1 for x in trades if x["why"] == k) for k in ("target", "stop", "close")}}


def main():
    out = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else os.path.join(ROOT, "trader", "backtest.json")
    results = {"generated": dt.datetime.now(ZoneInfo("Europe/Berlin")).isoformat(timespec="minutes"), "markets": {}}
    for name, mk in MARKETS.items():
        tz = ZoneInfo(mk["tz"])
        uni = json.load(open(os.path.join(ROOT, mk["universe"])))
        data = {}
        for ysym in uni:
            rows = bars60(ysym)
            if rows:
                data[ysym] = by_day(rows, tz)
        n_days = len({d for v in data.values() for d in v})
        o = dt.datetime.combine(dt.date.today(), t(mk["open"]))
        variants = []
        for or_min in (15, 30):
            or_start = (o + dt.timedelta(minutes=5 if name == "EU" else 0)).time()
            or_end = (o + dt.timedelta(minutes=or_min)).time()
            for entry_end_h in (1.5, 3, 6):
                entry_end = (o + dt.timedelta(hours=entry_end_h)).time()
                for tr in (1.0, 1.5, 2.0, 3.0):
                    for stop_rule in ("mid", "low"):
                        tr_list = simulate(mk, data, or_start, or_end, entry_end, tr, stop_rule)
                        s = summary(tr_list)
                        variants.append({"or_minutes": or_min, "entry_until": entry_end.strftime("%H:%M"),
                                         "target_r": tr, "stop": stop_rule, **s, "_trades": tr_list})
        variants.sort(key=lambda v: -(v.get("total_eur") or -1e9))
        live = next((v for v in variants if v["or_minutes"] == (30 if name == "EU" else 15) and v["stop"] == "mid"
                     and v["target_r"] == 2.0 and v["entry_until"] == ("15:30" if name == "EU" else "15:30")), None)
        results["markets"][name] = {
            "symbols": len(data), "days": n_days,
            "best": [{k: v[k] for k in v if k != "_trades"} for v in variants[:5]],
            "worst": [{k: v[k] for k in v if k != "_trades"} for v in variants[-3:]],
            "current_rules": live and {k: live[k] for k in live if k != "_trades"},
            "best_trades": variants[0]["_trades"][-15:],
        }
        print(name, json.dumps(results["markets"][name]["best"][:3]), flush=True)
    with open(out, "w") as f:
        json.dump(results, f, indent=1)
    print("wrote", out)


if __name__ == "__main__":
    main()
