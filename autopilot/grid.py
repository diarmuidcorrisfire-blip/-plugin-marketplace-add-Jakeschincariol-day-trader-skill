#!/usr/bin/env python3
"""Search thousands of rule variants honestly: choose on older data, judge on unseen recent data.

Families (daily bars, 2 years, EU + US autopilot universes; fees: US 0.15% FX each way, EU none):
  MR  mean reversion: RSI(n) below a level, optional trend filter, exit on a short average or RSI,
      max holding days, optional stop-loss (intraday low touches it -> exit at the stop or the gap open)
  BO  breakout/momentum: close at an N-day closing high above a trend average, fixed hold or exit on a
      lower average, optional stop-loss
Every variant is run as a portfolio: EUR SIZE per position, at most SLOTS positions at once, signals
taken in date order (lowest RSI / strongest breakout first) when a slot is free.

Split: TRAIN = everything before the last 120 trading days; TEST = the last 120 trading days
(about 5.5 months, never used for choosing). In TEST we report every rolling 31-calendar-day window.
  python3 autopilot/grid.py     -> trader/grid_results.json
"""
import datetime as dt
import itertools
import json
import os
import statistics as stt
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import research as R  # noqa: E402

ROOT = R.ROOT
TEST_DAYS = 120


def rsi_series(c, n):
    out = [None] * len(c)
    g = l = 0.0
    for i in range(1, len(c)):
        ch = c[i] - c[i - 1]
        up, dn = max(ch, 0.0), max(-ch, 0.0)
        if i <= n:
            g += up; l += dn
            if i == n:
                g /= n; l /= n
                out[i] = 100 - 100 / (1 + g / l) if l else 100.0
        else:
            g = (g * (n - 1) + up) / n; l = (l * (n - 1) + dn) / n
            out[i] = 100 - 100 / (1 + g / l) if l else 100.0
    return out


