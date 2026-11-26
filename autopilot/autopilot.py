#!/usr/bin/env python3
"""Autopilot: one small, rules-bound day trade a day on Trading 212 (EU stocks, EUR).

The user gave standing permission to trade inside the limits in trader/autopilot.json.
Hard rules enforced here, not left to judgment:
  - only money inside budget_eur, at most risk_per_trade_eur lost at the stop
  - max_trades_per_day entries; no entry outside entry_window_cet
  - every entry gets a sell stop immediately; if the stop can't be placed, sell at once
  - everything flat by flat_by_cet; never touches tickers the account already holds
  - stops itself (enabled=false) once realised losses in the window reach loss_cap_eur
Strategy: 30-minute opening-range breakout (09:05-09:30 CET, opening print skipped),
entry on a completed 5-minute close above the range high, stop at the range midpoint,
target at target_r x risk. Data: free Yahoo 5-minute bars (about 15 minutes delayed).

Run: python3 autopilot/autopilot.py [--dry-run]   (Trading 212 key is injected by the proxy)
Prints one JSON summary line last.
"""
import datetime as dt
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.path.join(ROOT, "trader", "autopilot.json")
STATE = os.path.join(ROOT, "trader", "autopilot_state.json")
LOG = os.path.join(ROOT, "trader", "autopilot_log.jsonl")
UNIVERSE = os.path.join(ROOT, "autopilot", "eu_universe.json")
T212 = "https://live.trading212.com/api/v0/equity"
CET = ZoneInfo("Europe/Berlin")
DRY = "--dry-run" in sys.argv
EVENTS = []


def log(msg, **kw):
    rec = {"ts": dt.datetime.now(CET).isoformat(timespec="seconds"), "msg": msg, **kw}
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
    for attempt in range(3):
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


