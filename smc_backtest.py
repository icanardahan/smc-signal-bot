"""
SMC (Smart Money Concepts) varyantı — ICT 2022 modeline alternatif.

Gerekçe: ICT 2022'nin sorunu avantajın komisyona çok yakın olması. Sebep,
stopların dar (medyan %0.58) ve işlemin sık olması. SMC'yi 4 SAATLİK yapı
üzerinde kurmak ikisini de yapısal olarak düzeltir:
  - Order block'lar 4H seviyesinde => stoplar çok daha geniş
  - Seans/kill zone kısıtı yok, ama kurulum çok daha seyrek
  - Nominal küçülür, komisyon yükü düşer

Model:
  1. Daily bias (günlük son yapı kırılımı yönü)
  2. 4H'de bias yönünde yapı kırılımı (BOS) ve onu yaratan order block
  3. Fiyat OB'ye geri çekilince BEKLEYEN (limit) emir
  4. SL order block'un ötesi, TP 1R (ICT taramasında doğrulanan çıkış)

Aynı titizlik: ileriye bakış yok, dönem ayrımıyla doğrulama, aynı komisyon
ve sermaye modeli (100$, 10x izole).

Kullanım:
    python smc_backtest.py [gün] [sembol]
"""

import sys
import time
from bisect import bisect_right

import backtest as bt
import ict_scanner as ict

H4 = "4h"
PIVOT_LEN = 3
OB_SEARCH_MAX = 30
SL_ATR_MULT = 0.5           # 4H ATR'ye göre tampon
MIN_RISK_ATR = 0.5          # stop en az bu kadar 4H ATR uzakta olmalı
TP_R = 1.0                  # çıkış: 1R (hedef taramasında en tutarlı çıkan)
OB_MAX_AGE_BARS = 30        # order block bundan eskiyse bayat
FILL_TIMEOUT_BARS = 30      # emir bu kadar 4H mumda dolmazsa iptal (~5 gün)
HOLD_TIMEOUT_BARS = 42      # pozisyon bu kadar tutulur (~7 gün)
SCAN_EVERY_BARS = 1         # her 4H mum kapanışında tarama


def structure(candles, length=PIVOT_LEN):
    """Pivot tabanlı yapı kırılımı ve kırılımı yaratan order block.
    Pivotlar `length` bar sonra görünür olduğu için ileriye bakış yoktur."""
    n = len(candles)
    hi = [c["high"] for c in candles]
    lo = [c["low"] for c in candles]
    op = [c["open"] for c in candles]
    cl = [c["close"] for c in candles]

    ph = [None] * n
    pl = [None] * n
    for i in range(length, n - length):
        w = hi[i - length:i + length + 1]
        if hi[i] == max(w) and w.count(max(w)) == 1:
            ph[i] = hi[i]
        w = lo[i - length:i + length + 1]
        if lo[i] == min(w) and w.count(min(w)) == 1:
            pl[i] = lo[i]

    last_ph = last_pl = None
    out = []
    ob_top = ob_bot = None
    ob_dir = 0
    ob_bar = -1
    for i in range(n):
        r = i - length
        if 0 <= r < n and ph[r] is not None:
            last_ph = ph[r]
        if 0 <= r < n and pl[r] is not None:
            last_pl = pl[r]
        prev = cl[i - 1] if i else None
        bull = last_ph is not None and cl[i] > last_ph and (prev is None or prev <= last_ph)
        bear = last_pl is not None and cl[i] < last_pl and (prev is None or prev >= last_pl)
        if bull:
            for j in range(i - 1, max(i - OB_SEARCH_MAX, -1), -1):
                if cl[j] < op[j]:
                    ob_top, ob_bot, ob_dir, ob_bar = hi[j], lo[j], 1, j
                    break
            last_ph = None
        if bear:
            for j in range(i - 1, max(i - OB_SEARCH_MAX, -1), -1):
                if cl[j] > op[j]:
                    ob_top, ob_bot, ob_dir, ob_bar = hi[j], lo[j], -1, j
                    break
            last_pl = None
        out.append((ob_top, ob_bot, ob_dir, ob_bar))
    return out


def atr(candles, length=14):
    if len(candles) < length + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        pc = candles[i - 1]["close"]
        trs.append(max(candles[i]["high"] - candles[i]["low"],
                       abs(candles[i]["high"] - pc), abs(candles[i]["low"] - pc)))
    return sum(trs[-length:]) / length


def evaluate_smc(daily, h4):
    """Kurulum varsa sözlük döner. h4'ün son mumu 'şu an' kabul edilir."""
    if len(daily) < 20 or len(h4) < 60:
        return None
    bias = ict.compute_daily_bias(daily)
    if bias is None:
        return None

    st = structure(h4)
    ob_top, ob_bot, ob_dir, ob_bar = st[-1]
    if ob_top is None:
        return None
    want = 1 if bias == "long" else -1
    if ob_dir != want:
        return None                       # order block bias yönünde değil
    if len(h4) - 1 - ob_bar > OB_MAX_AGE_BARS:
        return None                       # bayat order block

    cur = h4[-1]["close"]
    a = atr(h4)
    if a <= 0:
        return None

    entry = (ob_top + ob_bot) / 2         # order block ortasına limit emir
    if bias == "long":
        sl = ob_bot - a * SL_ATR_MULT
        if cur <= entry:
            return None                   # fiyat zaten OB'nin içinde/altında
    else:
        sl = ob_top + a * SL_ATR_MULT
        if cur >= entry:
            return None

    risk = abs(entry - sl)
    if risk < MIN_RISK_ATR * a:
        return None                       # bıçak sırtı kurulum

    tp1 = entry + TP_R * risk if bias == "long" else entry - TP_R * risk
    return {"dir": bias, "entry": entry, "sl": sl, "tp1": tp1,
            "risk_pct": 100 * risk / entry, "ob_bar": ob_bar}


