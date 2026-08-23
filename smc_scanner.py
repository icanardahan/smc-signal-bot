"""
SMC Haftalık/Günlük/4H tarayıcı — sürüklenen stoplu, long + short.

Kullanıcının TradingView'de takip ettiği LuxAlgo SMC göstergesinin kavramlarına
göre çalışır ve baktığı üç zaman dilimini kullanır: haftalık ve günlük yön,
4 saatlik giriş.

Akış:
  haftalık + günlük yön AYNI olmalı -> fiyat discount bölgesinde -> 4H'de
  bias yönünde taze CHoCH/BOS -> order block ortasına BEKLEYEN limit emir ->
  stop OB'nin altına -> çıkış SÜRÜKLENEN STOP (sabit kâr al YOK).

Neden bu biçim (hepsi 2 yıl, 40 sembol, sabit evren üzerinde ölçüldü):
  - Sürüklenen stop: sabit TP1/TP2/TP3 ile +0.122R (t=1.71, gürültü);
    sürüklenen stopla +0.240R (t=2.33). Kazancın çoğu birkaç büyük
    işlemden geldiği için TP1'de yarıyı kapatmak tam da onları kesiyordu.
  - Hedef seviyesi yalnızca R:R filtresi olarak kullanılır; işlem oraya
    varınca kapanmaz.

Yön: her ikisi de gönderilir. Ölçümde short beklentisi -0.046R, long
+0.116R idi — yani shortların avantajı gösterilemedi. Otomatik işlem KAPALI
olduğu için sinyaller tavsiye niteliğinde ve yön seçimi kullanıcıya ait.

DİKKAT — bu strateji KANITLANMIŞ DEĞİLDİR. Çok sayıda varyant denendiği için
t=2.33 tek başına yeterli kanıt sayılmaz ve 2026 dilimi eksidir.
Bot Binance'e hiçbir emir göndermez; yalnızca sinyal üretir.

Kurulum arama ve stop sürükleme mantığı smc_htf.py içindedir; backtest de
AYNI fonksiyonları çağırır, böylece ikisi ayrışamaz.
"""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import smc_htf as smc
import smc_report as rap
from ict_scanner import (BINANCE_BASE, get_usdt_symbols,
                         http_get_json, fetch_klines, send_telegram)

STATE_FILE = os.path.join(os.path.dirname(__file__), "smc_state.json")

# --- Doğrulanmış parametreler (backtest ile birebir aynı olmalı) ---
SETUP_MAX_AGE_BARS = 6      # kırılım en fazla 1 gün eski
SL_ATR_MULT = 0.25
MIN_RR = 1.5
MAX_RR = 6.0
LIQ_LEN = 20
DISCOUNT_MAX = 0.5
# Her iki yön de gönderilir. ÖLÇÜM UYARISI: 2 yıllık backtest'te long
# beklentisi +0.116R, short -0.046R idi — yani shortların ölçülebilir bir
# avantajı yok. Otomatik işlem kapalı olduğu için sinyaller tavsiye
# niteliğinde; short'ları alıp almamak kullanıcının kararı.
DIR_FILTER = os.environ.get("SMC_DIR", "")     # "long" | "short" | "" (ikisi)
TRAIL_LEN = 5
FILL_TIMEOUT_BARS = 30      # 5 gün dolmazsa emir iptal
HOLD_TIMEOUT_BARS = 60      # 10 gün sonra pozisyon kapatılır

# 0 = tüm USDT pariteleri (~480). Backtest ilk 40'ta yapılmıştı;
# geniş evren daha çok sinyal verir ama düşük hacimli paritelerde
# spread/kayma backtest'in komisyon modelinden yüksek olabilir.
UNIVERSE_SIZE = int(os.environ.get("SMC_UNIVERSE") or "0")
WORKERS = int(os.environ.get("SMC_WORKERS") or "12")
# Aynı anda açık pozisyon sınırı. GERÇEK PARADA bu bir risk sınırıdır:
# her işlem bakiyenin %2'sini riske ettiği için 5 slot = aynı anda %10 risk.
# 30 slot sinyal servisi içindi (%60 risk demek olurdu) ve hiç test edilmedi.
# Portföy simülasyonunda ölçülenler: 3 slot 499$/%25 düşüş, 5 slot 433$/%30,
# 10 slot 358$/%31. Kripto korelasyonu +0.25 olduğu için bu pozisyonlar
# birbirini dengelemiyor (etkin bağımsız işlem ~3.4).
MAX_OPEN = int(os.environ.get("SMC_MAX_OPEN") or "0")   # 0 = SINIRSIZ
RISK_PCT_OF_BALANCE = float(os.environ.get("SMC_RISK_PCT") or "2.0")
LEVERAGE = 10

H4_LIMIT = 1000     # Binance azami; 500 barda bazı kurulumlar kaçıyordu
D1_LIMIT = 300
W1_LIMIT = 200

BAR_MS = 4 * 3600 * 1000
STALE_MS = 3 * BAR_MS       # son 4H mum bundan eskiyse sembol atlanır

LIVE_STATES = ("pending", "open")
CLOSED_STATES = ("stopped", "trail_stop", "expired", "timeout", "invalidated")

# Kâğıt üzerinde takip: her işlem 10 USDT marj, 10x kaldıraç -> 100 USDT nominal
PAPER_MARGIN = 10.0
PAPER_LEVERAGE = 10
PAPER_NOTIONAL = PAPER_MARGIN * PAPER_LEVERAGE
FEE_PCT = 0.07              # gidiş-dönüş: maker %0.02 giriş + taker %0.05 çıkış


