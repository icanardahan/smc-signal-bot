"""
Strateji laboratuvarı — ICT/SMC dışı klasik yaklaşımları aynı titizlikle test eder.

Neden bu yaklaşımlar: ICT 2022 ve SMC'de avantaj (+0.03R) komisyondan küçük
kaldı. Sebep yapısaldı — sık işlem, dar stop, 1R kazanç. Komisyonu aşmak için
gereken profil: SEYREK işlem, GENİŞ stop, BÜYÜK kazanç. Bu profile uyan klasik
yöntem trend takibidir (breakout). Kontrol grubu olarak zıt karakterdeki
ortalamaya dönüş de test edilir.

Tüm testlerde aynı kurallar geçerli:
  - İleriye bakış yok (sinyal bar i'de, işlem bar i+1'de)
  - Dolum mumunda TP sayılmaz (kazancı şişiren hata)
  - Gerçek komisyon (maker/taker) ve 100$/10x izole sermaye modeli
  - Dönem ayrımı: 1. yarıda iyi olan 2. yarıda da tutmalı

Kullanım:
    python strategy_lab.py [gün] [sembol]
"""

import sys
import time

import backtest as bt

TF = "4h"
BARS_PER_DAY = 6


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


def sma(c, n):
    out = [None] * len(c)
    s = 0.0
    for i, x in enumerate(c):
        s += x["close"]
        if i >= n:
            s -= c[i - n]["close"]
        if i >= n - 1:
            out[i] = s / n
    return out


def rsi(c, n=14):
    out = [None] * len(c)
    gains = losses = 0.0
    for i in range(1, len(c)):
        d = c[i]["close"] - c[i - 1]["close"]
        g, l = max(d, 0), max(-d, 0)
        if i <= n:
            gains += g
            losses += l
            if i == n:
                ag, al = gains / n, losses / n
                out[i] = 100 - 100 / (1 + (ag / al if al else 999))
        else:
            ag = (ag * (n - 1) + g) / n
            al = (al * (n - 1) + l) / n
            out[i] = 100 - 100 / (1 + (ag / al if al else 999))
    return out


# ---------------- Stratejiler ----------------
# Her strateji: (bar index i'de sinyal) -> (yön, stop) veya None
# Giriş bir sonraki barın açılışında yapılır (ileriye bakış yok).

def donchian(c, i, ind, n_in=20, atr_mult=3.0):
    """Trend takibi: N barlık en yüksek/düşük kırılımı, ATR bazlı geniş stop."""
    if i < n_in + 1 or not ind["atr"][i]:
        return None
    hi = max(x["high"] for x in c[i - n_in:i])
    lo = min(x["low"] for x in c[i - n_in:i])
    a = ind["atr"][i]
    if c[i]["close"] > hi:
        return ("long", c[i]["close"] - atr_mult * a)
    if c[i]["close"] < lo:
        return ("short", c[i]["close"] + atr_mult * a)
    return None


def donchian_trend(c, i, ind, n_in=20, atr_mult=3.0):
    """Aynı kırılım ama sadece uzun vadeli trend yönünde (SMA200 filtresi)."""
    s = ind["sma200"][i]
    if s is None:
        return None
    sig = donchian(c, i, ind, n_in, atr_mult)
    if not sig:
        return None
    if sig[0] == "long" and c[i]["close"] < s:
        return None
    if sig[0] == "short" and c[i]["close"] > s:
        return None
    return sig


def mean_reversion(c, i, ind, atr_mult=2.0):
    """Ortalamaya dönüş: trend yönünde aşırı satım/alım geri çekilmesi."""
    r, s, a = ind["rsi"][i], ind["sma200"][i], ind["atr"][i]
    if r is None or s is None or not a:
        return None
    if c[i]["close"] > s and r < 30:
        return ("long", c[i]["close"] - atr_mult * a)
    if c[i]["close"] < s and r > 70:
        return ("short", c[i]["close"] + atr_mult * a)
    return None


STRATEGIES = [
    ("Donchian kırılım (20 bar)",      donchian,       "trail"),
    ("Donchian + SMA200 trend",        donchian_trend, "trail"),
    ("Ortalamaya dönüş (RSI+SMA200)",  mean_reversion, "target"),
]


