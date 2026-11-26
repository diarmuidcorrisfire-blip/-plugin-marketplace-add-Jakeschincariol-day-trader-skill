#!/usr/bin/env python3
"""Paper trading for the RSI(2) uptrend swing (the one strategy that passed research in Europe).

Rule (daily bars, European universe, EUR, no FX fee):
  BUY at the close when the close is above its 200-day average and RSI(2) < 10.
  SELL at the close when the close is above its 5-day average, or after 10 trading days.
Pretend money: EUR 130 split into 2 slots of EUR 65; a signal is taken only if a slot is free,
strongest dip (lowest RSI) first. Never trades NVDA or anything listed in never_trade.

Research (Nov 26 2026, ~16 months of trades, 39 EU stocks): 323 signals, 71% winners,
+0.65% average per trade, positive in both halves; EUR 130 in 2 slots made about +EUR 58.
Not a guarantee: a bull-market sample, current large caps only, and no stop-loss
(worst single trade -14.7%).

  python3 autopilot/swing_paper.py run      # once a day after the European close (~17:40 CET)
  python3 autopilot/swing_paper.py stats
Book: trader/swing_paper_state.json
"""
import datetime as dt
import json
import os
import sys
import urllib.request
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOOK = os.path.join(ROOT, "trader", "swing_paper_state.json")
UNIVERSE = os.path.join(ROOT, "autopilot", "eu_universe.json")
CET = ZoneInfo("Europe/Berlin")
CAPITAL, SLOTS, RSI_LO, MAX_HOLD = 130.0, 2, 10, 10


def daily(sym):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=1y&interval=1d"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=25) as r:
            res = json.load(r)["chart"]["result"][0]
    except Exception:
        return None
    q = res["indicators"]["quote"][0]
    return [(dt.datetime.fromtimestamp(t, CET).date(), c) for t, c in zip(res["timestamp"], q["close"]) if c is not None]


def rsi2(closes):
    g = l = 0.0
    out = None
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        up, dn = max(ch, 0), max(-ch, 0)
        if i <= 2:
            g += up; l += dn
            if i == 2:
                g /= 2; l /= 2
        else:
            g = (g + up) / 2; l = (l + dn) / 2
        if i >= 2:
            out = 100 - 100 / (1 + g / l) if l else 100.0
    return out


def load():
    try:
        with open(BOOK) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"open": [], "closed": [], "started": dt.date.today().isoformat()}


def save(b):
    with open(BOOK + ".tmp", "w") as f:
        json.dump(b, f, indent=1)
    os.replace(BOOK + ".tmp", BOOK)


def run():
    book = load()
    now = dt.datetime.now(CET)
    if now.weekday() >= 5:
        print("weekend"); return
    uni = json.load(open(UNIVERSE))
    cfg = json.load(open(os.path.join(ROOT, "trader", "autopilot.json")))
    never = set(cfg.get("never_trade", []))
    data = {}
    for s in uni:
        rows = daily(s)
        if rows and len(rows) > 205:
            data[s] = rows
    today = max((r[-1][0] for r in data.values()), default=None)
    if today is None or today != now.date():
        print("no bars for today (holiday?)"); return
    # exits first
    still = []
    for p in book["open"]:
        rows = data.get(p["yahoo"])
        if not rows:
            still.append(p); continue
        c = [x[1] for x in rows]
        held = sum(1 for x in rows if x[0] > dt.date.fromisoformat(p["date"]))
        if c[-1] > sum(c[-5:]) / 5 or held >= MAX_HOLD:
            ret = c[-1] / p["entry"] - 1
            p.update({"exit": round(c[-1], 3), "exit_date": today.isoformat(), "days": held,
                      "ret_pct": round(100 * ret, 2), "pnl_eur": round(p["eur"] * ret, 2)})
            book["closed"].append(p)
            print(f"PAPER SWING SOLD {p['yahoo']} {p['ret_pct']:+.2f}% EUR {p['pnl_eur']:+.2f} after {held} days")
        else:
            still.append(p)
    book["open"] = still
    # entries
    sigs = []
    for s, rows in data.items():
        if s in never or uni[s] in never or any(p["yahoo"] == s for p in book["open"]):
            continue
        c = [x[1] for x in rows]
        r = rsi2(c[-60:])
        if r is not None and r < RSI_LO and c[-1] > sum(c[-200:]) / 200:
            sigs.append((r, s, c[-1]))
    sigs.sort()
    for r, s, px in sigs:
        if len(book["open"]) >= SLOTS:
            break
        book["open"].append({"yahoo": s, "ticker": uni[s], "date": today.isoformat(), "entry": round(px, 3),
                             "rsi2": round(r, 1), "eur": CAPITAL / SLOTS})
        print(f"PAPER SWING BOUGHT {s} at {px:.2f} (RSI2 {r:.1f})")
    if not sigs:
        print("no swing signal today")
    save(book)
    stats()


def stats():
    b = load()
    cl = b["closed"]
    tot = sum(x["pnl_eur"] for x in cl)
    out = {"started": b.get("started"), "open": [{k: p[k] for k in ("yahoo", "date", "entry", "rsi2")} for p in b["open"]],
           "closed_trades": len(cl), "wins": sum(1 for x in cl if x["pnl_eur"] > 0), "total_eur": round(tot, 2)}
    print(json.dumps(out))
    return out


if __name__ == "__main__":
    run() if (len(sys.argv) > 1 and sys.argv[1] == "run") else stats()
