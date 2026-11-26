#!/usr/bin/env python3
"""Paper trading for autopilot: pretend buys and sells, real prices, no broker.

Uses autopilot.py's own scan, sizing and replay check (the Time Machine method) and the
backtest's exit rules: a trade exits at the stop if a bar's low reaches it (at the bar's open
if it gapped through), at the target if a bar's high reaches it, else at the market's flat time.
Results are kept at EUR 2 risk per trade and shown scaled to bigger sizes.

  python3 autopilot/paper.py backfill [DAYS]   # replay the last DAYS sessions (default 35)
  python3 autopilot/paper.py run               # hourly: open/close today's paper trades
  python3 autopilot/paper.py stats             # print the scoreboard
Book: trader/paper_state.json. Markets: EU and US, one paper trade a day per market.
"""
import datetime as dt
import json
import math
import os
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autopilot as ap  # noqa: E402

ROOT = ap.ROOT
BOOK = os.path.join(ROOT, "trader", "paper_state.json")
CFG = ap.load(os.path.join(ROOT, "trader", "autopilot.json"), {})
FX_DEFAULT = 1.10
SIZES = (2, 5, 20, 50)
_cache = {}
_orig_chart = ap.chart


def chart(sym, rng="1d"):
    key = (sym, rng)
    if key not in _cache:
        _cache[key] = _orig_chart(sym, rng)
    return _cache[key]


ap.chart = chart


class _NoSleep:
    def __getattr__(self, k):
        import time as _t
        return (lambda *a, **kw: None) if k == "sleep" else getattr(_t, k)


ap.time = _NoSleep()                              # no polite pauses needed on cached data
ap.log = lambda msg, quiet=False, **kw: None      # keep the live log clean


def markets():
    for name, mk in CFG["markets"].items():
        yield name, {**mk, "name": name, "enabled": True}


def load_book():
    return ap.load(BOOK, {"trades": [], "open": {}, "backfilled_through": None})


def save_book(b):
    tmp = BOOK + ".tmp"
    with open(tmp, "w") as f:
        json.dump(b, f, indent=1)
    os.replace(tmp, BOOK)


def day_bars(mk, ysym, d):
    rows = chart(ysym, "60d")
    if not rows:
        return []
    tz = ZoneInfo(mk["tz"])
    return [(r[0].astimezone(tz),) + r[1:] for r in rows["rows"] if r[0].astimezone(tz).date() == d]


def exit_trade(mk, tr, bars_after, flat):
    """Walk bars after entry: stop first (gap -> open), then target, else flat time / last bar."""
    for b in bars_after:
        lt, o, h, l, c = b[0], b[1], b[2], b[3], b[4]
        if l <= tr["stop"]:
            return min(tr["stop"], o), "stop", lt
        if h >= tr["target"]:
            return tr["target"], "target", lt
        if lt.time() >= flat:
            return c, "close", lt
    return None


def finish(tr, mk, exit_p, why, when):
    fee = mk.get("fx_fee_pct", 0) / 100
    fx = tr["fx"] if mk["currency"] == "USD" else 1.0
    pnl_ccy = (exit_p - tr["entry"]) * tr["qty"] - fee * (tr["entry"] + exit_p) * tr["qty"]
    tr.update({"exit": round(exit_p, 4), "why": why, "closed": when.strftime("%Y-%m-%d %H:%M"),
               "pnl_eur": round(pnl_ccy / fx, 3), "r": round((exit_p - tr["entry"]) / (tr["entry"] - tr["stop"]), 2)})
    return tr


def try_day(name, mk, d, now_l, held, fx, decided_at=None):
    """Find the first setup on day d as of now_l that passes the replay check. Returns a trade or None."""
    picks = ap.find_setups(CFG, mk, now_l.astimezone(ap.CET), held)
    for setup in picks[:3]:
        plan = ap.plan_trade({**CFG, "budget_eur": CFG["budget_eur"]}, mk, now_l.astimezone(ap.CET), setup, fx)
        if not plan:
            continue
        return {"market": name, "date": d.isoformat(), "ticker": setup["ticker"], "yahoo": setup["yahoo"],
                "opened": now_l.strftime("%Y-%m-%d %H:%M"), "entry": setup["signal_close"] * 1.0002,
                "stop": setup["stop"], "target": plan["target"], "qty": plan["qty"], "fx": fx,
                "replay": plan["replay"], "chg_pct": setup["chg_pct"]}
    return None


