#!/usr/bin/env python3
"""
Large-cap dip buyer — Alpaca PAPER trading bot (Python standard library only).

Strategy (tested 2017-2026, see ~/Trading/backtest):
  Universe : the 30 largest US companies (UNIVERSE below; refresh once a year).
  Entry    : RSI(2) < 10 AND price > 200-day average  -> buy next morning, lowest RSI first.
  Exit     : price > 5-day average, or after 10 trading days -> sell in the afternoon.
  Slots    : up to 2 positions, each = half of account equity at time of entry.
  Idle cash: parked in SPY while SPY > its 200-day average, otherwise kept in cash.

Runs:
  python3 bot.py afternoon   (~3:50pm ET)  exits, new signals, SPY parking
  python3 bot.py morning     (~9:35am ET)  buys yesterday's signals
  python3 bot.py status                     account + positions
  add --dry-run to any command to see what it WOULD do without placing orders.

Safety: refuses to run against anything but an Alpaca PAPER account.
"""
import json, math, os, sys, time, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timedelta, timezone

# ----------------------------- settings -----------------------------
UNIVERSE = ["NVDA", "AAPL", "GOOGL", "MSFT", "AMZN", "AVGO", "SPCX", "META", "TSLA", "LLY",
            "MU", "JPM", "WMT", "AMD", "V", "XOM", "JNJ", "MA", "INTC", "ABBV",
            "CSCO", "BAC", "AMAT", "COST", "CAT", "CVX", "UNH", "LRCX", "GE", "KO"]  # top 30 by market cap, Oct 2026
PARK = "SPY"
SLOTS = 2
RSI_LEN, RSI_BUY = 2, 10.0
TREND_LEN, EXIT_LEN, MAX_HOLD = 200, 5, 10
MIN_ORDER = 1.00  # Alpaca minimum notional

TRADE_API = "https://paper-api.alpaca.markets"   # PAPER ONLY. Do not change without a deliberate decision.
DATA_API = "https://data.alpaca.markets"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state.json")
LOGDIR = os.path.join(HERE, "logs")
os.makedirs(LOGDIR, exist_ok=True)
DRY = "--dry-run" in sys.argv


