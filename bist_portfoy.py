"""
BIST portföy simülasyonu — R'yi gerçek TL getirisine çevirir.

Neden ayrı: R (risk katı) tek başına para değeri değildir. BIST'te iki sert
kısıt var ve ikisi de sonucu belirliyor:

  1. KALDIRAÇ YOK. Nakit hesapta pozisyon sermayeyi aşamaz. Stoplar ortalama
     ~%1 uzakta olduğu için "sermayenin %2'sini riske at" kuralı burada
     uygulanamaz — o kural 5000 TL için ~9000 TL'lik pozisyon gerektirirdi.
  2. EŞZAMANLILIK. Aynı anda ancak N pozisyon tutulabilir; sermaye bölünür.
     Sinyal sayısı 1889 olsa da bunların çoğu aynı anda açık olacağı için
     hepsi alınamaz.

Bu ikisi modellenmezse sonuç saçma çıkar: çıkış zamanı kaydedilmediği bir
denemede her pozisyon "anında kapandı" sayıldı, eşzamanlılık hiç
uygulanmadı ve 5000 TL -> 98.000 TL (+1865%) gibi tamamen sahte bir rakam
üretildi. Bu dosya o hatayı tekrarlamamak için yazıldı.
"""

import json
import sys

VERI = "/tmp/bist_full_rows.json"


def simule(rows, sermaye=5000.0, max_acik=5, spread_pct=0.05, min_lot_tl=0.0):
    """Kronolojik portföy simülasyonu.

    rows: t_in (giriş), t (çıkış), R, risk_pct alanlarını içermeli.
    spread_pct: çıkışta ödenen tek yön maliyet (giriş bekleyen limit emir).
    """
    isl = sorted((r for r in rows if r["status"] != "expired"),
                 key=lambda x: x["t_in"])
    bakiye = sermaye
    acik = []                      # (cikis_ts, pozisyon_tl, R, risk_pct)
    alinan = kacan = 0
    zirve = bakiye
    mdd = 0.0
    egri = []

    def kapat_kadar(ts):
        nonlocal bakiye, zirve, mdd, acik
        for p in sorted([p for p in acik if p[0] <= ts]):
            _, poz, R, risk = p
            hareket = R * risk - spread_pct      # % cinsinden net hareket
            bakiye += poz * hareket / 100
            zirve = max(zirve, bakiye)
            if zirve > 0:
                mdd = max(mdd, 100 * (zirve - bakiye) / zirve)
            egri.append((p[0], bakiye))
        acik = [p for p in acik if p[0] > ts]

    for r in isl:
        kapat_kadar(r["t_in"])
        if bakiye <= 0:
            break
        if len(acik) >= max_acik:
            kacan += 1
            continue
        poz = bakiye / max_acik
        if poz < min_lot_tl:
            kacan += 1
            continue
        acik.append((r.get("t", r["t_in"]), poz, r["R"], r["risk_pct"]))
        alinan += 1

    kapat_kadar(float("inf"))
    return {"bakiye": bakiye, "alinan": alinan, "kacan": kacan,
            "mdd": mdd, "egri": egri}


def main():
    sermaye = float(sys.argv[1]) if len(sys.argv) > 1 else 5000.0
    rows = json.load(open(VERI))
    dolan = [r for r in rows if r["status"] != "expired"]
    if not dolan or "t" not in dolan[0]:
        print("HATA: veride çıkış zamanı ('t') yok — simülasyon yapılamaz.")
        return

    import datetime
    t0 = min(r["t_in"] for r in dolan) / 1000
    t1 = max(r["t"] for r in dolan) / 1000
    yil = (t1 - t0) / (365 * 86400)
    ort_risk = sum(r["risk_pct"] for r in dolan) / len(dolan)
    print(f"veri: {len({r['sym'] for r in dolan})} sembol, {len(dolan)} dolan işlem")
    print(f"dönem: {datetime.date.fromtimestamp(t0)} -> "
          f"{datetime.date.fromtimestamp(t1)} ({yil:.1f} yıl)")
    print(f"ortalama stop mesafesi: %{ort_risk:.2f}  (BIST'te kaldıraç yok)\n")

    print(f"SERMAYE {sermaye:,.0f} TL — eşzamanlı pozisyon sayısına göre:\n")
    print(f"{'poz.':>5}{'işlem':>8}{'kaçan':>8}{'son bakiye':>14}"
          f"{'getiri':>10}{'yıllık':>9}{'azami düşüş':>13}")
    for mo in (1, 2, 3, 5, 8, 10):
        s = simule(dolan, sermaye, mo)
        getiri = 100 * (s["bakiye"] / sermaye - 1)
        yillik = ((s["bakiye"] / sermaye) ** (1 / yil) - 1) * 100 if yil > 0 else 0
        print(f"{mo:>5}{s['alinan']:>8}{s['kacan']:>8}"
              f"{s['bakiye']:>13,.0f}₺{getiri:>+9.1f}%{yillik:>+8.1f}%"
              f"{s['mdd']:>12.0f}%")

    print("\nSPREAD DUYARLILIĞI (5 eşzamanlı pozisyon):")
    for sp in (0.0, 0.05, 0.10, 0.20):
        s = simule(dolan, sermaye, 5, spread_pct=sp)
        print(f"   spread %{sp:<5.2f} -> {s['bakiye']:>10,.0f}₺  "
              f"({100*(s['bakiye']/sermaye-1):+.1f}%)")


if __name__ == "__main__":
    main()