# ------------------------------------------------------------------ backfill
def backfill(days):
    """Historical paper trades with the replay check, using only data available at the time."""
    book = load_book()
    book["trades"] = [t for t in book["trades"] if t.get("source") != "backfill"]
    real_held = set(ap.positions())          # same exclusions as live (read-only call)
    fx = ap.eurusd() or FX_DEFAULT
    for name, mk in markets():
        tz = ZoneInfo(mk["tz"])
        uni = ap.load(os.path.join(ROOT, mk["universe"]), {})
        alld = sorted({r[0].astimezone(tz).date() for s in uni if (c := chart(s, "60d")) for r in c["rows"]})
        # index each symbol's 60-day bars by local date once: (start, end) offsets
        idx = {}
        for s in uni:
            c = chart(s, "60d")
            if not c:
                continue
            spans, cur, start = {}, None, 0
            for i, r in enumerate(c["rows"]):
                ld = r[0].astimezone(tz).date()
                if ld != cur:
                    if cur is not None:
                        spans[cur] = (start, i)
                    cur, start = ld, i
            if cur is not None:
                spans[cur] = (start, len(c["rows"]))
            idx[s] = (c["rows"], spans)
        for d in [x for x in alld if x < dt.datetime.now(tz).date()][-days:]:
            # pretend 1d charts for day d by slicing the 60d data
            def chart_day(sym, rng="1d", d=d):
                if sym not in idx:
                    return None
                rows, spans = idx[sym]
                if d not in spans:
                    return None
                a0, a1 = spans[d]
                if rng != "1d":
                    return {"prev": None, "rows": rows[:a0]}
                return {"prev": rows[a0 - 1][4] if a0 else None, "rows": rows[a0:a1]}
            ap.chart = chart_day
            trade = None
            a, b = ap.hm(mk["entry"][0]), ap.hm(mk["entry"][1])
            t0 = dt.datetime.combine(d, a, tz)
            while t0.time() <= b and not trade:         # check every 30 minutes, like a patient hourly run
                trade = try_day(name, mk, d, t0, real_held, fx)
                if not trade:
                    t0 += dt.timedelta(minutes=30)
            ap.chart = chart
            if not trade:
                continue
            bars = day_bars(mk, trade["yahoo"], d)
            after = [x for x in bars if x[0] > t0]
            res = exit_trade(mk, trade, after, ap.hm(mk["flat_by"]))
            if res:
                trade["source"] = "backfill"
                book["trades"].append(finish(trade, mk, *res))
            print(name, d, trade["yahoo"], trade.get("why"), trade.get("pnl_eur"), flush=True)
    book["backfilled_through"] = dt.date.today().isoformat()
    book["trades"].sort(key=lambda t: t["opened"])
    save_book(book)


# ------------------------------------------------------------------ live paper
def run():
    book = load_book()
    now = dt.datetime.now(ap.CET)
    real_held = set(ap.positions())
    fx = None
    for name, mk in markets():
        tz = ZoneInfo(mk["tz"])
        now_l = now.astimezone(tz)
        today = now_l.date().isoformat()
        op = book["open"].get(name)
        if op:
            bars = [b for b in day_bars(mk, op["yahoo"], dt.date.fromisoformat(op["date"]))
                    if b[0].strftime("%Y-%m-%d %H:%M") > op["opened"]]
            done = [b for b in bars if b[0] + dt.timedelta(minutes=5) <= now_l]
            res = exit_trade(mk, op, done, ap.hm(mk["flat_by"]))
            if not res and (now_l.date().isoformat() != op["date"] or now_l.time() >= ap.hm(mk["flat_by"])) and done:
                res = (done[-1][4], "close", done[-1][0])
            if res:
                op["source"] = "live"
                book["trades"].append(finish(op, mk, *res))
                del book["open"][name]
                print("PAPER CLOSED", name, op["yahoo"], op["why"], op["pnl_eur"])
            continue
        if now_l.weekday() >= 5 or (name == "US" and today in ap.NYSE_EARLY_CLOSE):
            continue
        if any(t["market"] == name and t["date"] == today for t in book["trades"]):
            continue
        a, b = ap.hm(mk["entry"][0]), ap.hm(mk["entry"][1])
        if not (a <= now_l.time() <= b):
            continue
        fx = fx or ap.eurusd() or FX_DEFAULT
        tr = try_day(name, mk, now_l.date(), now_l, real_held, fx)
        if tr:
            book["open"][name] = tr
            print("PAPER OPENED", name, tr["yahoo"], round(tr["entry"], 3), "stop", tr["stop"], "target", tr["target"])
    save_book(book)
    stats()


def stats():
    book = load_book()
    tr = book["trades"]
    out = {"open": {k: {x: v[x] for x in ("yahoo", "entry", "stop", "target")} for k, v in book["open"].items()}}
    for label, sel in (("all", tr), ("EU", [t for t in tr if t["market"] == "EU"]), ("US", [t for t in tr if t["market"] == "US"])):
        if not sel:
            out[label] = {"trades": 0}
            continue
        pnl = [t["pnl_eur"] for t in sel]
        eq = peak = dd = 0.0
        for p in pnl:
            eq += p; peak = max(peak, eq); dd = min(dd, eq - peak)
        out[label] = {"trades": len(sel), "win_rate": round(sum(p > 0 for p in pnl) / len(pnl), 3),
                      "total_at_eur2": round(sum(pnl), 2), "avg_at_eur2": round(sum(pnl) / len(pnl), 3),
                      "worst_drawdown_at_eur2": round(dd, 2),
                      "scaled_total": {f"risk_eur_{s}": round(sum(pnl) * s / 2, 2) for s in SIZES},
                      "exits": {k: sum(1 for t in sel if t["why"] == k) for k in ("target", "stop", "close")}}
    print(json.dumps(out, indent=1))
    return out


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    if cmd == "backfill":
        backfill(int(sys.argv[2]) if len(sys.argv) > 2 else 35)
        stats()
    elif cmd == "run":
        run()
    else:
        stats()