# ----------------------------- plumbing -----------------------------
def log(msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {'[DRY] ' if DRY else ''}{msg}"
    print(line)
    with open(os.path.join(LOGDIR, "bot.log"), "a") as f:
        f.write(line + "\n")


def record_trade(**row):
    path = os.path.join(LOGDIR, "trades.csv")
    new = not os.path.exists(path)
    with open(path, "a") as f:
        if new:
            f.write("time,action,symbol,notional_or_qty,ref_price,reason,dry_run\n")
        f.write(f"{datetime.now().isoformat(timespec='seconds')},{row['action']},{row['symbol']},"
                f"{row['amount']},{row.get('price', '')},{row.get('reason', '')},{DRY}\n")


def keys():
    k, s = os.environ.get("APCA_API_KEY_ID"), os.environ.get("APCA_API_SECRET_KEY")
    if k and s:
        return k, s
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
    sys.exit("No Alpaca keys found (Claude app config or APCA_API_KEY_ID / APCA_API_SECRET_KEY).")


KEY, SECRET = keys()
HDR = {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SECRET, "Content-Type": "application/json"}


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


def load_state():
    if os.path.exists(STATE):
        with open(STATE) as f:
            return json.load(f)
    return {"positions": {}, "pending": [], "last_afternoon": None, "last_morning": None}


def save_state(st):
    if DRY:
        return
    with open(STATE + ".tmp", "w") as f:
        json.dump(st, f, indent=2)
    os.replace(STATE + ".tmp", STATE)


# ----------------------------- market data -----------------------------
def daily_closes(symbols):
    """Completed daily closes (oldest -> newest) for each symbol, ~300 trading days."""
    start = (datetime.now(timezone.utc) - timedelta(days=440)).strftime("%Y-%m-%dT00:00:00Z")
    end = (datetime.now(timezone.utc) - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = {s: [] for s in symbols}
    token = None
    while True:
        p = dict(symbols=",".join(symbols), timeframe="1Day", start=start, end=end, limit=10000,
                 adjustment="all", feed="sip", sort="asc")
        if token:
            p["page_token"] = token
        d = api("GET", DATA_API, "/v2/stocks/bars", p)
        for s, bars in (d.get("bars") or {}).items():
            out[s] += [(b["t"][:10], b["c"]) for b in bars]
        token = d.get("next_page_token")
        if not token:
            return out


def latest_prices(symbols):
    d = api("GET", DATA_API, "/v2/stocks/trades/latest", dict(symbols=",".join(symbols), feed="iex"))
    return {s: t["p"] for s, t in (d.get("trades") or {}).items()}


def series_with_today(closes, today, price):
    """Completed closes, with today's live price as the latest value (replacing a partial today bar)."""
    vals = [c for (dt, c) in closes if dt < today]
    if price is not None:
        vals.append(price)
    return vals


def rsi_last(vals, n=RSI_LEN):
    """Wilder-style RSI via exponential smoothing alpha=1/n (same as the backtest), last value."""
    if len(vals) < n + 1:
        return None
    up = dn = 0.0
    first = True
    a = 1.0 / n
    for i in range(1, len(vals)):
        ch = vals[i] - vals[i - 1]
        u, d = max(ch, 0.0), max(-ch, 0.0)
        if first:
            up, dn, first = u, d, False
        else:
            up, dn = a * u + (1 - a) * up, a * d + (1 - a) * dn
    if dn == 0:
        return 100.0
    return 100 - 100 / (1 + up / dn)


def sma_last(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


# ----------------------------- decisions (pure, testable) -----------------------------
def exit_decisions(positions, series):
    """positions: {sym: {'held': int}} (held already incremented for today). Returns {sym: reason}."""
    out = {}
    for s, p in positions.items():
        v = series.get(s)
        if not v:
            continue
        s5 = sma_last(v, EXIT_LEN)
        if s5 is not None and v[-1] > s5:
            out[s] = "bounce: price above 5-day average"
        elif p["held"] >= MAX_HOLD:
            out[s] = f"time stop: held {p['held']} days"
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


# ----------------------------- broker helpers -----------------------------
def account():
    a = api("GET", TRADE_API, "/v2/account")
    if not str(a.get("account_number", "")).startswith("PA"):
        sys.exit("SAFETY STOP: this is not a paper account. The bot only runs on paper.")
    return a


def positions_now():
    return {p["symbol"]: p for p in api("GET", TRADE_API, "/v2/positions")}


def order(symbol, side, notional=None, qty=None, reason=""):
    body = {"symbol": symbol, "side": side, "type": "market", "time_in_force": "day"}
    if qty is not None:
        body["qty"] = f"{qty:.9f}".rstrip("0").rstrip(".")
        amt = body["qty"] + " sh"
    else:
        body["notional"] = f"{math.floor(notional * 100) / 100:.2f}"
        amt = "$" + body["notional"]
    log(f"ORDER {side.upper()} {symbol} {amt} — {reason}")
    record_trade(action=side, symbol=symbol, amount=amt, reason=reason)
    if DRY:
        return None
    return api("POST", TRADE_API, "/v2/orders", body=body)


def wait_filled(order_ids, timeout=90):
    if DRY or not order_ids:
        return
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = [api("GET", TRADE_API, f"/v2/orders/{i}")["status"] for i in order_ids]
        if all(s in ("filled", "canceled", "rejected", "expired") for s in st):
            return
        time.sleep(3)
    log("WARNING: some orders not filled within timeout")


WINDOWS = {"morning": (9 * 60 + 31, 11 * 60), "afternoon": (15 * 60 + 30, 15 * 60 + 58)}  # New York time


def market_open_today(run=None):
    """Alpaca's clock is in New York time. Also refuses to trade outside the run's time window
    (protects against the Mac waking up late and running a missed job at the wrong time)."""
    c = api("GET", TRADE_API, "/v2/clock")
    ts = c["timestamp"]  # e.g. 2026-11-17T15:50:01.123-05:00
    hhmm = int(ts[11:13]) * 60 + int(ts[14:16])
    if run and not DRY and "--force" not in sys.argv:
        lo, hi = WINDOWS[run]
        if not (lo <= hhmm <= hi):
            return False, ts[:10]
    return c["is_open"], ts[:10]


# ----------------------------- runs -----------------------------
def afternoon():
    is_open, today = market_open_today("afternoon")
    if not is_open:
        log("Market closed or outside the 3:30-3:58pm ET window — nothing to do."); return
    st = load_state()
    if st.get("last_afternoon") == today and not DRY:
        log("Afternoon run already done today."); return
    account()
    held = positions_now()
    syms = sorted(set(UNIVERSE) | {PARK} | set(st["positions"]))
    hist, live = daily_closes(syms), latest_prices(syms)
    series = {s: series_with_today(hist.get(s, []), today, live.get(s)) for s in syms}

    # 1) exits
    managed = {}
    for s, p in st["positions"].items():
        if s in held:
            managed[s] = {"held": p["held"] + 1, "entry_date": p["entry_date"]}
        else:
            log(f"note: {s} in state but not held at broker — dropping from state")
    exits = exit_decisions(managed, series)
    ids = []
    for s, why in exits.items():
        o = order(s, "sell", qty=float(held[s]["qty"]), reason=why)
        if o: ids.append(o["id"])
        managed.pop(s)
    wait_filled(ids)

    # 2) new signals for tomorrow morning
    free = SLOTS - len(managed)
    pending = entry_signals({s: series[s] for s in UNIVERSE if s in series}, set(managed), free)
    for s, r in pending:
        log(f"SIGNAL {s}: RSI(2)={r} — will buy tomorrow morning")
    if not pending:
        log("No new entry signals.")

    # 3) park idle cash in SPY (keep cash reserved for tomorrow's buys)
    a = account()
    equity, cash = float(a["equity"]), float(a["cash"])
    held = positions_now()
    spy_ok = park_allowed(series[PARK])
    if spy_ok:
        reserve = min(cash, equity / SLOTS * len(pending))
        spare = cash - reserve
        if spare >= MIN_ORDER:
            order(PARK, "buy", notional=spare, reason="park idle cash (SPY above 200-day avg)")
    elif PARK in held:
        order(PARK, "sell", qty=float(held[PARK]["qty"]), reason="SPY below 200-day avg — move to cash")

    st["positions"] = managed
    st["pending"] = [s for s, _ in pending]
    st["last_afternoon"] = today
    save_state(st)
    log(f"Afternoon done. equity=${equity:.2f} positions={list(managed)} pending={st['pending']} park={'on' if spy_ok else 'off'}")


def morning():
    is_open, today = market_open_today("morning")
    if not is_open:
        log("Market closed or outside the 9:31-11:00am ET window — nothing to do."); return
    st = load_state()
    if st.get("last_morning") == today and not DRY:
        log("Morning run already done today."); return
    account()
    held = positions_now()

    # --- catch-up: if yesterday's afternoon run was missed (Mac asleep, etc.), do its work now
    #     using yesterday's official closes. Entries then match the backtest exactly (signal at close,
    #     buy next open); exits happen this morning instead of yesterday afternoon.
    syms = sorted(set(UNIVERSE) | {PARK} | set(st["positions"]))
    hist = daily_closes(syms)
    prev_day = max((d for d, _ in hist.get(PARK, []) if d < today), default=None)
    if prev_day and st.get("last_afternoon") != prev_day:
        log(f"Afternoon run for {prev_day} was missed — catching up with that day's closing prices.")
        series = {s: [c for d, c in hist.get(s, []) if d < today] for s in syms}
        managed = {}
        for s, p in st["positions"].items():
            if s in held:
                managed[s] = {"held": p["held"] + 1, "entry_date": p["entry_date"]}
        ids = []
        for s, why in exit_decisions(managed, series).items():
            o = order(s, "sell", qty=float(held[s]["qty"]), reason=why + " (catch-up)")
            if o: ids.append(o["id"])
            managed.pop(s)
        wait_filled(ids)
        pending = entry_signals({s: series[s] for s in UNIVERSE if s in series}, set(managed), SLOTS - len(managed))
        for s, r in pending:
            log(f"SIGNAL {s}: RSI(2)={r} at {prev_day} close (catch-up)")
        st["positions"] = managed
        st["pending"] = [s for s, _ in pending]
        st["last_afternoon"] = prev_day
        held = positions_now() if not DRY else held

    if not st["pending"]:
        log("No pending buys.")
    else:
        a = account()
        equity, cash = float(a["equity"]), float(a["cash"])
        free = SLOTS - len(st["positions"])
        buys = [s for s in st["pending"] if s not in st["positions"]][:free]
        need = equity / SLOTS * len(buys)
        if cash < need and PARK in held:
            spy_val = float(held[PARK]["market_value"])
            sell_amt = min(need - cash, spy_val)
            if sell_amt >= spy_val * 0.98:
                o = order(PARK, "sell", qty=float(held[PARK]["qty"]), reason="fund dip buys")
            else:
                o = order(PARK, "sell", notional=sell_amt, reason="fund dip buys")
            if o: wait_filled([o["id"]])
            cash = float(account()["cash"]) if not DRY else cash + sell_amt
        for s in buys:
            amt = min(equity / SLOTS, cash) * 0.995  # small buffer for price movement
            if amt < MIN_ORDER:
                log(f"skip {s}: not enough cash"); continue
            order(s, "buy", notional=amt, reason="dip buy (RSI2 signal at last close)")
            cash -= amt
            st["positions"][s] = {"held": 0, "entry_date": today}
    st["pending"] = []
    st["last_morning"] = today
    save_state(st)
    log("Morning done.")


def status():
    a = account()
    log(f"Account {a['account_number']} (paper) equity=${float(a['equity']):.2f} cash=${float(a['cash']):.2f}")
    for s, p in positions_now().items():
        log(f"  {s}: {float(p['qty']):.4f} sh  value=${float(p['market_value']):.2f}  P/L=${float(p['unrealized_pl']):.2f}")
    log(f"  state: {json.dumps(load_state())}")


if __name__ == "__main__":
    cmd = next((a for a in sys.argv[1:] if not a.startswith("--")), "status")
    try:
        {"afternoon": afternoon, "morning": morning, "status": status}[cmd]()
    except Exception as e:
        log(f"ERROR: {e}")
        raise
