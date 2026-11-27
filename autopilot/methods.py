#!/usr/bin/env python3
"""Round 2 of the strategy search (pretend money): methods with long published track records,
tested without hindsight on 10 years of daily data (cache from bigsearch.py).

  * point-in-time universe: each day only the TOP_N most traded stocks of that day (60-day $ volume)
  * DIP   dip-buy RSI(3)<15 above 200-day avg, exit on 3-day avg or 5 days, optional market filter
  * MOM   monthly momentum: hold the 10 strongest 12-1 month performers, rebalance monthly
  * TREND index above its 200-day average -> hold the index, else cash
  * HOLD  buy and hold the index
Costs: US 0.15% FX each way on every buy and sell (Trading 212 EUR account); EU none.
Reports for 2017-2026: yearly returns, share of 31-day windows that were profitable, worst 31 days,
and the same split train (2017-2021) / test (2022-2026) for the few choices made.
  python3 autopilot/methods.py      -> trader/methods_results.json
"""
import datetime as dt
import gzip
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bigsearch as B  # noqa: E402

ROOT = B.ROOT
INDEX = {"US": "^GSPC", "EU": "^STOXX50E"}


def panel(name, min_dollar_vol=0):
    data = B.load_market(name, min_dollar_vol)
    dates = sorted({d for p in data.values() for d in p["d"]})
    pos = {d: i for i, d in enumerate(dates)}
    syms = list(data)
    T, N = len(dates), len(syms)
    C = np.full((N, T), np.nan); DV = np.zeros((N, T))
    for k, s in enumerate(syms):
        p = data[s]
        ix = np.array([pos[d] for d in p["d"]])
        C[k, ix] = p["c"]
        raw = p["c"]  # adjusted close is fine for ranking $ volume roughly
    # $ volume from raw rows (re-read compactly)
    for k, s in enumerate(syms):
        with gzip.open(os.path.join(B.CACHE, s + ".json.gz"), "rt") as f:
            rows = json.load(f)
        a = np.array(rows, float)
        dv = np.convolve(a[:, 6] * a[:, 5], np.ones(60) / 60, mode="full")[:len(a)]
        ix = np.array([pos[dt.datetime.utcfromtimestamp(t).date()] for t in a[:, 0]])
        DV[k, ix] = dv
    return data, syms, dates, C, DV


def index_series(name, dates):
    rows = B.fetch_daily(INDEX[name])
    m = {dt.datetime.utcfromtimestamp(r[0]).date(): r[4] for r in rows}
    out = np.full(len(dates), np.nan)
    last = np.nan
    for i, d in enumerate(dates):
        last = m.get(d, last)
        out[i] = last
    return out


def windows31(daily_pnl, dates, start):
    """daily_pnl: array of pnl per date (per 100 of capital). Rolling 31-day sums, step 7 days."""
    res, i0 = [], next(i for i, d in enumerate(dates) if d >= start)
    cum = np.concatenate([[0], np.cumsum(np.nan_to_num(daily_pnl))])
    j = i0
    while True:
        d0 = dates[j]
        k = j
        while k < len(dates) and dates[k] < d0 + dt.timedelta(days=31):
            k += 1
        if k >= len(dates):
            break
        res.append(cum[k] - cum[j])
        j2 = j
        while j2 < len(dates) and dates[j2] < d0 + dt.timedelta(days=7):
            j2 += 1
        j = j2
    return np.array(res)


def summarize(label, daily_pnl, dates, start=dt.date(2017, 1, 1)):
    """daily_pnl in % of capital per day."""
    w = windows31(daily_pnl, dates, start)
    yrs = {}
    for y in range(start.year, dates[-1].year + 1):
        m = np.array([d.year == y for d in dates])
        yrs[y] = round(float(np.nansum(daily_pnl[m])), 1)
    tr = np.array([start <= d < B.SPLIT for d in dates]); te = np.array([d >= B.SPLIT for d in dates])
    out = {"method": label, "pct_per_year_avg": round(float(np.mean(list(yrs.values()))), 1), "by_year_pct": yrs,
           "train_2017_21_pct": round(float(np.nansum(daily_pnl[tr])), 1), "test_2022_26_pct": round(float(np.nansum(daily_pnl[te])), 1),
           "windows31": len(w), "profitable31_share": round(float((w > 0).mean()), 3),
           "worst31_pct": round(float(w.min()), 1), "median31_pct": round(float(np.median(w)), 2)}
    print(json.dumps(out), flush=True)
    return out


