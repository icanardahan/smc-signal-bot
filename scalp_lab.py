"""
Scalp ve day-trading stratejileri laboratuvarı.

Aynı titizlik: ileriye bakış yok, dolum mumunda TP sayılmaz, gerçek komisyon,
dönem ayrımıyla doğrulama.

Bu laboratuvar ek olarak KOMİSYON ÖNCESİ ve SONRASI sonucu ayrı gösterir —
böylece "avantaj hiç yok mu, yoksa var ama komisyon mu yiyor" sorusu net
cevaplanır. Scalp'te bu ayrım kritiktir çünkü hedefler küçüktür.

Test edilenler:
  Scalp (5dk):      Bollinger geri dönüşü, VWAP sapması
  Day trade (15dk): Açılış aralığı kırılımı (ORB), EMA geri çekilme

Kullanım:
    python scalp_lab.py [gün] [sembol]
"""

import sys
import time
from datetime import datetime, timezone

import backtest as bt


# ---------------- Göstergeler ----------------
def atr_series(c, n=14):
    out = [0.0] * len(c)
    trs = []
    for i in range(1, len(c)):
        pc = c[i - 1]["close"]
        trs.append(max(c[i]["high"] - c[i]["low"],
                       abs(c[i]["high"] - pc), abs(c[i]["low"] - pc)))
        if len(trs) >= n:
            out[i] = sum(trs[-n:]) / n
    return out


def sma_std(c, n=20):
    m = [None] * len(c)
    s = [None] * len(c)
    for i in range(n - 1, len(c)):
        w = [x["close"] for x in c[i - n + 1:i + 1]]
        mu = sum(w) / n
        m[i] = mu
        s[i] = (sum((x - mu) ** 2 for x in w) / n) ** 0.5
    return m, s


def ema(c, n):
    out = [None] * len(c)
    k = 2 / (n + 1)
    e = None
    for i, x in enumerate(c):
        e = x["close"] if e is None else x["close"] * k + e * (1 - k)
        out[i] = e
    return out


def session_vwap(c):
    """Her NY gününde sıfırlanan VWAP."""
    out = [None] * len(c)
    day = None
    pv = vol = 0.0
    for i, x in enumerate(c):
        d = datetime.fromtimestamp(x["open_time"] / 1000, tz=timezone.utc).date()
        if d != day:
            day, pv, vol = d, 0.0, 0.0
        tp = (x["high"] + x["low"] + x["close"]) / 3
        pv += tp
        vol += 1
        out[i] = pv / vol
    return out


# ---------------- Stratejiler ----------------
def bollinger_scalp(c, i, ind):
    """Scalp: alt/üst banda dokunuşta ters yöne geri dönüş beklentisi."""
    m, s, a = ind["bb_m"][i], ind["bb_s"][i], ind["atr"][i]
    if m is None or not s or not a:
        return None
    if c[i]["close"] < m - 2 * s:
        return ("long", c[i]["close"] - 1.5 * a)
    if c[i]["close"] > m + 2 * s:
        return ("short", c[i]["close"] + 1.5 * a)
    return None


def vwap_fade(c, i, ind):
    """Scalp: seans VWAP'ından aşırı sapmayı fade et."""
    v, a = ind["vwap"][i], ind["atr"][i]
    if v is None or not a:
        return None
    if c[i]["close"] < v - 2 * a:
        return ("long", c[i]["close"] - 1.5 * a)
    if c[i]["close"] > v + 2 * a:
        return ("short", c[i]["close"] + 1.5 * a)
    return None


def orb(c, i, ind):
    """Day trade: NY gününün ilk 4 barının aralığının kırılması."""
    a = ind["atr"][i]
    if not a or i < 8:
        return None
    dt = datetime.fromtimestamp(c[i]["open_time"] / 1000, tz=timezone.utc)
    # günün başından itibaren kaçıncı bar
    j = i
    day = dt.date()
    while j > 0 and datetime.fromtimestamp(
            c[j - 1]["open_time"] / 1000, tz=timezone.utc).date() == day:
        j -= 1
    idx = i - j
    if idx < 4 or idx > 20:      # aralık kurulmuş olmalı, gün de bitmemiş olmalı
        return None
    hi = max(x["high"] for x in c[j:j + 4])
    lo = min(x["low"] for x in c[j:j + 4])
    if c[i]["close"] > hi:
        return ("long", lo)
    if c[i]["close"] < lo:
        return ("short", hi)
    return None


def ema_pullback(c, i, ind):
    """Day trade: EMA9/EMA21 trendinde EMA21'e geri çekilip devam."""
    e9, e21, a = ind["ema9"][i], ind["ema21"][i], ind["atr"][i]
    if e9 is None or e21 is None or not a or i < 25:
        return None
    up = e9 > e21
    touched = c[i]["low"] <= e21 <= c[i]["high"]
    if not touched:
        return None
    if up and c[i]["close"] > e21:
        return ("long", e21 - 1.5 * a)
    if not up and c[i]["close"] < e21:
        return ("short", e21 + 1.5 * a)
    return None


