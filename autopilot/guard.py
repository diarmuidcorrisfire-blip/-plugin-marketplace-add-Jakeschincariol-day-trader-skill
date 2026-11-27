#!/usr/bin/env python3
"""Position guard + trade journal for trades the user makes themselves in the Trading 212 app.

Watches positions that are NEW compared with the baseline (the holdings that existed when the guard
was set up) or that grew since then. Alerts once at +PROFIT_EUR and once at -LOSS_EUR unrealised P&L
(Trading 212's own EUR figure, FX included). When a watched position is closed, journals the real
result after fees from the order history. Never places or cancels orders.

  python3 autopilot/guard.py watch     # loop every 60 s; exits with an ALERT/CLOSED line so Claude can notify
  python3 autopilot/guard.py journal   # print the journal summary
Files: trader/guard_state.json, trader/journal.json. Skips positions opened by autopilot / today_us.
"""
import datetime as dt
import json
import os
import sys
import time
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autopilot as ap  # noqa: E402

ROOT = ap.ROOT
STATE = os.path.join(ROOT, "trader", "guard_state.json")
JOURNAL = os.path.join(ROOT, "trader", "journal.json")
CET = ZoneInfo("Europe/Berlin")
PROFIT_EUR, LOSS_EUR = 5.0, 10.0


def bot_tickers():
    out = set()
    for f in ("autopilot_state.json", "today_us_state.json"):
        s = ap.load(os.path.join(ROOT, "trader", f), {})
        if s.get("open"):
            out.add(s["open"]["ticker"])
    return out


def load_state(pos):
    st = ap.load(STATE, None)
    if st is None:
        st = {"baseline": {t: p["quantity"] for t, p in pos.items()}, "watched": {}, "created": dt.datetime.now(CET).isoformat(timespec="seconds")}
        ap.save(STATE, st)
    return st


def closed_result(ticker, since_iso):
    s, d = ap.t212("GET", f"/history/orders?ticker={ticker}&limit=10")
    if s != 200 or not isinstance(d, dict):
        return None
    out, paid = None, 0.0
    for it in d.get("items", []):
        o, f = it.get("order", {}), it.get("fill") or {}
        if o.get("status") != "FILLED" or (f.get("filledAt") or "") < since_iso[:16]:
            continue
        w = f.get("walletImpact") or {}
        if o.get("side") == "SELL" and out is None:
            out = {"sell_price": f.get("price"), "sold_at": f.get("filledAt"), "net_value_eur": w.get("netValue"),
                   "realised_before_fees_eur": w.get("realisedProfitLoss"),
                   "sell_fees_eur": sum(t.get("quantity", 0) for t in w.get("taxes", []))}
        elif o.get("side") == "BUY":
            paid += w.get("netValue") or 0.0          # what the user actually paid, buy FX fee included
    if out is not None:
        out["paid_eur"] = paid or None
    return out


def tick():
    pos = ap.positions()
    st = load_state(pos)
    events = []
    bots = bot_tickers()
    now = dt.datetime.now(CET).isoformat(timespec="seconds")
    # new or grown positions become watched
    for t, p in pos.items():
        if t in bots:
            continue
        base = st["baseline"].get(t, 0.0)
        w = st["watched"].get(t)
        if p["quantity"] > base * 1.001 + 1e-6 and not w:
            st["watched"][t] = {"name": p["instrument"]["name"], "since": p.get("createdAt") or now, "qty": p["quantity"],
                                "avg": p["averagePricePaid"], "cost_eur": p["walletImpact"]["totalCost"],
                                "profit_alert": False, "loss_alert": False}
            events.append(f"WATCHING {p['instrument']['name']} ({t}): {p['quantity']:.4g} @ {p['averagePricePaid']:.2f}, "
                          f"cost EUR {p['walletImpact']['totalCost']:.2f}; alerts at +EUR {PROFIT_EUR:g} / -EUR {LOSS_EUR:g}")
    # alerts and closes
    for t, w in list(st["watched"].items()):
        p = pos.get(t)
        if not p or p["quantity"] <= st["baseline"].get(t, 0.0) * 1.001 + 1e-6:
            res = closed_result(t, w["since"][:19]) or {}
            paid = res.get("paid_eur") or w["cost_eur"]
            net = res["net_value_eur"] - paid if res.get("net_value_eur") else None
            entry = {"ticker": t, "name": w["name"], "opened": w["since"], "closed": res.get("sold_at") or now,
                     "cost_eur": res.get("paid_eur") or w["cost_eur"], "proceeds_eur": res.get("net_value_eur"),
                     "profit_after_fees_eur": round(net, 2) if net is not None else None}
            j = ap.load(JOURNAL, {"trades": []}); j["trades"].append(entry); ap.save(JOURNAL, j)
            del st["watched"][t]
            events.append(f"CLOSED {w['name']}: profit after fees EUR {entry['profit_after_fees_eur']}")
            continue
        pl = p["walletImpact"]["unrealizedProfitLoss"]
        if pl >= PROFIT_EUR and not w["profit_alert"]:
            w["profit_alert"] = True
            events.append(f"ALERT {w['name']} up EUR {pl:.2f} (price {p['currentPrice']}); selling costs ~0.15% FX if US")
        if pl <= -LOSS_EUR and not w["loss_alert"]:
            w["loss_alert"] = True
            events.append(f"ALERT {w['name']} down EUR {pl:.2f} (price {p['currentPrice']}) - your loss limit")
    ap.save(STATE, st)
    return events


def watch():
    while True:
        try:
            ev = tick()
        except Exception as e:
            ev = []
            print("error", str(e)[:200], flush=True)
        if ev:
            for e in ev:
                print(e, flush=True)
            return
        time.sleep(60)


def journal():
    j = ap.load(JOURNAL, {"trades": []})
    done = [t for t in j["trades"] if t.get("profit_after_fees_eur") is not None]
    tot = sum(t["profit_after_fees_eur"] for t in done)
    print(json.dumps({"trades": len(done), "wins": sum(t["profit_after_fees_eur"] > 0 for t in done),
                      "total_after_fees_eur": round(tot, 2), "last": j["trades"][-5:]}, indent=1))


if __name__ == "__main__":
    {"watch": watch, "journal": journal}[sys.argv[1] if len(sys.argv) > 1 else "journal"]()
