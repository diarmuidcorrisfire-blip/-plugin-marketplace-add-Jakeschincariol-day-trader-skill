#!/usr/bin/env python3
"""Big strategy search on pretend money: every liquid US stock + every Trading 212 European stock,
10 years of daily bars, thousands of rule variants, walk-forward (choose on 2016-2021, judge on 2022-2026).

  python3 autopilot/bigsearch.py universe   # build autopilot/universe_big_{us,eu}.json
  python3 autopilot/bigsearch.py download   # 10y daily bars into the cache
  python3 autopilot/bigsearch.py search     # grid search -> trader/bigsearch_results.json
  python3 autopilot/bigsearch.py thanksgiving  # day-after-Thanksgiving study (US)

Costs: US trades pay 0.15% FX each way (Trading 212, EUR account); EU none. Entries and exits at the
close (signal day close in, exit-day close out); a stop exits at the stop or the gapped open.
Survivorship: the universe is today's listings, so dead companies are missing (results flatter).
"""
import datetime as dt
import gzip
import itertools
import json
import os
import sys
import time
import urllib.request

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.environ.get("BIG_CACHE", "/tmp/bigcache")
os.makedirs(CACHE, exist_ok=True)
FEE = {"US": 0.0015, "EU": 0.0}
SPLIT = dt.date(2022, 1, 1)


# ------------------------------------------------------------------ data
def fetch_daily(sym):
    path = os.path.join(CACHE, sym.replace("/", "_") + ".json.gz")
    if os.path.exists(path):
        with gzip.open(path, "rt") as f:
            return json.load(f)
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=10y&interval=1d"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30) as r:
                res = json.load(r)["chart"]["result"][0]
            break
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(10 * (attempt + 1)); continue
            res = None; break
        except Exception:
            res = None; break
    else:
        res = None
    rows = []
    if res and res.get("timestamp"):
        q = res["indicators"]["quote"][0]
        adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose")
        for i, t in enumerate(res["timestamp"]):
            o, h, l, c, v = q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i]
            if None in (o, h, l, c) or c <= 0:
                continue
            k = (adj[i] / c) if adj and adj[i] else 1.0       # split/dividend adjust everything
            rows.append([t, o * k, h * k, l * k, c * k, v or 0, c])
    with gzip.open(path, "wt") as f:
        json.dump(rows, f)
    return rows


def build_universe():
    # US: the scanner's Nasdaq Trader list (common stocks, ADRs)
    us = [ln.split("|")[0] for ln in open(os.path.join(ROOT, "trader", "universe.txt")) if "|" in ln]
    us = [s.replace(".", "-") for s in us]
    # EU: Trading 212 instruments, home-market listings only, mapped to Yahoo suffixes
    inst = json.load(open(os.environ.get("T212_INSTRUMENTS", "/tmp/t212_instruments.json")))
    suffix = {"d_EQ": (".DE", "DE"), "p_EQ": (".PA", "FR"), "a_EQ": (".AS", "NL"), "e_EQ": (".MC", "ES"),
              "m_EQ": (".MI", "IT"), "b_EQ": (".BR", "BE")}
    eu = {}
    for x in inst:
        if x.get("type") != "STOCK" or x.get("currencyCode") != "EUR":
            continue
        for suf, (ysuf, cc) in suffix.items():
            if x["ticker"].endswith(suf) and x.get("isin", "").startswith(cc):
                eu[x["shortName"].replace(".", "-") + ysuf] = x["ticker"]
    json.dump(us, open(os.path.join(ROOT, "autopilot", "universe_big_us.json"), "w"))
    json.dump(eu, open(os.path.join(ROOT, "autopilot", "universe_big_eu.json"), "w"), indent=0)
    print("US", len(us), "EU", len(eu))


def download():
    for name in ("us", "eu"):
        u = json.load(open(os.path.join(ROOT, "autopilot", f"universe_big_{name}.json")))
        syms = list(u) if isinstance(u, dict) else u
        t0 = time.time()
        for i, s in enumerate(syms):
            fetch_daily(s)
            if not os.path.exists(os.path.join(CACHE, s + ".json.gz.done")):
                time.sleep(0.15)
            if i % 250 == 0:
                print(name, i, "/", len(syms), round(time.time() - t0), "s", flush=True)


