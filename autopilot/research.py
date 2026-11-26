#!/usr/bin/env python3
"""Strategy research on paper: which rules would have made money, after fees, out of sample?

Data (free Yahoo): 60 days of 5-minute bars and 2 years of daily bars for the autopilot
universes (39 EU stocks, 51 US stocks). Every signal is traded with a pretend EUR 130 position,
fees included (US: 0.15% FX each way; EU: none), entries at the signal bar's close plus 0.02%.
Each strategy is scored on the FIRST half of its dates (in-sample) and the SECOND half
(out-of-sample). A strategy only counts if it is positive in both halves in both markets.

  python3 autopilot/research.py            # writes trader/research_results.json
"""
import datetime as dt
import gzip
import json
import math
import os
import statistics as st
import sys
import time
import urllib.request
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.environ.get("RESEARCH_CACHE", "/tmp/research_cache")
os.makedirs(CACHE_DIR, exist_ok=True)
NOTIONAL = 130.0
SLIP = 0.0002
MK = {
    "EU": {"universe": "autopilot/eu_universe.json", "tz": "Europe/Berlin", "open": dt.time(9, 0),
           "close": dt.time(17, 30), "fee": 0.0},
    "US": {"universe": "autopilot/us_universe.json", "tz": "America/New_York", "open": dt.time(9, 30),
           "close": dt.time(16, 0), "fee": 0.0015},
}


def fetch(sym, rng, interval):
    path = os.path.join(CACHE_DIR, f"{sym}_{rng}_{interval}.json.gz")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 6 * 3600:
        with gzip.open(path, "rt") as f:
            return json.load(f)
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval={interval}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30) as r:
            res = json.load(r)["chart"]["result"][0]
    except Exception:
        return None
    q = res["indicators"]["quote"][0]
    rows = [[t, o, h, l, c, v or 0] for t, o, h, l, c, v in zip(res["timestamp"], q["open"], q["high"], q["low"], q["close"], q["volume"])
            if None not in (o, h, l, c)]
    with gzip.open(path, "wt") as f:
        json.dump(rows, f)
    time.sleep(0.25)
    return rows


def intraday(mkname):
    mk = MK[mkname]
    tz = ZoneInfo(mk["tz"])
    out = {}
    for s in json.load(open(os.path.join(ROOT, mk["universe"]))):
        rows = fetch(s, "60d", "5m")
        if not rows:
            continue
        days = {}
        for t, o, h, l, c, v in rows:
            lt = dt.datetime.fromtimestamp(t, tz)
            if mk["open"] <= lt.time() < mk["close"]:
                days.setdefault(lt.date(), []).append((lt, o, h, l, c, v))
        out[s] = {d: b for d, b in days.items() if len(b) >= 60}
    return out


def daily(mkname):
    mk = MK[mkname]
    out = {}
    for s in json.load(open(os.path.join(ROOT, mk["universe"]))):
        rows = fetch(s, "2y", "1d")
        if rows and len(rows) > 250:
            out[s] = [(dt.datetime.fromtimestamp(r[0], ZoneInfo(mk["tz"])).date(),) + tuple(r[1:]) for r in rows]
    return out


def trade(mkname, d, sym, entry, exit_p, how, **kw):
    fee = MK[mkname]["fee"]
    e = entry * (1 + SLIP)
    ret = exit_p / e - 1 - 2 * fee
    return {"date": d.isoformat(), "sym": sym, "ret": ret, "pnl_eur": NOTIONAL * ret, "how": how, **kw}


def walk(bars, j, stop, target, flat):
    """From bar j+1: stop first (gap -> open), then target, else flat time. Returns (price, why)."""
    for b in bars[j + 1:]:
        if stop is not None and b[3] <= stop:
            return min(stop, b[1]), "stop"
        if target is not None and b[2] >= target:
            return target, "target"
        if b[0].time() >= flat:
            return b[4], "close"
    return bars[-1][4], "close"