def hold_and_trend(name, dates, fee):
    ix = index_series(name, dates)
    r = np.concatenate([[0], ix[1:] / ix[:-1] - 1]) * 100
    hold = summarize(f"{name} buy & hold index {INDEX[name]}", r, dates)
    ma = B.sma(np.nan_to_num(ix, nan=np.nanmean(ix)), 200)
    inm = np.zeros(len(dates), bool)
    for i in range(1, len(dates)):
        inm[i] = ix[i - 1] > ma[i - 1]           # decided at yesterday's close
    switches = np.abs(np.diff(inm.astype(int), prepend=0))
    tr = np.where(inm, r, 0) - switches * fee * 100
    trend = summarize(f"{name} index above 200-day avg else cash", tr, dates)
    regime = np.array([ix[i] > ma[i] for i in range(len(dates))])
    return hold, trend, regime


def dipbuy(name, data, syms, dates, DV, fee, top_n, regime=None, slots=5):
    pos = {d: i for i, d in enumerate(dates)}
    # rank by $ volume each day
    rank_ok = np.zeros_like(DV, bool)
    for t in range(DV.shape[1]):
        col = DV[:, t]
        if np.count_nonzero(col) < top_n:
            rank_ok[:, t] = col > 0
        else:
            thr = np.partition(col, -top_n)[-top_n]
            rank_ok[:, t] = col >= thr
    v = dict(fam="MR", n=3, lvl=15, trend=200, exit="ma3", hold=5, stop=0)
    ev = []
    for k, s in enumerate(syms):
        p = data[s]
        p["liquid"] = np.array([rank_ok[k, pos[d]] and (regime is None or regime[pos[d]]) for d in p["d"]])
        ei, xi, r = B.trades_for(p, v, fee)
        if len(r):
            ev.extend(zip(p["d"][ei], p["d"][xi], r, p["rsi"][3][ei]))
    ev.sort(key=lambda e: (e[0], e[3]))
    # slot book: each slot = 1/slots of capital; pnl booked on exit date, in % of capital
    daily = np.zeros(len(dates)); busy = []
    n = 0
    for ed, xd, r, _ in ev:
        busy = [b for b in busy if b >= ed]
        if len(busy) >= slots:
            continue
        busy.append(xd); n += 1
        daily[pos[xd]] += 100 * r / slots
    tag = f"{name} dip-buy, top {top_n} most-traded that day" + (", only when index above 200d" if regime is not None else "")
    out = summarize(tag + f", {slots} slots", daily, dates)
    out["trades"] = n
    return out


def momentum(name, syms, dates, C, DV, fee, top_n=200, hold_n=10, regime=None):
    months = [i for i in range(1, len(dates)) if dates[i].month != dates[i - 1].month]
    daily = np.zeros(len(dates))
    prev = set()
    for a, b in zip(months, months[1:]):
        if a < 260:
            continue
        col = DV[:, a - 1]
        liquid = np.nonzero(col >= np.partition(col, -top_n)[-top_n])[0] if np.count_nonzero(col) >= top_n else np.nonzero(col)[0]
        mom = C[liquid, a - 22] / C[liquid, a - 253] - 1
        ok = ~np.isnan(mom) & ~np.isnan(C[liquid, a - 1]) & ~np.isnan(C[liquid, b - 1])
        if regime is not None and not regime[a - 1]:
            prev = set(); continue
        cand = liquid[ok][np.argsort(-mom[ok])][:hold_n]
        if not len(cand):
            continue
        ret = C[cand, b - 1] / C[cand, a - 1] - 1
        turnover = len(set(cand.tolist()) - prev) / hold_n
        prev = set(cand.tolist())
        daily[b - 1] += 100 * (np.nanmean(ret) - 2 * fee * turnover)
    tag = f"{name} momentum: top {hold_n} of {top_n} most-traded, monthly" + (", only when index above 200d" if regime is not None else "")
    return summarize(tag, daily, dates)


def main():
    res = {"generated": dt.datetime.now().isoformat(timespec="minutes"), "results": []}
    for name in ("EU", "US"):
        fee = B.FEE[name]
        data, syms, dates, C, DV = panel(name)
        B.prep(data)
        print(name, "stocks", len(syms), "days", len(dates), flush=True)
        hold, trend, regime = hold_and_trend(name, dates, fee)
        res["results"] += [hold, trend]
        for top in (50, 100):
            res["results"].append(dipbuy(name, data, syms, dates, DV, fee, top))
            res["results"].append(dipbuy(name, data, syms, dates, DV, fee, top, regime))
        for top, n in ((100, 10), (200, 10), (200, 20)):
            res["results"].append(momentum(name, syms, dates, C, DV, fee, top, n))
            res["results"].append(momentum(name, syms, dates, C, DV, fee, top, n, regime))
        with open(os.path.join(ROOT, "trader", "methods_results.json"), "w") as f:
            json.dump(res, f, indent=1, default=str)
    print("done")


if __name__ == "__main__":
    main()