def load_market(name, min_dollar_vol):
    u = json.load(open(os.path.join(ROOT, "autopilot", f"universe_big_{name.lower()}.json")))
    syms = list(u) if isinstance(u, dict) else u
    out = {}
    for s in syms:
        path = os.path.join(CACHE, s + ".json.gz")
        if not os.path.exists(path):
            continue
        with gzip.open(path, "rt") as f:
            rows = json.load(f)
        if len(rows) < 400:
            continue
        a = np.array(rows, dtype=float)
        raw_c, vol = a[:, 6], a[:, 5]
        dv = np.convolve(raw_c * vol, np.ones(60) / 60, mode="full")[:len(a)]   # trailing 60-day $ volume
        dates = np.array([dt.datetime.utcfromtimestamp(t).date() for t in a[:, 0]])
        out[s] = {"d": dates, "o": a[:, 1], "h": a[:, 2], "l": a[:, 3], "c": a[:, 4],
                  "liquid": (dv >= min_dollar_vol) & (raw_c >= 5)}
    return out


# ------------------------------------------------------------------ indicators (numpy)
def sma(x, n):
    c = np.cumsum(np.insert(x, 0, 0.0))
    out = np.full(len(x), np.nan)
    out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def rsi(x, n):
    d = np.diff(x, prepend=x[0])
    up, dn = np.clip(d, 0, None), np.clip(-d, 0, None)
    out = np.full(len(x), np.nan)
    if len(x) <= n:
        return out
    g, l = up[1:n + 1].mean(), dn[1:n + 1].mean()
    for i in range(n, len(x)):
        if i > n:
            g = (g * (n - 1) + up[i]) / n; l = (l * (n - 1) + dn[i]) / n
        out[i] = 100 - 100 / (1 + g / l) if l > 0 else 100.0
    return out


def prep(data):
    for p in data.values():
        c = p["c"]
        p["rsi"] = {n: rsi(c, n) for n in (2, 3, 4)}
        p["ma"] = {n: sma(c, n) for n in (3, 5, 10, 20, 50, 100, 200)}
        p["hi"] = {n: np.concatenate([np.full(n, np.nan), np.array([c[i - n:i].max() for i in range(n, len(c))])])
                   for n in (20, 55)}
        p["ret1"] = np.concatenate([[np.nan], c[1:] / c[:-1] - 1])


# ------------------------------------------------------------------ vectorised trade evaluation
def trades_for(p, v, fee):
    """All trades for one stock and one variant. Returns arrays (entry_idx, exit_idx, ret)."""
    c, o, lo = p["c"], p["o"], p["l"]
    n = len(c)
    if v["fam"] == "MR":
        sig = (p["rsi"][v["n"]] < v["lvl"]) & p["liquid"]
        if v["trend"]:
            sig &= c > p["ma"][v["trend"]]
    elif v["fam"] == "BO":
        sig = (c > p["hi"][v["look"]]) & (c > p["ma"][v["trend"]]) & p["liquid"]
    else:  # GAPDN: down >= x% today, above trend -> buy close
        sig = (p["ret1"] <= -v["drop"] / 100) & (c > p["ma"][v["trend"]]) & p["liquid"]
    sig[:200] = False
    sig[-1] = False
    idx = np.nonzero(sig)[0]
    if not len(idx):
        return idx, idx, np.array([])
    H = v["hold"]
    exit_i = np.minimum(idx + H, n - 1)
    exit_p = c[exit_i].copy()
    done = np.zeros(len(idx), bool)
    stop = c[idx] * (1 - v["stop"] / 100) if v["stop"] else None
    for k in range(1, H + 1):
        j = np.minimum(idx + k, n - 1)
        if stop is not None:
            hit = (~done) & (lo[j] <= stop)
            exit_p[hit] = np.minimum(stop[hit], o[j][hit]); exit_i[hit] = j[hit]; done |= hit
        if v["exit"].startswith("ma"):
            m = p["ma"][int(v["exit"][2:])][j]
            hit = (~done) & ((c[j] > m) if v["fam"] != "BO" else (c[j] < m))
        elif v["exit"].startswith("rsi"):
            hit = (~done) & (p["rsi"][v["n"]][j] > int(v["exit"][3:]))
        else:
            hit = np.zeros(len(idx), bool)
        exit_p[hit] = c[j][hit]; exit_i[hit] = j[hit]; done |= hit
    ret = exit_p / c[idx] - 1 - 2 * fee
    # one position per stock at a time: drop entries while a previous trade is open
    keep, busy_until = [], -1
    for a, b in zip(idx, exit_i):
        if a > busy_until:
            keep.append(True); busy_until = b
        else:
            keep.append(False)
    keep = np.array(keep)
    return idx[keep], exit_i[keep], ret[keep]