def vwap_series(bars):
    pv = vv = 0.0
    out = []
    for b in bars:
        tp = (b[2] + b[3] + b[4]) / 3
        pv += tp * b[5]; vv += b[5]
        out.append(pv / vv if vv else b[4])
    return out


def flat_time(mk):
    c = dt.datetime.combine(dt.date.today(), mk["close"]) - dt.timedelta(minutes=25)
    return c.time()


# ------------------------------------------------------------------ intraday strategies
def s_orb(name, data, prevs, or_min=30, tr=1.5, stop_rule="low"):
    mk = MK[name]; flat = flat_time(mk); trades = []
    o = dt.datetime.combine(dt.date.today(), mk["open"])
    a = (o + dt.timedelta(minutes=5 if name == "EU" else 0)).time()
    e = (o + dt.timedelta(minutes=or_min)).time()
    for sym, days in data.items():
        for d, bars in days.items():
            prev = prevs.get(sym, {}).get(d)
            orng = [b for b in bars if a <= b[0].time() < e]
            if not prev or len(orng) < 2:
                continue
            hi, lo = max(b[2] for b in orng), min(b[3] for b in orng)
            for j, b in enumerate(bars):
                if b[0].time() < e:
                    continue
                if b[4] > hi:
                    if b[4] <= hi * 1.01 and b[4] > prev:
                        stop = (hi + lo) / 2 if stop_rule == "mid" else lo
                        r = b[4] - stop
                        if 0.003 * b[4] <= r <= 0.025 * b[4]:
                            px, why = walk(bars, j, stop, b[4] + tr * r, flat)
                            trades.append(trade(name, d, sym, b[4], px, why))
                    break
    return trades


def s_last_hour_momentum(name, data, prevs, thresh=1.0):
    """Intraday momentum: strong from the open into the last 90 minutes and above VWAP -> hold to the close."""
    mk = MK[name]; flat = flat_time(mk); trades = []
    cut = (dt.datetime.combine(dt.date.today(), mk["close"]) - dt.timedelta(minutes=90)).time()
    for sym, days in data.items():
        for d, bars in days.items():
            vw = vwap_series(bars)
            js = [j for j, b in enumerate(bars) if b[0].time() >= cut]
            if not js:
                continue
            j = js[0]
            o = bars[0][1]
            if bars[j][4] / o - 1 >= thresh / 100 and bars[j][4] > vw[j]:
                px, why = walk(bars, j, vw[j] * 0.995, None, flat)
                trades.append(trade(name, d, sym, bars[j][4], px, why))
    return trades


def s_vwap_pullback(name, data, prevs, up=1.5):
    """Up strongly by late morning, dips to VWAP, closes back above -> buy; stop 0.6% under VWAP; target 2R."""
    mk = MK[name]; flat = flat_time(mk); trades = []
    start = (dt.datetime.combine(dt.date.today(), mk["open"]) + dt.timedelta(minutes=90)).time()
    end = (dt.datetime.combine(dt.date.today(), mk["close"]) - dt.timedelta(minutes=120)).time()
    for sym, days in data.items():
        for d, bars in days.items():
            prev = prevs.get(sym, {}).get(d)
            if not prev:
                continue
            vw = vwap_series(bars)
            for j in range(1, len(bars)):
                t = bars[j][0].time()
                if t < start:
                    continue
                if t > end:
                    break
                b = bars[j]
                if bars[j - 1][3] <= vw[j - 1] * 1.001 and b[4] > vw[j] and b[4] / prev - 1 >= up / 100:
                    stop = vw[j] * 0.994
                    r = b[4] - stop
                    px, why = walk(bars, j, stop, b[4] + 2 * r, flat)
                    trades.append(trade(name, d, sym, b[4], px, why))
                    break
    return trades