def bars(symbol):
    s, d = http("GET", f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1d&interval=5m")
    if s != 200:
        return None
    try:
        r = d["chart"]["result"][0]
        q = r["indicators"]["quote"][0]
        rows = [(dt.datetime.fromtimestamp(t, CET), o, h, l, c, v or 0)
                for t, o, h, l, c, v in zip(r["timestamp"], q["open"], q["high"], q["low"], q["close"], q["volume"])
                if c is not None and h is not None and l is not None]
        return {"prev": r["meta"].get("chartPreviousClose"), "rows": rows}
    except (KeyError, IndexError, TypeError):
        return None


def last_sell_fill(ticker, since_iso):
    s, d = t212("GET", f"/history/orders?ticker={ticker}&limit=10")
    if s != 200:
        return None
    for it in d.get("items", []):
        o, f = it.get("order", {}), it.get("fill") or {}
        if o.get("side") == "SELL" and f.get("price") and (f.get("filledAt") or "") >= since_iso:
            return f["price"]
    return None


def realised_in_window(state, cfg, today):
    start = today - dt.timedelta(days=cfg["loss_cap_window_days"])
    return round(sum(t.get("pnl_eur", 0) for t in state["trades"]
                     if dt.date.fromisoformat(t["date"]) > start), 2)


def close_trade(state, pnl, exit_price, reason):
    op = state["open"]
    op.update({"exit": exit_price, "pnl_eur": round(pnl, 2), "exit_reason": reason,
               "closed_at": dt.datetime.now(CET).isoformat(timespec="seconds")})
    state["trades"].append(op)
    state["open"] = None
    log("TRADE CLOSED", ticker=op["ticker"], reason=reason, entry=op["entry"], exit=exit_price,
        qty=op["qty"], pnl_eur=round(pnl, 2))


def manage_open(state, cfg, now, pos):
    op = state["open"]
    p = pos.get(op["ticker"])
    if not p or p["quantity"] < op["qty"] * 0.999:
        fill = last_sell_fill(op["ticker"], op["opened_at"][:10]) or op["stop"]
        close_trade(state, (fill - op["entry"]) * op["qty"], fill, "stop-loss filled")
        return
    price = p["currentPrice"]
    flat_by = dt.time.fromisoformat(cfg["flat_by_cet"])
    reason = None
    if price >= op["target"]:
        reason = "target reached"
    elif now.time() >= flat_by or now.date().isoformat() != op["date"]:
        reason = "flat by close"
    if not reason:
        log("holding", ticker=op["ticker"], price=price, entry=op["entry"], stop=op["stop"],
            target=op["target"], open_pnl_eur=round((price - op["entry"]) * op["qty"], 2))
        return
    if DRY:
        log("DRY RUN: would sell", reason=reason, price=price)
        return
    if op.get("stop_id"):
        s, d = t212("DELETE", f"/orders/{op['stop_id']}")
        log("stop cancelled", status=s)
        time.sleep(2)
    s, d = t212("POST", "/orders/market", {"ticker": op["ticker"], "quantity": -op["qty"]})
    if s != 200:
        log("SELL FAILED - stop may be cancelled, needs attention", status=s, resp=d)
        # put the protective stop back so the position is never naked
        s2, d2 = t212("POST", "/orders/stop", {"ticker": op["ticker"], "quantity": -op["qty"],
                                                "stopPrice": op["stop"], "timeValidity": "DAY"})
        if s2 == 200:
            op["stop_id"] = d2["id"]
        return
    time.sleep(5)
    fill = last_sell_fill(op["ticker"], op["opened_at"][:10]) or price
    close_trade(state, (fill - op["entry"]) * op["qty"], fill, reason)


def find_setup(cfg, now, held):
    universe = load(UNIVERSE, {})
    picks = []
    for ysym, ticker in universe.items():
        if ticker in held:
            continue
        b = bars(ysym)
        time.sleep(0.3)
        if not b or not b["prev"]:
            continue
        rows = [r for r in b["rows"] if r[0].date() == now.date()]
        orng = [r for r in rows if dt.time(9, 5) <= r[0].time() < dt.time(9, 30)]
        done = [r for r in rows if r[0] + dt.timedelta(minutes=5) <= now - dt.timedelta(minutes=1)]
        if len(orng) < 4 or len(done) < 8:
            continue
        hi, lo = max(r[2] for r in orng), min(r[3] for r in orng)
        last = done[-1]
        close = last[4]
        if close < cfg["min_price_eur"] or close <= b["prev"]:
            continue
        if not (hi < close <= hi * 1.01):          # fresh breakout, not chased
            continue
        stop = round(math.floor((hi + lo) / 2 * 100) / 100, 2)
        risk_ps = close - stop
        if not (0.003 * close <= risk_ps <= 0.025 * close):
            continue
        value = sum(r[4] * r[5] for r in rows)
        picks.append({"yahoo": ysym, "ticker": ticker, "signal_close": close, "or_high": hi,
                      "or_low": lo, "stop": stop, "bar": last[0].strftime("%H:%M"),
                      "chg_pct": round(100 * (close / b["prev"] - 1), 2), "traded_value": value})
    picks.sort(key=lambda p: -p["traded_value"])
    return picks


def enter(state, cfg, now, setup):
    budget, risk = cfg["budget_eur"], cfg["risk_per_trade_eur"]
    px = setup["signal_close"]
    risk_ps = px - setup["stop"]
    # 0.75 of the risk budget, to leave room for delayed data and slippage
    qty = math.floor(min(0.75 * risk / risk_ps, 0.95 * budget / px) * 100) / 100
    if qty * px < 10:
        log("setup too small to trade", **setup)
        return
    if DRY:
        log("DRY RUN: would buy", qty=qty, **setup)
        return
    s, d = t212("POST", "/orders/market", {"ticker": setup["ticker"], "quantity": qty})
    if s != 200:
        log("BUY REJECTED", status=s, resp=d, qty=qty, **setup)
        state["day_entries"][now.date().isoformat()] = state["day_entries"].get(now.date().isoformat(), 0) + 1
        return
    order_id = d.get("id")
    state["day_entries"][now.date().isoformat()] = state["day_entries"].get(now.date().isoformat(), 0) + 1
    p = None
    for _ in range(12):
        time.sleep(5)
        p = positions().get(setup["ticker"])
        if p and p["quantity"] >= qty * 0.999:
            break
    if not p:
        log("BUY NOT FILLED after 60s - cancelling", order_id=order_id)
        t212("DELETE", f"/orders/{order_id}")
        return
    entry = p["averagePricePaid"]
    stop = setup["stop"]
    op = {"ticker": setup["ticker"], "yahoo": setup["yahoo"], "qty": qty, "entry": entry,
          "stop": stop, "target": round(entry + cfg["target_r"] * (entry - stop), 2),
          "date": now.date().isoformat(), "opened_at": now.isoformat(timespec="seconds"),
          "setup": setup, "buy_order_id": order_id, "stop_id": None}
    state["open"] = op
    if entry <= stop or (entry - stop) * qty > risk * 1.25:
        log("fill too far from plan - selling straight away", entry=entry, stop=stop, qty=qty)
        s, d = t212("POST", "/orders/market", {"ticker": op["ticker"], "quantity": -qty})
        time.sleep(5)
        fill = last_sell_fill(op["ticker"], op["date"]) or entry
        close_trade(state, (fill - entry) * qty, fill, "fill too far from plan")
        return
    s, d = t212("POST", "/orders/stop", {"ticker": op["ticker"], "quantity": -qty,
                                          "stopPrice": stop, "timeValidity": "DAY"})
    if s != 200:
        log("STOP REJECTED - selling straight away", status=s, resp=d)
        t212("POST", "/orders/market", {"ticker": op["ticker"], "quantity": -qty})
        time.sleep(5)
        fill = last_sell_fill(op["ticker"], op["date"]) or entry
        close_trade(state, (fill - entry) * qty, fill, "stop could not be placed")
        return
    op["stop_id"] = d["id"]
    log("TRADE OPENED", ticker=op["ticker"], qty=qty, entry=entry, stop=stop, target=op["target"],
        max_loss_eur=round((entry - stop) * qty, 2), value_eur=round(entry * qty, 2))


def main():
    cfg = load(CFG, None)
    state = load(STATE, {"open": None, "trades": [], "day_entries": {}})
    now = dt.datetime.now(CET)
    status = "idle"
    try:
        if not cfg or not cfg.get("enabled"):
            status = "disabled"
            return
        if now.date() < dt.date.fromisoformat(cfg["start_date"]) or now.weekday() >= 5:
            status = "not a trading day for autopilot"
            return
        pos = positions()
        if state["open"]:
            manage_open(state, cfg, now, pos)
            if state["open"]:
                status = "holding"
                return
        realised = realised_in_window(state, cfg, now.date())
        if realised <= -cfg["loss_cap_eur"]:
            cfg["enabled"] = False
            save(CFG, cfg)
            log("LOSS CAP HIT - autopilot switched off", realised_eur=realised)
            status = "switched off (loss cap)"
            return
        if state["day_entries"].get(now.date().isoformat(), 0) >= cfg["max_trades_per_day"]:
            status = "done for today"
            return
        a, b = (dt.time.fromisoformat(x) for x in cfg["entry_window_cet"])
        if not (a <= now.time() <= b):
            status = "outside entry window"
            return
        picks = find_setup(cfg, now, set(pos))
        if not picks:
            status = "no setup"
            log("no setup", checked=len(load(UNIVERSE, {})))
            return
        log("setups", top=[{k: p[k] for k in ("yahoo", "signal_close", "or_high", "stop", "chg_pct", "bar")}
                           for p in picks[:3]])
        enter(state, cfg, now, picks[0])
        status = "opened" if state["open"] else "entry attempted"
    except Exception as e:  # never crash silently
        log("ERROR", error=str(e)[:300])
        status = "error"
    finally:
        save(STATE, state)
        realised = realised_in_window(state, cfg or {"loss_cap_window_days": 31}, now.date()) if state["trades"] else 0
        print(json.dumps({"summary": status, "time_cet": now.strftime("%Y-%m-%d %H:%M"),
                          "open": state["open"] and {k: state["open"][k] for k in ("ticker", "qty", "entry", "stop", "target")},
                          "trades_total": len(state["trades"]), "realised_eur": realised,
                          "events": [e["msg"] for e in EVENTS]}))


if __name__ == "__main__":
    main()
