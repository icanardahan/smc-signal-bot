"""
SMC stratejisinin BIST'te backtest'i.

Kripto tarafındaki AYNI kurulum ve çıkış mantığı kullanılır
(smc_htf.find_setup + smc_htf_backtest.simulate) — kod kopyalanmaz ki
iki taraf birbirinden ayrışmasın.

Zaman dilimi eşlemesi:
  kripto:  haftalık / günlük / 4H
  BIST:    haftalık / günlük / 1H   (Yahoo 4H sunmuyor; BIST seansı da
                                     zaten günde ~8 saat)

Neden ayrı backtest: stratejinin ölçülmüş +0.240R sonucu KRİPTO 4H
verisinde elde edildi. BIST'in seans saatleri, likiditesi, fiyat adımı ve
işlem maliyeti farklı — o sonuç buraya taşınamaz, sıfırdan ölçülmeli.

Kullanım:  python bist_backtest.py [sembol_sayısı]
"""

import math
import sys
from bisect import bisect_right

import bist_data as bd
import smc_htf as smc
import smc_htf_backtest as bt

# Canlı strateji SÜRÜKLENEN STOP kullanıyor; backtest modülünün varsayılanı
# "scale" (sabit kademeli TP). Aynı çıkışı kullanmazsak karşılaştırma
# anlamsız olur — bu satır olmadan ilk denemede scale modunda ölçüm yapıldı.
bt.EXIT_MODE = "trail"

# BIST komisyonu kriptodan yüksek: aracı kurum + BSMV + borsa payı.
# Tek yön ~%0.05-0.15 arası değişiyor; temkinli tarafta gidiş-dönüş %0.2.
KOMISYON_PCT = 0.20