def s_gap_down_reversal(name, data, prevs, gap=2.0):
    """Opens >=gap% below yesterday, first 30 minutes close above the open -> buy; stop at the day's low; hold to close."""
    mk = MK[name]; flat = flat_time(mk); trades = []
    e = (dt.datetime.combine(dt.date.today(), mk["open"]) + dt.timedelta(minutes=30)).time()
    for sym, days in data.items():
        for d, bars in days.items():
            prev = prevs.get(sym, {}).get(d)
            if not prev or bars[0][1] / prev - 1 > -gap / 100:
                continue
            first = [b for b in bars if b[0].time() < e]
            if not first:
                continue
            j = len(first) - 1
            if first[-1][4] > bars[0][1]:
                stop = min(b[3] for b in first)
                px, why = walk(bars, j, stop, None, flat)
                trades.append(trade(name, d, sym, first[-1][4], px, why))
    return trades


def s_midday_dip_in_uptrend(name, data, prevs, ma50, dip=2.5):
    """Down >=dip% vs yesterday at midday while above its 50-day average -> buy, sell at the close."""
    mk = MK[name]; flat = flat_time(mk); trades = []
    mid = (dt.datetime.combine(dt.date.today(), mk["open"]) + dt.timedelta(hours=3)).time()
    for sym, days in data.items():
        for d, bars in days.items():
            prev = prevs.get(sym, {}).get(d)
            m = ma50.get(sym, {}).get(d)
            js = [j for j, b in enumerate(bars) if b[0].time() >= mid]
            if not prev or not m or not js or prev < m:
                continue
            j = js[0]
            if bars[j][4] / prev - 1 <= -dip / 100:
                px, why = walk(bars, j, None, None, flat)
                trades.append(trade(name, d, sym, bars[j][4], px, why))
    return trades


# ------------------------------------------------------------------ daily strategies
def rsi(closes, n):
    out = [None] * len(closes)
    gains = losses = 0.0
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        g, l = max(ch, 0), max(-ch, 0)
        if i <= n:
            gains += g; losses += l
            if i == n:
                gains /= n; losses /= n
                out[i] = 100 - 100 / (1 + gains / losses) if losses else 100
        else:
            gains = (gains * (n - 1) + g) / n; losses = (losses * (n - 1) + l) / n
            out[i] = 100 - 100 / (1 + gains / losses) if losses else 100
    return out


def s_rsi2_swing(name, ddata, lo=10, maxhold=10):
    """Close above 200-day average and RSI(2) under lo -> buy the close; sell when the close is above the 5-day average."""
    trades = []
    for sym, rows in ddata.items():
        c = [r[4] for r in rows]
        r2 = rsi(c, 2)
        i = 200
        while i < len(rows) - 1:
            ma200 = sum(c[i - 199:i + 1]) / 200
            if r2[i] is not None and r2[i] < lo and c[i] > ma200:
                for k in range(i + 1, min(i + 1 + maxhold, len(rows))):
                    if c[k] > sum(c[k - 4:k + 1]) / 5 or k == min(i + maxhold, len(rows) - 1):
                        trades.append(trade(name, rows[i][0], sym, c[i], c[k], "swing", days=k - i))
                        i = k
                        break
            i += 1
    return trades


def s_overnight(name, ddata):
    """Buy the close, sell the next open (overnight drift)."""
    trades = []
    for sym, rows in ddata.items():
        for i in range(200, len(rows) - 1):
            trades.append(trade(name, rows[i][0], sym, rows[i][4], rows[i + 1][1], "overnight"))
    return trades


def s_breakout_swing(name, ddata, look=20, hold=5):
    """Close at a 20-day closing high (the scanner's breakout_20d) above the 200-day average -> hold 5 days."""
    trades = []
    for sym, rows in ddata.items():
        c = [r[4] for r in rows]
        i = 200
        while i < len(rows) - hold:
            if c[i] > max(c[i - look:i]) and c[i] > sum(c[i - 199:i + 1]) / 200:
                trades.append(trade(name, rows[i][0], sym, c[i], c[i + hold], "hold"))
                i += hold
            i += 1
    return trades


