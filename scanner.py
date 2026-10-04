"""Nifty option-chain scalper scanner (CALL / PUT buying signals).
Angel One SmartAPI + Telegram. Used by app.py (Streamlit) and GitHub Actions.
Educational tool - not financial advice. Paper-trade first.
"""
import os, json, datetime as dt
import numpy as np, pandas as pd, requests, pyotp
from SmartApi import SmartConnect

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
MASTER = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
NIFTY, VIX = "99926000", "99926017"
STATE_FILE = "state.json"

CFG = dict(
    strikes_each_side=5,   # ATM +/- 5 strikes
    min_score=5,           # out of 9
    sl_pct=18, t1_pct=25, t2_pct=40,   # premium based SL / targets
    max_spread_pct=2.0,    # skip illiquid options
    start="09:25", end="15:00",
    cooldown_min=15,       # same-direction alert gap
)


# ---------- helpers ----------
def now_ist():
    return dt.datetime.now(IST)


def in_window(cfg=CFG):
    t = now_ist()
    if t.weekday() >= 5:
        return False
    return cfg["start"] <= t.strftime("%H:%M") <= cfg["end"]


def login():
    api = SmartConnect(api_key=os.environ["ANGEL_API_KEY"])
    totp = pyotp.TOTP(os.environ["ANGEL_TOTP_SECRET"]).now()
    d = api.generateSession(os.environ["ANGEL_CLIENT_ID"], os.environ["ANGEL_PIN"], totp)
    if not d.get("status"):
        raise RuntimeError(f"Angel login failed: {d.get('message')}")
    return api


def telegram(text):
    tok, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT_ID")
    if not tok or not chat:
        return False
    r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      data={"chat_id": chat, "text": text, "parse_mode": "HTML"}, timeout=15)
    return r.ok


def load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {}


def save_state(s):
    json.dump(s, open(STATE_FILE, "w"))


