#!/usr/bin/env python3
"""Autopilot: small, rules-bound day trades on Trading 212 (EU stocks in EUR, US stocks in USD).

The user gave standing permission to trade inside the limits in trader/autopilot.json.
Hard rules enforced here, not left to judgment:
  - only money inside budget_eur; at most risk_per_trade_eur lost at the stop (fees included)
  - max_trades_per_day entries across all markets; entries only inside each market's window
  - every entry gets a sell stop immediately; if the stop can't be placed, sell at once
  - everything flat by each market's flat_by; never trades never_trade or anything already held,
    and only ever sells shares it bought itself
  - before any entry, a replay check (the Time Machine method: thousands of simulated rests-of-day
    built from the stock's own last 60 days of 5-minute bars at the same time of day) picks the
    target with the best average result; no entry unless that average is positive after fees
  - switches itself off (enabled=false) once realised losses in the window reach loss_cap_eur
Setup: opening-range breakout. Entry on a completed 5-minute close above the range high
(not more than 1% above it), stop at the range midpoint. Data: free Yahoo bars (EU ~15 min delayed).

Run once (the hourly schedule):   python3 autopilot/autopilot.py
Run continuously for a session:   python3 autopilot/autopilot.py --loop [MAX_TRADES] [DAY_LOSS_CAP_EUR]
Test without placing orders:      add --dry-run
Prints one JSON summary line last.
"""
import datetime as dt
import json
import math
import os
import random
import sys
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.environ.get("AUTOPILOT_CFG") or os.path.join(ROOT, "trader", "autopilot.json")
STATE = os.path.join(ROOT, "trader", "autopilot_state.json")
LOG = os.path.join(ROOT, "trader", "autopilot_log.jsonl")
T212 = "https://live.trading212.com/api/v0/equity"
CET = ZoneInfo("Europe/Berlin")
DRY = "--dry-run" in sys.argv
EVENTS = []
NYSE_EARLY_CLOSE = {"2026-11-27", "2026-12-24", "2027-11-26", "2027-12-24"}


