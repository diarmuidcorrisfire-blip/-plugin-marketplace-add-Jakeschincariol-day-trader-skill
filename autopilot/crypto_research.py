#!/usr/bin/env python3
"""Crypto research (pretend money, never orders): every coin in Yahoo's crypto list (largest ~1,250
by market cap of the 8,002 Yahoo lists), stablecoins removed.

1. Snapshot now: trend (vs 20/50/200-day averages), 1-week / 1-month / 3-month change, distance from
   the 1-year high, daily volatility, 24h $ volume.
2. Backtests over the last 2 years of daily closes, 0.5% cost each way (typical crypto spread+fee),
   decided at yesterday's close, no hindsight:
   HOLD        buy and hold each coin
   TREND50     hold the coin only while it closed above its 50-day average
   BTC_FILTER  hold the coin only while Bitcoin is above its 200-day average
   MOM_TOP10   each week hold the 10 strongest coins by 4-week return (of the 100 most traded)
   DIP         buy after RSI(3) < 15 while above the 200-day average, sell when back above the 5-day avg
   Each reported as average result, share of coins / 31-day windows that made money.

  python3 autopilot/crypto_research.py LIST.json  -> trader/crypto_research_<date>.json
"""
import concurrent.futures as cf
import datetime as dt
import json
import os
import statistics as st
import sys
import time
import re
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COST = 0.005
STABLE = {"USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "PYUSD", "USDD", "BUSD", "USDS", "FRAX", "USD1",
          "RLUSD", "USDX", "GUSD", "USDP", "LUSD", "EURC", "EURS", "USD0", "SUSD", "USDB", "USDY", "XAUT", "PAXG"}