def variants():
    for n, lvl, trend, ex, hold, stop in itertools.product(
            (2, 3, 4), (5, 10, 15, 20), (None, 50, 100, 200), ("ma3", "ma5", "ma10", "rsi50", "rsi70"),
            (3, 5, 10), (0, 10, 20)):
        yield {"fam": "MR", "n": n, "lvl": lvl, "trend": trend, "exit": ex, "hold": hold, "stop": stop}
    for look, trend, ex, hold, stop in itertools.product((20, 55), (50, 200), ("time", "ma10", "ma20"),
                                                         (5, 10, 20), (0, 8, 15)):
        yield {"fam": "BO", "look": look, "trend": trend, "exit": ex, "hold": hold, "stop": stop}
    for drop, trend, ex, hold, stop in itertools.product((3, 5, 8), (50, 200), ("ma3", "ma5", "time"),
                                                         (1, 3, 5), (0, 10)):
        yield {"fam": "GAPDN", "drop": drop, "trend": trend, "exit": ex, "hold": hold, "stop": stop, "n": 3}


def label(v):
    s = {"0": "no stop"}.get(str(v["stop"]), f"stop {v['stop']}%")
    if v["fam"] == "MR":
        return f"Dip-buy RSI({v['n']})<{v['lvl']}" + (f" above {v['trend']}d avg" if v["trend"] else "") + f", exit {v['exit']}, max {v['hold']}d, {s}"
    if v["fam"] == "BO":
        return f"Breakout {v['look']}d high above {v['trend']}d avg, exit {v['exit']}, max {v['hold']}d, {s}"
    return f"Big down day (-{v['drop']}%) above {v['trend']}d avg, exit {v['exit']}, max {v['hold']}d, {s}"


def portfolio(events, slots, size, start, end):
    """events: list of (entry_date, exit_date, ret, strength). Slot-limited book. Returns per-trade pnl list."""
    out, busy = [], []
    for ed, xd, r, _ in events:
        if ed < start or ed >= end:
            continue
        busy = [b for b in busy if b >= ed]
        if len(busy) >= slots:
            continue
        busy.append(xd)
        out.append((ed, xd, size * r))
    return out


def windows31(trades, start, end):
    res, w0 = [], start
    while w0 + dt.timedelta(days=31) <= end:
        w1 = w0 + dt.timedelta(days=31)
        res.append(sum(p for _, x, p in trades if w0 <= x < w1))
        w0 += dt.timedelta(days=7)
    return res


