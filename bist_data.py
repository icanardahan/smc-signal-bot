"""
BIST (Borsa İstanbul) veri katmanı — Yahoo Finance üzerinden.

Neden Yahoo: Binance'te BIST yok. Ücretsiz, anahtar gerektirmeyen ve
BIST sembollerini `.IS` ekiyle sunan tek pratik kaynak. Ölçüldü:
1 saatlik veri 2 yıl (4574 bar), günlük 5 yıl, haftalık 5 yıl geriye gidiyor.

KRİPTODAN FARKLAR (stratejiyi doğrudan taşımadan önce bilinmeli):
  - BIST hafta içi ~10:00-18:00 (TRT) açık; kripto 7/24. Gün başına
    ~8 saatlik bar var, kriptoda 4H'de 6 bar oluyordu.
  - Yahoo 4 saatlik aralık SUNMUYOR (1m/5m/15m/30m/60m/1h/1d/1wk/1mo).
    Bu yüzden giriş zaman dilimi olarak 1 SAATLİK kullanılıyor; haftalık
    ve günlük yön aynen korunuyor.
  - Perakende için açığa satış kısıtlı -> yalnızca LONG üretilir.
  - Emir gönderilecek bir aracı kurum API'si yok -> yalnızca SİNYAL.

UYARI: Stratejinin ölçülmüş sonucu (+0.240R) KRİPTO 4H verisinde elde
edildi. BIST tamamen farklı bir piyasa (seans saatleri, likidite, takas,
fiyat adımı). O sonuç buraya taşınamaz; bist_backtest.py ile ayrıca
ölçülmelidir.
"""

import json
import time
import urllib.request
import urllib.error

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart"
UA = {"User-Agent": "Mozilla/5.0"}

# BIST'te en çok işlem gören paritelerden bir çekirdek liste. Yahoo'da
# bulunamayan semboller (ör. isim değişikliği) sessizce atlanır.
BIST_SEMBOLLER = [
    "THYAO", "GARAN", "AKBNK", "ISCTR", "YKBNK", "BIMAS", "ASELS", "KCHOL",
    "SAHOL", "TUPRS", "EREGL", "FROTO", "TCELL", "SISE", "PGSUS", "TOASO",
    "HEKTS", "PETKM", "VESTL", "ARCLK", "ENKAI", "TAVHL", "MGROS", "SASA",
    "KRDMD", "OYAKC", "TTKOM", "DOHOL", "ALARK", "EKGYO", "GUBRF", "ODAS",
    "SOKM", "TKFEN", "AEFES", "CIMSA", "AKSEN", "BRSAN", "ISDMR", "KONTR",
]


def _istek(url, deneme=3):
    for i in range(deneme):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and i < deneme - 1:
                time.sleep(2 * (i + 1))
                continue
            raise
        except Exception:
            if i < deneme - 1:
                time.sleep(1)
                continue
            raise
    return None


def fetch_bist(symbol, interval="1h", rng="2y"):
    """BIST mumları, ict_scanner.fetch_klines ile AYNI sözlük biçiminde.

    Yahoo eksik bar (None) döndürebiliyor — tatil/seans dışı boşluklar.
    Bunlar atılır, aksi halde yapı analizi None üzerinde patlar."""
    sym = symbol if symbol.endswith(".IS") else f"{symbol}.IS"
    d = _istek(f"{YAHOO}/{sym}?interval={interval}&range={rng}")
    res = (d or {}).get("chart", {}).get("result")
    if not res:
        return []
    r = res[0]
    ts = r.get("timestamp") or []
    q = (r.get("indicators", {}).get("quote") or [{}])[0]
    o, h, l, c, v = (q.get("open"), q.get("high"), q.get("low"),
                     q.get("close"), q.get("volume"))
    if not all([o, h, l, c]):
        return []

    # Bar süresi: close_time hesaplamak için (kripto tarafıyla aynı sözleşme:
    # close_time = barın KAPANDIĞI an).
    saniye = {"1h": 3600, "60m": 3600, "1d": 86400, "1wk": 604800}.get(interval, 3600)

    out = []
    simdi = time.time()
    for i, t in enumerate(ts):
        if None in (o[i], h[i], l[i], c[i]):
            continue                      # eksik bar
        kapanis = t + saniye
        if kapanis > simdi:
            continue                      # henüz kapanmamış bar
        out.append({
            "open_time": int(t * 1000),
            "close_time": int(kapanis * 1000) - 1,
            "open": float(o[i]), "high": float(h[i]),
            "low": float(l[i]), "close": float(c[i]),
            "volume": float(v[i]) if v and v[i] is not None else 0.0,
        })
    return out


def bist_acik_mi(ts=None):
    """BIST seansı açık mı? (kaba kontrol: hafta içi 10:00-18:00 TRT)

    Kapalıyken taramak anlamsız — yeni mum oluşmaz, aynı sinyaller
    tekrar üretilir. Tatiller bu kontrole dahil değil; kapalı günde
    zaten yeni bar gelmeyeceği için tarama boşa döner, zarar vermez."""
    from datetime import datetime, timezone, timedelta
    trt = timezone(timedelta(hours=3))
    n = datetime.fromtimestamp(ts or time.time(), trt)
    if n.weekday() >= 5:                  # cumartesi/pazar
        return False
    return 10 <= n.hour < 18