SCALP = [("Bollinger geri dönüşü (5dk)", bollinger_scalp, 1.0),
         ("VWAP sapması fade (5dk)", vwap_fade, 1.0)]
DAY = [("Açılış aralığı kırılımı (15dk)", orb, 2.0),
       ("EMA21 geri çekilme (15dk)", ema_pullback, 2.0)]


def run(c, ind, fn, tp_r, max_bars):
    trades = []
    i = 30
    n = len(c)
    while i < n - 2:
        s = fn(c, i, ind)
        if not s:
            i += 1
            continue
        d, stop = s
        entry = c[i + 1]["open"]
        lg = d == "long"
        risk = abs(entry - stop)
        if risk <= 0 or risk / entry < 0.0005:
            i += 1
            continue
        tp = entry + tp_r * risk if lg else entry - tp_r * risk
        out = None
        for k in range(i + 1, min(n, i + 1 + max_bars)):
            b = c[k]
            if (b["low"] <= stop) if lg else (b["high"] >= stop):
                out = (-1.0, "stop", k)
                break
            if k > i + 1 and ((b["high"] >= tp) if lg else (b["low"] <= tp)):
                out = (tp_r, "tp", k)
                break
        if out is None:
            k = min(n, i + 1 + max_bars) - 1
            r = ((c[k]["close"] - entry) if lg else (entry - c[k]["close"])) / risk
            out = (r, "timeout", k)
        trades.append({"R": out[0], "status": out[1],
                       "risk_pct": 100 * risk / entry,
                       "move_pct": out[0] * 100 * risk / entry,
                       "t": c[out[2]]["close_time"]})
        i = out[2] + 1
    return trades


def report(name, trades, mid):
    if not trades:
        print(f"{name:<32} işlem yok")
        return
    line = f"{name:<32}"
    for sel in (lambda t: t["t"] <= mid, lambda t: t["t"] > mid, lambda t: True):
        h = [t for t in trades if sel(t)]
        if not h:
            line += f"{'-':>30}"
            continue
        exp = sum(t["R"] for t in h) / len(h)
        # komisyon öncesi ve sonrası dolar
        gross = [{"status": "tp1_hit", "move_pct": t["move_pct"], "exit_time": t["t"],
                  "market_entry": True} for t in h]
        net = [{"status": "tp1_hit" if t["R"] > 0 else "sl_hit",
                "move_pct": t["move_pct"], "exit_time": t["t"],
               "market_entry": True} for t in h]
        old = bt.MAKER_FEE, bt.TAKER_FEE
        bt.MAKER_FEE = bt.TAKER_FEE = 0.0
        gb, _, _ = bt.simulate_equity(gross)
        bt.MAKER_FEE, bt.TAKER_FEE = old
        nb, _, _ = bt.simulate_equity(net)
        line += f"{len(h):>7}{exp:>+7.2f}R{gb-100:>+9.1f}${nb-100:>+8.1f}$"
    print(line)


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    n_sym = int(sys.argv[2]) if len(sys.argv) > 2 else 25

    end = int(time.time() * 1000)
    start = end - days * 86400_000
    symbols = bt.top_symbols_by_volume(n_sym)
    print(f"{len(symbols)} sembol, son {days} gün — scalp & day trading\n")

    groups = [("5m", SCALP, 60), ("15m", DAY, 40)]
    results = {}
    for tf, strats, maxbars in groups:
        for i, sym in enumerate(symbols, 1):
            try:
                c = bt.fetch_range(sym, tf, start, end)
            except Exception:
                continue
            if len(c) < 500:
                continue
            ind = {"atr": atr_series(c), "vwap": session_vwap(c),
                   "ema9": ema(c, 9), "ema21": ema(c, 21)}
            m, s = sma_std(c)
            ind["bb_m"], ind["bb_s"] = m, s
            for name, fn, tp_r in strats:
                results.setdefault(name, []).extend(run(c, ind, fn, tp_r, maxbars))
            if i % 10 == 0:
                print(f"  {tf}: {i}/{len(symbols)}", flush=True)

    every = [t["t"] for ts in results.values() for t in ts]
    if not every:
        print("İşlem yok.")
        return
    mid = sorted(every)[len(every) // 2]

    print()
    print("=" * 126)
    print("SONUÇ — blok: işlem / beklenti / KOMİSYONSUZ net$ / KOMİSYONLU net$  (100$, 10x izole)")
    print("=" * 126)
    print(f"{'strateji':<32}{'------ 1. YARI ------':>30}"
          f"{'------ 2. YARI ------':>30}{'------- TÜMÜ -------':>30}")
    print("-" * 126)
    for name, _, _ in SCALP + DAY:
        report(name, results.get(name, []), mid)
    print("-" * 126)
    print("KOMİSYONSUZ sütunu avantajın var olup olmadığını, KOMİSYONLU sütunu")
    print("gerçekte ne kaldığını gösterir. İkisi arasındaki fark = komisyon yükü.")


if __name__ == "__main__":
    main()