# ---------- indicators ----------
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def atr(df, n=14):
    tr = pd.concat([df.high - df.low, (df.high - df.close.shift()).abs(),
                    (df.low - df.close.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


# ---------- data ----------
def spot_candles(api, interval="FIVE_MINUTE", days=5):
    to = now_ist()
    frm = to - dt.timedelta(days=days)
    r = api.getCandleData({"exchange": "NSE", "symboltoken": NIFTY, "interval": interval,
                           "fromdate": frm.strftime("%Y-%m-%d %H:%M"),
                           "todate": to.strftime("%Y-%m-%d %H:%M")})
    df = pd.DataFrame(r["data"], columns=["time", "open", "high", "low", "close", "vol"])
    df["time"] = pd.to_datetime(df["time"]).dt.tz_localize(None)
    now = to.replace(tzinfo=None)
    # drop the still-forming candle
    df = df[df.time + pd.Timedelta(minutes=5) <= now].reset_index(drop=True)
    return df


def option_master(spot):
    m = pd.DataFrame(requests.get(MASTER, timeout=90).json())
    m = m[(m.name == "NIFTY") & (m.instrumenttype == "OPTIDX") & (m.exch_seg == "NFO")].copy()
    m["exp"] = pd.to_datetime(m.expiry, format="%d%b%Y")
    today = pd.Timestamp(now_ist().date())
    exp = m[m.exp >= today].exp.min()
    m = m[m.exp == exp].copy()
    m["strike"] = m.strike.astype(float) / 100
    m["type"] = m.symbol.str[-2:]
    atm = int(round(spot / 50) * 50)
    n = CFG["strikes_each_side"]
    m = m[(m.strike >= atm - n * 50) & (m.strike <= atm + n * 50)]
    return m[["token", "symbol", "strike", "type"]], atm, exp.date()


def chain_snapshot(api, opts, atm_unused=None):
    toks = list(opts.token)
    r = api.getMarketData("FULL", {"NFO": toks, "NSE": [NIFTY, VIX]})
    rows = {x["symbolToken"]: x for x in r["data"]["fetched"]}
    out = []
    for _, o in opts.iterrows():
        x = rows.get(o.token)
        if not x:
            continue
        d = x.get("depth", {})
        bid = (d.get("buy") or [{}])[0].get("price", 0)
        ask = (d.get("sell") or [{}])[0].get("price", 0)
        out.append(dict(token=o.token, symbol=o.symbol, strike=o.strike, type=o.type,
                        ltp=x.get("ltp", 0), oi=x.get("opnInterest", 0),
                        vol=x.get("tradeVolume", 0), bid=bid, ask=ask))
    vix = rows.get(VIX, {}).get("ltp")
    spot = rows.get(NIFTY, {}).get("ltp")
    return pd.DataFrame(out), spot, vix


# ---------- strategy ----------
def analyze(api):
    df = spot_candles(api)
    df["ema9"], df["ema21"] = ema(df.close, 9), ema(df.close, 21)
    df["rsi"] = rsi(df.close)
    df["atr"] = atr(df)
    today = df[df.time.dt.date == df.time.iloc[-1].date()].copy()
    tp = (today.high + today.low + today.close) / 3
    sess_avg = tp.expanding().mean().iloc[-1]  # VWAP proxy (index has no volume)
    last = df.iloc[-1]
    spot_ltp_candle = float(last.close)

    opts, atm, expiry = option_master(spot_ltp_candle)
    chain, spot, vix = chain_snapshot(api, opts)
    spot = float(spot or spot_ltp_candle)
    day = now_ist().strftime("%Y-%m-%d")

    st = load_state()
    if st.get("date") != day:
        st = dict(date=day, oi0={t: int(o) for t, o in zip(chain.token, chain.oi)})
    oi0 = st["oi0"]
    chain["oi_chg"] = chain.apply(lambda r: r.oi - oi0.get(r.token, r.oi), axis=1)

    ce, pe = chain[chain.type == "CE"], chain[chain.type == "PE"]
    pcr = pe.oi.sum() / max(ce.oi.sum(), 1)
    near = chain[(chain.strike - atm).abs() <= 150]
    ce_chg = near[near.type == "CE"].oi_chg.sum()
    pe_chg = near[near.type == "PE"].oi_chg.sum()
    resistance = float(ce.loc[ce.oi.idxmax(), "strike"]) if len(ce) else None
    support = float(pe.loc[pe.oi.idxmax(), "strike"]) if len(pe) else None

    bull, bear, why = 0, 0, []

    def add(side, pts, text):
        nonlocal bull, bear
        if side == "CALL":
            bull += pts
        else:
            bear += pts
        why.append((side, pts, text))

    # 1 Trend (max 2)
    if last.ema9 > last.ema21 and spot > sess_avg:
        add("CALL", 2, "EMA9>EMA21 & spot > session avg")
    elif last.ema9 < last.ema21 and spot < sess_avg:
        add("PUT", 2, "EMA9<EMA21 & spot < session avg")
    elif spot > sess_avg:
        add("CALL", 1, "Spot above session avg")
    else:
        add("PUT", 1, "Spot below session avg")
    # 2 Momentum (max 2)
    if last.rsi > 58:
        add("CALL", 1, f"RSI {last.rsi:.0f} bullish")
    elif last.rsi < 42:
        add("PUT", 1, f"RSI {last.rsi:.0f} bearish")
    hi6, lo6 = df.high.iloc[-7:-1].max(), df.low.iloc[-7:-1].min()
    if last.close > hi6:
        add("CALL", 1, "Breakout above last 6-candle high")
    elif last.close < lo6:
        add("PUT", 1, "Breakdown below last 6-candle low")
    # 3 PCR (max 1)
    if pcr > 1.15:
        add("CALL", 1, f"PCR {pcr:.2f} (put heavy, bullish)")
    elif pcr < 0.8:
        add("PUT", 1, f"PCR {pcr:.2f} (call heavy, bearish)")
    # 4 OI buildup (max 2)
    if pe_chg > 0 and pe_chg > 1.3 * max(ce_chg, 0):
        add("CALL", 2, "Put writing near ATM (support building)")
    elif ce_chg > 0 and ce_chg > 1.3 * max(pe_chg, 0):
        add("PUT", 2, "Call writing near ATM (resistance building)")
    # 5 Volume skew (max 1)
    cv, pv = near[near.type == "CE"].vol.sum(), near[near.type == "PE"].vol.sum()
    if cv > 1.3 * pv:
        add("CALL", 1, "Call volume dominance")
    elif pv > 1.3 * cv:
        add("PUT", 1, "Put volume dominance")
    # 6 OI walls (max 1, acts as room-to-run check)
    if resistance and resistance - spot < 40:
        add("PUT", 1, f"Near call wall {resistance:.0f}")
    elif support and spot - support < 40:
        add("CALL", 1, f"Near put wall {support:.0f}")

    side = "CALL" if bull > bear else "PUT" if bear > bull else None
    score = max(bull, bear)
    warns = []
    if vix and vix > 24:
        warns.append(f"VIX {vix:.1f} high - premiums costly")
    if now_ist().weekday() == 3:
        warns.append("Check expiry day - theta fast")

    sig = dict(time=now_ist().strftime("%H:%M"), spot=spot, atm=atm, expiry=str(expiry),
               vix=vix, pcr=round(pcr, 2), support=support, resistance=resistance,
               bull=bull, bear=bear, side=None, score=score, warns=warns, why=why,
               rsi=round(float(last.rsi), 1), atr=round(float(last.atr), 1))

    if side and score >= CFG["min_score"]:
        typ = "CE" if side == "CALL" else "PE"
        pick = chain[(chain.type == typ) & (chain.strike == atm)].iloc[0]
        spread = (pick.ask - pick.bid) / pick.ltp * 100 if pick.ltp and pick.ask and pick.bid else 99
        if spread <= CFG["max_spread_pct"] and pick.ltp > 0:
            e = float(pick.ltp)
            sig.update(side=side, symbol=pick.symbol, entry=e,
                       sl=round(e * (1 - CFG["sl_pct"] / 100), 1),
                       t1=round(e * (1 + CFG["t1_pct"] / 100), 1),
                       t2=round(e * (1 + CFG["t2_pct"] / 100), 1),
                       spread=round(spread, 2))
        else:
            sig["warns"].append(f"Signal {side} but spread {spread:.1f}% too wide - skipped")

    sig["chain"] = chain
    sig["state"] = st
    return sig


def format_msg(s):
    emoji = "🟢" if s["side"] == "CALL" else "🔴"
    lines = [f"{emoji} <b>NIFTY {s['side']} BUY</b>  ({s['time']} IST)",
             f"<b>{s['symbol']}</b>",
             f"Entry ~ {s['entry']}  |  SL {s['sl']}  |  T1 {s['t1']}  |  T2 {s['t2']}",
             f"Score {s['score']}/9 | Spot {s['spot']:.0f} | PCR {s['pcr']} | VIX {s['vix']}",
             f"Support {s['support']:.0f} | Resistance {s['resistance']:.0f}", ""]
    lines += [f"• {t}" for sd, _, t in s["why"] if sd == s["side"]]
    lines += [f"⚠️ {w}" for w in s["warns"]]
    lines.append("\nMove SL to cost after T1. Exit by 15:15. Paper-trade first.")
    return "\n".join(lines)


def run_once(send=True, force=False):
    if not force and not in_window():
        print("Outside scan window")
        return None
    api = login()
    s = analyze(api)
    st = s.pop("state")
    s.pop("chain")
    if s["side"]:
        last = st.get("last", {})
        recent = False
        if last.get("side") == s["side"]:
            gap = (now_ist() - dt.datetime.fromisoformat(last["at"])).total_seconds() / 60
            recent = gap < CFG["cooldown_min"]
        if not recent:
            if send:
                telegram(format_msg(s))
            st["last"] = dict(side=s["side"], at=now_ist().isoformat())
    save_state(st)
    print(json.dumps({k: v for k, v in s.items() if k != "why"}, default=str, indent=1))
    return s


if __name__ == "__main__":
    run_once()
        
