#!/usr/bin/env python3
"""Run autopilot continuously for one afternoon (the user asked for a few trades today).

Uses autopilot.py's own entry/exit code and limits, with today's overrides:
up to MAX_TRADES entries, entries until ENTRY_END, flat by FLAT_BY, and no new
entry once today's realised P&L reaches -DAY_LOSS_CAP. Polls every 20 seconds.
Usage: python3 autopilot/session.py MAX_TRADES ENTRY_END FLAT_BY DAY_LOSS_CAP
"""
import datetime as dt
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autopilot as ap  # noqa: E402

MAX_TRADES, ENTRY_END, FLAT_BY, DAY_CAP = int(sys.argv[1]), sys.argv[2], sys.argv[3], float(sys.argv[4])

cfg = ap.load(ap.CFG, {})
cfg = {**cfg, "enabled": True, "max_trades_per_day": MAX_TRADES,
       "entry_window_cet": ["09:45", ENTRY_END], "flat_by_cet": FLAT_BY}
end = dt.time.fromisoformat(FLAT_BY)
last_scan = None

while True:
    now = dt.datetime.now(ap.CET)
    state = ap.load(ap.STATE, {"open": None, "trades": [], "day_entries": {}})
    today = now.date().isoformat()
    try:
        pos = ap.positions()
        if state["open"]:
            ap.manage_open(state, cfg, now, pos)
        else:
            day_pnl = sum(t.get("pnl_eur", 0) for t in state["trades"] if t["date"] == today)
            entries = state["day_entries"].get(today, 0)
            if now.time() >= end or (entries >= MAX_TRADES) or day_pnl <= -DAY_CAP \
                    or now.time() > dt.time.fromisoformat(ENTRY_END):
                ap.save(ap.STATE, state)
                ap.log("SESSION DONE", trades_today=entries, day_pnl_eur=round(day_pnl, 2))
                break
            # scan at most every 5 minutes (a new 5-minute bar)
            if last_scan is None or (now - last_scan).total_seconds() >= 300:
                last_scan = now
                picks = ap.find_setup(cfg, now, set(pos))
                traded = {t["ticker"] for t in state["trades"] if t["date"] == today}
                picks = [p for p in picks if p["ticker"] not in traded]
                # prefer the strongest movers late in the day, then traded value
                picks.sort(key=lambda p: (-p["chg_pct"], -p["traded_value"]))
                if picks:
                    ap.log("setups", top=[{k: p[k] for k in ("yahoo", "signal_close", "stop", "chg_pct", "bar")}
                                          for p in picks[:3]])
                    ap.enter(state, cfg, now, picks[0])
                else:
                    ap.log("no setup this bar")
        ap.save(ap.STATE, state)
    except Exception as e:
        ap.log("ERROR", error=str(e)[:300])
    time.sleep(20)

state = ap.load(ap.STATE, {"open": None, "trades": [], "day_entries": {}})
print(json.dumps({"summary": "session done", "trades": [t for t in state["trades"] if t["date"] == today]}))
