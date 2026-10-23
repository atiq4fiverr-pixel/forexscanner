"""
Forex Scanner: 28 forex pairs + BTC + ETH + GOLD, 1H + 4H
Alerts (Telegram):
  1) NEAR   : price kisi swing high/low ke qareeb
  2) REJECT : last closed candle ne swing se reject kiya (wick + wapas close)
  3) OB     : price 1H/4H Order Block zone ke andar

Install:  pip install pandas numpy requests   (yfinance ki zaroorat nahi - Android/Termux friendly)
Run:      export TG_TOKEN="..." ; export TG_CHAT="..."
          python forex_scanner.py          (har 5 min loop)
          python forex_scanner.py --once   (sirf ek baar)
          python forex_scanner.py --once --listen 240   (scan + 240 sec bot commands sunna)

Bot: Telegram mein pair ka naam likhein (btcusd, eth, gold, eurusd) -> levels mil jayen ge.
"""
import os, sys, json, time, re
import numpy as np
import pandas as pd
import requests

TOKEN = os.getenv("TG_TOKEN", "")
CHAT = os.getenv("TG_CHAT", "")

PAIRS = ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY",
         "EURGBP", "EURAUD", "EURNZD", "EURCAD", "EURCHF", "EURJPY",
         "GBPAUD", "GBPNZD", "GBPCAD", "GBPCHF", "GBPJPY",
         "AUDNZD", "AUDCAD", "AUDCHF", "AUDJPY",
         "NZDCAD", "NZDCHF", "NZDJPY", "CADCHF", "CADJPY", "CHFJPY"]

# Extra instruments: naam -> Yahoo ticker
EXTRA = {"BTCUSD": "BTC-USD", "ETHUSD": "ETH-USD", "XAUUSD": "GC=F"}  # GC=F = Gold futures

ALL = PAIRS + list(EXTRA)


def ysym(name):
    return EXTRA.get(name, name + "=X")


# ---- Settings (apni marzi se badlein) ----
SWING_N = 5            # swing ke liye left/right candles
NEAR_ATR = 0.3         # swing se kitna qareeb (ATR ka hissa)
OB_IMPULSE_ATR = 1.0   # OB ke baad kam az kam itni move (ATR)
COOLDOWN_H = 6         # same alert dobara kitne ghante baad
LOOP_SECONDS = 300     # har 5 minute
STATE_FILE = "state.json"


def send(text):
    if not TOKEN or not CHAT:
        print(text)
        return
    for i in range(0, len(text), 3800):
        try:
            requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                          data={"chat_id": CHAT, "text": text[i:i + 3800]}, timeout=15)
        except Exception as e:
            print("telegram error:", e)


def fmt(pair, x):
    if pair in ("BTCUSD", "ETHUSD", "XAUUSD"):
        return f"{x:.2f}"
    return f"{x:.3f}" if "JPY" in pair else f"{x:.5f}"


