"""
Portföy simülatörü — eşzamanlı pozisyon sınırıyla.

Neden gerekli: önceki bakiye simülasyonu işlemleri sırayla işliyordu, yani
2026-07'de açılan 83 short'u 83 bağımsız işlem sayıyordu. Gerçekte bunlar
aynı anda açık, aynı yönde ve kripto korelasyonu yüzünden neredeyse aynı
bahis. Sonucu hem kâr hem risk tarafında çarpıtıyor.

Burada gerçek işleyiş modellenir:
  - işlemler GİRİŞ zamanına göre sıralanır
  - aynı anda en fazla MAX_OPEN pozisyon; slot doluysa sinyal KAÇIRILIR
  - her pozisyon bakiyenin 1/MAX_OPEN'ı kadar teminat, LEVERAGE kaldıraç
  - komisyon: giriş bekleyen limit (maker), çıkış stop/piyasa (taker)
"""

MAKER = 0.02 / 100
TAKER = 0.05 / 100


def run(rows, max_open=5, leverage=10, start_balance=100.0):
    """rows: t_in, t (çıkış), move_pct, dir alanlarını içeren işlem listesi."""
    islemler = sorted((r for r in rows if r["status"] != "expired"),
                      key=lambda r: r["t_in"])
    bakiye = start_balance
    zirve = bakiye
    mdd = 0.0
    acik = []                 # (cikis_zamani, teminat, move_pct)
    alinan = kacan = 0
    egri = []

    def kapat_kadar(t):
        nonlocal bakiye, zirve, mdd, acik
        for p in sorted([p for p in acik if p[0] <= t]):
            _, teminat, move = p
            notional = teminat * leverage
            pnl = notional * move / 100 - notional * (MAKER + TAKER)
            bakiye += pnl
            zirve = max(zirve, bakiye)
            if zirve > 0:
                mdd = max(mdd, 100 * (zirve - bakiye) / zirve)
            egri.append((p[0], bakiye))
        acik = [p for p in acik if p[0] > t]

    for r in islemler:
        kapat_kadar(r["t_in"])
        if bakiye <= 1:
            break
        if len(acik) >= max_open:
            kacan += 1
            continue
        teminat = bakiye / max_open
        acik.append((r["t"], teminat, r["move_pct"]))
        alinan += 1

    kapat_kadar(float("inf"))
    return {"bakiye": bakiye, "mdd": mdd, "alinan": alinan,
            "kacan": kacan, "egri": egri}
