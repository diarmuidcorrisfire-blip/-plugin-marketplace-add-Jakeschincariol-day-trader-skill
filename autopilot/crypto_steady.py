#!/usr/bin/env python3
"""Steady-coin ranking (ideas only, never orders): big coins (>= $700M) ranked by how often they were
higher 7 days later in the same market conditions as now (Bitcoin vs its 20/200-day averages and the
coin vs its 50-day average), over 2 years of daily closes, 1% round-trip cost.
  python3 autopilot/crypto_steady.py  -> prints the top 15 as JSON lines and trader/crypto_steady_<date>.json
"""
import concurrent.futures as cf, datetime as dt, json, os, re, statistics as st, sys, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crypto_research as R  # noqa: E402

def coin_list():
    out, start = [], 0
    while start < 1250:
        u = f"https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=all_cryptocurrencies_us&count=250&start={start}"
        try:
            out += json.load(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=25))["finance"]["result"][0]["quotes"]
        except Exception:
            break
        start += 250
    return out

def main():
    bad = re.compile(r"xStock|Ondo|Tokenized|Dollar|Treasury|Gold|Wrapped|Staked|Bridged|USD1|stETH|Liquid", re.I)
    skip = R.STABLE | {"WBTC", "WETH", "STETH", "WSTETH", "WBETH", "CBBTC", "WEETH", "LEO"}
    qs = [q for q in coin_list() if (q.get("marketCap") or 0) >= 7e8 and q["symbol"].split("-")[0] not in skip
          and not bad.search(q.get("shortName") or "") and not re.match(r"^(USD|EUR|AAPL|TSLA|SGOV|NVDA|SPY|QQQ|STRC|CBBTC|WEETH|BSC)", q["symbol"])]
    rows = {}
    with cf.ThreadPoolExecutor(6) as ex:
        for q, r in zip(qs, ex.map(lambda q: R.daily(q["symbol"]), qs)):
            if r and len(r) >= 300:
                rows[q["symbol"]] = (q, r)
    btc = {d: c for d, c, _ in rows["BTC-USD"][1]}; bd = sorted(btc); bc = [btc[d] for d in bd]
    reg = {bd[i]: (bc[i] < sum(bc[i - 199:i + 1]) / 200, bc[i] < sum(bc[i - 19:i + 1]) / 20) for i in range(199, len(bd))}
    cur = reg[bd[-1]]
    out = []
    for s, (q, r) in rows.items():
        c = [x[1] for x in r]; d = [x[0] for x in r]
        if st.pstdev([c[i] / c[i - 1] - 1 for i in range(1, len(c))]) < 0.008:
            continue
        now_below50 = c[-1] < sum(c[-50:]) / 50
        allr, cond = [], []
        for i in range(200, len(c) - 7):
            f = c[i + 7] / c[i] - 1 - 0.01
            allr.append(f)
            if reg.get(d[i]) == cur and (c[i] < sum(c[i - 49:i + 1]) / 50) == now_below50:
                cond.append(f)
        if len(cond) < 40:
            continue
        out.append({"sym": s, "name": q.get("shortName"), "price_usd": c[-1], "mcap_bn": round(q["marketCap"] / 1e9, 1),
                    "p_up_7d_now_like": round(sum(x > 0 for x in cond) / len(cond), 2), "n": len(cond),
                    "median_7d_pct": round(100 * st.median(cond), 1), "bad_week_p10_pct": round(100 * sorted(cond)[len(cond) // 10], 1),
                    "p_up_7d_all": round(sum(x > 0 for x in allr) / len(allr), 2), "chg_30d_pct": round(100 * (c[-1] / c[-31] - 1), 1),
                    "chg_24h_pct": round(100 * (c[-1] / c[-2] - 1), 1), "daily_vol_pct": round(100 * st.pstdev([c[i] / c[i - 1] - 1 for i in range(len(c) - 60, len(c))]), 1)})
    out.sort(key=lambda x: -x["p_up_7d_now_like"])
    res = {"generated": dt.datetime.now().isoformat(timespec="minutes"), "btc_below_200d_20d": cur, "ranking": out}
    json.dump(res, open(os.path.join(R.ROOT, "trader", f"crypto_steady_{dt.date.today().isoformat()}.json"), "w"), indent=1, default=str)
    print("btc_below_200d,below_20d", cur)
    for o in out[:15]:
        print(json.dumps(o))

if __name__ == "__main__":
    main()