def fetch(pair):
    """Yahoo chart API se seedha 1H candles (yfinance ke bagair)."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ysym(pair)}"
    r = requests.get(url, params={"interval": "1h", "range": "60d"},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    idx = pd.to_datetime(res["timestamp"], unit="s")
    d = pd.DataFrame({"Open": q["open"], "High": q["high"],
                      "Low": q["low"], "Close": q["close"]}, index=idx)
    return d.dropna()


def atr(df, n=14):
    pc = df.Close.shift(1)
    tr = pd.concat([df.High - df.Low, (df.High - pc).abs(), (df.Low - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def swings(df, n=SWING_N):
    H, L, out = df.High.values, df.Low.values, []
    for i in range(n, len(df) - n):
        if H[i] == H[i - n:i + n + 1].max():
            out.append(("HIGH", i, H[i]))
        if L[i] == L[i - n:i + n + 1].min():
            out.append(("LOW", i, L[i]))
    return out[-8:]


def order_blocks(df, a):
    """Bullish OB: akhri bearish candle jiske baad strong upar move.
       Bearish OB: akhri bullish candle jiske baad strong neeche move.
       Agar baad mein close zone ke paar chala gaya to OB khatam."""
    obs = []
    start = max(1, len(df) - 120)
    for i in range(start, len(df) - 3):
        av = a.iloc[i]
        if np.isnan(av):
            continue
        o, c, h, l = df.Open.iloc[i], df.Close.iloc[i], df.High.iloc[i], df.Low.iloc[i]
        nxt = df.iloc[i + 1:i + 4]
        after = df.iloc[i + 4:]
        if c < o and nxt.High.max() - h > OB_IMPULSE_ATR * av:
            if not (after.Close < l).any():
                obs.append(("BULL", l, h, df.index[i]))
        elif c > o and l - nxt.Low.min() > OB_IMPULSE_ATR * av:
            if not (after.Close > h).any():
                obs.append(("BEAR", l, h, df.index[i]))
    return obs[-4:]


def analyze(pair, tf, df, price):
    """df = sirf closed candles. Alerts ki list wapas karta hai."""
    alerts = []
    if len(df) < 40:
        return alerts
    a = atr(df)
    av = a.iloc[-1]
    if np.isnan(av):
        return alerts
    last = df.iloc[-1]
    ltime = str(df.index[-1])

    for kind, i, lvl in swings(df):
        # NEAR
        if abs(price - lvl) <= NEAR_ATR * av:
            alerts.append((f"{pair}|{tf}|NEAR|{lvl:.5f}",
                           f"📍 {pair} {tf}: price swing {kind} ke qareeb\n   Level {fmt(pair, lvl)} | Price {fmt(pair, price)}"))
        # REJECT
        rng = last.High - last.Low
        if rng <= 0:
            continue
        tol = 0.1 * av
        if kind == "HIGH" and last.High >= lvl - tol and last.Close < lvl:
            wick = last.High - max(last.Open, last.Close)
            if wick >= 0.5 * rng:
                alerts.append((f"{pair}|{tf}|REJ|{lvl:.5f}|{ltime}",
                               f"🔻 {pair} {tf}: swing HIGH se REJECTION (bearish)\n   Level {fmt(pair, lvl)} | Close {fmt(pair, last.Close)}"))
        if kind == "LOW" and last.Low <= lvl + tol and last.Close > lvl:
            wick = min(last.Open, last.Close) - last.Low
            if wick >= 0.5 * rng:
                alerts.append((f"{pair}|{tf}|REJ|{lvl:.5f}|{ltime}",
                               f"🔺 {pair} {tf}: swing LOW se REJECTION (bullish)\n   Level {fmt(pair, lvl)} | Close {fmt(pair, last.Close)}"))

    for kind, lo, hi, t in order_blocks(df, a):
        if lo - 0.05 * av <= price <= hi + 0.05 * av:
            icon = "🟢" if kind == "BULL" else "🔴"
            alerts.append((f"{pair}|{tf}|OB|{kind}|{lo:.5f}",
                           f"{icon} {pair} {tf}: {kind} Order Block mein price\n   Zone {fmt(pair, lo)} - {fmt(pair, hi)} | Price {fmt(pair, price)}"))
    return alerts


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def run_once():
    state = load_state()
    now = time.time()
    new_msgs = []
    for pair in ALL:
        try:
            d = fetch(pair)
            if len(d) < 100:
                continue
            price = float(d.Close.iloc[-1])  # latest live price
            h1 = d.iloc[:-1]                 # closed 1H candles
            h4 = d.resample("4h").agg({"Open": "first", "High": "max",
                                       "Low": "min", "Close": "last"}).dropna().iloc[:-1]
            for tf, df in (("1H", h1), ("4H", h4)):
                for key, msg in analyze(pair, tf, df, price):
                    if now - state.get(key, 0) > COOLDOWN_H * 3600:
                        state[key] = now
                        new_msgs.append(msg)
        except Exception as e:
            print(pair, "error:", e)
        time.sleep(0.3)

    state = {k: v for k, v in state.items() if now - v < 48 * 3600}
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)
    for m in new_msgs:          # har alert alag message
        send(m)
        time.sleep(1)           # Telegram rate limit se bachne ke liye
    print(time.strftime("%H:%M:%S"), "alerts:", len(new_msgs))


# ================= On-demand bot: pair likho -> levels lo =================
ALIASES = {"BTC": "BTCUSD", "ETH": "ETHUSD", "GOLD": "XAUUSD", "XAU": "XAUUSD"}

HELP = ("🤖 Pair ka naam likhein, main levels bhej dunga.\n"
        "Misal: btcusd  |  eth  |  gold  |  eurusd  |  gbpjpy\n"
        "Aik message mein 3 pairs tak: btc eth gold\n"
        "/list = saari pairs")


def norm(tok):
    t = tok.split("@")[0]
    t = "".join(ch for ch in t.upper() if ch.isalnum())
    t = ALIASES.get(t, t)
    return t if t in ALL else None


def all_swings(df, n=SWING_N, lookback=200):
    H, L, out = df.High.values, df.Low.values, []
    for i in range(max(n, len(df) - lookback), len(df) - n):
        if H[i] == H[i - n:i + n + 1].max():
            out.append(("HIGH", H[i]))
        if L[i] == L[i - n:i + n + 1].min():
            out.append(("LOW", L[i]))
    return out


def fvgs(df, a, lookback=120):
    """Unfilled Fair Value Gaps (3 candle gap)."""
    out = []
    H, L = df.High.values, df.Low.values
    n = len(df)
    for i in range(max(1, n - lookback), n - 1):
        av = a.iloc[i]
        if np.isnan(av):
            continue
        if L[i + 1] > H[i - 1] and L[i + 1] - H[i - 1] >= 0.1 * av:
            lo, hi = H[i - 1], L[i + 1]
            if not (L[i + 2:] <= lo).any():
                out.append(("BULL", lo, hi))
        elif H[i + 1] < L[i - 1] and L[i - 1] - H[i + 1] >= 0.1 * av:
            lo, hi = H[i + 1], L[i - 1]
            if not (H[i + 2:] >= hi).any():
                out.append(("BEAR", lo, hi))
    return out


def tf_report(pair, tf, df, price):
    if len(df) < 40:
        return f"⏱ {tf}: data kam hai"
    a = atr(df)
    sell, buy = [], []   # (distance, text)

    def zone_txt(label, lo, hi):
        if abs(hi - lo) < 1e-12:
            return f"{label} {fmt(pair, lo)}"
        return f"{label} {fmt(pair, lo)} - {fmt(pair, hi)}"

    def add(side, label, lo, hi):
        if side == "SELL":
            if hi < price:
                return
            dist = max(0.0, lo - price)
        else:
            if lo > price:
                return
            dist = max(0.0, price - hi)
        txt = zone_txt(label, lo, hi)
        if lo <= price <= hi:
            txt += "  ← price andar"
        else:
            txt += f"  ({dist / price * 100:.2f}%)"
        (sell if side == "SELL" else buy).append((dist, txt))

    for kind, lvl in all_swings(df):
        if kind == "HIGH":
            add("SELL", "Swing High", lvl, lvl)
        else:
            add("BUY", "Swing Low", lvl, lvl)
    for kind, lo, hi, _t in order_blocks(df, a):
        add("SELL" if kind == "BEAR" else "BUY", f"{'Bear' if kind == 'BEAR' else 'Bull'} OB", lo, hi)
    for kind, lo, hi in fvgs(df, a):
        add("SELL" if kind == "BEAR" else "BUY", f"{'Bear' if kind == 'BEAR' else 'Bull'} FVG", lo, hi)

    def lines(items):
        seen, out = set(), []
        for _d, t in sorted(items, key=lambda x: x[0]):
            if t not in seen:
                seen.add(t)
                out.append("   • " + t)
            if len(out) == 4:
                break
        return "\n".join(out) if out else "   (koi level nahi)"

    return (f"⏱ {tf}\n🔴 Possible SELL zones (upar):\n{lines(sell)}\n"
            f"🟢 Possible BUY zones (neeche):\n{lines(buy)}")


def build_report(pair):
    d = fetch(pair)
    price = float(d.Close.iloc[-1])
    h1 = d.iloc[:-1]
    h4 = d.resample("4h").agg({"Open": "first", "High": "max",
                               "Low": "min", "Close": "last"}).dropna().iloc[:-1]
    parts = [f"📊 {pair} | Price {fmt(pair, price)}",
             tf_report(pair, "4H", h4, price),
             tf_report(pair, "1H", h1, price),
             "ℹ️ Ye sirf technical levels hain, entry/exit ki salah ya guarantee nahi."]
    return "\n\n".join(parts)


def handle_text(text):
    t = text.strip()
    low = t.lower()
    if low.startswith("/list"):
        send("Pairs:\n" + ", ".join(ALL))
        return
    if low.startswith(("/start", "/help")):
        send(HELP)
        return
    toks = [x for x in re.split(r"[\s,]+", t) if x][:3]
    found = False
    for tok in toks:
        p = norm(tok)
        if not p:
            continue
        found = True
        try:
            send(build_report(p))
        except Exception as e:
            send(f"{p}: data nahi aya, thori der baad try karein ({e})")
    if not found:
        send("Pair samajh nahi aya 🤔\n\n" + HELP)


def tg_get(offset, timeout):
    params = {"timeout": timeout, "allowed_updates": json.dumps(["message"])}
    if offset is not None:
        params["offset"] = offset
    r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates",
                     params=params, timeout=timeout + 15)
    return r.json().get("result", [])


def listen(seconds):
    """Telegram messages sunta hai; sirf aap (TG_CHAT) ke messages ka jawab deta hai."""
    if not TOKEN or not CHAT:
        print("TG_TOKEN / TG_CHAT set nahi - listen skip")
        return
    end = time.time() + seconds
    offset = None
    while True:
        left = end - time.time()
        if left < 3:
            break
        try:
            ups = tg_get(offset, int(min(25, left - 1)))
        except Exception as e:
            print("getUpdates error:", e)
            time.sleep(3)
            continue
        for u in ups:
            offset = u["update_id"] + 1
            m = u.get("message") or {}
            if str(m.get("chat", {}).get("id")) != str(CHAT) or not m.get("text"):
                continue
            try:
                handle_text(m["text"])
            except Exception as e:
                print("handle error:", e)
    if offset is not None:
        try:
            tg_get(offset, 0)   # aakhri updates confirm
        except Exception:
            pass
    print("listen khatam")


if __name__ == "__main__":
    if "--once" in sys.argv or "--listen" in sys.argv:
        if "--once" in sys.argv:
            run_once()
        if "--listen" in sys.argv:
            listen(int(sys.argv[sys.argv.index("--listen") + 1]))
    else:
        send("✅ Scanner shuru ho gaya (Forex + BTC + ETH + Gold)")
        while True:
            try:
                run_once()
            except Exception as e:
                print("run error:", e)
            time.sleep(LOOP_SECONDS)
