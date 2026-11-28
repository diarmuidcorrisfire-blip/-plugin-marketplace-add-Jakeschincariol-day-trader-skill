#!/usr/bin/env python3
"""Crypto "jump" scan (ideas only, never orders): which coin is breaking out right now, and how often
that same signal has actually led to a big jump.

1. Full Yahoo crypto list (all ~8,000 coins), keep coins with >= MIN_VOL_USD traded in 24h, drop
   stablecoins / tokenized stocks / pegged tokens.
2. Hourly bars for the last 30 days of every kept coin.
3. Backtest across every coin and every hour: signal = hourly close above the prior 24h high, on
   hourly volume >= 3x the prior 24h average. For each past signal, what happened in the next 24h:
   share that touched +20% / +10%, share that fell -10% first, average close-to-close result after a
   1% round-trip cost. Compared with any random hour (baseline).
4. Coins where the signal fired in the last 3 hours, ranked; plan with entry / stop / target.

  python3 autopilot/crypto_jump.py   -> trader/crypto_jump_<date>.json
"""
import concurrent.futures as cf
import datetime as dt
import json
import os
import re
import statistics as st
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIN_VOL_USD, COST = 1e6, 0.01
STABLE = {"USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "PYUSD", "USDD", "BUSD", "USDS", "FRAX", "USD1", "RLUSD",
          "USDX", "GUSD", "USDP", "LUSD", "EURC", "EURS", "USD0", "SUSD", "USDB", "USDY", "XAUT", "PAXG", "WBTC", "WETH",
          "STETH", "WSTETH", "WBETH", "CBBTC", "WEETH", "RETH", "CBETH"}
UA = {"User-Agent": "Mozilla/5.0"}


def get(url, tries=3):
    for a in range(tries):
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25))
        except Exception:
            time.sleep(1 + 2 * a)
    return None


def coin_list():
    out, start = [], 0
    while True:
        d = get(f"https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=all_cryptocurrencies_us&count=250&start={start}", 4)
        if not d:
            break
        r = d["finance"]["result"][0]
        out += r["quotes"]
        start += 250
        if start >= (r.get("total") or 0) or not r["quotes"]:
            break
    return out


def hourly(sym):
    d = get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=1mo&interval=1h")
    try:
        r = d["chart"]["result"][0]
        q = r["indicators"]["quote"][0]
        return [(t, o, h, l, c, v or 0) for t, o, h, l, c, v in zip(r["timestamp"], q["open"], q["high"], q["low"], q["close"], q["volume"])
                if c and h and l]
    except Exception:
        return None


def signals(b):
    """indexes i where hour i closes above the prior 24h high on >= 3x the prior 24h average volume."""
    out = []
    for i in range(24, len(b)):
        prev = b[i - 24:i]
        hi = max(x[2] for x in prev)
        av = sum(x[5] for x in prev) / 24
        if b[i][4] > hi and av > 0 and b[i][5] >= 3 * av:
            out.append(i)
    return out


def forward(b, i):
    if i + 24 >= len(b):
        return None
    e = b[i][4]
    up20 = up10 = down_first = False
    for x in b[i + 1:i + 25]:
        if x[3] <= e * 0.9 and not (up10 or up20):
            down_first = True
        if x[2] >= e * 1.1:
            up10 = True
        if x[2] >= e * 1.2:
            up20 = True
    return {"up20": up20, "up10": up10, "down10_first": down_first, "ret": b[i + 24][4] / e - 1 - COST}


def summarize(ev):
    if not ev:
        return None
    return {"n": len(ev), "touched_plus20_pct": round(100 * sum(e["up20"] for e in ev) / len(ev), 1),
            "touched_plus10_pct": round(100 * sum(e["up10"] for e in ev) / len(ev), 1),
            "fell_10_first_pct": round(100 * sum(e["down10_first"] for e in ev) / len(ev), 1),
            "avg_24h_result_pct": round(100 * st.mean(e["ret"] for e in ev), 2),
            "median_24h_result_pct": round(100 * st.median(e["ret"] for e in ev), 2),
            "share_profitable_24h": round(sum(e["ret"] > 0 for e in ev) / len(ev), 3)}


def main():
    lst = coin_list()
    keep = []
    for q in lst:
        base = q["symbol"].split("-")[0]
        nm = (q.get("shortName") or "") + " " + (q.get("longName") or "")
        vol = q.get("volume24Hr") or q.get("regularMarketVolume") or 0
        if vol and q.get("regularMarketPrice") and vol < 1e5:     # volume24Hr is in USD for crypto; regularMarketVolume may be units
            pass
        if base in STABLE or re.search(r"xStock|Ondo|Tokenized|\bUSD\b.*Coin|Dollar|Treasury|Gold|Wrapped|Staked|Bridged", nm, re.I):
            continue
        if re.match(r"^(USD|EUR|SUSD|AAPL|TSLA|SGOV|NVDA|SPY|QQQ|GOOGL|MSFT|AMZN|META|STRC)", base):
            continue
        if (q.get("volume24Hr") or 0) < MIN_VOL_USD:
            continue
        keep.append(q)
    print("listed", len(lst), "liquid kept", len(keep), flush=True)
    data = {}
    with cf.ThreadPoolExecutor(6) as ex:
        for q, b in zip(keep, ex.map(lambda q: hourly(q["symbol"]), keep)):
            if b and len(b) >= 200:
                rets = [b[j][4] / b[j - 1][4] - 1 for j in range(1, len(b))]
                if st.pstdev(rets) > 0.002:               # drop pegged tokens
                    data[q["symbol"]] = (q, b)
    print("with hourly data", len(data), flush=True)
    sig_ev, base_ev, live = [], [], []
    now = time.time()
    for s, (q, b) in data.items():
        idx = signals(b)
        for i in idx:
            f = forward(b, i)
            if f:
                f["sym"] = s; sig_ev.append(f)
        for i in range(24, len(b) - 24, 6):
            f = forward(b, i)
            if f:
                base_ev.append(f)
        for i in idx:
            if now - b[i][0] <= 3 * 3600 + 600:
                e = b[-1][4]
                lo24 = min(x[3] for x in b[-24:])
                past = [forward(b, j) for j in idx if forward(b, j)]
                live.append({"sym": s, "name": q.get("shortName"), "price_usd": e, "signal_hour_utc": dt.datetime.utcfromtimestamp(b[i][0]).strftime("%H:%M"),
                             "chg_24h_pct": round(100 * (e / b[-25][4] - 1), 1), "vol_24h_usd": q.get("volume24Hr"), "mcap_usd": q.get("marketCap"),
                             "vol_x_avg": round(b[i][5] / (sum(x[5] for x in b[i - 24:i]) / 24), 1),
                             "stop_usd": round(max(lo24, e * 0.85), 8), "target_usd": round(e * 1.2, 8),
                             "this_coin_past_signals": summarize(past)})
                break
    live.sort(key=lambda x: -(x["vol_24h_usd"] or 0))
    out = {"generated_utc": dt.datetime.utcnow().isoformat(timespec="minutes"), "listed_by_yahoo": len(lst), "liquid_researched": len(data),
           "signal_backtest_30d": summarize(sig_ev), "any_hour_baseline_30d": summarize(base_ev), "live_breakouts_last_3h": live[:15]}
    path = os.path.join(ROOT, "trader", f"crypto_jump_{dt.date.today().isoformat()}.json")
    json.dump(out, open(path, "w"), indent=1, default=str)
    print(json.dumps(out, indent=1, default=str)[:6000])


if __name__ == "__main__":
    main()
