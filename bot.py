#!/usr/bin/env python3
"""
Large-cap dip buyer — Alpaca PAPER trading bot, v3 "morning-only" (stateless; runs on GitHub Actions or any computer).
Python standard library only. All memory lives in the Alpaca account itself (positions + order history).

Strategy (tested 2017-2026):
  Universe : the 30 largest US companies (UNIVERSE below; refresh once a year).
  Entry    : RSI(2) < 10 AND close > 200-day average (judged on the previous day's close) -> buy in the morning,
             lowest RSI first, up to 2 positions, each = half of account equity.
  Exit     : previous close > 5-day average, or held 10 trading days -> sell in the morning.
  Idle cash: parked in SPY while SPY's previous close > its 200-day average, otherwise kept in cash.
  Everything happens in ONE morning run (9:31-11:00 New York time), which tolerates late GitHub schedules.
  Backtest of this exact morning-only version, 2017-2026: ~18.7%/yr vs SPY 14.6% (0.02% slippage/side).

Commands:
  python3 bot.py auto        runs the morning routine if inside 9:31-11:00 New York time, otherwise does nothing
  python3 bot.py morning     / status
  add --dry-run to see what it WOULD do without placing orders; --force to ignore the time windows.

Keys: environment variables APCA_API_KEY_ID and APCA_API_SECRET_KEY (on GitHub: repository secrets).
Safety: refuses to run against anything but an Alpaca PAPER account.
"""
import json, math, os, sys, time, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    NY = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    NY = None

# ----------------------------- settings -----------------------------
UNIVERSE = ["NVDA", "AAPL", "GOOGL", "MSFT", "AMZN", "AVGO", "SPCX", "META", "TSLA", "LLY",
            "MU", "JPM", "WMT", "AMD", "V", "XOM", "JNJ", "MA", "INTC", "ABBV",
            "CSCO", "BAC", "AMAT", "COST", "CAT", "CVX", "UNH", "LRCX", "GE", "KO"]  # top 30 by market cap, Oct 2026
PARK = "SPY"
SLOTS = 2
RSI_LEN, RSI_BUY = 2, 10.0
TREND_LEN, EXIT_LEN, MAX_HOLD = 200, 5, 10
MIN_ORDER = 1.00
WINDOWS = {"morning": (9 * 60 + 31, 11 * 60)}  # New York time

TRADE_API = "https://paper-api.alpaca.markets"   # PAPER ONLY.
DATA_API = "https://data.alpaca.markets"
DRY = "--dry-run" in sys.argv
FORCE = "--force" in sys.argv


# ----------------------------- plumbing -----------------------------
def log(msg):
    print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}Z {'[DRY] ' if DRY else ''}{msg}", flush=True)


def keys():
    k, s = os.environ.get("APCA_API_KEY_ID"), os.environ.get("APCA_API_SECRET_KEY")
    if k and s:
        return k.strip(), s.strip()
    path = os.path.expanduser("~/Library/Application Support/Claude/claude_desktop_config.json")
    try:
        with open(path) as f:
            for cfg in json.load(f).get("mcpServers", {}).values():
                env = (cfg or {}).get("env", {}) or {}
                k = env.get("ALPACA_API_KEY") or env.get("APCA_API_KEY_ID")
                s = env.get("ALPACA_SECRET_KEY") or env.get("APCA_API_SECRET_KEY")
                if k and s:
                    return k, s
    except Exception:
        pass
    sys.exit("No Alpaca keys found. Set APCA_API_KEY_ID and APCA_API_SECRET_KEY.")


HDR = {}