# ---------------------------------------------------------------- plumbing
def log(msg, quiet=False, **kw):
    rec = {"ts": dt.datetime.now(CET).isoformat(timespec="seconds"), "msg": msg, **kw}
    if not quiet:
        EVENTS.append(rec)
    print(json.dumps(rec), flush=True)
    if not DRY:
        with open(LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(path, obj):
    if DRY:
        return
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def http(method, url, body=None, headers=None, timeout=20):
    data = json.dumps(body).encode() if body is not None else None
    h = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode()
            return r.status, (json.loads(txt) if txt.strip() else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]
    except Exception as e:
        return 0, str(e)[:300]


def t212(method, path, body=None):
    # Placeholder credentials: the session proxy overwrites the Authorization header.
    s, d = 0, None
    for _ in range(3):
        s, d = http(method, T212 + path, body, {"Authorization": "Basic cHJveHk6aW5qZWN0ZWQ="})
        if s != 429:
            return s, d
        time.sleep(6)
    return s, d


def positions():
    s, d = t212("GET", "/positions")
    if s != 200:
        raise RuntimeError(f"positions failed: {s} {d}")
    return {p["instrument"]["ticker"]: p for p in d}


def chart(symbol, rng="1d"):
    s, d = http("GET", f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={rng}&interval=5m")
    if s != 200:
        return None
    try:
        r = d["chart"]["result"][0]
        q = r["indicators"]["quote"][0]
        rows = [(dt.datetime.fromtimestamp(t, dt.timezone.utc), o, h, l, c, v or 0)
                for t, o, h, l, c, v in zip(r["timestamp"], q["open"], q["high"], q["low"], q["close"], q["volume"])
                if c is not None and h is not None and l is not None and o is not None]
        return {"prev": r["meta"].get("chartPreviousClose"), "rows": rows}
    except (KeyError, IndexError, TypeError):
        return None


def eurusd():
    b = chart("EURUSD=X")
    return b["rows"][-1][4] if b and b["rows"] else None


def hm(s):
    return dt.time.fromisoformat(s)


def to_eur(amount, mk, fx):
    return amount / fx if mk["currency"] == "USD" else amount


# ---------------------------------------------------------------- replay check
def replay(mk, ysym, now_l, entry, stop, targets, n=4000):
    """Average outcome per share for each target, from now to flat_by, the Time Machine way."""
    b = chart(ysym, "60d")
    if not b:
        return None
    tz = ZoneInfo(mk["tz"])
    days = {}
    for r in b["rows"]:
        lt = r[0].astimezone(tz)
        days.setdefault(lt.date(), []).append((lt, r[2], r[3], r[4]))
    past = [v for d, v in days.items() if d < now_l.date() and len(v) >= 60]
    if len(past) < 20:
        return None
    mins = lambda t: t.hour * 60 + t.minute
    start, end = mins(now_l), mins(hm(mk["flat_by"]))
    pool = {}
    for v in past:
        for a, c in zip(v, v[1:]):
            s = mins(c[0])
            if start <= s <= end:
                pool.setdefault(s - s % 5, []).append((c[1] / a[3], c[2] / a[3], c[3] / a[3]))
    slots = sorted(pool)
    if not slots:
        return None
    rnd = random.Random(7)
    paths = []
    for _ in range(n):
        p, path = entry, []
        for s in slots:
            h, l, c = rnd.choice(pool[s])
            path.append((p * h, p * l))
            p *= c
        paths.append((path, p))
    out = []
    for tgt in targets:
        hit = stp = 0
        tot = 0.0
        for path, pend in paths:
            x = None
            for hi, lo in path:
                if lo <= stop:
                    x = stop; stp += 1; break
                if hi >= tgt:
                    x = tgt; hit += 1; break
            tot += (x if x is not None else pend) - entry
        out.append({"target": round(tgt, 2), "p_target": round(hit / n, 3), "p_stop": round(stp / n, 3),
                    "avg_per_share": tot / n})
    return {"sessions": len(past), "results": out}


# ---------------------------------------------------------------- scanning and entry
def find_setups(cfg, mk, now, held):
    tz = ZoneInfo(mk["tz"])
    now_l = now.astimezone(tz)
    universe = load(os.path.join(ROOT, mk["universe"]), {})
    never = set(cfg.get("never_trade", []))
    or_a, or_b = hm(mk["or"][0]), hm(mk["or"][1])
    picks = []
    for ysym, ticker in universe.items():
        if ticker in held or ticker in never or ysym in never:
            continue
        b = chart(ysym)
        time.sleep(0.25)
        if not b or not b["prev"]:
            continue
        rows = [(r[0].astimezone(tz),) + r[1:] for r in b["rows"]]
        rows = [r for r in rows if r[0].date() == now_l.date()]
        orng = [r for r in rows if or_a <= r[0].time() < or_b]
        done = [r for r in rows if r[0] + dt.timedelta(minutes=5) <= now_l - dt.timedelta(minutes=1)]
        if len(orng) < 3 or len(done) < len(orng) + 3:
            continue
        hi, lo = max(r[2] for r in orng), min(r[3] for r in orng)
        close = done[-1][4]
        if close < mk["min_price"] or close <= b["prev"]:
            continue
        if not (hi < close <= hi * 1.01):          # fresh breakout, not chased
            continue
        stop = math.floor((hi + lo) / 2 * 100) / 100
        if not (0.003 * close <= close - stop <= 0.025 * close):
            continue
        picks.append({"market": mk["name"], "yahoo": ysym, "ticker": ticker, "signal_close": round(close, 4),
                      "or_high": round(hi, 4), "or_low": round(lo, 4), "stop": stop,
                      "bar": done[-1][0].strftime("%H:%M"), "chg_pct": round(100 * (close / b["prev"] - 1), 2),
                      "traded_value": sum(r[4] * r[5] for r in rows)})
    picks.sort(key=lambda p: (-p["chg_pct"], -p["traded_value"]))
    return picks


def plan_trade(cfg, mk, now, setup, fx):
    """Size the trade and pick the target with the replay check. Returns a plan or None."""
    px, stop = setup["signal_close"], setup["stop"]
    r = px - stop
    fee = mk.get("fx_fee_pct", 0) / 100
    fxr = fx if mk["currency"] == "USD" else 1.0
    risk_ccy = cfg["risk_per_trade_eur"] * fxr
    budget_ccy = cfg["budget_eur"] * fxr
    loss_ps = r + fee * (px + stop)             # loss per share at the stop, fees included
    qty = math.floor(min(0.75 * risk_ccy / loss_ps, 0.95 * budget_ccy / px) * 100) / 100
    if qty * px < 10 * fxr:
        log("setup too small to trade", **setup)
        return None
    targets = [px + k * r for k in (1.0, 1.5, 2.0)]
    targets = [t for t in targets if 100 * (t / px - 1) >= mk.get("min_target_pct", 0)] or [px + 2 * r]
    rp = replay(mk, setup["yahoo"], now.astimezone(ZoneInfo(mk["tz"])), px, stop, targets)
    if not rp:
        log("replay unavailable - skipping", **setup)
        return None
    best = max(rp["results"], key=lambda x: x["avg_per_share"])
    avg_after_fees = (best["avg_per_share"] - fee * 2 * px) * qty
    verdict = {k: best[k] for k in ("target", "p_target", "p_stop")}
    if avg_after_fees <= 0:
        log("replay says no - skipping", avg_after_fees=round(to_eur(avg_after_fees, mk, fx), 3),
            sessions=rp["sessions"], **verdict, ticker=setup["ticker"])
        return None
    return {"qty": qty, "target": best["target"], "replay": {**verdict, "sessions": rp["sessions"],
            "avg_eur": round(to_eur(avg_after_fees, mk, fx), 3)}}


def enter(state, cfg, mk, now, setup, fx):
    plan = plan_trade(cfg, mk, now, setup, fx)
    if not plan:
        return False
    qty, stop = plan["qty"], setup["stop"]
    if DRY:
        log("DRY RUN: would buy", qty=qty, target=plan["target"], replay=plan["replay"], **setup)
        return True
    today = now.date().isoformat()
    s, d = t212("POST", "/orders/market", {"ticker": setup["ticker"], "quantity": qty})
    state["day_entries"][today] = state["day_entries"].get(today, 0) + 1
    if s != 200:
        log("BUY REJECTED", status=s, resp=d, qty=qty, **setup)
        return True
    order_id = d.get("id")
    p = None
    for _ in range(12):
        time.sleep(5)
        p = positions().get(setup["ticker"])
        if p and p["quantity"] >= qty * 0.999:
            break
        p = None
    if not p:
        log("BUY NOT FILLED after 60s - cancelling", order_id=order_id)
        t212("DELETE", f"/orders/{order_id}")
        return True
    entry = p["averagePricePaid"]
    r = entry - stop
    target = max(plan["target"], round(entry + 0.5 * r, 2)) if r > 0 else plan["target"]
    op = {"market": mk["name"], "currency": mk["currency"], "fx": fx, "ticker": setup["ticker"],
          "yahoo": setup["yahoo"], "qty": qty, "entry": entry, "stop": stop, "target": target,
          "date": today, "opened_at": now.isoformat(timespec="seconds"), "setup": setup,
          "replay": plan["replay"], "buy_order_id": order_id, "stop_id": None}
    state["open"] = op
    fee = mk.get("fx_fee_pct", 0) / 100
    max_loss = to_eur((r + fee * (entry + stop)) * qty, mk, fx)
    if r <= 0 or max_loss > cfg["risk_per_trade_eur"] * 1.25:
        log("fill too far from plan - selling straight away", entry=entry, stop=stop, qty=qty)
        sell_now(state, cfg, mk, "fill too far from plan", entry)
        return True
    s, d = t212("POST", "/orders/stop", {"ticker": op["ticker"], "quantity": -qty,
                                          "stopPrice": stop, "timeValidity": "DAY"})
    if s != 200:
        log("STOP REJECTED - selling straight away", status=s, resp=d)
        sell_now(state, cfg, mk, "stop could not be placed", entry)
        return True
    op["stop_id"] = d["id"]
    log("TRADE OPENED", market=mk["name"], ticker=op["ticker"], qty=qty, entry=entry, stop=stop,
        target=target, currency=mk["currency"], max_loss_eur=round(max_loss, 2),
        value_eur=round(to_eur(entry * qty, mk, fx), 2), replay=plan["replay"])
    return True


# ---------------------------------------------------------------- exits
def last_sell_fill(ticker, since):
    s, d = t212("GET", f"/history/orders?ticker={ticker}&limit=10")
    if s != 200 or not isinstance(d, dict):
        return None
    for it in d.get("items", []):
        o, f = it.get("order", {}), it.get("fill") or {}
        if o.get("side") == "SELL" and f.get("price") and (f.get("filledAt") or "") >= since:
            return f["price"]
    return None


def close_trade(state, mk, exit_price, reason):
    op = state["open"]
    fee = mk.get("fx_fee_pct", 0) / 100
    pnl_ccy = (exit_price - op["entry"]) * op["qty"] - fee * (op["entry"] + exit_price) * op["qty"]
    pnl = to_eur(pnl_ccy, mk, op.get("fx") or 1.0)
    op.update({"exit": exit_price, "pnl_eur": round(pnl, 2), "exit_reason": reason,
               "closed_at": dt.datetime.now(CET).isoformat(timespec="seconds")})
    state["trades"].append(op)
    state["open"] = None
    log("TRADE CLOSED", ticker=op["ticker"], reason=reason, entry=op["entry"], exit=exit_price,
        qty=op["qty"], pnl_eur=round(pnl, 2))


def sell_now(state, cfg, mk, reason, price):
    op = state["open"]
    if op.get("stop_id"):
        s, _ = t212("DELETE", f"/orders/{op['stop_id']}")
        log("stop cancelled", status=s, quiet=True)
        time.sleep(2)
    s, d = t212("POST", "/orders/market", {"ticker": op["ticker"], "quantity": -op["qty"]})
    if s != 200:
        log("SELL FAILED - restoring stop", status=s, resp=d)
        s2, d2 = t212("POST", "/orders/stop", {"ticker": op["ticker"], "quantity": -op["qty"],
                                                "stopPrice": op["stop"], "timeValidity": "DAY"})
        op["stop_id"] = d2["id"] if s2 == 200 else None
        return
    time.sleep(5)
    close_trade(state, mk, last_sell_fill(op["ticker"], op["date"]) or price, reason)


def manage_open(state, cfg, now, pos):
    op = state["open"]
    mk = cfg["markets"][op.get("market", "EU")]
    p = pos.get(op["ticker"])
    if not p or p["quantity"] < op["qty"] * 0.999:
        close_trade(state, mk, last_sell_fill(op["ticker"], op["date"]) or op["stop"], "stop-loss filled")
        return
    price = p["currentPrice"]
    now_l = now.astimezone(ZoneInfo(mk["tz"]))
    flat = hm(mk["flat_by"])
    if mk["name"] == "US" and now_l.date().isoformat() in NYSE_EARLY_CLOSE:
        flat = min(flat, dt.time(12, 40))
    reason = None
    if price >= op["target"]:
        reason = "target reached"
    elif now_l.time() >= flat or now_l.date().isoformat() != op["date"]:
        reason = "flat by close"
    if not reason:
        log("holding", quiet=True, ticker=op["ticker"], price=price, entry=op["entry"], stop=op["stop"],
            target=op["target"], open_pnl=round((price - op["entry"]) * op["qty"], 2))
        return
    if DRY:
        log("DRY RUN: would sell", reason=reason, price=price)
        return
    sell_now(state, cfg, mk, reason, price)


# ---------------------------------------------------------------- one pass
def realised_in_window(state, cfg, today):
    start = today - dt.timedelta(days=cfg["loss_cap_window_days"])
    return round(sum(t.get("pnl_eur", 0) for t in state["trades"]
                     if dt.date.fromisoformat(t["date"]) > start), 2)


def one_pass(cfg, state, now, max_trades, day_cap=None):
    pos = positions()
    if state["open"]:
        manage_open(state, cfg, now, pos)
        if state["open"]:
            return "holding"
    today = now.date().isoformat()
    realised = realised_in_window(state, cfg, now.date())
    if realised <= -cfg["loss_cap_eur"]:
        cfg["enabled"] = False
        save(CFG, cfg)
        log("LOSS CAP HIT - autopilot switched off", realised_eur=realised)
        return "switched off (loss cap)"
    day_pnl = sum(t.get("pnl_eur", 0) for t in state["trades"] if t["date"] == today)
    if day_cap is not None and day_pnl <= -day_cap:
        return "day loss cap reached"
    if state["day_entries"].get(today, 0) >= max_trades:
        return "done for today"
    traded_today = {t["ticker"] for t in state["trades"] if t["date"] == today}
    fx = None
    for name, mk in cfg["markets"].items():
        mk = {**mk, "name": name}
        if not mk.get("enabled", True) or now.date() < dt.date.fromisoformat(mk.get("start_date", "2000-01-01")):
            continue
        now_l = now.astimezone(ZoneInfo(mk["tz"]))
        if now_l.weekday() >= 5:
            continue
        if name == "US" and now_l.date().isoformat() in NYSE_EARLY_CLOSE:
            continue                                  # half days: not enough session left
        a, b = hm(mk["entry"][0]), hm(mk["entry"][1])
        if not (a <= now_l.time() <= b):
            continue
        fx = fx or eurusd()
        if not fx:
            log("no EURUSD rate - skipping", quiet=True)
            return "no fx rate"
        picks = [p for p in find_setups(cfg, mk, now, set(pos)) if p["ticker"] not in traded_today]
        if not picks:
            log("no setup", quiet=True, market=name)
            continue
        log("setups", quiet=True, market=name, top=[{k: p[k] for k in ("yahoo", "signal_close", "stop", "chg_pct", "bar")}
                                                    for p in picks[:3]])
        for setup in picks[:3]:                       # replay-check the best three, take the first that passes
            if enter(state, cfg, mk, now, setup, fx):
                return "opened" if state["open"] else "entry attempted"
        return "no setup passed the replay check"
    return "no market in its entry window"


def main():
    cfg = load(CFG, None)
    state = load(STATE, {"open": None, "trades": [], "day_entries": {}})
    now = dt.datetime.now(CET)
    loop = "--loop" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    max_trades = int(args[0]) if loop and args else (cfg or {}).get("max_trades_per_day", 1)
    day_cap = float(args[1]) if loop and len(args) > 1 else None
    status = "idle"
    try:
        if not cfg or not cfg.get("enabled"):
            status = "disabled"
        elif now.date() < dt.date.fromisoformat(cfg["start_date"]):
            status = "not started yet"
        else:
            while True:
                now = dt.datetime.now(CET)
                status = one_pass(cfg, state, now, max_trades, day_cap)
                save(STATE, state)
                if not loop:
                    break
                if not state["open"] and status in ("done for today", "day loss cap reached",
                                                    "switched off (loss cap)", "no market in its entry window"):
                    break
                time.sleep(20 if state["open"] else 240)   # watch a position closely, rescan every 4 min
    except Exception as e:  # never crash silently
        log("ERROR", error=str(e)[:300])
        status = "error"
    finally:
        save(STATE, state)
        realised = realised_in_window(state, cfg, now.date()) if (cfg and state["trades"]) else 0
        op = state["open"]
        print(json.dumps({"summary": status, "time_cet": now.strftime("%Y-%m-%d %H:%M"),
                          "open": op and {k: op[k] for k in ("market", "ticker", "qty", "entry", "stop", "target")},
                          "trades_total": len(state["trades"]), "realised_eur": realised,
                          "events": [e["msg"] for e in EVENTS]}))


if __name__ == "__main__":
    main()