def daily(sym):
    u = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=2y&interval=1d"
    for a in range(3):
        try:
            r = json.load(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=25))
            res = r["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            rows = [(dt.date.fromtimestamp(t), c, (v or 0) * c) for t, c, v in zip(res["timestamp"], q["close"], q["volume"]) if c]
            return rows
        except Exception:
            time.sleep(1 + a)
    return None


def sma(c, n, i):
    return sum(c[i - n + 1:i + 1]) / n if i >= n - 1 else None


def rsi(c, n=3):
    out = [None] * len(c)
    for i in range(n, len(c)):
        g = [max(c[j] - c[j - 1], 0) for j in range(i - n + 1, i + 1)]
        l = [max(c[j - 1] - c[j], 0) for j in range(i - n + 1, i + 1)]
        out[i] = 100 if sum(l) == 0 else 100 - 100 / (1 + sum(g) / sum(l))
    return out


def snapshot(sym, rows):
    c = [r[1] for r in rows]
    i = len(c) - 1
    def ch(k):
        return round(100 * (c[-1] / c[-1 - k] - 1), 1) if len(c) > k else None
    rets = [c[j] / c[j - 1] - 1 for j in range(max(1, len(c) - 90), len(c))]
    hi = max(c[-365:])
    return {"sym": sym, "price": c[-1], "above20": c[-1] > (sma(c, 20, i) or 1e99), "above50": c[-1] > (sma(c, 50, i) or 1e99),
            "above200": c[-1] > (sma(c, 200, i) or 1e99), "chg_1w": ch(7), "chg_1m": ch(30), "chg_3m": ch(90),
            "from_1y_high_pct": round(100 * (c[-1] / hi - 1), 1), "daily_vol_pct": round(100 * st.pstdev(rets), 2) if len(rets) > 5 else None,
            "usd_vol_30d": round(sum(r[2] for r in rows[-30:]) / 30)}


def run_rule(c, inpos):
    """inpos[i] = hold during day i (decided at close i-1). Returns daily returns list."""
    out, prev = [], False
    for i in range(1, len(c)):
        r = c[i] / c[i - 1] - 1 if inpos[i] else 0.0
        if inpos[i] != prev:
            r -= COST
        prev = inpos[i]
        out.append(r)
    return out


def total(rs):
    t = 1.0
    for r in rs:
        t *= 1 + r
    return t - 1


def main():
    lst = json.load(open(sys.argv[1]))
    syms = []
    for q in lst:
        base = q["symbol"].split("-")[0]
        nm = (q.get("shortName") or "") + " " + (q.get("longName") or "")
        if base in STABLE or (q.get("marketCap") or 0) < 5e6 or re.search(r"xStock|Ondo|Tokenized|\\bUSD|Dollar|Treasury|Gold", nm, re.I) \
                or re.match(r"^(USD|EUR|SUSD|AAPL|TSLA|SGOV|NVDA|SPY|QQQ|GOOGL|MSFT|AMZN|META|STRC)", base):
            continue
        syms.append((q["symbol"], q.get("marketCap") or 0, q.get("shortName")))
    print("coins to research", len(syms), flush=True)
    data = {}
    with cf.ThreadPoolExecutor(6) as ex:
        for (s, mc, nm), rows in zip(syms, ex.map(lambda x: daily(x[0]), syms)):
            vol = st.pstdev([rows[j][1] / rows[j - 1][1] - 1 for j in range(1, len(rows))]) if rows and len(rows) > 30 else 0
            if rows and len(rows) >= 60 and vol > 0.01:          # drops pegged / stable-like tokens
                data[s] = {"rows": rows, "mcap": mc, "name": nm}
    print("with data", len(data), flush=True)
    btc = {r[0]: r[1] for r in data["BTC-USD"]["rows"]}
    bd = sorted(btc)
    bc = [btc[d] for d in bd]
    btc_up = {bd[i]: bc[i] > sma(bc, 200, i) for i in range(199, len(bd))}

    snaps, rule = [], {k: [] for k in ("HOLD", "TREND50", "BTC_FILTER", "DIP")}
    for s, d in data.items():
        rows = d["rows"]
        sn = snapshot(s, rows); sn["mcap"] = d["mcap"]; sn["name"] = d["name"]; snaps.append(sn)
        c = [r[1] for r in rows]
        dates = [r[0] for r in rows]
        if len(c) < 260:
            continue
        start = 200
        cc = c[start:]
        n = len(cc)
        hold = [True] * n
        tr = [False] + [c[start + i - 1] > sma(c, 50, start + i - 1) for i in range(1, n)]
        bf = [False] + [btc_up.get(dates[start + i - 1], False) for i in range(1, n)]
        rs3 = rsi(c)
        dip, inp = [False] * n, False
        for i in range(1, n):
            k = start + i - 1
            if not inp and rs3[k] is not None and rs3[k] < 15 and c[k] > sma(c, 200, k):
                inp = True
            elif inp and c[k] > sma(c, 5, k):
                inp = False
            dip[i] = inp
        for name, pos in (("HOLD", hold), ("TREND50", tr), ("BTC_FILTER", bf), ("DIP", dip)):
            rule[name].append((s, run_rule(cc, pos), d["mcap"]))

    def summarize(name, items):
        tots = [total(r) for _, r, _ in items]
        big = [total(r) for s, r, mc in items if mc >= 1e9]
        # 31-day windows (step 7) of the equal-weight average across coins
        full = [(x, r) for x, r, mc in items if len(r) >= 450 and mc >= 1e8]   # coins with ~2 years and >= $100M
        L = min(len(r) for _, r in full)
        avg = [st.mean(max(-0.5, min(0.5, r[-L + j])) for _, r in full) for j in range(L)]
        w = [total(avg[j:j + 31]) for j in range(0, L - 31, 7)]
        return {"rule": name, "coins": len(items), "median_coin_pct": round(100 * st.median(tots), 1),
                "share_coins_profitable": round(sum(t > 0 for t in tots) / len(tots), 3),
                "median_big_coin_pct (mcap>=$1bn)": round(100 * st.median(big), 1) if big else None,
                "basket_coins": len(full), "equal_weight_basket_pct": round(100 * total(avg), 1), "days": L,
                "share_31d_windows_profitable": round(sum(x > 0 for x in w) / len(w), 3) if w else None,
                "worst_31d_pct": round(100 * min(w), 1) if w else None}

    results = [summarize(k, v) for k, v in rule.items() if v]

    # weekly momentum rotation among the 100 most traded
    common = [d for d in bd if d >= bd[-1] - dt.timedelta(days=500)]
    px = {s: {r[0]: r[1] for r in d["rows"]} for s, d in data.items()}
    dv = {s: {r[0]: r[2] for r in d["rows"]} for s, d in data.items()}
    eq, prev, curve = 1.0, set(), []
    for i in range(28, len(common) - 7, 7):
        d0, d1 = common[i], common[i + 7]
        liq = sorted([s for s in data if d0 in px[s] and common[i - 28] in px[s] and d1 in px[s]],
                     key=lambda s: -dv[s].get(d0, 0))[:100]
        mom = sorted(liq, key=lambda s: -(px[s][d0] / px[s][common[i - 28]]))[:10]
        r = st.mean(px[s][d1] / px[s][d0] - 1 for s in mom) - 2 * COST * len(set(mom) - prev) / 10
        prev = set(mom); eq *= 1 + r; curve.append(r)
    m4 = [total(curve[j:j + 4]) for j in range(len(curve) - 4)]
    results.append({"rule": "MOM_TOP10 weekly", "weeks": len(curve), "equal_weight_basket_pct": round(100 * (eq - 1), 1),
                    "share_4w_windows_profitable": round(sum(x > 0 for x in m4) / len(m4), 3), "worst_4w_pct": round(100 * min(m4), 1)})

    snaps.sort(key=lambda x: -x["mcap"])
    breadth = {"coins": len(snaps), "above20_pct": round(100 * sum(s["above20"] for s in snaps) / len(snaps), 1),
               "above50_pct": round(100 * sum(s["above50"] for s in snaps) / len(snaps), 1),
               "above200_pct": round(100 * sum(s["above200"] for s in snaps) / len(snaps), 1),
               "median_1m_pct": st.median(s["chg_1m"] for s in snaps if s["chg_1m"] is not None),
               "median_3m_pct": st.median(s["chg_3m"] for s in snaps if s["chg_3m"] is not None),
               "median_from_1y_high_pct": st.median(s["from_1y_high_pct"] for s in snaps)}
    liquid = [s for s in snaps if s["usd_vol_30d"] >= 5e6]
    strong = sorted([s for s in liquid if s["above20"] and s["above50"] and s["above200"]], key=lambda s: -(s["chg_1m"] or 0))
    out = {"generated": dt.datetime.now().isoformat(timespec="minutes"), "listed_by_yahoo": 8002, "researched": len(data),
           "breadth": breadth, "backtests": results, "uptrend_liquid_coins": strong[:25], "top20_by_mcap": snaps[:20]}
    path = os.path.join(ROOT, "trader", f"crypto_research_{dt.date.today().isoformat()}.json")
    json.dump(out, open(path, "w"), indent=1, default=str)
    print(json.dumps({k: out[k] for k in ("researched", "breadth", "backtests")}, indent=1, default=str))
    print("UPTREND", [(s["sym"], s["chg_1m"], s["usd_vol_30d"]) for s in strong[:25]])


if __name__ == "__main__":
    main()