# ---------------- Simülasyon ----------------
def run_strategy(c, ind, sig_fn, exit_mode, tp_r=2.0, trail_atr=3.0,
                 max_bars=120):
    """Bar bar dolaşır, aynı anda tek pozisyon tutar."""
    trades = []
    i = 0
    n = len(c)
    while i < n - 2:
        s = sig_fn(c, i, ind)
        if not s:
            i += 1
            continue
        direction, stop = s
        entry = c[i + 1]["open"]          # giriş bir sonraki barın açılışında
        lg = direction == "long"
        risk = abs(entry - stop)
        if risk <= 0:
            i += 1
            continue
        tp = entry + tp_r * risk if lg else entry - tp_r * risk

        best = entry
        out = None
        for k in range(i + 1, min(n, i + 1 + max_bars)):
            b = c[k]
            if (b["low"] <= stop) if lg else (b["high"] >= stop):
                out = (-abs(entry - stop) / risk if False else
                       (stop - entry) / risk if lg else (entry - stop) / risk, "stop", k)
                break
            if exit_mode == "target" and k > i + 1:
                if (b["high"] >= tp) if lg else (b["low"] <= tp):
                    out = (tp_r, "tp", k)
                    break
            if exit_mode == "trail":
                best = max(best, b["high"]) if lg else min(best, b["low"])
                a = ind["atr"][k] or 0
                ns = best - trail_atr * a if lg else best + trail_atr * a
                stop = max(stop, ns) if lg else min(stop, ns)
        if out is None:
            k = min(n, i + 1 + max_bars) - 1
            r = ((c[k]["close"] - entry) if lg else (entry - c[k]["close"])) / risk
            out = (r, "timeout", k)
        trades.append({"R": out[0], "status": out[1],
                       "risk_pct": 100 * risk / entry,
                       "t": c[out[2]]["close_time"],
                       "move_pct": out[0] * 100 * risk / entry})
        i = out[2] + 1                     # pozisyon kapanana kadar yeni işlem yok
    return trades


def report(name, trades, mid):
    if not trades:
        print(f"{name:<34} sinyal yok")
        return
    line = f"{name:<34}"
    for sel in (lambda t: t["t"] <= mid, lambda t: t["t"] > mid, lambda t: True):
        h = [t for t in trades if sel(t)]
        if not h:
            line += f"{'-':>26}"
            continue
        wins = sum(1 for t in h if t["R"] > 0)
        exp = sum(t["R"] for t in h) / len(h)
        eq = [{"status": "tp1_hit" if t["R"] > 0 else "sl_hit",
               "move_pct": t["move_pct"], "exit_time": t["t"],
               "market_entry": True} for t in h]
        bal, _, _ = bt.simulate_equity(eq)
        line += f"{len(h):>6}{100*wins/len(h):>6.0f}%{exp:>+7.2f}R{bal-100:>+8.1f}$"
    print(line)


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 180
    n_sym = int(sys.argv[2]) if len(sys.argv) > 2 else 40

    end = int(time.time() * 1000)
    start = end - days * 86400_000
    symbols = bt.top_symbols_by_volume(n_sym)
    print(f"{len(symbols)} sembol, son {days} gün, {TF} — ICT/SMC dışı yaklaşımlar\n")

    data = {}
    for i, sym in enumerate(symbols, 1):
        try:
            c = bt.fetch_range(sym, TF, start - 60 * 86400_000, end)
        except Exception:
            continue
        if len(c) < 260:
            continue
        data[sym] = c
        print(f"[{i}/{len(symbols)}] {sym}: {len(c)} bar", flush=True)

    print(f"\n{len(data)} sembol hazır.\n")
    all_t = {name: [] for name, _, _ in STRATEGIES}
    for sym, c in data.items():
        ind = {"atr": atr_series(c), "sma200": sma(c, 200), "rsi": rsi(c)}
        for name, fn, mode in STRATEGIES:
            all_t[name] += run_strategy(c, ind, fn, mode)

    every = [t["t"] for ts in all_t.values() for t in ts]
    if not every:
        print("Hiç işlem yok.")
        return
    mid = sorted(every)[len(every) // 2]

    print("=" * 112)
    print("SONUÇ — her blok: işlem / isabet / beklenti / net$ (100$, 10x izole)")
    print("=" * 112)
    print(f"{'strateji':<34}{'--- 1. YARI ---':>26}{'--- 2. YARI ---':>26}{'--- TÜMÜ ---':>26}")
    print("-" * 112)
    for name, _, _ in STRATEGIES:
        report(name, all_t[name], mid)
    print("-" * 112)
    print("Bir strateji ancak İKİ yarıda da artı beklenti veriyorsa gerçek sayılır.")


if __name__ == "__main__":
    main()
