"""
BIST tarayıcı — SMC haftalık/günlük/1H, yalnızca LONG, sinyal amaçlı.

Kripto tarayıcısıyla AYNI kurulum mantığını kullanır (smc_htf.find_setup);
strateji kodu kopyalanmaz, tek yerde durur.

KRİPTODAN FARKLAR — kasıtlı:
  - Giriş zaman dilimi 1 SAATLİK. Yahoo 4H sunmuyor ve BIST seansı zaten
    günde ~8 saat (kriptoda 4H'de günde 6 bar oluyordu).
  - Yalnızca LONG. Perakende için açığa satış kısıtlı.
  - EMİR GÖNDERİLMEZ. Aracı kurum API'si yok; bu tarayıcı sadece Telegram'a
    sinyal yollar, pozisyon takibi/stop yönetimi YAPMAZ.
  - Seans kapalıyken çalışmaz (yeni bar oluşmuyor, aynı sinyali tekrarlar).

Durum dosyası kriptodan AYRI (bist_state.json) — iki piyasanın kayıtları
karışmamalı.
"""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import bist_data as bd
import smc_htf as smc
from ict_scanner import send_telegram

STATE_FILE = os.path.join(os.path.dirname(__file__), "bist_state.json")

# Kripto tarafıyla aynı doğrulanmış parametreler
SETUP_MAX_AGE_BARS = 6
SL_ATR_MULT = 0.25
MIN_RR = 1.5
MAX_RR = 6.0
LIQ_LEN = 20
DISCOUNT_MAX = 0.5
WORKERS = int(os.environ.get("BIST_WORKERS") or "6")
SEANS_ZORUNLU = os.environ.get("BIST_SEANS_ZORUNLU", "1") == "1"


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"gonderilen": {}}


def save_state(state):
    # Sonsuz büyümesin: yalnızca son 300 sinyal hatırlanır (tekrar
    # göndermemek için yeterli).
    g = state.get("gonderilen", {})
    if len(g) > 300:
        for k in sorted(g, key=lambda x: g[x])[:len(g) - 300]:
            g.pop(k, None)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def _fmt(v):
    if v is None:
        return "n/a"
    return f"{v:,.2f}"


def sembol_verisi(sym):
    try:
        return sym, (bd.fetch_bist(sym, "1h", "2y"),
                     bd.fetch_bist(sym, "1d", "5y"),
                     bd.fetch_bist(sym, "1wk", "5y"))
    except Exception as e:
        print(f"[{sym}] veri alınamadı: {e}")
        return sym, None


def sinyal_mesaji(sym, sig):
    risk = abs(sig["entry"] - sig["sl"])
    tp_satir = []
    for i, tp in enumerate(sig["tps"], 1):
        if tp is None:
            continue
        rr = abs(tp - sig["entry"]) / risk if risk else 0
        tp_satir.append(f"  TP{i}: {_fmt(tp)}  (R:R {rr:.2f})")
    return (
        f"🇹🇷 <b>{sym} LONG</b>  (BIST · SMC H/G/1S)\n"
        f"<i>{sig['tip']} — order block girişi</i>\n\n"
        f"Giriş (limit) : <b>{_fmt(sig['entry'])} TL</b>\n"
        f"Stop          : <b>{_fmt(sig['sl'])} TL</b>  (%{sig['risk_pct']:.2f})\n"
        f"Referans hedefler:\n" + ("\n".join(tp_satir) or "  (yok)") + "\n\n"
        f"⚠️ Bu sinyal <b>otomatik işleme dönüşmez</b> — aracı kurum bağlantısı "
        f"yok, emri kendin girersin. Stop da senin takibinde.\n"
        f"⚠️ Strateji BIST'te KANITLANMIŞ DEĞİL; ölçülen sonuç kripto "
        f"verisinden geliyordu. bist_backtest.py ile ayrıca ölçüldü."
    )


def main():
    if SEANS_ZORUNLU and not bd.bist_acik_mi():
        print("BIST seansı kapalı (hafta içi 10:00-18:00 TRT), tarama atlandı.")
        return

    state = load_state()
    gonderilen = state.setdefault("gonderilen", {})
    semboller = bd.BIST_SEMBOLLER
    print(f"{len(semboller)} BIST sembolü taranıyor...")

    veriler = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for fut in as_completed([ex.submit(sembol_verisi, s) for s in semboller]):
            sym, v = fut.result()
            if v:
                veriler[sym] = v

    say = {"veri": len(veriler), "kisa": 0, "degerlendirilen": 0}
    bulunan = []
    for sym, (h1, d1, w1) in veriler.items():
        if len(h1) < 120 or len(d1) < 60 or len(w1) < 20:
            say["kisa"] += 1
            continue
        say["degerlendirilen"] += 1
        sig = smc.find_setup(h1, d1, w1, setup_max_age=SETUP_MAX_AGE_BARS,
                             sl_atr_mult=SL_ATR_MULT, min_rr=MIN_RR,
                             max_rr=MAX_RR, liq_len=LIQ_LEN,
                             discount_max=DISCOUNT_MAX, dir_filter="long")
        if not sig:
            continue
        # Aynı kurulumu tekrar gönderme (order block girişi anahtar)
        anahtar = f"{sym}:{round(sig['entry'], 6)}"
        if anahtar in gonderilen:
            continue
        bulunan.append((sym, sig, anahtar))

    print(f"Veri {say['veri']}/{len(semboller)} | kısa geçmiş {say['kisa']} | "
          f"değerlendirilen {say['degerlendirilen']} | YENİ kurulum {len(bulunan)}")

    for sym, sig, anahtar in bulunan:
        print(f"[{sym}] KURULUM {sig['tip']} giriş={sig['entry']:.4g} "
              f"stop={sig['sl']:.4g} R:R={sig['rr']:.2f}")
        send_telegram(sinyal_mesaji(sym, sig))
        gonderilen[anahtar] = int(time.time())

    if bulunan:
        send_telegram(
            f"🇹🇷 <b>BIST tarama özeti</b>\n"
            f"Taranan: {say['degerlendirilen']}/{len(semboller)} sembol\n"
            f"Yeni kurulum: <b>{len(bulunan)}</b>")

    save_state(state)
    print("BIST taraması bitti.")


if __name__ == "__main__":
    main()
