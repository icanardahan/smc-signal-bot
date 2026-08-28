"""
TÜM BIST hisselerinde SMC backtest'i + likidite dilimine göre kırılım.

Neden dilim: 40 sembollük ilk koşuda tüm evren +0.259R verirken, EN LİKİT
8 sembol yalnızca +0.040R veriyordu. Yani avantaj likit isimlerden
gelmiyordu. Bu kritik, çünkü avantaj likit olmayan hisselerden geliyorsa
oralarda spread daha geniştir ve maliyet varsayımı ters yönde bozulur —
"kârlı" görünen sonuç pratikte işlem yapılamaz olabilir.

Sonuçlar artımlı olarak /tmp/bist_full_rows.json'a yazılır; koşu uzun
sürdüğü için yarıda kesilse bile eldeki veri analiz edilebilir.

Kullanım:  python bist_full_backtest.py
"""

import json
import math
import os
import time

import bist_data as bd
import smc_htf as smc
import smc_htf_backtest as bt

bt.EXIT_MODE = "trail"          # canlı stratejiyle aynı çıkış

from bisect import bisect_right   # noqa: E402

CIKTI = "/tmp/bist_full_rows.json"


def sembol_test(sym):
    h1 = bd.fetch_bist(sym, "1h", "2y")
    d1 = bd.fetch_bist(sym, "1d", "5y")
    w1 = bd.fetch_bist(sym, "1wk", "5y")
    if len(h1) < 300 or len(d1) < 80 or len(w1) < 25:
        return [], 0.0

    # Likidite: son 20 günün ortalama TL işlem hacmi
    hacim = sum(c["volume"] * c["close"] for c in d1[-20:]) / 20 if len(d1) >= 20 else 0.0

    dc = [c["close_time"] for c in d1]
    wc = [c["close_time"] for c in w1]
    rows, son_ob = [], None
    for k in range(200, len(h1)):
        t = h1[k]["close_time"]
        sig = smc.find_setup(h1[:k + 1], d1[:bisect_right(dc, t)],
                             w1[:bisect_right(wc, t)],
                             setup_max_age=bt.SETUP_MAX_AGE_BARS,
                             sl_atr_mult=bt.SL_ATR_MULT, min_rr=1.5, max_rr=6.0,
                             liq_len=bt.LIQ_LEN, discount_max=bt.DISCOUNT_MAX,
                             dir_filter="long")
        if not sig:
            continue
        anahtar = (sig["dir"], round(sig["entry"], 10))
        if anahtar == son_ob:
            continue
        son_ob = anahtar
        r, durum, hareket, cikis, mfe = bt.simulate(sig, h1, k + 1)
        cikis = min(cikis, len(h1) - 1)
        # ÇIKIŞ ZAMANI ŞART: portföy simülasyonu pozisyonun ne kadar açık
        # kaldığını bilmeden eşzamanlılık sınırını uygulayamaz. Kaydedilmediği
        # ilk denemede her pozisyon "anında kapandı" sayılıp 1889 kez üst üste
        # bileşik getiri uygulandı ve +1865% gibi tamamen sahte bir sonuç çıktı.
        rows.append({"sym": sym, "R": r, "status": durum,
                     "risk_pct": sig["risk_pct"], "hacim": hacim,
                     "t_in": h1[k]["close_time"], "t": h1[cikis]["close_time"],
                     "fiyat": h1[k]["close"]})
    return rows, hacim


def ozet(rows, ad, maliyet_pct=0.0):
    dolan = [r for r in rows if r["status"] != "expired"]
    n = len(dolan)
    if n < 5:
        return f"{ad:<22} n={n:<5} (yetersiz)"
    R = [r["R"] for r in dolan]
    ort = sum(R) / n
    sd = math.sqrt(sum((x - ort) ** 2 for x in R) / (n - 1)) if n > 1 else 0
    se = sd / math.sqrt(n) if n else 0
    t = ort / se if se else 0
    isabet = 100 * sum(1 for x in R if x > 0) / n
    kom = (sum(maliyet_pct / r["risk_pct"] for r in dolan if r["risk_pct"]) / n
           if maliyet_pct else 0.0)
    return (f"{ad:<22} n={n:<5} isabet=%{isabet:<5.1f} brüt={ort:+.3f}R "
            f"t={t:+5.2f} {'ANLAMLI' if abs(t) > 2 else 'gürültü ':<8} "
            + (f"net(%{maliyet_pct})={ort - kom:+.3f}R" if maliyet_pct else ""))


def main():
    semboller = [s for s in open("/tmp/bist_uygun.txt").read().split()]
    print(f"{len(semboller)} BIST sembolü taranacak (tahmini 4-6 saat)\n", flush=True)

    tum = []
    if os.path.exists(CIKTI):        # yarıda kalmışsa devam et
        try:
            tum = json.load(open(CIKTI))
            bitmis = {r["sym"] for r in tum}
            semboller = [s for s in semboller if s not in bitmis]
            print(f"kaldığı yerden devam: {len(bitmis)} sembol zaten var\n")
        except Exception:
            tum = []

    for i, s in enumerate(semboller, 1):
        t0 = time.time()
        try:
            rows, hacim = sembol_test(s)
        except Exception as e:
            print(f"[{s}] hata: {str(e)[:60]}", flush=True)
            continue
        tum += rows
        print(f"[{i}/{len(semboller)}] {s}: {len(rows)} sinyal "
              f"({time.time()-t0:.0f}sn)", flush=True)
        if i % 10 == 0:
            json.dump(tum, open(CIKTI, "w"))

    json.dump(tum, open(CIKTI, "w"))
    rapor(tum)


def rapor(tum):
    print("\n" + "=" * 78)
    print("TÜM BIST — SMC (haftalık/günlük/1H, sadece long, sürüklenen stop)")
    print("=" * 78)
    print(ozet(tum, "TÜM EVREN", 0.0))
    print(ozet(tum, "  (spread %0.05)", 0.05))
    print(ozet(tum, "  (spread %0.10)", 0.10))

    # Likidite dilimleri — avantaj nereden geliyor?
    hacimler = sorted({r["hacim"] for r in tum if r.get("hacim")}, reverse=True)
    if not hacimler:
        return
    print("\nLİKİDİTE DİLİMİNE GÖRE (günlük ort. TL hacim):")
    dilimler = [
        ("çok likit >1000M", lambda h: h > 1000e6),
        ("likit 200-1000M", lambda h: 200e6 < h <= 1000e6),
        ("orta 50-200M", lambda h: 50e6 < h <= 200e6),
        ("düşük 10-50M", lambda h: 10e6 < h <= 50e6),
        ("çok düşük <10M", lambda h: h <= 10e6),
    ]
    for ad, kosul in dilimler:
        alt = [r for r in tum if kosul(r.get("hacim", 0))]
        print("  " + ozet(alt, ad, 0.05))


if __name__ == "__main__":
    main()
