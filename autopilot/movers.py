#!/usr/bin/env python3
"""Open movers: the US stocks moving hardest on volume in the first minutes, with a ready plan.

For each liquid US stock (autopilot universe + research names): move since the open, volume of the
first bars versus its usual first-bars volume (last 20 sessions), and a plan sized for the user's own
trades: entry = last price, stop = low of the session so far (min 0.5% / max 3% below), target 2R,
loss at the stop and gain at the target for a EUR 1,000 position after Trading 212's 0.15% FX fee
each way, and the replay odds (Time Machine method) until 15:45 ET. Ideas only; never places orders.

  python3 autopilot/movers.py [--top 3] [--json]
"""
import datetime as dt
import json
import math
import os
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autopilot as ap  # noqa: E402

NY = ZoneInfo("America/New_York")
POSITION_EUR, FEE = 1000.0, 0.0015
EXTRA = "AMD MU DELL MRVL ARM COIN MSTR HOOD KSS BBY AMZN HD LOW WMT TGT SHOP PLTR TSLA INTC SMCI".split()


def main():
    top = int(sys.argv[sys.argv.index("--top") + 1]) if "--top" in sys.argv else 3
    uni = list(ap.load(os.path.join(ap.ROOT, "autopilot", "us_universe.json"), {})) + EXTRA
    uni = [s for s in dict.fromkeys(uni) if s != "NVDA"]
    now = dt.datetime.now(NY)
    fx = ap.eurusd() or 1.10
    rows = []
    for s in uni:
        b = ap.chart(s, "1mo")
        if not b or not b["rows"]:
            continue
        days = {}
        for r in b["rows"]:
            lt = r[0].astimezone(NY)
            if dt.time(9, 30) <= lt.time() < dt.time(16, 0):
                days.setdefault(lt.date(), []).append((lt,) + r[1:])
        today = days.get(now.date())
        past = [v for d, v in days.items() if d < now.date()]
        if not today or len(past) < 5:
            continue
        n = len(today)
        prev_close = past[-1][-1][4]
        o, last = today[0][1], today[-1][4]
        vol = sum(x[5] for x in today)
        usual = sum(sum(x[5] for x in v[:n]) for v in past[-20:]) / len(past[-20:]) or 1
        move = 100 * (last / o - 1)
        gap = 100 * (o / prev_close - 1)
        lo = min(x[3] for x in today)
        stop = min(max(lo, last * 0.97), last * 0.995)
        stop = math.floor(stop * 100) / 100
        target = round(last + 2 * (last - stop), 2)
        rows.append({"sym": s, "price": round(last, 2), "move_since_open_pct": round(move, 2), "gap_pct": round(gap, 2),
                     "volume_x_usual": round(vol / usual, 1), "stop": stop, "target": target})
    # strongest up-moves on unusual volume first
    rows = [r for r in rows if r["move_since_open_pct"] > 0 and r["volume_x_usual"] >= 1.2]
    rows.sort(key=lambda r: -(r["move_since_open_pct"] * min(r["volume_x_usual"], 5)))
    mk = {"tz": "America/New_York", "flat_by": "15:45"}
    picks = []
    for r in rows[:top + 3]:
        rp = ap.replay(mk, r["sym"], now, r["price"], r["stop"], [r["target"]])
        if rp:
            x = rp["results"][0]
            qty = POSITION_EUR * fx / r["price"]
            r["odds_target_pct"] = round(100 * x["p_target"]); r["odds_stop_pct"] = round(100 * x["p_stop"])
            r["replay_avg_eur_1000"] = round((x["avg_per_share"] - 2 * FEE * r["price"]) * qty / fx, 2)
        qty = POSITION_EUR * fx / r["price"]
        r["loss_at_stop_eur_1000"] = round(((r["price"] - r["stop"]) * qty + 2 * FEE * POSITION_EUR * fx) / fx, 2)
        r["gain_at_target_eur_1000"] = round(((r["target"] - r["price"]) * qty - 2 * FEE * POSITION_EUR * fx) / fx, 2)
        per_share_loss_eur = ((r["price"] - r["stop"]) + 2 * FEE * r["price"]) / fx
        r["size_eur_for_10_loss"] = round(min(POSITION_EUR, 10.0 / per_share_loss_eur * r["price"] / fx), 0)
        picks.append(r)
        if len(picks) >= top:
            break
    out = {"time_ny": now.strftime("%H:%M"), "picks": picks}
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
