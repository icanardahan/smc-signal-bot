"""
SMC W/D/4H stratejisinin backtest'i.

Kurulum:
  1. Haftalık ve günlük yapı yönü (bias) belirlenir — ikisi de aynı yönü
     göstermezse işlem aranmaz.
  2. Fiyat, bias yönüne uygun bölgede olmalı: long için discount (aralığın
     alt yarısı), short için premium.
  3. 4 saatlikte bias yönünde CHoCH veya BOS oluşur.
  4. Kırılımın order block'una BEKLEYEN limit emir konur.
  5. SL order block'un ötesi, TP kademeli likidite seviyeleri.

Aynı titizlik: ileriye bakış yok, dolum mumunda TP sayılmaz, gerçek
maker/taker komisyonu, dönem ayrımıyla doğrulama.

Kullanım:  python smc_htf_backtest.py [gün] [sembol]
"""

import sys
import time
from bisect import bisect_right

import os

import backtest as bt
import smc_htf as smc

SL_ATR_MULT = 0.25         # order block'un ötesine 4H ATR payı
FILL_TIMEOUT_BARS = 30     # emir 30 bar (5 gün) dolmazsa iptal
HOLD_TIMEOUT_BARS = int(os.environ.get("HOLD_BARS", "60"))  # 60 bar = 10 gün
SETUP_MAX_AGE_BARS = 6     # kırılım en fazla 6 bar (1 gün) eski olabilir
MIN_RR = float(os.environ.get("MIN_RR", "1.5"))
MAX_RR = float(os.environ.get("MAX_RR", "6.0"))
LIQ_LEN = int(os.environ.get("LIQ_LEN", "20"))
REQUIRE_CHOCH = os.environ.get("REQUIRE_CHOCH", "0") == "1"
DISCOUNT_MAX = 0.5         # long için fiyat aralığın alt yarısında olmalı

# Hedef: aralığın ucu DEĞİL, bir sonraki likidite havuzu.
# İlk testte aralık ucu hedeflendiğinde R:R medyanı 11.7 çıktı ve
# isabet %2.3'e düştü — pozisyonlar hedefe varmadan zaman aşımına giriyordu.
# (Eski TP_MODE anahtarı kaldırıldı; find_setup her zaman likidite hedefi
#  üretiyor ve zaten yalnızca R:R filtresi olarak kullanılıyor.)
PAY = (0.5, 0.3, 0.2)      # TP1/TP2/TP3 kapatma oranları (canlı botla aynı)

# Çıkış modeli. Ölçüm: long kazancının %83'ü 462 işlemin 5'inden geliyor.
# TP1'de yarıyı kapatmak, tam da sonucu taşıyan o birkaç işlemi kesiyor olabilir.
#   scale  : TP1/TP2/TP3 kademeli (mevcut)
#   trail  : TP yok, stop yapının arkasından sürüklenir
#   runner : TP1'de %50, kalan %50 sürüklenen stopla taşınır
EXIT_MODE = os.environ.get("EXIT_MODE", "scale")
# Açık pozisyonda TERS yönde 4H yapı kırılımı olursa kapat.
# ÖLÇÜLDÜ ve KÖTÜ: +0.240R -> +0.031R, net +390.79$ -> -29.70$ ve 2. yarı
# eksiye düşüyor. Sebebi TP1'de yarıyı kapatmakla aynı: kâr birkaç büyük
# işlemden geliyor, ters kırılım ise trend içi normal geri çekilmelerde de
# oluşuyor ve tam o işlemleri erken kesiyor. Bu yüzden canlı bot yalnızca
# UYARIYOR, pozisyonu kapatmıyor.
# Not: structure() olayları yalnızca kendi barına kadarki veriye dayanır
# (pivotlar geriye bakar), bu yüzden tek seferde hesaplamak ileriye bakış
# yaratmaz.
EXIT_ON_BREAK = os.environ.get("EXIT_ON_BREAK", "0") == "1"
TRAIL_LEN = 5              # stopun arkasına çekileceği pivot uzunluğu
DIR_FILTER = os.environ.get("DIR_FILTER", "")   # "long" | "short" | ""

# --- Yeni filtreler (varsayılan KAPALI — mevcut ölçülmüş davranışı bozmaz) ---
USE_FIB = os.environ.get("USE_FIB", "0") == "1"
FIB_MIN = float(os.environ.get("FIB_MIN", "0.618"))
FIB_MAX = float(os.environ.get("FIB_MAX", "0.786"))
USE_TREND = os.environ.get("USE_TREND", "0") == "1"
TREND_LEN = int(os.environ.get("TREND_LEN", "200"))
USE_VOLUME = os.environ.get("USE_VOLUME", "0") == "1"
VOLUME_MULT = float(os.environ.get("VOLUME_MULT", "2.0"))