def simulate(sig, h4, i):
    """Emir dolar mı, sonra TP mi SL mi? (R, durum)."""
    e, sl, tp = sig["entry"], sig["sl"], sig["tp1"]
    lg = sig["dir"] == "long"
    fill = None
    for k in range(i, min(len(h4), i + FILL_TIMEOUT_BARS)):
        if h4[k]["low"] <= e <= h4[k]["high"]:
            fill = k
            break
    if fill is None:
        return (0.0, "expired")
    for k in range(fill, min(len(h4), fill + HOLD_TIMEOUT_BARS)):
        c = h4[k]
        if (c["low"] <= sl) if lg else (c["high"] >= sl):
            return (-1.0, "sl")
        # Dolum mumunda TP SAYILMAZ: 4H mumun aralığı 1R'den geniş olduğu için
        # "aynı mumda hem doldu hem TP" varsayımı kazançları yapay şişiriyor.
        # Mum içi sıralama bilinemez; muhafazakâr taraf seçilir.
        if k == fill:
            continue
        if (c["high"] >= tp) if lg else (c["low"] <= tp):
            return (TP_R, "tp")
    k = min(len(h4), fill + HOLD_TIMEOUT_BARS) - 1
    move = ict.pct_move(e, h4[k]["close"], lg)
    return (move / sig["risk_pct"], "timeout")


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    n_sym = int(sys.argv[2]) if len(sys.argv) > 2 else 60

    end = int(time.time() * 1000)
    start = end - days * 86400_000
    symbols = bt.top_symbols_by_volume(n_sym)
    print(f"{len(symbols)} sembol, son {days} gün — SMC (4H) varyantı\n")

    rows = []
    for i, sym in enumerate(symbols, 1):
        try:
            daily = bt.fetch_range(sym, "1d", start - 200 * 86400_000, end)
            h4 = bt.fetch_range(sym, H4, start - 40 * 86400_000, end)
        except Exception:
            continue
        if len(h4) < 120 or len(daily) < 40:
            continue

        dc = [c["close_time"] for c in daily]
        n = 0
        last_ob = None
        for k in range(60, len(h4)):
            if h4[k]["close_time"] < start:
                continue
            idd = bisect_right(dc, h4[k]["close_time"])
            s = evaluate_smc(daily[:idd], h4[:k + 1])
            if not s or s["ob_bar"] == last_ob:
                continue
            last_ob = s["ob_bar"]
            r, stt = simulate(s, h4, k + 1)
            rows.append({"sym": sym, "t": h4[k]["close_time"], "R": r,
                         "status": stt, "risk_pct": s["risk_pct"],
                         "dir": s["dir"]})
            n += 1
        print(f"[{i}/{len(symbols)}] {sym}: {n} sinyal", flush=True)

    if not rows:
        print("Sinyal yok.")
        return

    mid = sorted(r["t"] for r in rows)[len(rows) // 2]
    print()
    print("=" * 74)
    print(f"SMC (4H) SONUCU — çıkış {TP_R}R, 100$/10x izole")
    print("=" * 74)
    print(f"{'dönem':<12}{'işlem':>7}{'isabet':>9}{'beklenti':>11}{'bakiye':>11}{'net':>10}")
    print("-" * 74)
    for label, sel in (("1. yarı", lambda r: r["t"] <= mid),
                       ("2. yarı", lambda r: r["t"] > mid),
                       ("TÜMÜ", lambda r: True)):
        h = [r for r in rows if sel(r) and r["status"] != "expired"]
        if not h:
            continue
        dec = [r for r in h if r["status"] in ("tp", "sl")]
        wr = 100 * sum(1 for r in dec if r["status"] == "tp") / len(dec) if dec else 0
        exp = sum(r["R"] for r in h) / len(h)
        trades = [{"status": "tp1_hit" if r["status"] == "tp" else "sl_hit",
                   "move_pct": r["R"] * r["risk_pct"], "exit_time": r["t"]} for r in h]
        bal, mdd, _ = bt.simulate_equity(trades)
        print(f"{label:<12}{len(h):>7}{wr:>8.1f}%{exp:>+10.3f}R{bal:>10.2f}$"
              f"{bal-bt.START_BALANCE:>+9.2f}$")
    print("-" * 74)
    rp = sorted(r["risk_pct"] for r in rows)
    print(f"Risk mesafesi medyanı: %{rp[len(rp)//2]:.2f} "
          f"(ICT 2022'de %0.58 idi — geniş stop = az komisyon)")
    print(f"Toplam sinyal: {len(rows)}  |  iptal: {sum(1 for r in rows if r['status']=='expired')}")


if __name__ == "__main__":
    main()