# ---------------- Evren ----------------
def futures_symbols():
    """Vadeli (USDⓈ-M perpetual) olarak işlem gören semboller, yoksa None.

    Mum verisi SPOT ucundan geliyor ama emirler VADELİ hesapta açılıyor ve her
    spot paritesinin vadeli karşılığı yok. Ölçüldü: 481 spot paritesinin
    152'sinin (%32) vadeli piyasası yok — hacimde ilk 5'teki ALLOUSDT dahil.
    Bunlar elenmezse Telegram'a sinyal gider, state'e pozisyon yazılır ama
    emir borsada açılamaz."""
    import binance_trader as bt
    base = bt.TESTNET_BASE if bt._testnet() else bt.LIVE_BASE
    try:
        info = http_get_json(f"{base}/fapi/v1/exchangeInfo")
    except Exception as e:
        print(f"UYARI: vadeli sembol listesi alınamadı ({e}); "
              "evren filtrelenmedi, bazı sinyallerde emir açılamayabilir.")
        return None
    return {s["symbol"] for s in info.get("symbols", [])
            if s.get("contractType") == "PERPETUAL" and s.get("status") == "TRADING"}


def top_symbols(n=UNIVERSE_SIZE):
    """Hacme göre sıralı, GERÇEKTEN işlem gören USDT pariteleri. n=0 ise hepsi.

    İşlem durumu exchangeInfo'dan doğrulanır. 24 saatlik ticker tek başına
    yeterli değil: listeden kalkmış pariteler orada hâlâ hacimle görünüyor.
    Ölçüldü — TOMOUSDT status=BREAK, son mumu 2023-11-20, ama ticker 1.09M
    hacim bildiriyordu ve tarayıcı ona kurulum üretip emir açacaktı."""
    canli = set(get_usdt_symbols())
    vadeli = futures_symbols()
    if vadeli:
        atilan = len(canli - vadeli)
        canli &= vadeli
        print(f"Vadeli piyasası olmayan {atilan} sembol evrenden çıkarıldı.")
    data = http_get_json(f"{BINANCE_BASE}/api/v3/ticker/24hr")
    rows = []
    for t in data:
        s = t["symbol"]
        if s not in canli:
            continue
        try:
            rows.append((float(t["quoteVolume"]), s))
        except (KeyError, ValueError):
            continue
    rows.sort(reverse=True)
    return [s for _, s in (rows[:n] if n else rows)]


def fetch_symbol_data(symbol):
    """Bir sembol için üç zaman dilimi. Hata olursa (symbol, None) döner."""
    try:
        return symbol, (fetch_klines(symbol, "4h", H4_LIMIT),
                        fetch_klines(symbol, "1d", D1_LIMIT),
                        fetch_klines(symbol, "1w", W1_LIMIT))
    except Exception as e:
        print(f"[{symbol}] veri alınamadı: {e}")
        return symbol, None


def fetch_all(symbols, workers=WORKERS):
    """Sembol verilerini paralel çeker, tamamlanma sırasıyla verir.

    Seri çekimde 481 sembol ~42 dakika sürüyordu; saatlik cron ile koşular
    üst üste binerdi. Binance ağırlık limiti dakikada 6000, bu tarama
    ~3000 ağırlık tutuyor, bu yüzden işçi sayısı ölçülü tutuldu."""
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for fut in as_completed([ex.submit(fetch_symbol_data, s) for s in symbols]):
            yield fut.result()


# ---------------- Durum ----------------
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"positions": {}}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def _fmt(v):
    if v is None:
        return "n/a"
    return f"{v:.8f}".rstrip("0").rstrip(".") if v < 1 else f"{v:,.4f}".rstrip("0").rstrip(".")