def evaluate(h4, daily, weekly):
    """Kurulum arama artık smc_htf.find_setup içinde — canlı bot da AYNI
    fonksiyonu çağırır, böylece ikisi birbirinden ayrışamaz."""
    return smc.find_setup(h4, daily, weekly,
                          setup_max_age=SETUP_MAX_AGE_BARS,
                          sl_atr_mult=SL_ATR_MULT, min_rr=MIN_RR,
                          max_rr=MAX_RR, liq_len=LIQ_LEN,
                          discount_max=DISCOUNT_MAX,
                          require_choch=REQUIRE_CHOCH,
                          dir_filter=DIR_FILTER or None,
                          use_fib=USE_FIB, fib_min=FIB_MIN, fib_max=FIB_MAX,
                          use_trend=USE_TREND, trend_len=TREND_LEN,
                          use_volume=USE_VOLUME, volume_mult=VOLUME_MULT)


def simulate(sig, h4, i, ters=None):
    """Emir dolar mı, sonra kademeli çıkış nasıl gider?

    Canlı bottaki davranışın birebir aynısı: TP1'de %50 kapanır ve stop
    başabaşa çekilir, TP2'de %30, TP3'te %20. Aynı mumda hem stop hem TP
    varsa STOP önce sayılır (temkinli varsayım)."""
    e, sl = sig["entry"], sig["sl"]
    tps = sig["tps"]
    lg = sig["dir"] == "long"
    risk = abs(e - sl)

    fill = None
    for k in range(i, min(len(h4), i + FILL_TIMEOUT_BARS)):
        if h4[k]["low"] <= e <= h4[k]["high"]:
            fill = k
            break
    if fill is None:
        return (0.0, "expired", 0.0, i, 0.0)

    stop = sl
    kalan = 1.0
    R = 0.0
    mfe = 0.0          # işlem boyunca görülen EN YÜKSEK lehte hareket (R)
    hareket = 0.0        # ağırlıklı fiyat hareketi (%)
    vurulan = 0
    son = fill

    def kapat(pay, fiyat):
        nonlocal R, hareket, kalan
        fark = (fiyat - e) if lg else (e - fiyat)
        R += pay * fark / risk
        hareket += pay * fark / e * 100
        kalan -= pay

    tl = smc.trail_levels(h4, lg, TRAIL_LEN)

    for k in range(fill, min(len(h4), fill + HOLD_TIMEOUT_BARS)):
        c = h4[k]
        son = k
        uc = (c["high"] - e) if lg else (e - c["low"])
        mfe = max(mfe, uc / risk)
        if (c["low"] <= stop) if lg else (c["high"] >= stop):
            kapat(kalan, stop)
            durum = "be" if vurulan else "sl"
            if vurulan:
                durum = f"tp{vurulan}_be"
            return (R, durum, hareket, k, mfe)
        if k == fill:                     # dolum mumunda TP sayılmaz
            continue

        if EXIT_ON_BREAK and ters and ters.get(k) not in (None, 1 if lg else -1):
            kapat(kalan, c["close"])
            return (R, "yapi_bozuldu", hareket, k, mfe)
        while vurulan < 3 and EXIT_MODE != "trail":
            t = tps[vurulan]
            if t is None:
                break
            if (c["high"] >= t) if lg else (c["low"] <= t):
                if EXIT_MODE == "runner":
                    kapat(0.5, t)
                    vurulan += 1
                    stop = max(stop, e) if lg else min(stop, e)
                    break                 # kalan %50 sürüklenen stopla taşınır
                kapat(PAY[vurulan] if vurulan < 2 else kalan, t)
                vurulan += 1
                stop = max(stop, e) if lg else min(stop, e)   # TP1 sonrası başabaş
            else:
                break
        if kalan <= 1e-9:
            return (R, f"tp{vurulan}", hareket, k, mfe)

        # Stopu yapının arkasından sürükle (pivot `TRAIL_LEN` bar sonra kesinleşir)
        if EXIT_MODE != "scale" and (EXIT_MODE == "trail" or vurulan >= 1):
            yeni = tl[k]
            if yeni is not None:
                stop = max(stop, yeni) if lg else min(stop, yeni)

    kapat(kalan, h4[son]["close"])
    return (R, f"timeout{vurulan}" if vurulan else "timeout", hareket, son, mfe)


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 365
    n_sym = int(sys.argv[2]) if len(sys.argv) > 2 else 40

    end = int(time.time() * 1000)
    start = end - days * 86400_000
    # Sembol evrenini SABİTLE. top_symbols_by_volume() her çağrıda canlı hacim
    # sıralamasını çeker; iki koşu arasında liste değişince karşılaştırma
    # aynı evren üzerinde olmaz (ölçüldü: ENAUSDT iki koşu arasında düştü).
    sf = os.environ.get("SYMBOLS_FILE")
    if sf and os.path.exists(sf):
        symbols = open(sf).read().split()
    else:
        symbols = bt.top_symbols_by_volume(n_sym)
        if sf:
            open(sf, "w").write("\n".join(symbols))
    print(f"{len(symbols)} sembol, son {days} gün — SMC (Haftalık/Günlük/4H)\n")

    rows = []
    for n, sym in enumerate(symbols, 1):
        try:
            h4 = bt.fetch_range(sym, "4h", start - 200 * 86400_000, end)
            d1 = bt.fetch_range(sym, "1d", start - 400 * 86400_000, end)
            w1 = bt.fetch_range(sym, "1w", start - 900 * 86400_000, end)
        except Exception as e:
            print(f"[{sym}] veri hatası: {e}")
            continue
        if len(h4) < 300 or len(d1) < 80 or len(w1) < 25:
            continue

        ters = {}
        if EXIT_ON_BREAK:
            for ev in smc.structure(h4, smc.INTERNAL_LEN)[0]:
                ters[ev[0]] = ev[1]

        dc = [c["close_time"] for c in d1]
        wc = [c["close_time"] for c in w1]
        say = 0
        son_ob = None
        for k in range(200, len(h4)):
            t = h4[k]["close_time"]
            if t < start:
                continue
            sig = evaluate(h4[:k + 1], d1[:bisect_right(dc, t)], w1[:bisect_right(wc, t)])
            if not sig:
                continue
            anahtar = (sig["dir"], round(sig["entry"], 10))
            if anahtar == son_ob:            # aynı order block'a tekrar girme
                continue
            son_ob = anahtar
            r, durum, hareket, cikis, mfe = simulate(sig, h4, k + 1, ters)
            # "expired" dönüşünde cikis = i = k+1 olabilir; bu, sinyal verinin
            # SON barında bulunduğunda len(h4)'e eşit çıkıp IndexError verirdi
            # (nadir — end=now() her koşuda kaydığı için rastgele tetiklenir).
            # Yalnızca zaman damgası için kullanıldığından sınırlamak güvenli.
            cikis = min(cikis, len(h4) - 1)
            rows.append({"sym": sym, "R": r, "status": durum, "move_pct": hareket,
                         "risk_pct": sig["risk_pct"], "t": h4[cikis]["close_time"],
                         "tip": sig["tip"], "rr": sig["rr"], "dir": sig["dir"], "mfe": mfe,
                         "t_in": h4[k]["close_time"]})
            say += 1
        print(f"[{n}/{len(symbols)}] {sym}: {say} sinyal", flush=True)

    if os.environ.get("DUMP"):
        import json
        with open(os.environ["DUMP"], "w") as f:
            json.dump(rows, f)

    if not rows:
        print("Sinyal yok.")
        return

    mid = sorted(r["t"] for r in rows)[len(rows) // 2]
    print()
    print("=" * 76)
    print("SMC W/D/4H SONUCU — 100$, 10x izole, gerçek komisyon")
    print("=" * 76)
    print(f"{'dönem':<10}{'işlem':>7}{'isabet':>9}{'beklenti':>11}{'bakiye':>11}{'net':>10}")
    print("-" * 76)
    for ad, sec in (("1. yarı", lambda r: r["t"] <= mid),
                    ("2. yarı", lambda r: r["t"] > mid),
                    ("TÜMÜ", lambda r: True)):
        h = [r for r in rows if sec(r) and r["status"] != "expired"]
        if not h:
            continue
        # Kademeli çıkışta "isabet" = artıda kapanan işlem oranı
        wr = 100 * sum(1 for r in h if r["R"] > 0) / len(h)
        exp = sum(r["R"] for r in h) / len(h)
        tr = [{"status": "tp1_hit" if r["R"] > 0 else "sl_hit",
               "move_pct": r["move_pct"], "exit_time": r["t"]} for r in h]
        bal, mdd, _ = bt.simulate_equity(tr)
        print(f"{ad:<10}{len(h):>7}{wr:>8.1f}%{exp:>+10.3f}R{bal:>10.2f}${bal-100:>+9.2f}$")
    print("-" * 76)
    rp = sorted(r["risk_pct"] for r in rows)
    rr = sorted(r["rr"] for r in rows)
    print(f"Risk mesafesi medyanı : %{rp[len(rp)//2]:.2f}   (5dk modelde %0.51 idi)")
    print(f"R:R medyanı           : {rr[len(rr)//2]:.2f}")
    print(f"Toplam sinyal {len(rows)} | dolmayan {sum(1 for r in rows if r['status']=='expired')} | "
          f"CHoCH {sum(1 for r in rows if r['tip']=='CHoCH')} / BOS {sum(1 for r in rows if r['tip']=='BOS')}")


if __name__ == "__main__":
    main()