def sma_series(c, n):
    out = [None] * len(c)
    s = 0.0
    for i, x in enumerate(c):
        s += x
        if i >= n:
            s -= c[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def rolling_max_prev(c, n):
    out = [None] * len(c)
    for i in range(n, len(c)):
        out[i] = max(c[i - n:i])
    return out


def prep(ddata):
    P = {}
    for sym, rows in ddata.items():
        d = [r[0] for r in rows]; o = [r[1] for r in rows]; h = [r[2] for r in rows]
        lo = [r[3] for r in rows]; c = [r[4] for r in rows]
        P[sym] = {"d": d, "o": o, "h": h, "l": lo, "c": c,
                  "rsi": {n: rsi_series(c, n) for n in (2, 3, 4)},
                  "ma": {n: sma_series(c, n) for n in (3, 5, 10, 20, 50, 100, 200)},
                  "hi": {n: rolling_max_prev(c, n) for n in (10, 20, 55)}}
    return P


def signals(P, v):
    """All (date_index_global_key, sym, i, strength) entry signals for variant v."""
    out = []
    for sym, p in P.items():
        c = p["c"]
        for i in range(201, len(c) - 1):
            if v["fam"] == "MR":
                r = p["rsi"][v["n"]][i]
                if r is None or r >= v["lvl"]:
                    continue
                if v["trend"] and (p["ma"][v["trend"]][i] is None or c[i] <= p["ma"][v["trend"]][i]):
                    continue
                out.append((p["d"][i], r, sym, i))
            else:
                hi = p["hi"][v["look"]][i]
                if hi is None or c[i] <= hi:
                    continue
                tm = p["ma"][v["trend"]][i]
                if tm is None or c[i] <= tm:
                    continue
                out.append((p["d"][i], -(c[i] / hi - 1), sym, i))
    out.sort()
    return out


def exit_for(p, i, v):
    """Walk from day i+1; returns (exit_index, exit_price)."""
    c, lo, o = p["c"], p["l"], p["o"]
    entry = c[i]
    stop = entry * (1 - v["stop"] / 100) if v["stop"] else None
    last = min(i + v["hold"], len(c) - 1)
    for k in range(i + 1, last + 1):
        if stop and lo[k] <= stop:
            return k, min(stop, o[k])
        if v["fam"] == "MR":
            if v["exit"].startswith("ma"):
                m = p["ma"][int(v["exit"][2:])][k]
                if m and c[k] > m:
                    return k, c[k]
            else:
                r = p["rsi"][v["n"]][k]
                if r and r > int(v["exit"][3:]):
                    return k, c[k]
        else:
            if v["exit"] != "time":
                m = p["ma"][int(v["exit"][2:])][k]
                if m and c[k] < m:
                    return k, c[k]
    return last, c[last]


def portfolio(P, sigs, v, fee, size, slots, start, end):
    """Trades taken by a SLOTS-position book between dates [start, end)."""
    busy = []          # list of exit dates
    held = set()
    trades = []
    for d, strength, sym, i in sigs:
        if d < start or d >= end:
            continue
        busy = [x for x in busy if x[0] >= d]
        held = {x[1] for x in busy}
        if len(busy) >= slots or sym in held:
            continue
        p = P[sym]
        k, px = exit_for(p, i, v)
        ret = px / p["c"][i] - 1 - 2 * fee
        busy.append((p["d"][k], sym))
        trades.append({"d": d, "exit_d": p["d"][k], "sym": sym, "ret": ret, "pnl": size * ret})
    return trades


def window_stats(trades, start, end):
    """Rolling 31-day windows (step 7 days) by exit date inside [start, end)."""
    res = []
    w0 = start
    while w0 + dt.timedelta(days=31) <= end:
        w1 = w0 + dt.timedelta(days=31)
        res.append(sum(t["pnl"] for t in trades if w0 <= t["exit_d"] < w1))
        w0 += dt.timedelta(days=7)
    return res


def variants():
    for n, lvl, trend, ex, hold, stop in itertools.product(
            (2, 3, 4), (5, 10, 15, 20, 25), (None, 50, 100, 200),
            ("ma3", "ma5", "ma10", "rsi50", "rsi70"), (3, 5, 10, 15), (0, 5, 8, 12)):
        yield {"fam": "MR", "n": n, "lvl": lvl, "trend": trend, "exit": ex, "hold": hold, "stop": stop}
    for look, trend, ex, hold, stop in itertools.product(
            (10, 20, 55), (50, 200), ("time", "ma10", "ma20"), (3, 5, 10, 20), (0, 5, 8, 12)):
        yield {"fam": "BO", "look": look, "trend": trend, "exit": ex, "hold": hold, "stop": stop}


def label(v):
    if v["fam"] == "MR":
        return (f"Dip-buy: RSI({v['n']})<{v['lvl']}" + (f", above {v['trend']}-day avg" if v["trend"] else "") +
                f", exit {v['exit']}, max {v['hold']}d" + (f", stop {v['stop']}%" if v["stop"] else ", no stop"))
    return (f"Breakout: {v['look']}-day high above {v['trend']}-day avg, exit {v['exit']}, max {v['hold']}d" +
            (f", stop {v['stop']}%" if v["stop"] else ", no stop"))


def main():
    out = {"generated": dt.datetime.now().isoformat(timespec="minutes"), "test_days": TEST_DAYS, "markets": {}}
    for name in ("EU", "US"):
        fee = R.MK[name]["fee"]
        P = prep(R.daily(name))
        all_dates = sorted({d for p in P.values() for d in p["d"]})
        split = all_dates[-TEST_DAYS]
        first = all_dates[201]
        end = all_dates[-1] + dt.timedelta(days=1)
        rows = []
        vs = list(variants())
        print(name, "variants", len(vs), "train", first, "->", split, "test", split, "->", all_dates[-1], flush=True)
        for idx, v in enumerate(vs):
            sigs = signals(P, v)
            for slots in (1, 2, 3):
                tr_train = portfolio(P, sigs, v, fee, 100.0, slots, first, split)
                if len(tr_train) < 15:
                    continue
                rets = [t["ret"] for t in tr_train]
                m = stt.mean(rets)
                sd = stt.pstdev(rets) or 1e-9
                wins_tr = window_stats(tr_train, first, split)
                rows.append({"v": v, "slots": slots, "train_n": len(rets), "train_avg_pct": 100 * m,
                             "train_t": m / sd * len(rets) ** 0.5,
                             "train_win31": sum(w > 0 for w in wins_tr) / max(len(wins_tr), 1),
                             "train_total_per100": sum(t["pnl"] for t in tr_train)})
            if idx % 500 == 0:
                print(name, idx, "/", len(vs), flush=True)
        # choose on TRAIN only: robust = positive t, most 31-day windows positive
        rows.sort(key=lambda r: (r["train_win31"] >= 0.7, r["train_t"]), reverse=True)
        picks = rows[:25]
        tested = []
        for r in picks:
            v, slots = r["v"], r["slots"]
            sigs = signals(P, v)
            tr = portfolio(P, sigs, v, fee, 100.0, slots, split, end)
            wins = window_stats(tr, split, end)
            tested.append({"rule": label(v), "slots": slots, **{k: round(r[k], 3) for k in r if k.startswith("train")},
                           "test_trades": len(tr), "test_total_per100": round(sum(t["pnl"] for t in tr), 2),
                           "test_avg_pct": round(100 * stt.mean([t["ret"] for t in tr]), 3) if tr else None,
                           "test_31d_windows": len(wins), "test_31d_profitable": sum(w > 0 for w in wins),
                           "test_31d_worst_per100": round(min(wins), 2) if wins else None,
                           "test_31d_median_per100": round(stt.median(wins), 2) if wins else None,
                           "v": v})
        out["markets"][name] = {"variants_tested": len(vs) * 3, "kept_with_15plus_trades": len(rows),
                                "train_period": [str(first), str(split)], "test_period": [str(split), str(all_dates[-1])],
                                "top25_chosen_on_train_then_tested": tested}
        for t in tested[:10]:
            print(name, t["rule"], "slots", t["slots"], "| train t", t["train_t"], "win31", t["train_win31"],
                  "| TEST total/EUR100", t["test_total_per100"], "31d windows +", t["test_31d_profitable"], "/", t["test_31d_windows"],
                  "worst", t["test_31d_worst_per100"], flush=True)
    with open(os.path.join(ROOT, "trader", "grid_results.json"), "w") as f:
        json.dump(out, f, indent=1, default=str)
    print("done")


if __name__ == "__main__":
    main()
