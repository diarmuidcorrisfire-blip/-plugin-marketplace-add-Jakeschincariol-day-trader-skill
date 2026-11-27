#!/usr/bin/env python3
"""One REAL US trade for Fri Nov 27 2026 (Black Friday half day), approved by the user in chat.

Limits: EUR 130 max in the trade, EUR 2 max loss at the stop incl. FX fees, one entry, stop placed
right after the fill (or sell at once), flat by 12:50 ET (18:50 CET) before the 13:00 ET close.
Setup: 15-minute opening-range breakout, gap-ups >= 1% ranked first (day-after-Thanksgiving study),
entry only if the replay check (Time Machine method, 60 days of the stock's 5-minute bars,
until the flat time) is positive after the 0.3% round-trip fee. Never NVDA or anything held.
Uses autopilot.py's own scan / sizing / entry / exit code.

  python3 autopilot/today_us.py [--dry-run]
"""
import datetime as dt
import json
import os
import sys
import time
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autopilot as ap  # noqa: E402

NY = ZoneInfo("America/New_York")
STATE = os.path.join(ap.ROOT, "trader", "today_us_state.json")
EXTRA = "AMD MU DELL MRVL ARM COIN MSTR HOOD KSS BBY AMZN HD LOW WMT TGT M SHOP PLTR TSLA INTC".split()

base = ap.load(ap.CFG, {})
uni = ap.load(os.path.join(ap.ROOT, "autopilot", "us_universe.json"), {})
for s in EXTRA:
    uni.setdefault(s, f"{s}_US_EQ")
uni_path = os.path.join(ap.ROOT, "trader", "today_us_universe.json")
json.dump(uni, open(uni_path, "w"), indent=0)

MK = {"name": "US", "universe": os.path.relpath(uni_path, ap.ROOT), "tz": "America/New_York", "currency": "USD",
      "or": ["09:30", "09:45"], "entry": ["09:47", "11:30"], "flat_by": "12:50", "min_price": 5,
      "min_target_pct": 0.8, "fx_fee_pct": 0.15}
CFG = {**base, "enabled": True, "budget_eur": 130, "risk_per_trade_eur": 2,
       "markets": {"US": MK}, "never_trade": base.get("never_trade", []) + ["NVDA"]}


def gapped_up(ysym):
    b = ap.chart(ysym)
    if not b or not b["prev"] or not b["rows"]:
        return None
    rows = [r for r in b["rows"] if r[0].astimezone(NY).date() == dt.datetime.now(NY).date()]
    return rows[0][1] / b["prev"] - 1 if rows else None


def main():
    state = ap.load(STATE, {"open": None, "trades": [], "day_entries": {}, "checked": []})
    while True:
        now = dt.datetime.now(ap.CET)
        now_l = now.astimezone(NY)
        try:
            pos = ap.positions()
            if state["open"]:
                ap.manage_open(state, CFG, now, pos)
                ap.save(STATE, state)
                if not state["open"]:
                    ap.log("TODAY DONE", trades=state["trades"][-1:])
                    break
                time.sleep(20)
                continue
            if state["trades"] or state["day_entries"].get(now.date().isoformat(), 0) >= 1:
                ap.log("TODAY DONE", note="already traded"); break
            if now_l.time() > ap.hm(MK["entry"][1]):
                ap.log("NO TRADE TODAY", reason="no setup passed the checks by 11:30 ET", checked=state["checked"][-10:])
                break
            if now_l.time() < ap.hm(MK["entry"][0]):
                time.sleep(30); continue
            fx = ap.eurusd() or 1.10
            picks = ap.find_setups(CFG, MK, now, set(pos))
            for p in picks:
                p["gap_pct"] = round(100 * (gapped_up(p["yahoo"]) or 0), 2)
            picks.sort(key=lambda p: (p["gap_pct"] < 1.0, -p["chg_pct"]))
            ap.log("candidates", top=[{k: p[k] for k in ("yahoo", "signal_close", "stop", "chg_pct", "gap_pct", "bar")} for p in picks[:5]])
            for setup in picks[:4]:
                if setup["ticker"] in state["checked"]:
                    continue
                state["checked"].append(setup["ticker"])
                if ap.enter(state, CFG, MK, now, setup, fx):
                    break
            ap.save(STATE, state)
        except Exception as e:
            ap.log("ERROR", error=str(e)[:300])
        time.sleep(60 if not state["open"] else 20)
    ap.save(STATE, state)
    print(json.dumps({"summary": "done", "state": state}, default=str))


if __name__ == "__main__":
    main()
