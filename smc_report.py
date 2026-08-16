"""
İleri test raporlaması — kâr/zarar sayacının ötesinde, "bu avantaj gerçek mi"
sorusuna cevap üretmeye çalışan kısım.

İçerik:
  - etkin_bagimsiz(): kaç sinyalin GERÇEKTEN bağımsız bahis olduğu
  - haftalik_rapor(): gerçekleşen beklentiyi backtest'inkiyle karşılaştırır
  - karar_suresi(): bu hızda kaç hafta sonra karar verilebileceğini kestirir

Neden: backtest +0.240R diyor ama t=2.33 ve çok sayıda varyant denendi.
İleri testin tek amacı bunu görülmemiş veriyle sınamak; sayıları toplamak
yetmiyor, beklentiyle KARŞILAŞTIRMAK gerekiyor.
"""

import math

BACKTEST_BEKLENTI = 0.240      # R — sadece long, sürüklenen stop, 2 yıl, 40 sembol
BACKTEST_ISABET = 37.8         # %


def _std(x):
    if len(x) < 2:
        return 0.0
    m = sum(x) / len(x)
    return math.sqrt(sum((v - m) ** 2 for v in x) / (len(x) - 1))


def _kor(a, b):
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    pay = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    payda = math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))
    return pay / payda if payda else 0.0


def etkin_bagimsiz(seriler):
    """Eşit ağırlıklı sepette KAÇ bağımsız bahis olduğu ve ortalama korelasyon.

    Kripto pariteleri birlikte hareket ettiği için 10 sinyal 10 bağımsız
    işlem değildir; çoğu aynı anda kazanır, aynı anda kaybeder. Bu oturumda
    ölçüldü: 2026-07'de üretilen 83 short toplam -17.7R yaptı — 83 ayrı
    kayıp değil, tek bir bahsin 83 parçası.

    N_etkin = (tek sembolün ortalama oynaklığı / sepetin oynaklığı)^2
    Tam bağımsızlarsa N, tam korelasyonluysa 1 verir."""
    seriler = [s for s in seriler if len(s) > 5]
    n = len(seriler)
    if n < 2:
        return n, 0.0, None
    boy = min(len(s) for s in seriler)
    seriler = [s[-boy:] for s in seriler]

    ciftler = [_kor(seriler[i], seriler[j])
               for i in range(n) for j in range(i + 1, n)]
    ort_kor = sum(ciftler) / len(ciftler) if ciftler else 0.0

    tek = sum(_std(s) for s in seriler) / n
    sepet = _std([sum(s[i] for s in seriler) / n for i in range(boy)])
    if sepet <= 0 or tek <= 0:
        return n, ort_kor, None
    return n, ort_kor, min(n, (tek / sepet) ** 2)


def korelasyon_satiri(n, ort_kor, etkin, yon_sayi):
    if etkin is None or n < 2:
        return None
    uyari = "⚠️ " if etkin < n * 0.6 else ""
    yonler = " / ".join(f"{v} {k}" for k, v in yon_sayi.items())
    return (f"{uyari}<b>{n}</b> sinyal ({yonler}) ama etkin bağımsız işlem "
            f"≈ <b>{etkin:.1f}</b> (ort. korelasyon {ort_kor:+.2f}).\n"
            f"Hepsini eşit büyüklükte alırsan risk {n} işleme yayılmıyor.")


def r_istatistigi(gecmis):
    """Dolan ve kapanan işlemlerin R dağılımı."""
    r = [g["R"] for g in gecmis if g.get("dolmus") and g.get("R") is not None]
    if not r:
        return None
    n = len(r)
    ort = sum(r) / n
    sd = _std(r)
    se = sd / math.sqrt(n) if n > 1 else 0.0
    return {"n": n, "ort": ort, "sd": sd, "se": se,
            "t": ort / se if se else 0.0,
            "isabet": 100 * sum(1 for x in r if x > 0) / n,
            "alt": ort - 1.96 * se, "ust": ort + 1.96 * se}


def karar_suresi(ist, hafta_sayisi):
    """Bu işlem hızıyla, beklentiyi sıfırdan ayırmak kaç hafta daha sürer?

    Gereken örneklem: n >= (1.96 * sd / beklenti)^2"""
    if not ist or ist["n"] < 5 or hafta_sayisi <= 0:
        return None
    hedef = abs(BACKTEST_BEKLENTI)
    if ist["sd"] <= 0 or hedef <= 0:
        return None
    gereken = (1.96 * ist["sd"] / hedef) ** 2
    hiz = ist["n"] / hafta_sayisi
    if hiz <= 0:
        return None
    return max(0.0, (gereken - ist["n"]) / hiz), int(gereken)


def haftalik_rapor(gecmis, hafta_sayisi, toplam_pnl):
    """Backtest beklentisiyle karşılaştırmalı karne."""
    ist = r_istatistigi(gecmis)
    s = ["📋 <b>Haftalık karne — ileri test</b>", ""]

    dolan = sum(1 for g in gecmis if g.get("dolmus"))
    toplam = len(gecmis)
    if toplam:
        s.append(f"Kapanan kayıt: {toplam} (dolan {dolan}, "
                 f"dolmadan iptal {toplam - dolan})")

    if not ist:
        s.append("\nHenüz dolup kapanan işlem yok — karşılaştırma için erken.")
        return "\n".join(s)

    fark = ist["ort"] - BACKTEST_BEKLENTI
    s += [f"İşlem: <b>{ist['n']}</b> | isabet %{ist['isabet']:.1f} "
          f"(backtest %{BACKTEST_ISABET:.1f})",
          f"Beklenti: <b>{ist['ort']:+.3f}R</b> "
          f"(%95: {ist['alt']:+.3f} … {ist['ust']:+.3f})",
          f"Backtest beklentisi: {BACKTEST_BEKLENTI:+.3f}R → fark {fark:+.3f}R",
          f"Kâğıt üzerinde sonuç: <b>{toplam_pnl:+.2f}$</b>", ""]

    # Yorum: sıfırdan ve backtest'ten ayırt edilebiliyor mu?
    if ist["alt"] > 0:
        s.append("✅ Beklenti sıfırın ANLAMLI ölçüde üstünde.")
    elif ist["ust"] < 0:
        s.append("🔴 Beklenti sıfırın ANLAMLI ölçüde altında — avantaj yok.")
    else:
        s.append("⏳ Güven aralığı sıfırı içeriyor: henüz karar verilemez.")

    if not (ist["alt"] <= BACKTEST_BEKLENTI <= ist["ust"]):
        s.append("⚠️ Gerçekleşen, backtest beklentisiyle uyuşmuyor "
                 "(aralık +0.240R'yi kapsamıyor).")

    ks = karar_suresi(ist, hafta_sayisi)
    if ks:
        kalan, gereken = ks
        s.append(f"\nBu hızda karar için ~<b>{gereken}</b> işlem gerekiyor; "
                 f"tahminen <b>{kalan:.0f}</b> hafta daha.")
    return "\n".join(s)