def _ts(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%d.%m %H:%M UTC")


def getiri_serisi(h4, bar=540):
    """Korelasyon için 4H getiri serisi (~90 gün). Ek istek gerektirmez."""
    kap = [c["close"] for c in h4[-bar:]]
    return [(kap[i] - kap[i - 1]) / kap[i - 1]
            for i in range(1, len(kap)) if kap[i - 1]]


# ---------------- Pozisyon takibi ----------------
def monitor(pos, h4):
    """Bekleyen/açık pozisyonu yeni 4H barlarıyla ilerletir.

    Backtest'teki simulate() ile aynı sırayı izler: önce stop kontrolü, dolum
    mumunda stop sürüklenmez, stop yalnızca lehe hareket eder."""
    olaylar = []
    entry, stop = pos["entry"], pos["stop"]
    is_long = pos["dir"] == "long"
    tl = smc.trail_levels(h4, is_long, TRAIL_LEN)

    for k, c in enumerate(h4):
        if c["close_time"] <= pos["last_bar"]:
            continue
        pos["last_bar"] = c["close_time"]
        pos["bars"] = pos.get("bars", 0) + 1
        just_filled = False

        if pos["status"] == "pending":
            if c["low"] <= entry <= c["high"]:
                pos["status"] = "open"
                pos["fill_time"] = c["close_time"]
                pos["bars"] = 0
                just_filled = True
                olaylar.append(("filled", c))
            elif pos["bars"] >= FILL_TIMEOUT_BARS:
                pos["status"] = "expired"
                olaylar.append(("expired", c))
                break
            else:
                continue

        # Dolum mumunda STOP KONTROLÜ YAPILMAZ. Eskiden stop kontrolü bu
        # kontrolden ÖNCE yapılıyordu; geniş bir mumda fiyat hem girişe hem
        # stop seviyesine değince bot pozisyonu "stopped" sayıp izlemeyi
        # BIRAKIYORDU — ama borsada stop emri henüz hiç kurulmamıştı (bir
        # sonraki adımda kurulacaktı), yani pozisyon gerçekte AÇIK ve
        # KORUMASIZ kalmaya devam ediyordu ve bot artık onu görmüyordu.
        # PAXGUSDT'de tam bu yaşandı. Backtest'teki simulate() de aynı
        # kuralı uygular: dolum mumunda ne kâr ne zarar sayılmaz.
        if just_filled:
            continue

        if (c["low"] <= stop) if is_long else (c["high"] >= stop):
            pos["status"] = "trail_stop" if pos.get("trailed") else "stopped"
            pos["exit"] = stop
            olaylar.append((pos["status"], c))
            break

        yeni = tl[k]
        if yeni is not None:
            ileri = (yeni > stop) if is_long else (yeni < stop)
            if ileri:
                stop = yeni
                pos["stop"] = stop
                pos["trailed"] = True
                olaylar.append(("trail", c))

        if pos["bars"] >= HOLD_TIMEOUT_BARS:
            pos["status"] = "timeout"
            pos["exit"] = c["close"]
            olaylar.append(("timeout", c))
            break

    return olaylar


def setup_still_valid(pos, h4, d1, w1):
    """Bekleyen kurulumun dayandığı yapı hâlâ ayakta mı? (gerekçe, geçerli mi)

    Stop ihlali ölçüt olarak KULLANILAMAZ: giriş order block'un ortasında,
    stop ise altında/üstünde: fiyat stopa ulaşmak için önce girişten geçmek
    zorunda, yani emir zaten dolardı. Kurulumu bozan şey fiyat değil YAPI —
    üst zaman dilimi yönünün dönmesi ya da 4H'de ters kırılım."""
    yon = smc.BULLISH if pos["dir"] == "long" else smc.BEARISH
    w = smc.bias_of(w1, 10)
    d = smc.bias_of(d1, 20)
    if not w or not d or w != d:
        return False, "haftalık ve günlük yön artık uyuşmuyor"
    if w != yon:
        return False, "üst zaman dilimi yönü döndü"
    olaylar, _ = smc.structure(h4, smc.INTERNAL_LEN)
    for i, y, *_ in olaylar:
        if i < len(h4) and h4[i]["close_time"] > pos["signal_time"] and y != yon:
            return False, "4H'de ters yönde yapı kırılımı oluştu"
    return True, ""


def pnl_usd(move_pct, kapandi=True):
    """Kâğıt üzerinde sonuç: her işlem 10 USDT marj, 10x → 100 USDT nominal.

    Kapanan işlemlerde gidiş-dönüş komisyonu düşülür (maker giriş %0.02 +
    taker çıkış %0.05 = nominalin %0.07'si)."""
    kar = PAPER_NOTIONAL * move_pct / 100
    return kar - (PAPER_NOTIONAL * FEE_PCT / 100 if kapandi else 0.0)


def pnl_pct(pos, price):
    e = pos["entry"]
    return (price - e) / e * 100 if pos["dir"] == "long" else (e - price) / e * 100


# ---------------- Mesajlar ----------------
def summary_message(taranan, degerlendirilen, kurulum, positions, fiyatlar,
                    gecmis, seriler=None):
    """Her taramada gönderilen özet: ne tarandı, ne açık, toplam kâr/zarar."""
    canli = [(s, p) for s, p in positions.items() if p["status"] in LIVE_STATES]

    kapanan = pnl_usd_toplam = 0.0
    kazanan = 0
    for g in gecmis:
        kapanan += 1
        pnl_usd_toplam += g["pnl"]
        if g["pnl"] > 0:
            kazanan += 1

    # Açık pozisyonların anlık (gerçekleşmemiş) sonucu — komisyon düşülmez,
    # işlem henüz kapanmadı.
    acik_pnl = sum(pnl_usd(pnl_pct(p, fiyatlar[s]), kapandi=False)
                   for s, p in canli
                   if p["status"] == "open" and s in fiyatlar)

    sinir = MAX_OPEN or "∞"
    satir = [f"📊 <b>Tarama özeti</b>",
             f"Taranan: <b>{taranan}</b> sembol "
             f"(değerlendirilen {degerlendirilen})",
             f"Bulunan kurulum: <b>{kurulum}</b>",
             f"Açık/bekleyen: <b>{len(canli)}</b>/{sinir}",
             "",
             f"💰 <b>Kâğıt üzerinde sonuç</b> "
             f"(işlem başına {PAPER_MARGIN:.0f}$ marj, {PAPER_LEVERAGE}x)",
             f"Kapanan {int(kapanan)} işlem"
             + (f", isabet %{100 * kazanan / kapanan:.0f}" if kapanan else "")
             + f" → <b>{pnl_usd_toplam:+.2f}$</b>"]
    if acik_pnl:
        satir.append(f"Açık pozisyonlar: <b>{acik_pnl:+.2f}$</b>")
        satir.append(f"Toplam: <b>{pnl_usd_toplam + acik_pnl:+.2f}$</b>")

    # Sinyaller bağımsız mı? Kripto pariteleri birlikte hareket ettiği için
    # N sinyal N ayrı bahis değildir; bu satır kaç bahsin olduğunu söyler.
    if seriler:
        yon_sayi = {}
        for _s, p in canli:
            yon_sayi[p["dir"]] = yon_sayi.get(p["dir"], 0) + 1
        n, ort_kor, etkin = rap.etkin_bagimsiz(
            [seriler[s] for s, _p in canli if s in seriler])
        sat = rap.korelasyon_satiri(n, ort_kor, etkin, yon_sayi)
        if sat:
            satir += ["", sat]

    if canli:
        satir.append("")
        for s, p in sorted(canli, key=lambda x: x[1]["status"]):
            yon = p["dir"].upper()
            # TP'ler yalnızca referans (kapatma yapılmaz) — bkz. signal_message.
            tp_liste = [t for t in (p.get("tps") or []) if t is not None]
            tp_str = f"  TP: {' / '.join(_fmt(t) for t in tp_liste)}" if tp_liste else ""
            if p["status"] == "pending":
                f = fiyatlar.get(s)
                uzak = f"  (fiyat {_fmt(f)}, %{abs(f - p['entry']) / p['entry'] * 100:.1f} uzakta)" \
                    if f else ""
                satir.append(f"⏳ <b>{s}</b> {yon} bekliyor @ {_fmt(p['entry'])}{uzak}"
                             f"{tp_str}")
            else:
                f = fiyatlar.get(s)
                kz = f"  <b>{pnl_pct(p, f):+.2f}%</b>" if f else ""
                kilit = " 🔒" if p.get("trailed") else ""
                kilit += " ⚠️yapı" if p.get("uyarildi") else ""
                satir.append(f"▶️ <b>{s}</b> {yon} @ {_fmt(p['entry'])}{kz}\n"
                             f"     stop {_fmt(p['stop'])}{kilit}{tp_str}")
    else:
        satir.append("\nAçık pozisyon yok.")
    return "\n".join(satir)


def signal_message(symbol, sig, bal):
    # Sizing sabit marjin DEĞİL: stop mesafesi işlemden işleme çok değişiyor,
    # sabit marjinde risk 16 kata kadar farklılaşıyor. Doğrulanan sonuç
    # işlem başına bakiyenin %2'sini riske eden sizing ile ölçüldü.
    uzun = sig["dir"] == "long"
    risk_usdt = bal * RISK_PCT_OF_BALANCE / 100
    # MAX_OPEN=0 (sınırsız) durumunda bölme yapılamaz; binance_trader ile
    # AYNI tavan kullanılır (bakiyenin %10'u = 10 slotluk ayarın karşılığı).
    pay = (1.0 / MAX_OPEN) if MAX_OPEN else 0.10
    notional = min(risk_usdt / (sig["risk_pct"] / 100), bal * pay * LEVERAGE)
    marj = notional / LEVERAGE
    risk_mesafe = abs(sig["entry"] - sig["sl"])

    # TP1/TP2/TP3 yalnızca REFERANS — otomatik kısmi kapatma YOK, çıkışı hâlâ
    # sürüklenen stop yönetiyor. Ölçüldü: sabit kademeli TP ile kısmi kapatma
    # +0.122R (t=1.71, gürültü) veriyordu, sürüklenen stopla +0.240R
    # (t=2.33) — kazancın çoğu birkaç büyük işlemden geldiği için TP1'de
    # yarıyı kapatmak tam da onları kesiyordu. Bu satırlar sadece "fiyat
    # buraya gelirse R:R şu olur" bilgisini veriyor.
    tp_satirlari = []
    for i, tp in enumerate(sig["tps"], 1):
        if tp is None:
            continue
        rr = abs(tp - sig["entry"]) / risk_mesafe if risk_mesafe else 0
        tp_satirlari.append(f"  TP{i}: {_fmt(tp)}  (R:R {rr:.2f})")
    tp_blok = "\n".join(tp_satirlari) if tp_satirlari else "  (hesaplanamadı)"

    return (
        f"{'🟢' if uzun else '🔴'} <b>{symbol} {sig['dir'].upper()}</b>  (SMC H/G/4S)\n"
        f"<i>{sig['tip']} — order block girişi</i>\n\n"
        f"Giriş (limit) : <b>{_fmt(sig['entry'])}</b>\n"
        f"Başlangıç stop: <b>{_fmt(sig['sl'])}</b>  (%{sig['risk_pct']:.2f})\n"
        f"Referans hedefler (kapatma yapılmaz):\n{tp_blok}\n\n"
        f"⚠️ <b>Sabit kâr al yok.</b> Stop, 4H yapının arkasından "
        f"{'yukarı' if uzun else 'aşağı'} "
        f"sürüklenir; işlem stop ile kapanır. TP seviyeleri yalnızca "
        f"kurulum filtresi/referanstır, orada otomatik kapatma yapılmaz.\n\n"
        f"Büyüklük: <b>{notional:.1f} USDT</b> nominal "
        f"(~{marj:.2f} USDT marj, {LEVERAGE}x izole)\n"
        f"Riskin: <b>{risk_usdt:.2f} USDT</b> — stop mesafesi %{sig['risk_pct']:.2f} "
        f"olduğu için büyüklük buna göre küçültüldü.\n"
        f"Emir {FILL_TIMEOUT_BARS // 6} gün dolmazsa iptal."
    )


def event_message(symbol, pos, event, candle):
    p = pos["entry"]
    if event == "filled":
        return (f"✅ <b>{symbol}</b> emir doldu → pozisyon açık\n"
                f"Giriş {_fmt(p)} | stop {_fmt(pos['stop'])}")
    if event == "trail":
        return (f"🔒 <b>{symbol}</b> stop {'yukarı' if pos['dir'] == 'long' else 'aşağı'} "
                f"taşındı → <b>{_fmt(pos['stop'])}</b>\n"
                f"Giriş {_fmt(p)} | fiyat {_fmt(candle['close'])} "
                f"({pnl_pct(pos, candle['close']):+.2f}%)")
    if event in ("stopped", "trail_stop"):
        r = pnl_pct(pos, pos["exit"])
        ikon = "🟡" if event == "trail_stop" else "🔴"
        ad = "sürüklenen stop" if event == "trail_stop" else "stop"
        return (f"{ikon} <b>{symbol}</b> {ad} ile kapandı @ {_fmt(pos['exit'])}\n"
                f"Sonuç: <b>{r:+.2f}%</b> (10x → {r * 10:+.1f}% marj)")
    if event == "timeout":
        r = pnl_pct(pos, pos["exit"])
        return (f"⏱ <b>{symbol}</b> süre doldu, kapatıldı @ {_fmt(pos['exit'])}\n"
                f"Sonuç: <b>{r:+.2f}%</b>")
    if event == "expired":
        return f"⚪️ <b>{symbol}</b> emir {FILL_TIMEOUT_BARS // 6} günde dolmadı, iptal."
    if event == "yapi_uyari":
        return (f"⚠️ <b>{symbol}</b> {pos['dir'].upper()} — <b>yapı bozuldu</b>: "
                f"{pos.get('sebep', '')}.\n"
                f"Pozisyon KAPATILMADI, çıkış hâlâ sürüklenen stopta "
                f"({_fmt(pos['stop'])}). Erken kapatmak istersen karar senin.")
    if event == "invalidated":
        return (f"❌ <b>{symbol}</b> {pos['dir'].upper()} kurulumu GEÇERSİZ — "
                f"{pos.get('sebep', 'yapı bozuldu')}.\nBekleyen emir listeden çıkarıldı; "
                f"bu emri verdiysen iptal et.")
    return f"{symbol}: {event}"


# ---------------- Ana akış ----------------
def _init_trading():
    try:
        import binance_trader as bt
    except Exception as e:
        print(f"Otomatik işlem modülü yüklenemedi: {e}")
        return None, None, 0.0
    if not bt._enabled():
        return None, None, 0.0
    api = bt.BinanceFutures()
    try:
        api.load_filters()
        bal = api.sizing_balance()
    except Exception as e:
        print(f"Binance bağlantı hatası, otomatik işlem devre dışı: {e}")
        return None, None, 0.0
    mod = "TESTNET" if bt._testnet() else "GERÇEK PARA"
    if bt._dry_run():
        mod += " / KURU ÇALIŞMA"
    print(f"Otomatik işlem AÇIK — {mod} | bakiye {bal:.2f} USDT")

    # Vadeli YAZMA izni var mı? Okuma çalışsa bile emir açılamayabilir
    # (ölçüldü: enableFutures=False iken bakiye okunuyor, emir -2015 veriyor).
    # Binance bu izni yalnızca IP kısıtlı anahtarlara verdiği için, ev IP'si
    # değiştiğinde de aynı hata gelir; mesajda güncel IP'yi bildiriyoruz.
    if not bt._testnet():
        izin = api.api_izinleri()
        if izin is not None and not izin.get("enableFutures", True):
            ip = api.dis_ip()
            uyari = ("🚫 <b>Emir açılamıyor — API anahtarında vadeli izni yok</b>\n"
                     "<code>enableFutures=False</code>\n\n"
                     "Binance bu izni yalnızca IP kısıtlaması uygulanmış "
                     f"anahtarlara veriyor. Bu makinenin IP'si:\n<code>{ip}</code>\n\n"
                     "Anahtar, vadeli hesap açılmadan ÖNCE oluşturulduysa izin "
                     "eklenemez; yeni anahtar üretmen gerekir.")
            print("UYARI: enableFutures=False — emirler açılamaz. IP: " + ip)
            send_telegram(uyari)
            return bt, api, bal
    return bt, api, bal


def main():
    state = load_state()
    positions = state.setdefault("positions", {})
    gecmis = state.setdefault("gecmis", [])   # kapanan işlemler (kâğıt üzerinde)
    kosu = state.setdefault("kosu", {})
    simdi_ts = int(time.time())
    kosu["sayi"] = kosu.get("sayi", 0) + 1
    kosu.setdefault("ilk", simdi_ts)
    kosu["son"] = simdi_ts
    bt, api, bal = _init_trading()
    kuru = bool(api and api.dry)
    if not bal:
        bal = 100.0

    # Borsa ile state mutabakatı: bekleyen kaydın borsada karşılığı yoksa
    # (ne pozisyon ne emir) o kayıt gerçek değildir ve slot işgal eder.
    if api and not kuru:
        try:
            borsa_poz = api.positions()   # dict: {symbol: {amt, entry, side}}
            borsa_emir = {o["symbol"] for o in api.open_orders()}
            hayalet = [s for s, p in positions.items()
                       if p["status"] == "pending"
                       and s not in borsa_poz and s not in borsa_emir]
            if hayalet:
                print(f"UYARI: borsada karşılığı olmayan {len(hayalet)} bekleyen "
                      f"kayıt düşürüldü: {', '.join(hayalet)}")
                for s in hayalet:
                    positions.pop(s, None)
                send_telegram(
                    f"🧹 <b>Hayalet kayıt temizlendi</b>\n"
                    f"{len(hayalet)} bekleyen kaydın Binance'te karşılığı yoktu "
                    f"(ne pozisyon ne emir): {', '.join(hayalet)}\n"
                    f"Slotlar boşaltıldı.")

            # TERSİ yön: state "kapandı" diyor ama borsada pozisyon HÂLÂ
            # AÇIK. Bu, monitor()'un mum verisine göre "stopa değdi" tahmini
            # ile gerçek borsa durumu arasında (özellikle geçmişte yaşanan
            # dolum-mumunda-stop hatası yüzünden) uyuşmazlık olduğunda
            # oluşur — CLOSED_STATES'e düşen kayıt bir daha hiç izlenmez ve
            # borsadaki pozisyon sonsuza kadar unutulurdu.
            unutulan = [s for s, p in positions.items()
                        if p["status"] in CLOSED_STATES and s in borsa_poz]
            if unutulan:
                print(f"UYARI: {len(unutulan)} kayıt 'kapandı' sanılıyordu "
                      f"ama borsada hâlâ AÇIK: {', '.join(unutulan)}")
                for s in unutulan:
                    pb = borsa_poz[s]
                    positions[s]["status"] = "open"
                    positions[s]["dir"] = pb["side"]
                    positions[s]["entry"] = pb["entry"]
                    positions[s].pop("kaydedildi", None)
                send_telegram(
                    f"🚨 <b>Unutulmuş pozisyon bulundu</b>\n"
                    f"{len(unutulan)} kayıt kapandı sanılıyordu, borsada hâlâ "
                    f"açık: {', '.join(unutulan)}\nYeniden izlemeye alındı, "
                    f"koruma kontrol ediliyor.")
        except Exception as e:
            print(f"borsa mutabakatı yapılamadı: {e}")

    fiyatlar = {}
    seriler = {}

    # 1) Açık/bekleyen pozisyonları ilerlet
    for symbol, pos in list(positions.items()):
        if pos["status"] not in LIVE_STATES:
            continue
        try:
            h4 = fetch_klines(symbol, "4h", H4_LIMIT)
        except Exception as e:
            print(f"[{symbol}] veri alınamadı: {e}")
            continue
        if h4:
            fiyatlar[symbol] = h4[-1]["close"]
            seriler[symbol] = getiri_serisi(h4)

        olaylar = monitor(pos, h4)

        # Dolumu MUM KAPANMASINI BEKLEMEDEN yakala. monitor() yalnızca
        # KAPANMIŞ mumlara bakar; bir limit emir 4H bar henüz kapanmadan
        # içeride dolabilir ve bu durumda status "pending" kalmaya devam
        # eder — ta ki bar kapanana kadar (en kötü ~4 saat). O süre boyunca
        # ensure_protection hiç çağrılmıyordu ve pozisyon borsada tamamen
        # açık, stopsuz kalıyordu (15 pozisyonda böyle yaşandı, ölçüldü).
        if api and pos["status"] == "pending":
            try:
                p_borsa = api.positions().get(symbol)
                if p_borsa and p_borsa["amt"]:
                    pos["status"] = "open"
                    pos["fill_time"] = int(time.time() * 1000)
                    pos["bars"] = 0
                    olaylar.append(("filled", h4[-1] if h4 else {"close": pos["entry"]}))
            except Exception as e:
                print(f"  [{symbol}] borsa pozisyon kontrolü başarısız: {e}")

        # Dayandığı YAPI hâlâ ayakta mı?
        #   bekleyen -> emri beklemenin anlamı yok, listeden düşer
        #   AÇIK     -> yalnızca UYARILIR, pozisyon kapatılmaz. Çıkışı
        #               değiştirmek stratejiyi backtest edilenden farklı hale
        #               getirir; kapatma varyantı ayrıca ölçülüyor.
        if pos["status"] in LIVE_STATES and not pos.get("uyarildi"):
            try:
                d1 = fetch_klines(symbol, "1d", D1_LIMIT)
                w1 = fetch_klines(symbol, "1w", W1_LIMIT)
                gecerli, sebep = setup_still_valid(pos, h4, d1, w1)
                if not gecerli:
                    pos["sebep"] = sebep
                    if pos["status"] == "pending":
                        pos["status"] = "invalidated"
                        olaylar.append(("invalidated", h4[-1]))
                    else:
                        pos["uyarildi"] = True    # her saat tekrarlamasın
                        olaylar.append(("yapi_uyari", h4[-1]))
            except Exception as e:
                print(f"  [{symbol}] geçerlilik kontrolü yapılamadı: {e}")

        # Kapanış olayları (stopped/trail_stop/timeout) botun KENDİ mum
        # simülasyonuna dayanır — canlıda bu bir tahmindir, kanıt değil.
        # Ölçüldü: PAXGUSDT için bot "trail_stop" (kapandı) dedi ama borsada
        # pozisyon HÂLÂ AÇIKTI (gerçek stop henüz o seviyeye taşınmamıştı).
        # Böyle bir olay borsayla doğrulanmadan geçerse hem yanlış bir
        # "kapandı" mesajı gider hem de kâğıt geçmişine sahte kayıt girer.
        if api and pos["status"] in CLOSED_STATES:
            try:
                hâlâ_acik = symbol in api.positions()
            except Exception:
                hâlâ_acik = False   # borsaya sorulamadıysa iyimser davranma
            if hâlâ_acik:
                print(f"  [{symbol}] {pos['status']} SİMÜLE EDİLDİ ama borsada "
                      f"hâlâ açık — olay İPTAL, izlemeye devam")
                pos["status"] = "open"
                pos.pop("exit", None)
                olaylar = [(e, c) for e, c in olaylar
                          if e not in ("stopped", "trail_stop", "timeout")]

        for event, candle in olaylar:
            print(f"  [{symbol}] {event}")
            send_telegram(event_message(symbol, pos, event, candle))

        if api and pos["status"] == "open":
            try:
                p = api.positions().get(symbol)
                if p and p["amt"]:
                    bt.ensure_protection(api, symbol, pos["dir"], p["amt"],
                                         pos["stop"], (None, None, None))
                    # Stopu HER taramada mutabakatla, yalnızca "trail" olayında
                    # değil. Tek bir başarısız taşıma, state ile borsayı sessizce
                    # ayrıştırıyordu: bot stopu yeni seviyede sanarken borsada
                    # eski seviye kalıyor ve aynı seviye için bir daha olay
                    # üretilmediği için fark hiç kapanmıyordu.
                    bt.update_stop(api, symbol, pos["dir"], pos["stop"])
            except Exception as e:
                # Bu hata eskiden yalnızca terminale yazılıyordu. Yaşandı:
                # PAXGUSDT'de update_stop eski stopu iptal edip yeniyi
                # koyamadı, pozisyon bir tur boyunca TAMAMEN KORUMASIZ kaldı
                # ve kimseye haber gitmedi — bu tam olarak fark edilmemesi
                # gereken türden bir olay. "KORUMASIZ" geçen hatalar artık
                # acil Telegram uyarısı olarak gidiyor.
                print(f"  [{symbol}] koruma/stop mutabakatı başarısız: {e}")
                if "KORUMASIZ" in str(e):
                    send_telegram(
                        f"🚨🚨 <b>{symbol} KORUMASIZ</b>\n"
                        f"Stop taşınamadı ve pozisyonun borsada koruma emri "
                        f"YOK olabilir. Binance'i hemen kontrol et.\n"
                        f"Hata: {str(e)[:200]}")

        if pos["status"] in CLOSED_STATES and not pos.get("kaydedildi"):
            pos["kaydedildi"] = True
            dolmus = pos["status"] in ("stopped", "trail_stop", "timeout")
            hareket = pnl_pct(pos, pos.get("exit", pos["entry"])) if dolmus else None
            risk = pos.get("risk_pct") or 0
            gecmis.append({
                "sym": symbol, "dir": pos["dir"], "tip": pos.get("tip"),
                "durum": pos["status"], "dolmus": dolmus,
                "rr": pos.get("rr"), "risk_pct": risk,
                "hacim_sirasi": pos.get("hacim_sirasi"),
                "move_pct": hareket,
                "R": (hareket / risk) if (dolmus and risk) else None,
                "pnl": pnl_usd(hareket) if dolmus else 0.0,
                "t_sinyal": pos.get("signal_time"), "t": pos["last_bar"],
            })

        if api and pos["status"] in CLOSED_STATES:
            try:
                if pos["status"] == "timeout":
                    bt.close_position(api, symbol, pos["dir"])
                else:
                    bt.cancel_everything(api, symbol)
            except Exception as e:
                print(f"  [{symbol}] kapanış temizliği başarısız: {e}")

    acik = sum(1 for p in positions.values() if p["status"] in LIVE_STATES)

    # 2) Yeni kurulum ara
    symbols = top_symbols()
    print(f"{len(symbols)} sembol taranıyor | açık/bekleyen: {acik}/{MAX_OPEN}")
    simdi = int(time.time() * 1000)
    sira = {s: i for i, s in enumerate(symbols)}      # hacim sırası
    aday = [s for s in symbols
            if (positions.get(s) or {}).get("status") not in LIVE_STATES]

    # ÖNCE hepsini değerlendir, SONRA seç. Paralel çekimde sonuçlar tamamlanma
    # sırasıyla geliyor; slotları ilk gelene vermek, 480 sembol içinde seçimi
    # rastgeleleştirirdi. Hacim sırasına göre seçmek hem belirlenimli hem de
    # likit pariteleri önceler (backtest de en likit 40 üzerinde yapılmıştı).
    bulunan = []
    say = {"donen": 0, "hata": 0, "bayat": 0, "kisa": 0, "degerlendirilen": 0}
    for symbol, veri in fetch_all(aday):
        say["donen"] += 1
        if veri is None:
            say["hata"] += 1
            continue
        h4, d1, w1 = veri
        # Veri bayatlık koruması: sembol listesi filtresinden sızan ölü bir
        # piyasa kalırsa, eski mumlar üzerinde kurulum üretilmesin.
        if not h4 or (simdi - h4[-1]["close_time"]) > STALE_MS:
            say["bayat"] += 1
            continue
        # find_setup kısa geçmişte None döner; bunu "kurulum yok" ile
        # karıştırmamak için ayrı sayılıyor, yoksa kaç sembolün gerçekten
        # değerlendirildiği görünmez.
        if len(h4) < 120 or len(d1) < 60 or len(w1) < 20:
            say["kisa"] += 1
            continue
        say["degerlendirilen"] += 1
        sig = smc.find_setup(h4, d1, w1, setup_max_age=SETUP_MAX_AGE_BARS,
                             sl_atr_mult=SL_ATR_MULT, min_rr=MIN_RR,
                             max_rr=MAX_RR, liq_len=LIQ_LEN,
                             discount_max=DISCOUNT_MAX, dir_filter=DIR_FILTER)
        if not sig:
            continue
        # Aynı order block'a tekrar girme
        p = positions.get(symbol)
        if p and abs(p.get("entry", 0) - sig["entry"]) < 1e-12:
            continue
        bulunan.append((sira[symbol], symbol, sig, h4[-1]["close_time"],
                        h4[-1]["close"], getiri_serisi(h4)))

    print(f"Veri dönen {say['donen']}/{len(aday)} | hata {say['hata']} | "
          f"bayat {say['bayat']} | kısa geçmiş {say['kisa']} | "
          f"DEĞERLENDİRİLEN {say['degerlendirilen']} | kurulum {len(bulunan)}")

    bulunan.sort()
    if bulunan:
        alinacak = len(bulunan) if not MAX_OPEN else min(len(bulunan), MAX_OPEN - acik)
        print(f"{len(bulunan)} kurulum bulundu, {alinacak} "
              f"tanesi alınacak (hacim sırasına göre):")
        for _, s, g, _t, _f, _r in bulunan:
            print(f"    {s:14s} {g['tip']:5s} R:R={g['rr']:.2f} risk=%{g['risk_pct']:.2f}")

    for _, symbol, sig, bar_time, son_fiyat, seri in bulunan:
        if MAX_OPEN and acik >= MAX_OPEN:
            print("Eşzamanlı pozisyon sınırı dolu, kalan kurulumlar alınmadı.")
            break

        print(f"[{symbol}] KURULUM {sig['tip']} giriş={sig['entry']:.6g} "
              f"stop={sig['sl']:.6g} R:R={sig['rr']:.2f}")
        send_telegram(signal_message(symbol, sig, bal))

        if kuru:
            # KURU çalışmada emir GÖNDERİLMİYOR; state'e yazmak kaydı
            # gerçekmiş gibi gösterir. Bir kez başımıza geldi: prova koşusu
            # 5 sahte "pending" yazdı, gerçek işlem açılınca bot "sınır dolu"
            # deyip HİÇ emir açmadı ve bu sessizce oldu.
            print(f"  [{symbol}] KURU — state'e yazılmadı")
            continue

        # ÖNCE emri aç, SONRA kaydet. Ters sırada yapılıyordu ve emir
        # reddedilince (izin hatası, yetersiz marj, filtre) kayıt state'te
        # kalıp slotu kilitliyordu; sonraki koşular "sınır dolu" deyip hiç
        # işlem açmıyordu. Bir kez tam olarak bu yaşandı: 5 emir -2015 aldı,
        # 5 hayalet kayıt kaldı.
        if api:
            try:
                sonuc = bt.open_trade_trailing(api, symbol, sig["dir"], sig["entry"],
                                               sig["sl"], bal,
                                               risk_pct_of_balance=RISK_PCT_OF_BALANCE,
                                               max_open=MAX_OPEN)
            except Exception as e:
                print(f"  [{symbol}] emir açılamadı: {e}")
                continue
            if not sonuc:
                print(f"  [{symbol}] emir yerleştirilemedi, kaydedilmedi")
                continue

        positions[symbol] = {
            "status": "pending", "dir": sig["dir"], "entry": sig["entry"],
            "sl": sig["sl"], "stop": sig["sl"], "trailed": False, "bars": 0,
            "signal_time": bar_time, "last_bar": bar_time,
            "rr": sig["rr"], "risk_pct": sig["risk_pct"], "tip": sig["tip"],
            "hacim_sirasi": sira.get(symbol), "tps": sig["tps"],
        }
        fiyatlar[symbol] = son_fiyat
        seriler[symbol] = seri
        acik += 1

    # SON GÜVENLİK AĞI: hangi sebepten olursa olsun (bilinen/bilinmeyen hata,
    # zamanlama sorunu, API gecikmesi) korumasız kalan pozisyon MUTLAKA
    # yakalanır. Bu turda tam olarak böyle bir durum yaşandı — ensure_protection
    # çağrılması gerekirken sessizce atlandı, sebep tam olarak tespit
    # edilemedi. Kök sebep bulunamasa bile bu kontrol açığı kapatır.
    if api and not kuru:
        try:
            son_poz = api.positions()
            son_algo = api.algo_open_orders()
            son_stoplu = {o.get("symbol") for o in son_algo
                         if (o.get("orderType") or o.get("type")) == "STOP_MARKET"}
            korumasiz = [s for s in son_poz if s not in son_stoplu]
            if korumasiz:
                print(f"SON KONTROL: {len(korumasiz)} pozisyon KORUMASIZ, "
                      f"acil stop koyuluyor: {', '.join(korumasiz)}")
                for sym in korumasiz:
                    p = son_poz[sym]
                    try:
                        fiyat = float(api._request(
                            "GET", "/fapi/v1/ticker/price", {"symbol": sym})["price"])
                        acil = api.round_price(
                            sym, fiyat * (1.015 if p["side"] == "short" else 0.985))
                        api.place_stop(sym, p["side"], acil)
                        print(f"  [{sym}] acil stop kondu @ {acil}")
                    except Exception as e:
                        print(f"  [{sym}] ACİL STOP DA BAŞARISIZ: {e}")
                        send_telegram(f"🚨🚨🚨 <b>{sym} KORUMASIZ VE ACİL STOP "
                                      f"KONULAMADI</b>\nHemen Binance'i kontrol et.\n{e}")
                        continue
                send_telegram(
                    f"🚨 <b>Son kontrolde {len(korumasiz)} korumasız pozisyon "
                    f"bulundu</b>\n{', '.join(korumasiz)}\n"
                    f"Acil stop kondu, ama NEDEN korumasız kaldığı bilinmiyor "
                    f"— logu kontrol et.")
        except Exception as e:
            print(f"son güvenlik kontrolü yapılamadı: {e}")

    save_state(state)
    send_telegram(summary_message(len(symbols), say["degerlendirilen"],
                                  len(bulunan), positions, fiyatlar, gecmis,
                                  seriler))

    # Haftalık karne
    if simdi_ts - kosu.get("son_rapor", kosu["ilk"]) >= 7 * 86400:
        kosu["son_rapor"] = simdi_ts
        hafta = max(1e-9, (simdi_ts - kosu["ilk"]) / (7 * 86400))
        send_telegram(rap.haftalik_rapor(gecmis, hafta,
                                         sum(g["pnl"] for g in gecmis)))

    # Günlük nabız — bot sustuğunda fark edebilmek için
    if simdi_ts - kosu.get("son_nabiz", 0) >= 86400:
        kosu["son_nabiz"] = simdi_ts
        gun = (simdi_ts - kosu["ilk"]) / 86400
        send_telegram(f"💚 <b>Bot ayakta</b> — {kosu['sayi']} koşu, "
                      f"{gun:.1f} gündür çalışıyor.\n"
                      f"Son tarama: {len(symbols)} sembol, "
                      f"{len([1 for p in positions.values() if p['status'] in LIVE_STATES])} "
                      f"canlı kayıt.")
    print("Tarama bitti.")


if __name__ == "__main__":
    main()
