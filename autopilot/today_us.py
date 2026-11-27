#!/usr/bin/env python3
"""One REAL US trade a day (approved by the user in chat on Nov 27 2026: "you have to be able to make me
money as well"). Runs each US trading day from a scheduled task.

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

HALF = dt.datetime.now(NY).date().isoformat() in ap.NYSE_EARLY_CLOSE
MK = {"name": "US", "universe": os.path.relpath(uni_path, ap.ROOT), "tz": "America/New_York", "currency": "USD",
      "or": ["09:30", "09:45"], "entry": ["09:47", "11:30" if HALF else "14:00"], "flat_by": "12:50" if HALF else "15:40",
      "min_price": 5,
      "min_target_pct": 0.8, "fx_fee_pct": 0.15}
BUDGET = float(os.environ.get("TODAY_BUDGET_EUR", "130"))      # hard cap on money in a trade
MAX_TRADES = int(os.environ.get("TODAY_MAX_TRADES", "1"))
DAY_LOSS_CAP = float(os.environ.get("TODAY_LOSS_CAP_EUR", "2"))
CFG = {**base, "enabled": True, "budget_eur": BUDGET, "risk_per_trade_eur": 2,
       "markets": {"US": MK}, "never_trade": base.get("never_trade", []) + ["NVDA"]}


def gapped_up(ysym):
    b = ap.chart(ysym)
    if not b or not b["prev"] or not b["rows"]:
        return None
    rows = [r for r in b["rows"] if r[0].astimezone(NY).date() == dt.datetime.now(NY).date()]
    return rows[0][1] / b["prev"] - 1 if rows else None


def main():
    state = ap.load(STATE, {"open": None, "trades": [], "day_entries": {}, "checked": []})
    today = dt.datetime.now(NY).date().isoformat()
    if state.get("date") != today and not state.get("open"):
        hist = ap.load(STATE.replace(".json", "_history.json"), {"days": []})
        if state.get("date"):
            hist["days"].append(state); ap.save(STATE.replace(".json", "_history.json"), hist)
        state = {"date": today, "open": None, "trades": [], "day_entries": {}, "checked": []}
    while True:
        now = dt.datetime.now(ap.CET)
        now_l = now.astimezone(NY)
        try:
            pos = ap.positions()
            if state["open"]:
                ap.manage_open(state, CFG, now, pos)
                ap.save(STATE, state)
                if not state["open"]:
                    ap.log("TRADE RESULT", trade=state["trades"][-1:])
                time.sleep(20)
                continue
            day_pnl = sum(t.get("pnl_eur", 0) for t in state["trades"])
            if state["day_entries"].get(now.date().isoformat(), 0) >= MAX_TRADES or day_pnl <= -DAY_LOSS_CAP:
                ap.log("TODAY DONE", trades=len(state["trades"]), day_pnl_eur=round(day_pnl, 2)); break
            if now_l.time() > ap.hm(MK["entry"][1]):
                ap.log("NO TRADE TODAY", reason=f"no setup passed the checks by {MK['entry'][1]} ET", checked=state["checked"][-10:])
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