def sembol_test(sym, min_rr=1.5, max_rr=6.0):
    """Bir sembolün tüm geçmişini tarar, işlem listesi döner."""
    h1 = bd.fetch_bist(sym, "1h", "2y")
    d1 = bd.fetch_bist(sym, "1d", "5y")
    w1 = bd.fetch_bist(sym, "1wk", "5y")
    if len(h1) < 300 or len(d1) < 80 or len(w1) < 25:
        return []

    dc = [c["close_time"] for c in d1]
    wc = [c["close_time"] for c in w1]
    rows, son_ob = [], None

    for k in range(200, len(h1)):
        t = h1[k]["close_time"]
        sig = smc.find_setup(h1[:k + 1], d1[:bisect_right(dc, t)],
                             w1[:bisect_right(wc, t)],
                             setup_max_age=bt.SETUP_MAX_AGE_BARS,
                             sl_atr_mult=bt.SL_ATR_MULT,
                             min_rr=min_rr, max_rr=max_rr,
                             liq_len=bt.LIQ_LEN, discount_max=bt.DISCOUNT_MAX,
                             dir_filter="long")   # BIST: açığa satış kısıtlı
        if not sig:
            continue
        anahtar = (sig["dir"], round(sig["entry"], 10))
        if anahtar == son_ob:
            continue
        son_ob = anahtar
        r, durum, hareket, cikis, mfe = bt.simulate(sig, h1, k + 1)
        cikis = min(cikis, len(h1) - 1)
        rows.append({"sym": sym, "R": r, "status": durum, "move_pct": hareket,
                     "risk_pct": sig["risk_pct"], "t": h1[cikis]["close_time"],
                     "t_in": h1[k]["close_time"], "tip": sig["tip"],
                     "rr": sig["rr"], "mfe": mfe})
    return rows


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    semboller = bd.BIST_SEMBOLLER[:n]
    print(f"{len(semboller)} BIST sembolü, haftalık/günlük/1H, sadece LONG\n")

    rows = []
    for i, s in enumerate(semboller, 1):
        try:
            r = sembol_test(s)
        except Exception as e:
            print(f"[{s}] hata: {str(e)[:70]}")
            continue
        rows += r
        print(f"[{i}/{len(semboller)}] {s}: {len(r)} sinyal", flush=True)

    dolan = [r for r in rows if r["status"] != "expired"]
    if not dolan:
        print("\nDolan işlem yok.")
        return

    R = [r["R"] for r in dolan]
    n_ = len(R)
    ort = sum(R) / n_
    sd = math.sqrt(sum((x - ort) ** 2 for x in R) / (n_ - 1)) if n_ > 1 else 0
    se = sd / math.sqrt(n_) if n_ > 1 else 0
    t = ort / se if se else 0
    isabet = 100 * sum(1 for x in R if x > 0) / n_

    # Komisyon R cinsine İŞLEM BAŞINA çevrilir: her işlemin kendi stop
    # mesafesine bölünüp ortalaması alınır. Ortalama riske bölmek (ilk
    # yaptığım) komisyonu HAFİFE ALIYOR — 1/risk konveks olduğu için.
    # Ölçüldü: 100 işlemlik örneklemde 0.243R yerine 0.267R, yani %10 fark.
    ort_risk = sum(r["risk_pct"] for r in dolan) / n_
    kom_R = sum(KOMISYON_PCT / r["risk_pct"] for r in dolan
                if r["risk_pct"]) / n_
    ort_net = ort - kom_R

    print("\n" + "=" * 68)
    print("BIST SONUCU (sadece long, sürüklenen stop)")
    print("=" * 68)
    print(f"toplam sinyal   : {len(rows)}  (dolmayan {len(rows) - n_})")
    print(f"dolan işlem     : {n_}")
    print(f"isabet          : %{isabet:.1f}")
    print(f"beklenti (brüt) : {ort:+.3f}R   ±{1.96 * se:.3f} (95%)  t={t:+.2f}"
          f"  {'ANLAMLI' if abs(t) > 2 else 'GÜRÜLTÜ'}")
    print(f"ortalama risk   : %{ort_risk:.2f}")
    print(f"komisyon yükü   : -{kom_R:.3f}R  (gidiş-dönüş %{KOMISYON_PCT})")
    print(f"beklenti (NET)  : {ort_net:+.3f}R")
    print("-" * 68)
    # Sonuç tamamen komisyon varsayımına bağlı: stoplar çok dar (%~0.9)
    # olduğu için komisyon brüt avantajın büyük kısmını yiyor. Aracı kurum
    # oranına göre hüküm DEĞİŞİYOR, o yüzden tablo halinde gösteriliyor.
    print("KOMİSYON DUYARLILIĞI (gidiş-dönüş):")
    for k in (0.05, 0.10, 0.15, 0.20, 0.30):
        kr = sum(k / r["risk_pct"] for r in dolan if r["risk_pct"]) / n_
        net = ort - kr
        print(f"   %{k:<5.2f} -> komisyon {kr:.3f}R, net {net:+.3f}R"
              f"   {'KÂRLI' if net > 0 else 'ZARARLI'}")
    basabas = ort * (n_ / sum(1 / r["risk_pct"] for r in dolan if r["risk_pct"]))
    print(f"   başabaş komisyon: %{basabas:.3f} (bunun üstünde strateji zarar eder)")
    print("-" * 68)
    print("Kıyas — kripto 4H, sadece long, sürüklenen stop: +0.116R (t=1.25)")

    # Kazanç yoğunlaşması: bu projede defalarca birkaç işlemin sonucu
    # domine ettiği görüldü, o yüzden her sonuçta kontrol ediliyor.
    s_R = sorted(R, reverse=True)
    if n_ >= 10:
        top5 = sum(s_R[:5])
        toplam = sum(s_R)
        if toplam > 0:
            print(f"yoğunlaşma      : en iyi 5 işlem toplam kazancın "
                  f"%{100 * top5 / toplam:.0f}'i")


if __name__ == "__main__":
    main()