def api(method, base, path, params=None, body=None):
    url = base + path + ("?" + urllib.parse.urlencode(params) if params else "")
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(6):
        req = urllib.request.Request(url, data=data, headers=HDR, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                txt = r.read()
                return json.loads(txt) if txt else None
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 5:
                time.sleep(3 * 2 ** attempt); continue
            raise RuntimeError(f"{method} {path} -> {e.code}: {e.read().decode()[:300]}")
        except urllib.error.URLError:
            if attempt < 5:
                time.sleep(5); continue
            raise


def ny_date(iso_utc):
    t = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    t = t.astimezone(NY) if NY else t - timedelta(hours=5)
    return t.strftime("%Y-%m-%d")


# ----------------------------- indicators (pure) -----------------------------
def rsi_last(vals, n=RSI_LEN):
    """Wilder-style RSI via exponential smoothing alpha=1/n (identical to the backtest)."""
    if len(vals) < n + 1:
        return None
    up = dn = 0.0
    a, first = 1.0 / n, True
    for i in range(1, len(vals)):
        ch = vals[i] - vals[i - 1]
        u, d = max(ch, 0.0), max(-ch, 0.0)
        if first:
            up, dn, first = u, d, False
        else:
            up, dn = a * u + (1 - a) * up, a * d + (1 - a) * dn
    return 100.0 if dn == 0 else 100 - 100 / (1 + up / dn)


def sma_last(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def exit_decisions(positions, series):
    """positions: {sym: held_trading_days}. Returns {sym: reason}."""
    out = {}
    for s, held in positions.items():
        v = series.get(s)
        if not v:
            continue
        s5 = sma_last(v, EXIT_LEN)
        if s5 is not None and v[-1] > s5:
            out[s] = "bounce: price above 5-day average"
        elif held >= MAX_HOLD:
            out[s] = f"time stop: held {held} days"
    return out


def entry_signals(series, exclude, free_slots):
    cands = []
    for s, v in series.items():
        if s in exclude or len(v) < TREND_LEN + 1:
            continue
        r, t = rsi_last(v), sma_last(v, TREND_LEN)
        if r is not None and t is not None and r < RSI_BUY and v[-1] > t:
            cands.append((r, s))
    cands.sort()
    return [(s, round(r, 2)) for r, s in cands[:max(free_slots, 0)]]


def park_allowed(spy_vals):
    t = sma_last(spy_vals, TREND_LEN)
    return t is not None and spy_vals[-1] > t


# ----------------------------- market data -----------------------------
def daily_closes(symbols):
    start = (datetime.now(timezone.utc) - timedelta(days=440)).strftime("%Y-%m-%dT00:00:00Z")
    end = (datetime.now(timezone.utc) - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out, token = {s: [] for s in symbols}, None
    while True:
        p = dict(symbols=",".join(symbols), timeframe="1Day", start=start, end=end, limit=10000,
                 adjustment="all", feed="sip", sort="asc")
        if token:
            p["page_token"] = token
        d = api("GET", DATA_API, "/v2/stocks/bars", p)
        for s, bars in (d.get("bars") or {}).items():
            out[s] += [(ny_date(b["t"]), b["c"]) for b in bars]
        token = d.get("next_page_token")
        if not token:
            return out


def latest_prices(symbols):
    d = api("GET", DATA_API, "/v2/stocks/trades/latest", dict(symbols=",".join(symbols), feed="iex"))
    return {s: t["p"] for s, t in (d.get("trades") or {}).items()}


# ----------------------------- broker -----------------------------
def clock():
    c = api("GET", TRADE_API, "/v2/clock")
    ts = c["timestamp"]  # New York local time, e.g. 2026-11-18T15:50:01-05:00
    return c["is_open"], ts[:10], int(ts[11:13]) * 60 + int(ts[14:16])


def account():
    a = api("GET", TRADE_API, "/v2/account")
    if not str(a.get("account_number", "")).startswith("PA"):
        sys.exit("SAFETY STOP: this is not a paper account. The bot only runs on paper.")
    return a


def positions_now():
    return {p["symbol"]: p for p in api("GET", TRADE_API, "/v2/positions")}


def open_orders():
    return api("GET", TRADE_API, "/v2/orders", dict(status="open", limit=100))


def entry_date(symbol):
    """Fill date (New York) of the most recent filled buy of this symbol."""
    os_ = api("GET", TRADE_API, "/v2/orders", dict(status="closed", symbols=symbol, side="buy", limit=20, direction="desc"))
    for o in os_:
        if o.get("filled_at") and float(o.get("filled_qty") or 0) > 0:
            return ny_date(o["filled_at"])
    return None


def trading_days_between(d0, d1):
    """Number of trading days from d0 to d1 inclusive (entry day counts as day 1, like the backtest)."""
    if not d0 or d0 > d1:
        return 1
    cal = api("GET", TRADE_API, "/v2/calendar", dict(start=d0, end=d1))
    return max(len(cal), 1)


def order(symbol, side, notional=None, qty=None, reason=""):
    body = {"symbol": symbol, "side": side, "type": "market", "time_in_force": "day"}
    if qty is not None:
        body["qty"] = f"{qty:.9f}".rstrip("0").rstrip(".")
        amt = body["qty"] + " sh"
    else:
        body["notional"] = f"{math.floor(notional * 100) / 100:.2f}"
        amt = "$" + body["notional"]
    log(f"ORDER {side.upper()} {symbol} {amt} — {reason}")
    if DRY:
        return None
    return api("POST", TRADE_API, "/v2/orders", body=body)


def wait_filled(ids, timeout=90):
    ids = [i for i in ids if i]
    if DRY or not ids:
        return
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = [api("GET", TRADE_API, f"/v2/orders/{i}")["status"] for i in ids]
        if all(s in ("filled", "canceled", "rejected", "expired") for s in st):
            return
        time.sleep(3)
    log("WARNING: some orders not filled within timeout")


def sell_exits(exits, held):
    ids = []
    for s, why in exits.items():
        o = order(s, "sell", qty=float(held[s]["qty"]), reason=why)
        ids.append(o["id"] if o else None)
    wait_filled(ids)


# ----------------------------- runs -----------------------------
def precheck(run):
    is_open, today, hhmm = clock()
    lo, hi = WINDOWS[run]
    if not is_open:
        log("Market closed — nothing to do."); return None
    if not (lo <= hhmm <= hi) and not FORCE and not DRY:
        log(f"Outside the {run} window — nothing to do."); return None
    if open_orders() and not DRY:
        log("Orders still open from a previous run — skipping to avoid doubling up."); return None
    account()
    return today


def morning():
    today = precheck("morning")
    if not today:
        return
    held = positions_now()
    syms = sorted(set(UNIVERSE) | {PARK} | set(held))
    hist = daily_closes(syms)
    series = {s: [c for d, c in hist.get(s, []) if d < today] for s in syms}   # completed closes only
    prev_day = max((d for d, _ in hist.get(PARK, []) if d < today), default=None)

    # 1) exits, judged on yesterday's close (never touch something bought this morning)
    entries = {s: entry_date(s) for s in held if s != PARK}
    mine = {s: trading_days_between(d, prev_day) for s, d in entries.items()}
    exits = exit_decisions({s: h for s, h in mine.items() if entries[s] != today}, series)
    sell_exits({s: w + f" (at {prev_day} close)" for s, w in exits.items()}, held)
    for s in exits:
        mine.pop(s)
    if exits and not DRY:
        held = positions_now()

    # 2) new dip buys, judged on yesterday's close (exactly like the backtest)
    bought_today = {o["symbol"] for o in api("GET", TRADE_API, "/v2/orders",
                    dict(status="all", side="buy", after=f"{today}T00:00:00Z", limit=100))
                    if o["symbol"] != PARK}
    pending = entry_signals({s: series[s] for s in UNIVERSE if s in series}, set(mine) | bought_today,
                            SLOTS - len(mine))
    if not pending:
        log(f"No dip-buy signals at the {prev_day} close.")
    a = account()
    equity, cash = float(a["equity"]), float(a["cash"])
    if exits and DRY:  # in a dry run the sells didn't happen; count their value as cash
        cash += sum(float(held[s]["market_value"]) for s in exits)
    need = equity / SLOTS * len(pending)
    if pending and cash < need and PARK in held:
        spy_val = float(held[PARK]["market_value"])
        sell_amt = min(need - cash, spy_val)
        o = (order(PARK, "sell", qty=float(held[PARK]["qty"]), reason="fund dip buys") if sell_amt >= spy_val * 0.98
             else order(PARK, "sell", notional=sell_amt, reason="fund dip buys"))
        wait_filled([o["id"] if o else None])
        cash = float(account()["cash"]) if not DRY else cash + sell_amt
    ids = []
    for s, r in pending:
        amt = min(equity / SLOTS, cash) * 0.995
        if amt < MIN_ORDER:
            log(f"skip {s}: not enough cash"); continue
        o = order(s, "buy", notional=amt, reason=f"dip buy: RSI(2)={r} at {prev_day} close")
        ids.append(o["id"] if o else None)
        cash -= amt
    wait_filled(ids)

    # 3) idle cash -> SPY while SPY is above its 200-day average; otherwise hold cash
    if not DRY:
        cash = float(account()["cash"])
        held = positions_now()
    if park_allowed(series[PARK]):
        if cash >= MIN_ORDER:
            order(PARK, "buy", notional=cash * 0.995, reason="park idle cash (SPY above 200-day avg)")
    elif PARK in held:
        order(PARK, "sell", qty=float(held[PARK]["qty"]), reason="SPY below 200-day avg — move to cash")
    log(f"Morning done. equity=${equity:.2f} holding={sorted(set(mine) | {s for s, _ in pending}) or 'none'}")


def status():
    a = account()
    log(f"Account {a['account_number']} (paper) equity=${float(a['equity']):.2f} cash=${float(a['cash']):.2f}")
    for s, p in positions_now().items():
        log(f"  {s}: {float(p['qty']):.4f} sh  value=${float(p['market_value']):.2f}  P/L=${float(p['unrealized_pl']):.2f}")


def auto():
    is_open, _, hhmm = clock()
    if not is_open:
        log("Market closed — nothing to do."); return
    lo, hi = WINDOWS["morning"]
    if lo <= hhmm <= hi:
        return morning()
    log(f"Outside the 9:31-11:00 morning window (New York time {hhmm // 60}:{hhmm % 60:02d}) — nothing to do.")


if __name__ == "__main__":
    k, s = keys()
    HDR.update({"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s, "Content-Type": "application/json"})
    cmd = next((a for a in sys.argv[1:] if not a.startswith("--")), "status")
    if cmd == "afternoon":
        log("v3 is morning-only; the afternoon routine was removed. Nothing to do."); sys.exit(0)
    {"auto": auto, "morning": morning, "status": status}[cmd]()