# ------------------------------------------------------------------ scoring
def score(trades):
    if len(trades) < 10:
        return {"n": len(trades)}
    rets = [t["ret"] for t in trades]
    m = st.mean(rets)
    sd = st.pstdev(rets) or 1e-9
    wins = [x for x in rets if x > 0]
    gl = -sum(x for x in rets if x < 0)
    return {"n": len(rets), "win": round(len(wins) / len(rets), 3), "avg_pct": round(100 * m, 3),
            "t": round(m / sd * math.sqrt(len(rets)), 2), "pf": round(sum(wins) / gl, 2) if gl else None,
            "eur_per_trade_130": round(NOTIONAL * m, 3)}


def halves(trades):
    ds = sorted({t["date"] for t in trades})
    if not ds:
        return [], []
    cut = ds[len(ds) // 2]
    return [t for t in trades if t["date"] < cut], [t for t in trades if t["date"] >= cut]


def main():
    results = {"generated": dt.datetime.now(ZoneInfo("Europe/Berlin")).isoformat(timespec="minutes"),
               "notional_eur": NOTIONAL, "strategies": {}}
    for name in ("EU", "US"):
        print("loading", name, flush=True)
        idata = intraday(name)
        ddata = daily(name)
        prevs, ma50 = {}, {}
        for sym, rows in ddata.items():
            c = [r[4] for r in rows]
            prevs[sym] = {rows[i][0]: c[i - 1] for i in range(1, len(rows))}
            ma50[sym] = {rows[i][0]: sum(c[i - 50:i]) / 50 for i in range(50, len(rows))}
        tests = {
            "ORB 30m, stop range low, 1.5R": lambda: s_orb(name, idata, prevs, 30, 1.5, "low"),
            "ORB 15m, stop range low, 1R": lambda: s_orb(name, idata, prevs, 15, 1.0, "low"),
            "Last-90-min momentum (+1%)": lambda: s_last_hour_momentum(name, idata, prevs, 1.0),
            "Last-90-min momentum (+2%)": lambda: s_last_hour_momentum(name, idata, prevs, 2.0),
            "VWAP pullback (up 1.5%)": lambda: s_vwap_pullback(name, idata, prevs, 1.5),
            "Gap-down reversal (2%)": lambda: s_gap_down_reversal(name, idata, prevs, 2.0),
            "Midday dip in uptrend (2.5%)": lambda: s_midday_dip_in_uptrend(name, idata, prevs, ma50, 2.5),
            "RSI(2) swing in uptrend (2y daily)": lambda: s_rsi2_swing(name, ddata),
            "20-day breakout, hold 5 days (2y daily)": lambda: s_breakout_swing(name, ddata),
            "Overnight: buy close, sell open (2y daily)": lambda: s_overnight(name, ddata),
        }
        for label, fn in tests.items():
            tr = fn()
            a, b = halves(tr)
            res = {"all": score(tr), "first_half": score(a), "second_half": score(b),
                   "period": [min((t["date"] for t in tr), default=None), max((t["date"] for t in tr), default=None)]}
            results["strategies"].setdefault(label, {})[name] = res
            print(f"{name} {label:45} all {res['all']} | OOS {res['second_half']}", flush=True)
    passed = []
    for label, by in results["strategies"].items():
        ok = all(by.get(m, {}).get(h, {}).get("avg_pct", -1) > 0 for m in ("EU", "US") for h in ("first_half", "second_half"))
        strong = ok and all(by[m]["all"].get("t", 0) >= 2 for m in ("EU", "US"))
        by["verdict"] = "PASSES (both markets, both halves, t>=2)" if strong else ("positive but weak" if ok else "fails")
        if ok:
            passed.append(label)
    results["passed"] = passed
    with open(os.path.join(ROOT, "trader", "research_results.json"), "w") as f:
        json.dump(results, f, indent=1)
    print("PASSED:", passed)


if __name__ == "__main__":
    main()