def search():
    out = {"generated": dt.datetime.now().isoformat(timespec="minutes"), "split": str(SPLIT), "markets": {}}
    total_evals = 0
    for name, mdv in (("EU", 2e6), ("US", 20e6)):
        data = load_market(name, mdv)
        prep(data)
        fee = FEE[name]
        vs = list(variants())
        rows = []
        t0 = time.time()
        for k, v in enumerate(vs):
            ev = []
            for s, p in data.items():
                ei, xi, r = trades_for(p, v, fee)
                total_evals += 1
                if len(r):
                    strength = p["rsi"][v.get("n", 3)][ei]
                    ev.extend(zip(p["d"][ei], p["d"][xi], r, strength))
            if len(ev) < 50:
                continue
            ev.sort(key=lambda e: (e[0], e[3]))
            arr = np.array([e[2] for e in ev])
            dts = np.array([e[0] for e in ev])
            tr, te = arr[dts < SPLIT], arr[dts >= SPLIT]
            if len(tr) < 30 or len(te) < 30:
                continue
            rows.append({"v": v, "rule": label(v), "n_train": len(tr), "n_test": len(te),
                         "train_avg_pct": round(100 * tr.mean(), 3), "train_t": round(tr.mean() / (tr.std() + 1e-12) * len(tr) ** .5, 2),
                         "test_avg_pct": round(100 * te.mean(), 3), "test_t": round(te.mean() / (te.std() + 1e-12) * len(te) ** .5, 2),
                         "test_win": round(float((te > 0).mean()), 3), "_ev": ev})
            if k % 100 == 0:
                print(name, k, "/", len(vs), "stocks", len(data), round(time.time() - t0), "s", flush=True)
        # choose on TRAIN only
        rows.sort(key=lambda r: r["train_t"], reverse=True)
        top = rows[:30]
        for r in top:
            ev = r.pop("_ev")
            last = max(e[1] for e in ev) + dt.timedelta(days=1)
            best = None
            for slots in (3, 5, 10):
                pf = portfolio(ev, slots, 100.0, SPLIT, last)
                w = windows31(pf, SPLIT, last)
                cand = {"slots": slots, "test_trades": len(pf), "test_total_per100": round(sum(x[2] for x in pf), 1),
                        "windows": len(w), "profitable_windows": int(sum(x > 0 for x in w)),
                        "worst_31d_per100": round(min(w), 1) if w else None,
                        "median_31d_per100": round(float(np.median(w)), 2) if w else None,
                        "by_year": {y: round(sum(x[2] for x in pf if x[1].year == y), 1) for y in range(2022, 2027)}}
                if best is None or cand["test_total_per100"] > best["test_total_per100"]:
                    best = cand
            r["portfolio_test"] = best
        for r in rows[30:]:
            r.pop("_ev", None)
        out["markets"][name] = {"stocks": len(data), "variants": len(vs), "kept": len(rows), "top30_by_train": top}
        for r in top[:8]:
            print(name, r["rule"], "| train", r["train_avg_pct"], "t", r["train_t"], "| TEST", r["test_avg_pct"], "t", r["test_t"],
                  "| book", r["portfolio_test"], flush=True)
    out["stock_strategy_evaluations"] = total_evals
    for m in out["markets"].values():
        for r in m["top30_by_train"]:
            r["v"] = {k: v for k, v in r["v"].items()}
    with open(os.path.join(ROOT, "trader", "bigsearch_results.json"), "w") as f:
        json.dump(out, f, indent=1, default=str)
    print("evaluations", total_evals, "done")


def thanksgiving():
    """US: what did stocks do on the day after Thanksgiving (half day), 2016-2025?"""
    data = load_market("US", 20e6)
    days = []
    for y in range(2016, 2026):
        nov1 = dt.date(y, 11, 1)
        th = nov1 + dt.timedelta(days=(3 - nov1.weekday()) % 7 + 21)
        days.append(th + dt.timedelta(days=1))
    res = {}
    for d in days:
        oc, cc, gaps = [], [], []
        for s, p in data.items():
            ix = np.nonzero(p["d"] == d)[0]
            if not len(ix) or ix[0] == 0 or not p["liquid"][ix[0]]:
                continue
            i = ix[0]
            oc.append(p["c"][i] / p["o"][i] - 1)
            cc.append(p["c"][i] / p["c"][i - 1] - 1)
            gaps.append((p["o"][i] / p["c"][i - 1] - 1, p["c"][i] / p["o"][i] - 1))
        if oc:
            up_gap = [b for a, b in gaps if a > 0.01]
            dn_gap = [b for a, b in gaps if a < -0.01]
            res[str(d)] = {"stocks": len(oc), "avg_open_to_close_pct": round(100 * np.mean(oc), 3),
                           "share_up_open_to_close": round(float(np.mean(np.array(oc) > 0)), 3),
                           "avg_close_to_close_pct": round(100 * np.mean(cc), 3),
                           "gap_up_gt1pct_then_open_to_close": round(100 * np.mean(up_gap), 3) if up_gap else None,
                           "gap_down_gt1pct_then_open_to_close": round(100 * np.mean(dn_gap), 3) if dn_gap else None}
    json.dump(res, open(os.path.join(ROOT, "trader", "thanksgiving_study.json"), "w"), indent=1)
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    {"universe": build_universe, "download": download, "search": search, "thanksgiving": thanksgiving}[sys.argv[1]]()
