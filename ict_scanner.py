"""
ICT 2022 Trading Model — Binance USDT paritelerini tarar, kurulum bulduğunda
Telegram'a sinyal gönderir ve açılan pozisyonu SL/TP'ye kadar izler.

Michael Huddleston'ın "Complete ICT Trading Strategy – 2022 Trading Model"
dokümanının otomatik uygulamasıdır.

Modelin akışı (dokümandaki sıra):
  1. Daily bias belirlenir (günlük grafik). Net bias yoksa işlem aranmaz.
  2. NY gece yarısı açılışından (00:00 NY) seans açılışına kadar olan fiyat
     aralığının HIGH/LOW'u işaretlenir:
       - London kurulumu   -> aralık 00:00-03:00 NY
       - New York kurulumu -> aralık 00:00-08:00 NY (dokümandaki "senaryo II",
         yalnızca London aralığı süpürmediyse geçerlidir)
  3. Seans açılınca aralığın likiditesi bias'ın TERSİ yönde süpürülür
     (Liquidity Sweep / Judas swing).
  4. Süpürmeden sonra 5 dakikalık grafikte bias YÖNÜNDE Market Structure Shift
     + Displacement aranır.
  5. Displacement'ın bıraktığı PD Array (FVG) işaretlenir; giriş buraya
     BEKLEYEN (limit) emirle konur. FVG yoksa bacağın OTE (0.618-0.786)
     bölgesi kullanılır.
  6. SL süpürülen ekstremin ötesine, TP aralığın karşı tarafına konur.
     Doküman 1:3 ve üzeri R:R hedefler (MIN_TP1_RR).

Zaman dilimleri: Daily -> bias, 5m -> MSS/displacement/giriş.
Tüm saatler New York yerel saatiyle hesaplanır (zoneinfo), böylece yaz/kış
saati (EST/EDT) geçişinde pencereler kaymaz.

Bu, otomatikleştirilebilir bir yaklaşımdır — dokümandaki metodolojinin birebir
yerine geçmez, yatırım tavsiyesi değildir.
"""

import json
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

# api.binance.com bazı bölgelerden (ör. GitHub Actions ABD sunucuları) 451 ile
# engelleniyor; data-api.binance.vision aynı public uçları kısıtlamasız sunuyor.
BINANCE_BASE = "https://data-api.binance.vision"
STATE_FILE = os.path.join(os.path.dirname(__file__), "ict_state.json")

# ---------------- Anahtar saatler (New York yerel saati, dokümandaki tablo) ----------------
NY_TZ = ZoneInfo("America/New_York")
MIDNIGHT_OPEN_H = 0     # NY Midnight Open      00:00
LONDON_OPEN_H = 3       # London Session Open   03:00
NY_OPEN_H = 8           # New York Session Open 08:00
NY_LUNCH = (12, 14)     # NY Lunch 12:00-14:00 — ranging, işlem aranmaz
LONDON_KZ_NY = (2, 5)   # London Kill Zone
NY_KZ_NY = (7, 10)      # New York AM Kill Zone

# ---------------- Model parametreleri ----------------
DAILY_INTERVAL = "1d"
DAILY_LIMIT = 400
ENTRY_INTERVAL = "5m"          # doküman: MSS/displacement/giriş 5m-3m-1m
# 5dk mikro seviyelerle günlük makro seviyeler arasında 4 saatlik yapı vardı ve
# kullanılmıyordu. Stop 5dk fitiline dayanınca gürültü süpürüyor, hedef ise
# fiyatın gerçekten çekildiği likidite havuzuna denk gelmiyordu.
H4_INTERVAL = "4h"
H4_LIMIT = 300                 # ~50 gün
H4_PIVOT_LEN = 2               # 4H swing: komşu 2 mumdan daha uçta olan mum
H4_SL_BUFFER = 0.25            # swing'in ötesine eklenecek 4H ATR payı
# SL/TP 4H swing seviyelerinden mi kurulsun?
# VARSAYILAN KAPALI. 180 günlük A/B testi (40 sembol, aynı dönem ve komisyon)
# 4H seviyelerinin her ölçütte daha kötü olduğunu gösterdi:
#   4H açık : 90 işlem, isabet %48.9, +0.028R, -12.41$
#   4H kapalı: 222 işlem, isabet %53.2, +0.063R,  -7.08$
# Fikir yapısal olarak savunulabilirdi (stop 5dk fitili yerine gerçek seviyede)
# ama ölçüm desteklemedi. Kod duruyor, USE_H4=1 ile tekrar denenebilir.
USE_H4_LEVELS = os.environ.get("USE_H4", "0") == "1"
ENTRY_LIMIT = 1000             # ~3.5 gün
BIAS_PIVOT_LEN = 3
DISPLACEMENT_BODY_MULT = 1.5   # displacement gövdesi / önceki 20 mumun ortalaması
MSS_PIVOT_LEN = 1              # ICT kısa vadeli swing: komşularından daha uçta mum
MSS_SEARCH_BARS = 36           # MSS, süpürmeden sonraki 3 saat içinde olmalı
SETUP_MAX_AGE_HOURS = 12       # bundan eski kurulumlar bayat sayılır
SESSION_DAYS_BACK = 2
SL_ATR_MULT = 0.15             # süpürme ekstremine eklenecek tampon
# Dokümanın 1:3 şartı KURULUM KALİTESİ filtresi olarak korunur: aralık hedefi
# en az 3R uzakta olmalı — bu, girişin discount/premium bölgede olmasını
# matematiksel olarak zorunlu kılar.
MIN_TP1_RR = 3.0
# Ancak KÂR ALMA bunun tamamını beklemez. 90 günlük backtest'te hedef taraması,
# çıkışın 1R'de yapılmasının hem en tutarlı hem kârlı seçenek olduğunu gösterdi
# (dönem ayrımında iki yarıda da +0.14R / +0.15R). 1.5R ve 2R ikinci yarıda
# eksiye döndüğü için tercih edilmedi.
TP1_R_MULTIPLE = 1.0           # TP1 = giriş ± bu kat × risk
# FVG bazen süpürme ekstremine yapışık oluşur; giriş SL'in bir kıl payı altında
# kalır, risk ~%0.09'a düşer ve R:R yapay olarak 10+ görünür. Böyle "bıçak
# sırtı" kurulumlar emir dolar dolmaz stop oluyor. Stop, girişten en az bu
# kadar uzakta olmalı.
# 1.5 değeri filtre taramasıyla seçildi: 90 günlük veri iki yarıya bölünüp test
# edildiğinde HER İKİ yarıda da beklentiyi ve dolar sonucunu artı tutan tek
# filtre buydu (1. yarı +0.134R, 2. yarı +0.098R). 2.0 ve üzeri ikinci yarıda
# eksiye döndüğü, 1.0 ise başabaşa yaklaştığı için tercih edilmedi.
# Mekanizma: geniş stop -> sabit % risk için küçük nominal -> az komisyon.
MIN_RISK_ATR = 1.5             # 5dk ATR katı
MIN_RISK_PCT = 0.15            # ve fiyatın en az bu yüzdesi
MIN_TP_DISTANCE_PCT = 0.30     # girişe bundan yakın hedefler elenir
REQUEST_SLEEP = 0.15           # rate-limit için istekler arası bekleme

# ---------------- Pozisyon takibi ----------------
FILL_TIMEOUT_HOURS = 12        # bekleyen emir bu sürede dolmazsa iptal
POSITION_TIMEOUT_HOURS = 12    # dolan pozisyon bu sürede SL/TP3 görmezse kapat

# ---------------- Sermaye / risk gösterimi ----------------
# Sinyal mesajında somut para karşılığını göstermek için kullanılır.
ACCOUNT_BALANCE = 100.0        # varsayılan cüzdan (USDT)
LEVERAGE = 10                  # izole marjin kaldıracı
MARGIN_PCT = 0.10              # işlem başına ayrılan marjin (bakiyenin oranı)

# Checklist: 3 çekirdek (hepsi şart) + giriş tetiği (en az 1 şart)
CORE_CRITERIA = ["killzone", "liquidity_sweep", "mss_displacement"]
CONFIRM_CRITERIA = ["fvg_entry", "ote_entry"]
MIN_CONFIRMATIONS = 1

CRITERIA_LABELS = {
    "killzone": "Kill Zone: sweep ve MSS pencere içinde (lunch hariç)",
    "liquidity_sweep": "Liquidity Sweep (NY 00:00 → seans açılışı aralığı)",
    "mss_displacement": "MSS + Displacement (5dk, bias yönünde)",
    "fvg_entry": "PD Array: displacement'ın FVG'si geçerli",
    "ote_entry": "OTE: 0.618-0.786 bölgesi geçerli",
}

EXCLUDE_SUFFIXES = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
EXCLUDE_BASE_STABLES = {"USDC", "BUSD", "FDUSD", "TUSD", "DAI", "USDP",
                        "EUR", "GBP", "AEUR", "USTC"}


# ---------------- Veri ----------------
def http_get_json(url, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ict-signal-bot"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 418):
                wait = 5 * (attempt + 1)
                print(f"Rate limited ({e.code}), {wait}s bekleniyor...")
                time.sleep(wait)
                continue
            raise
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2)
    raise RuntimeError(f"İstek başarısız: {url}")


def get_usdt_symbols():
    info = http_get_json(f"{BINANCE_BASE}/api/v3/exchangeInfo")
    symbols = []
    for s in info["symbols"]:
        if s["quoteAsset"] != "USDT" or s["status"] != "TRADING":
            continue
        if not s.get("isSpotTradingAllowed", True):
            continue
        sym = s["symbol"]
        if not sym.isascii() or sym.endswith(EXCLUDE_SUFFIXES):
            continue
        if s["baseAsset"] in EXCLUDE_BASE_STABLES:
            continue
        symbols.append(sym)
    return sorted(symbols)


def fetch_klines(symbol, interval, limit):
    url = f"{BINANCE_BASE}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    now_ms = int(time.time() * 1000)
    out = []
    for row in http_get_json(url):
        if row[6] > now_ms:
            continue  # henüz kapanmamış mum
        out.append({
            "open_time": row[0], "close_time": row[6],
            "open": float(row[1]), "high": float(row[2]),
            "low": float(row[3]), "close": float(row[4]),
            "volume": float(row[5]),   # hacim/OBV teyidi için
        })
    return out


def compute_atr(candles, length=14):
    if len(candles) < length + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        pc = candles[i - 1]["close"]
        trs.append(max(candles[i]["high"] - candles[i]["low"],
                       abs(candles[i]["high"] - pc), abs(candles[i]["low"] - pc)))
    w = trs[-length:]
    return sum(w) / len(w)


# ---------------- Zaman ----------------
@lru_cache(maxsize=400_000)
def to_ny(ms):
    """Epoch (ms) -> New York yerel saati. Yaz/kış saati (EST/EDT) otomatik.
    Aynı mum zamanları defalarca sorgulandığı için önbelleklenir."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(NY_TZ)


def ny_hour(ms):
    dt = to_ny(ms)
    return dt.hour + dt.minute / 60


def in_killzone(ms):
    """Hareketin London (02:00-05:00 NY) veya NY (07:00-10:00 NY) kill zone
    penceresinde gerçekleşip gerçekleşmediği. NY lunch ranging olduğu için
    hariç tutulur."""
    h = ny_hour(ms)
    if NY_LUNCH[0] <= h < NY_LUNCH[1]:
        return False
    return (LONDON_KZ_NY[0] <= h < LONDON_KZ_NY[1]) or (NY_KZ_NY[0] <= h < NY_KZ_NY[1])


# ---------------- Daily bias ----------------
def compute_daily_bias(candles, length=BIAS_PIVOT_LEN):
    """Günlük grafikte en son yapı kırılımının yönü = ICT Daily Bias.
    Pivotlar ancak `length` bar sonra görünür olduğu için ileriye bakış yoktur."""
    n = len(candles)
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    closes = [c["close"] for c in candles]

    raw_ph = [None] * n
    raw_pl = [None] * n
    for i in range(length, n - length):
        w = highs[i - length:i + length + 1]
        if highs[i] == max(w) and w.count(max(w)) == 1:
            raw_ph[i] = highs[i]
        w = lows[i - length:i + length + 1]
        if lows[i] == min(w) and w.count(min(w)) == 1:
            raw_pl[i] = lows[i]

    last_ph = last_pl = None
    bias = None
    for i in range(n):
        r = i - length
        if 0 <= r < n and raw_ph[r] is not None:
            last_ph = raw_ph[r]
        if 0 <= r < n and raw_pl[r] is not None:
            last_pl = raw_pl[r]
        if last_ph is not None and closes[i] > last_ph:
            bias, last_ph = "long", None
        if last_pl is not None and closes[i] < last_pl:
            bias, last_pl = "short", None
    return bias


# ---------------- Model adımları ----------------
def get_session_range(candles, ny_date, start_h, end_h):
    """NY gece yarısından seans açılışına kadar olan aralığın high/low'u."""
    w = [c for c in candles
         if to_ny(c["open_time"]).date() == ny_date
         and start_h <= ny_hour(c["open_time"]) < end_h]
    if not w:
        return None, None
    return max(c["high"] for c in w), min(c["low"] for c in w)


def find_sweep(candles, ny_date, open_h, end_h, range_high, range_low, bias):
    """Seans penceresinde aralığın likiditesini süpüren ilk mum.
    Arama SADECE o seansın saatlerinde yapılır — aksi halde London kurulumu,
    NY seansındaki bir süpürmeyi kendi süpürmesi sanar.
    Süpürme bias'ın TERSİ yöndedir (bullish bias -> aralık dibi süpürülür)."""
    if range_high is None:
        return None
    for i, c in enumerate(candles):
        if to_ny(c["open_time"]).date() != ny_date:
            continue
        if not (open_h <= ny_hour(c["open_time"]) < end_h):
            continue
        if bias == "long" and c["low"] < range_low:
            return i
        if bias == "short" and c["high"] > range_high:
            return i
    return None


def find_mss(candles, sweep_idx, bias, max_bars=MSS_SEARCH_BARS):
    """Süpürmeden sonra bias yönünde Market Structure Shift + Displacement.

    ICT sırası: (1) süpürme ekstremi oluşur, (2) fiyat tepki verip karşı yönde
    kısa vadeli bir swing bırakır, (3) bu swing displacement'lı mumla kırılır.
    Referans süpürme ÖNCESİ değil SONRASI swing'dir; aksi halde yapı kırılımı
    yerine tam dönüş şartı aranmış olur.

    (mss_idx, extreme_idx) döner; MSS yoksa (None, extreme_idx)."""
    end = min(len(candles), sweep_idx + 1 + max_bars)
    rng = range(sweep_idx, end)
    ext_idx = (min(rng, key=lambda k: candles[k]["low"]) if bias == "long"
               else max(rng, key=lambda k: candles[k]["high"]))

    L = MSS_PIVOT_LEN
    ref = None
    for j in range(ext_idx + 1, end):
        c = candles[j]

        # Kırılım kontrolü, swing güncellemesinden ÖNCE yapılır; aksi halde
        # kıran mum swing'i geçersiz kılıp kendi kırılımını gizler.
        if ref is not None:
            prior = candles[max(0, j - 20):j]
            bodies = [abs(p["close"] - p["open"]) for p in prior]
            avg_body = sum(bodies) / len(bodies) if bodies else 0
            body = abs(c["close"] - c["open"])
            if avg_body > 0 and body >= DISPLACEMENT_BODY_MULT * avg_body:
                if bias == "long" and c["close"] > ref and c["close"] > c["open"]:
                    return j, ext_idx
                if bias == "short" and c["close"] < ref and c["close"] < c["open"]:
                    return j, ext_idx

        i = j - L
        if i - 1 > ext_idx:
            after = candles[i + 1:j + 1]
            if bias == "long":
                if candles[i]["high"] > candles[i - 1]["high"] and \
                   all(x["high"] < candles[i]["high"] for x in after):
                    ref = candles[i]["high"]
            else:
                if candles[i]["low"] < candles[i - 1]["low"] and \
                   all(x["low"] > candles[i]["low"] for x in after):
                    ref = candles[i]["low"]
    return None, ext_idx


def find_fvg(candles, mss_idx, bias):
    """Displacement mumunun bıraktığı Fair Value Gap (PD Array). (alt, üst)."""
    if mss_idx is None or mss_idx < 1 or mss_idx + 1 >= len(candles):
        return None
    before, after = candles[mss_idx - 1], candles[mss_idx + 1]
    if bias == "long" and after["low"] > before["high"]:
        return before["high"], after["low"]
    if bias == "short" and after["high"] < before["low"]:
        return after["high"], before["low"]
    return None


def ote_zone(sweep_extreme, leg_extreme, bias):
    """Süpürme ekstreminden teslimatın ulaştığı uca çizilen bacağın
    0.618-0.786 Optimal Trade Entry bölgesi."""
    rng = abs(leg_extreme - sweep_extreme)
    if rng <= 0:
        return None
    if bias == "long":
        return leg_extreme - 0.786 * rng, leg_extreme - 0.618 * rng
    return leg_extreme + 0.618 * rng, leg_extreme + 0.786 * rng


def session_vwap(candles):
    """NY gününde sıfırlanan VWAP. Yazının önerisi: long'da fiyat VWAP'ın
    altındaysa 'ucuz', short'ta üstündeyse 'pahalı'."""
    day = None
    pv = vol = 0.0
    v = None
    for x in candles:
        d = to_ny(x["open_time"]).date()
        if d != day:
            day, pv, vol = d, 0.0, 0.0
        tp = (x["high"] + x["low"] + x["close"]) / 3
        q = x.get("volume", 0) or 1.0
        pv += tp * q
        vol += q
        v = pv / vol if vol else None
    return v


def ichimoku_bias(candles):
    """Ichimoku bulutu: fiyat bulutun üstündeyse 'long', altındaysa 'short',
    içindeyse None. Yazı bunu 4H bağlam teyidi olarak öneriyor."""
    n = len(candles)
    if n < 52 + 26:
        return None
    def mid(period, end):
        w = candles[end - period:end]
        return (max(x["high"] for x in w) + min(x["low"] for x in w)) / 2
    # Bulut 26 bar ileri kaydırıldığı için 26 bar öncesinin değerleri geçerli
    e = n - 26
    if e < 52:
        return None
    span_a = (mid(9, e) + mid(26, e)) / 2
    span_b = mid(52, e)
    price = candles[-1]["close"]
    top, bot = max(span_a, span_b), min(span_a, span_b)
    if price > top:
        return "long"
    if price < bot:
        return "short"
    return None


def volume_surge(candles, idx, lookback=20):
    """Displacement mumunun hacmi, önceki mumların ortalamasının kaç katı.
    ICT'de kurumsal hareketin hacimle gelmesi beklenir."""
    if idx is None or idx < 1:
        return None
    prior = candles[max(0, idx - lookback):idx]
    vols = [c.get("volume", 0) for c in prior]
    avg = sum(vols) / len(vols) if vols else 0
    cur = candles[idx].get("volume", 0)
    return (cur / avg) if avg > 0 else None


def obv_slope(candles, lookback=30):
    """OBV eğimi: son `lookback` mumda hacim ağırlıklı yön. Pozitifse alıcı
    baskısı artıyor demektir."""
    seg = candles[-lookback:]
    if len(seg) < 3:
        return None
    obv = 0.0
    first = None
    for i in range(1, len(seg)):
        q = seg[i].get("volume", 0)
        if seg[i]["close"] > seg[i - 1]["close"]:
            obv += q
        elif seg[i]["close"] < seg[i - 1]["close"]:
            obv -= q
        if i == len(seg) // 2:
            first = obv
    return None if first is None else obv - first


def _vwap_ok(m5, bias):
    """Yazının kuralı: long'da fiyat VWAP altında (ucuz), short'ta üstünde."""
    v = session_vwap(m5[-288:])          # son ~1 gün
    if v is None:
        return None
    px = m5[-1]["close"]
    return px < v if bias == "long" else px > v


def _obv_ok(m5, bias):
    s = obv_slope(m5)
    if s is None:
        return None
    return s > 0 if bias == "long" else s < 0


def h4_swings(h4, length=H4_PIVOT_LEN):
    """4 saatlik grafiğin swing tepe ve dipleri = fiyatın çekildiği likidite
    havuzları. Pivotlar `length` bar sonra kesinleştiği için ileriye bakış yok:
    son `length` mum değerlendirmeye alınmaz."""
    highs, lows = [], []
    n = len(h4)
    for i in range(length, n - length):
        w = h4[i - length:i + length + 1]
        if h4[i]["high"] == max(x["high"] for x in w):
            highs.append(h4[i]["high"])
        if h4[i]["low"] == min(x["low"] for x in w):
            lows.append(h4[i]["low"])
    return highs, lows


def h4_stop(entry, direction, h4_highs, h4_lows, atr4, fallback):
    """SL: girişi koruyan en yakın 4H swing'in ötesi. 5dk fitiline değil
    yapısal seviyeye dayandığı için gürültüyle süpürülmez."""
    if direction == "long":
        below = [lv for lv in h4_lows if lv < entry]
        if not below:
            return fallback
        return max(below) - atr4 * H4_SL_BUFFER
    above = [lv for lv in h4_highs if lv > entry]
    if not above:
        return fallback
    return min(above) + atr4 * H4_SL_BUFFER


def h4_targets(entry, direction, h4_highs, h4_lows, min_gap_pct):
    """TP kademeleri: girişin ötesindeki 4H swing seviyeleri, yakından uzağa."""
    if direction == "long":
        cand = sorted(lv for lv in h4_highs if lv > entry)
    else:
        cand = sorted((lv for lv in h4_lows if lv < entry), reverse=True)
    picked = []
    for lv in cand:
        if abs(lv - entry) / entry < min_gap_pct / 100:
            continue
        if all(abs(lv - p) / p >= min_gap_pct / 100 for p in picked):
            picked.append(lv)
        if len(picked) == 3:
            break
    return picked + [None] * (3 - len(picked))


def pick_targets(entry, direction, range_high, range_low, daily):
    """TP1 dokümanın BİRİNCİL hedefidir: aralığın karşı tarafı. Bu seviye
    başka bir seviyeyle ikame EDİLMEZ — girişe çok yakınsa kurulumun kalan
    getirisi yok demektir ve hedefler boş döner (kurulum reddedilir).
    Aksi halde daha uzak bir seviye TP1 yerine geçip R:R'ı yapay şişiriyor,
    hareketi bitmiş bir işlem geçerli görünüyordu.

    TP2/TP3 dokümandaki diğer likidite tipleridir: önceki gün ve önceki
    hafta high/low'u — yalnızca TP1'in ötesindeyseler kullanılır."""
    d = entry * MIN_TP_DISTANCE_PCT / 100
    tp1 = range_high if direction == "long" else range_low
    if tp1 is None:
        return [None, None, None]
    if (tp1 <= entry + d) if direction == "long" else (tp1 >= entry - d):
        return [None, None, None]  # birincil hedefe yer kalmamış

    extra = []
    if daily:
        prev_day = daily[-1]  # en son KAPANMIŞ gün = "önceki gün"
        extra.append(prev_day["high"] if direction == "long" else prev_day["low"])
    if len(daily) >= 8:
        prev_week = daily[-8:-1]  # ondan önceki 7 gün
        extra.append(max(c["high"] for c in prev_week) if direction == "long"
                     else min(c["low"] for c in prev_week))

    picked = [tp1]
    beyond = sorted({lv for lv in extra if lv is not None and lv > tp1}) \
        if direction == "long" else \
        sorted({lv for lv in extra if lv is not None and lv < tp1}, reverse=True)
    for lv in beyond:
        # Hedefler birbirine de çok yakın olmamalı
        if all(abs(lv - p) / p >= MIN_TP_DISTANCE_PCT / 100 for p in picked):
            picked.append(lv)
        if len(picked) == 3:
            break
    return picked + [None] * (3 - len(picked))


def is_valid_setup(entry, sl, tp1, direction, min_rr=MIN_TP1_RR):
    """Geometri tutarlı mı ve TP1 R:R en az min_rr mi?"""
    if entry is None or sl is None or tp1 is None:
        return False
    if direction == "long":
        if not sl < entry < tp1:
            return False
    elif not tp1 < entry < sl:
        return False
    risk = abs(entry - sl)
    return risk > 0 and abs(tp1 - entry) / risk >= min_rr


def analyze_session(m5, daily, h4, bias, ny_date, session):
    """Tek bir seans için (london / ny) modelin tüm adımlarını uygular."""
    if session == "london":
        range_end_h, open_h, end_h = LONDON_OPEN_H, LONDON_OPEN_H, NY_OPEN_H
    else:
        # Doküman: NY "senaryo II" yalnızca London aralığı SÜPÜRMEDİYSE geçerli.
        l_high, l_low = get_session_range(m5, ny_date, MIDNIGHT_OPEN_H, LONDON_OPEN_H)
        if find_sweep(m5, ny_date, LONDON_OPEN_H, NY_OPEN_H, l_high, l_low, bias) is not None:
            return None  # London zaten süpürdü -> NY kurulumu bu haliyle geçersiz
        range_end_h, open_h, end_h = NY_OPEN_H, NY_OPEN_H, NY_LUNCH[0]

    range_high, range_low = get_session_range(m5, ny_date, MIDNIGHT_OPEN_H, range_end_h)
    if range_high is None:
        return None

    sweep_idx = find_sweep(m5, ny_date, open_h, end_h, range_high, range_low, bias)
    if sweep_idx is None:
        return None

    mss_idx, ext_idx = find_mss(m5, sweep_idx, bias)
    sweep_candle = m5[sweep_idx]
    sweep_extreme = m5[ext_idx]["low"] if bias == "long" else m5[ext_idx]["high"]
    current = m5[-1]["close"]

    # PD Array: fiyatın geri döneceği bölge. Doküman buraya BEKLEYEN (limit)
    # emir koyar; bölge fiyat içinden tamamen geçmediyse hâlâ geçerlidir.
    fvg = find_fvg(m5, mss_idx, bias)
    fvg_entry = bool(fvg and (current >= fvg[0] if bias == "long" else current <= fvg[1]))

    # OTE bacağı: dokümandaki "London low'dan NY geri çekilmesi öncesindeki
    # high'a" tarifi — bacak MSS mumunda bitmez, teslimatın ucuna kadar uzar.
    ote = leg_extreme = None
    if mss_idx is not None:
        leg = [c for c in m5[mss_idx:] if to_ny(c["open_time"]).date() == ny_date]
        if leg:
            leg_extreme = (max(c["high"] for c in leg) if bias == "long"
                           else min(c["low"] for c in leg))
            ote = ote_zone(sweep_extreme, leg_extreme, bias)
    ote_entry = bool(ote and (current >= ote[0] if bias == "long" else current <= ote[1]))

    # Giriş = PD array seviyesi (bekleyen emir): FVG ortası, yoksa OTE ortası.
    if fvg_entry:
        entry, entry_kind = (fvg[0] + fvg[1]) / 2, "FVG"
    elif ote_entry:
        entry, entry_kind = (ote[0] + ote[1]) / 2, "OTE"
    else:
        entry = entry_kind = None

    criteria = {
        "killzone": bool(mss_idx is not None
                         and in_killzone(sweep_candle["open_time"])
                         and in_killzone(m5[mss_idx]["open_time"])),
        "liquidity_sweep": True,  # buraya gelindiyse süpürme bulunmuştur
        "mss_displacement": mss_idx is not None,
        "fvg_entry": fvg_entry,
        "ote_entry": ote_entry,
    }

    atr = compute_atr(m5, 14)
    sl = (sweep_extreme - atr * SL_ATR_MULT if bias == "long"
          else sweep_extreme + atr * SL_ATR_MULT)
    atr_pct = 100 * atr / current if current else None

    # SL'i 5dk fitiline değil 4H swing yapısına dayandır (varsa). Girişi
    # koruyan en yakın 4H swing'in ötesi, gürültüyle süpürülmeyen seviyedir.
    h4_hi = h4_lo = None
    if USE_H4_LEVELS and h4 and entry is not None:
        h4_hi, h4_lo = h4_swings(h4)
        atr4 = compute_atr(h4, 14)
        sl = h4_stop(entry, bias, h4_hi, h4_lo, atr4, sl)

    # Bıçak sırtı elemesi: stop girişe çok yakınsa (gürültünün içinde) kurulum
    # geçersizdir — emir dolar dolmaz stop olur, R:R ise yanıltıcı yüksektir.
    if entry is not None:
        risk_abs = abs(entry - sl)
        if risk_abs < max(MIN_RISK_ATR * atr, entry * MIN_RISK_PCT / 100):
            entry = entry_kind = None

    ref = entry if entry is not None else current
    range_tp, tp_far1, tp_far2 = pick_targets(ref, bias, range_high, range_low, daily)

    # Hareket bitmişse işleme girilmez: fiyat aralık hedefini zaten geçtiyse
    # alınacak bir şey kalmamıştır.
    if range_tp is not None and ((current >= range_tp) if bias == "long"
                                 else (current <= range_tp)):
        range_tp = tp_far1 = tp_far2 = None

    # Kâr alma kademeleri.
    if entry is not None and range_tp is not None:
        if USE_H4_LEVELS and h4_hi is not None:
            # Hedefler 4H swing seviyeleri = fiyatın gerçekten çekildiği
            # likidite havuzları. Bulunamazsa eski mantığa düşülür.
            t1, t2, t3 = h4_targets(entry, bias, h4_hi, h4_lo, MIN_TP_DISTANCE_PCT)
            tp1 = t1 if t1 is not None else range_tp
            tp2 = t2 if t2 is not None else range_tp
            tp3 = t3 if t3 is not None else tp_far1
        else:
            risk_abs = abs(entry - sl)
            tp1 = (entry + TP1_R_MULTIPLE * risk_abs if bias == "long"
                   else entry - TP1_R_MULTIPLE * risk_abs)
            tp2, tp3 = range_tp, tp_far1
    else:
        tp1 = tp2 = tp3 = None
    risk = abs(ref - sl)
    rrs = [abs(tp - ref) / risk if (tp is not None and risk > 0) else None
           for tp in (tp1, tp2, tp3)]

    # Dokümandaki NY "senaryo I": London süpürüp teslimatı yaptıysa ve geri
    # çekilme NY seansında OTE'ye denk geliyorsa bu bir devam işlemidir.
    continuation = (session == "london" and ote_entry
                    and NY_OPEN_H <= ny_hour(m5[-1]["open_time"]) < NY_LUNCH[0])

    return {
        "session": session, "continuation": continuation, "ny_date": str(ny_date),
        "direction": bias, "criteria": criteria, "score": sum(criteria.values()),
        "price": current, "entry": entry, "entry_kind": entry_kind, "sl": sl,
        "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "rr1": rrs[0], "rr2": rrs[1], "rr3": rrs[2],
        "range_high": range_high, "range_low": range_low,
        "range_tp": range_tp,   # kalite filtresi bu hedefe göre yapılır
        "atr_pct": atr_pct,
        # --- konfluans teyitleri (test edilmek üzere, filtre olarak zorunlu değil) ---
        "vwap_ok": _vwap_ok(m5, bias),
        "ichimoku_ok": (ichimoku_bias(h4) == bias) if h4 else None,
        "vol_surge": volume_surge(m5, mss_idx),
        "obv_ok": _obv_ok(m5, bias),
        "risk_pct": (100 * abs(ref - sl) / ref) if ref else None,
        "sweep_time": sweep_candle["open_time"],
        "mss_time": m5[mss_idx]["open_time"] if mss_idx is not None else None,
    }


def evaluate(daily, m5, h4=None):
    """Veri çekmeden, verilen mumlar üzerinde modeli çalıştırır.
    m5'in son mumu 'şu an' kabul edilir — backtest bu sayede aynı kod yolunu
    geçmiş bir ana dilim vererek kullanabilir."""
    if len(daily) < BIAS_PIVOT_LEN * 2 + 5 or len(m5) < 100:
        return None

    bias = compute_daily_bias(daily)
    if bias is None:
        return None

    latest_date = to_ny(m5[-1]["open_time"]).date()
    now_ms = m5[-1]["close_time"]

    best = None
    for off in range(SESSION_DAYS_BACK):
        d = latest_date - timedelta(days=off)
        for session in ("ny", "london"):
            r = analyze_session(m5, daily, h4, bias, d, session)
            if r is None or r["mss_time"] is None:
                continue
            if (now_ms - r["mss_time"]) / 3600000 > SETUP_MAX_AGE_HOURS:
                continue
            if best is None or r["mss_time"] > best["mss_time"]:
                best = r
        if best:
            break

    if best is None:
        return None

    core_ok = all(best["criteria"][k] for k in CORE_CRITERIA)
    confirms = sum(best["criteria"][k] for k in CONFIRM_CRITERIA)
    # Kalite filtresi ARALIK hedefine göre (dokümanın 1:3 şartı) — kâr alma
    # daha yakında yapılsa da kurulumun potansiyeli bu şartla ölçülür.
    best["qualifies"] = (core_ok and confirms >= MIN_CONFIRMATIONS
                         and best["entry"] is not None
                         and is_valid_setup(best["entry"], best["sl"],
                                            best["range_tp"], best["direction"]))
    return best


def evaluate_symbol(symbol):
    """Canlı tarama: veriyi çeker ve modeli çalıştırır."""
    daily = fetch_klines(symbol, DAILY_INTERVAL, DAILY_LIMIT)
    time.sleep(REQUEST_SLEEP)
    m5 = fetch_klines(symbol, ENTRY_INTERVAL, ENTRY_LIMIT)
    time.sleep(REQUEST_SLEEP)
    h4 = fetch_klines(symbol, H4_INTERVAL, H4_LIMIT) if USE_H4_LEVELS else None
    if USE_H4_LEVELS:
        time.sleep(REQUEST_SLEEP)
    return evaluate(daily, m5, h4), m5


# ---------------- Pozisyon takibi ----------------
def pct_move(entry, price, is_long):
    return (price - entry) / entry * 100 if is_long else (entry - price) / entry * 100


EVENT_LABELS = {
    "filled": ("✅", "EMİR DOLDU"),
    "sl_hit": ("🛑", "SL VURULDU"),
    "be_stop": ("🟰", "BAŞABAŞ ÇIKIŞ (TP1 sonrası stop girişe çekilmişti)"),
    "tp1_hit": ("🎯", "TP1 VURULDU"),
    "tp2_hit": ("🎯🎯", "TP2 VURULDU"),
    "tp3_hit": ("🏁", "TP3 VURULDU (pozisyon kapandı)"),
    "expired": ("⌛", "EMİR İPTAL (dolmadı)"),
    "timeout": ("⏳", "ZAMAN AŞIMI"),
}
EVENT_PRICE_KEY = {"sl_hit": "sl", "tp1_hit": "tp1", "tp2_hit": "tp2", "tp3_hit": "tp3"}
CLOSED_STATES = ("sl_hit", "tp3_hit", "be_stop", "timeout", "expired")
LIVE_STATES = ("pending", "open", "tp1_hit", "tp2_hit")


def monitor_position(pos, m5, direction):
    """Bekleyen emri ve dolduktan sonra pozisyonu izler.
    pending -> (fiyat girişe değdi) -> open -> tp1/tp2/tp3 veya sl.
    Dolmayan emir FILL_TIMEOUT_HOURS sonra iptal, dolan pozisyon
    POSITION_TIMEOUT_HOURS sonra zaman aşımı olur."""
    if pos.get("status") in CLOSED_STATES:
        return pos, []
    entry, sl = pos.get("entry"), pos.get("sl")
    if entry is None or sl is None:
        return pos, []

    last_checked = pos.get("last_checked_close_time", pos.get("signal_time", 0))
    new = [c for c in m5 if c["close_time"] > last_checked]
    if not new:
        return pos, []

    is_long = direction == "long"
    tp1, tp2, tp3 = pos.get("tp1"), pos.get("tp2"), pos.get("tp3")
    status = pos.get("status", "pending")
    events = []

    for c in new:
        just_filled = False
        if status == "pending":
            # Limit emir, fiyat giriş seviyesine değdiğinde dolar
            if c["low"] <= entry <= c["high"]:
                status = "open"
                just_filled = True
                pos["fill_time"] = c["close_time"]
                events.append("filled")
            else:
                continue

        # TP1 vurulduktan sonra stop başabaşa (girişe) çekilir. Aksi halde
        # hedefine ulaşmış bir işlem geri dönüp tam zarara kapanabiliyordu.
        stop = entry if status in ("tp1_hit", "tp2_hit") else sl

        if (c["low"] <= stop) if is_long else (c["high"] >= stop):
            status = "be_stop" if status in ("tp1_hit", "tp2_hit") else "sl_hit"
            pos["exit_time"] = c["close_time"]
            events.append(status)
            break
        # Dolum mumunda TP SAYILMAZ: mum içi sıralama bilinemediği için
        # "aynı mumda hem doldu hem hedefe ulaştı" varsayımı kazançları
        # yapay şişiriyor. Muhafazakâr taraf seçilir.
        if just_filled:
            continue

        if status == "open" and tp1 is not None and \
           ((c["high"] >= tp1) if is_long else (c["low"] <= tp1)):
            status = "tp1_hit"
            pos["tp1_reached"] = True   # zaman aşımına gitse bile kazanç sayılsın
            events.append("tp1_hit")
        if status == "tp1_hit" and tp2 is not None and \
           ((c["high"] >= tp2) if is_long else (c["low"] <= tp2)):
            status = "tp2_hit"
            events.append("tp2_hit")
        if status == "tp2_hit" and tp3 is not None and \
           ((c["high"] >= tp3) if is_long else (c["low"] <= tp3)):
            status = "tp3_hit"
            pos["exit_time"] = c["close_time"]
            events.append("tp3_hit")

    now_ms = new[-1]["close_time"]
    if status == "pending":
        age = (now_ms - pos.get("signal_time", now_ms)) / 3600000
        if age >= FILL_TIMEOUT_HOURS:
            status = "expired"
            pos["exit_time"] = now_ms
            events.append("expired")
    elif status in ("open", "tp1_hit", "tp2_hit"):
        age = (now_ms - pos.get("fill_time", now_ms)) / 3600000
        if age >= POSITION_TIMEOUT_HOURS:
            pos["timeout_after_hours"] = round(age, 1)
            pos["exit_price"] = new[-1]["close"]
            pos["exit_time"] = now_ms
            status = "timeout"
            events.append("timeout")

    pos["status"] = status
    pos["last_checked_close_time"] = now_ms
    return pos, events


def format_event_message(symbol, direction, pos, event, current_price=None):
    emoji, label = EVENT_LABELS[event]
    is_long = direction == "long"
    entry = pos["entry"]
    head = f"{emoji} <b>{label}</b> — {symbol} {direction.upper()}"

    if event == "expired":
        return (f"{head}\nGiriş emri: {entry:.6g}  Güncel: {current_price:.6g}\n"
                f"{FILL_TIMEOUT_HOURS} saatte fiyat giriş seviyesine gelmedi, emir iptal edildi.")
    if event == "filled":
        return f"{head}\nGiriş: {entry:.6g}\nPozisyon açıldı, SL/TP takibi başladı."
    if event == "be_stop":
        return (f"{head}\nGiriş: {entry:.6g}\n"
                f"TP1 alındıktan sonra fiyat girişe döndü, pozisyon başabaş kapandı.")
    if event == "timeout":
        p = current_price if current_price is not None else entry
        return (f"{head}\nGiriş: {entry:.6g}  Güncel: {p:.6g}\n"
                f"P&L: {pct_move(entry, p, is_long):+.2f}%\n"
                f"{pos.get('timeout_after_hours', POSITION_TIMEOUT_HOURS)} saatte "
                f"SL/TP3 gelmedi, pozisyon kapatıldı.")

    price = pos[EVENT_PRICE_KEY[event]]
    return (f"{head}\nGiriş: {entry:.6g}  Seviye: {price:.6g}\n"
            f"P&L: {pct_move(entry, price, is_long):+.2f}%")


def _lvl(v):
    return f"{v:.6g}" if v is not None else "n/a"


def format_open_positions_digest(items):
    """items: [(symbol, direction, pos, current_price), ...]"""
    if not items:
        return []
    labels = {"pending": "emir bekliyor", "open": "açık",
              "tp1_hit": "TP1 vuruldu, devam", "tp2_hit": "TP2 vuruldu, devam"}
    blocks = []
    for symbol, direction, pos, price in items:
        is_long = direction == "long"
        entry = pos["entry"]
        st = pos.get("status", "pending")
        pnl = (f"P&L: {pct_move(entry, price, is_long):+.2f}%"
               if st != "pending" else "henüz dolmadı")
        blocks.append(
            f"{'🟢' if is_long else '🔴'} <b>{symbol} {direction.upper()}</b> — {labels.get(st, st)}\n"
            f"Giriş: {entry:.6g}  Güncel: {price:.6g}  {pnl}\n"
            f"SL: {_lvl(pos.get('sl'))}  TP1: {_lvl(pos.get('tp1'))}  "
            f"TP2: {_lvl(pos.get('tp2'))}  TP3: {_lvl(pos.get('tp3'))}"
        )

    msgs, cur = [], "📊 <b>Takip edilen pozisyonlar</b>\n\n"
    for b in blocks:
        if len(cur) + len(b) + 2 > 3500:
            msgs.append(cur.rstrip())
            cur = ""
        cur += b + "\n\n"
    if cur.strip():
        msgs.append(cur.rstrip())
    return msgs


# ---------------- Telegram ----------------
def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID tanımlı değil, mesaj gönderilemedi:")
        print(message)
        return
    payload = json.dumps({"chat_id": chat_id, "text": message,
                          "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                 data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except Exception as e:
        print(f"Telegram gönderim hatası: {e}")


def _tp(tp, rr):
    if tp is None:
        return "n/a"
    return f"{tp:.6g}  (R:R ≈ {rr:.2f})" if rr else f"{tp:.6g}"


def format_signal_message(symbol, r):
    d = r["direction"]
    emoji = "🟢" if d == "long" else "🔴"
    if r.get("continuation"):
        sess = "London kurulumu → NY devam işlemi (senaryo I)"
    else:
        sess = "London" if r["session"] == "london" else "New York (senaryo II)"
    rng = "00:00-03:00" if r["session"] == "london" else "00:00-08:00"

    lines = [
        f"{emoji} <b>{d.upper()} — {symbol}</b>   (ICT 2022, skor {r['score']}/5)",
        f"Seans: {sess}",
        f"Daily bias: {d.upper()} | Aralık ({rng} NY): {r['range_low']:.6g} – {r['range_high']:.6g}",
        "",
        f"📍 Giriş (bekleyen emir, {r['entry_kind']}): <b>{r['entry']:.6g}</b>",
        f"   Güncel fiyat: {r['price']:.6g}",
        f"🛑 SL: {r['sl']:.6g}   (süpürülen ekstremin ötesi)",
        f"🎯 TP1: {_tp(r['tp1'], r['rr1'])}",
        f"🎯 TP2: {_tp(r['tp2'], r['rr2'])}",
        f"🎯 TP3: {_tp(r['tp3'], r['rr3'])}",
        "",
    ]

    # Risk mesafesi ve 10x izole marjinde somut para karşılığı.
    # (Testlerde en çok işe yarayan iki ölçüt: stopun ATR'ye göre genişliği ve
    #  işlemin gerçekte kaç dolar riske attığı.)
    risk_pct, atr_pct = r.get("risk_pct"), r.get("atr_pct")
    if risk_pct:
        margin = ACCOUNT_BALANCE * MARGIN_PCT
        notional = margin * LEVERAGE
        risk_usd = notional * risk_pct / 100
        line = (f"📏 Risk mesafesi: %{risk_pct:.2f}"
                f"{f'  ({risk_pct/atr_pct:.1f}× 5dk ATR)' if atr_pct else ''}")
        lines.append(line)
        if atr_pct and risk_pct / atr_pct < 1:
            lines.append("   ⚠️ Stop, tek mumluk dalgalanmanın içinde — dar")
        lines.append(
            f"💵 {ACCOUNT_BALANCE:.0f}$ · {LEVERAGE}x izole · marjin {margin:.0f}$ → "
            f"risk {risk_usd:.2f}$ / TP1 kazanç {risk_usd * (r['rr1'] or 0):.2f}$")
        lines.append("")

    lines.append("<b>Çekirdek (hepsi sağlandı):</b>")
    for k in CORE_CRITERIA:
        lines.append(f"{'✅' if r['criteria'][k] else '❌'} {CRITERIA_LABELS[k]}")
    lines.append("")
    lines.append("<b>Giriş tetiği (en az 1):</b>")
    for k in CONFIRM_CRITERIA:
        lines.append(f"{'✅' if r['criteria'][k] else '❌'} {CRITERIA_LABELS[k]}")
    lines += [
        "",
        f"⏱ Likidite avı: {to_ny(r['sweep_time']).strftime('%Y-%m-%d %H:%M')} NY",
        f"⏱ MSS/Displacement: {to_ny(r['mss_time']).strftime('%Y-%m-%d %H:%M')} NY",
        "",
        "⚠️ Yatırım tavsiyesi değildir.",
    ]
    return "\n".join(lines)


# ---------------- Durum ve ana akış ----------------
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def _init_trading():
    """Otomatik işlem açıksa Binance bağlantısını hazırlar.
    Kapalıysa (varsayılan) None döner ve bot sadece sinyal gönderir."""
    try:
        import binance_trader as bt
    except Exception as e:
        print(f"Otomatik işlem modülü yüklenemedi: {e}")
        return None, None
    if not bt._enabled():
        return None, None
    api = bt.BinanceFutures()
    try:
        api.load_filters()
        bal = api.balance_usdt()
    except Exception as e:
        print(f"Binance bağlantı hatası, otomatik işlem devre dışı: {e}")
        return None, None
    mod = "TESTNET" if bt._testnet() else "GERÇEK PARA"
    if bt._dry_run():
        mod += " / KURU ÇALIŞMA (emir gönderilmez)"
    print(f"Otomatik işlem AÇIK — {mod} | bakiye: {bal:.2f} USDT")

    uyari = ""
    if bal <= 0:
        uyari = ("\n⚠️ Bakiye 0 — bu haliyle emir açılamaz "
                 "(pozisyon büyüklüğü bakiyeden hesaplanıyor).\n"
                 "Testnet hesabına bakiye yükle: testnet.binancefuture.com")
    send_telegram(f"🤖 <b>Otomatik işlem açık</b>\nMod: {mod}\n"
                  f"Bakiye: {bal:.2f} USDT{uyari}")
    return bt, api


def main():
    state = load_state()
    symbols = get_usdt_symbols()
    print(f"{len(symbols)} sembol taranacak.")

    trader, api = _init_trading()
    balance = 0.0
    exch_pos = {}
    if api:
        try:
            balance = api.balance_usdt()
            exch_pos = api.positions()          # borsadaki GERÇEK pozisyonlar
            print(f"Borsada açık pozisyon: {len(exch_pos)}")
        except Exception as e:
            print(f"Borsa durumu okunamadı: {e}")
            trader = api = None

    sent = events_sent = 0
    live = []

    for i, symbol in enumerate(symbols):
        try:
            result, m5 = evaluate_symbol(symbol)
        except Exception as e:
            print(f"[{symbol}] hata: {e}")
            continue
        if not m5:
            continue

        sym_state = state.get(symbol, {})
        price = m5[-1]["close"]

        for d in ("long", "short"):
            pos = sym_state.get(d)

            # 1) Mevcut emri/pozisyonu güncelle
            if isinstance(pos, dict) and pos.get("entry") is not None:
                pos, evs = monitor_position(pos, m5, d)
                sym_state[d] = pos
                for ev in evs:
                    print(f"[{symbol}] {d} {ev}")
                    send_telegram(format_event_message(symbol, d, pos, ev, price))
                    events_sent += 1

                # --- SL kademelendirme: borsadaki gerçek pozisyona göre ---
                if api and trader and symbol in exch_pos and pos.get("order_qty"):
                    try:
                        before = pos.get("sl_level", 0)
                        pos = trader.trail_stop(
                            api, symbol, d, exch_pos[symbol]["amt"],
                            pos["order_qty"],
                            (pos.get("tp1"), pos.get("tp2"), pos.get("tp3")), pos)
                        if pos.get("sl_level", 0) > before:
                            lv = pos["sl_level"]
                            send_telegram(
                                f"🔒 <b>SL taşındı</b> — {symbol} {d.upper()}\n"
                                f"TP{lv} doldu, stop TP{lv} seviyesine çekildi "
                                f"({pos.get(f'tp{lv}')}). Bu kadarı garanti.")
                            sym_state[d] = pos
                    except Exception as e:
                        print(f"[{symbol}] SL taşıma hatası: {e}")

                if pos.get("status") in LIVE_STATES:
                    live.append((symbol, d, pos, price))

            # 2) Yeni sinyal
            if not result or not result["qualifies"] or result["direction"] != d:
                continue
            pos = sym_state.get(d)
            if isinstance(pos, dict) and pos.get("mss_time") == result["mss_time"]:
                continue  # bu MSS için zaten sinyal gönderildi
            if isinstance(pos, dict) and pos.get("status") in LIVE_STATES:
                print(f"[{symbol}] {d}: takipte pozisyon var, yeni sinyal atlandı")
                continue

            print(f"[{symbol}] {d} {result['session']} skor={result['score']}/5 gönderiliyor")
            send_telegram(format_signal_message(symbol, result))
            new_pos = {
                "mss_time": result["mss_time"],
                "signal_time": m5[-1]["close_time"],
                "last_checked_close_time": m5[-1]["close_time"],
                "entry": result["entry"], "entry_kind": result["entry_kind"],
                "sl": result["sl"], "tp1": result["tp1"],
                "tp2": result["tp2"], "tp3": result["tp3"],
                "status": "pending",
            }
            # --- Otomatik emir yerleştirme ---
            if api and trader:
                try:
                    if symbol in exch_pos:
                        print(f"[{symbol}] borsada zaten pozisyon var, emir açılmadı")
                    elif len(exch_pos) >= trader.MAX_OPEN_POSITIONS:
                        print(f"[{symbol}] eşzamanlı pozisyon sınırı ({trader.MAX_OPEN_POSITIONS}), atlandı")
                    else:
                        o = trader.open_trade(
                            api, symbol, d, result["entry"], result["sl"],
                            (result["tp1"], result["tp2"], result["tp3"]), balance)
                        if o:
                            new_pos["order_qty"] = o["qty"]
                            new_pos["sl_level"] = 0
                            exch_pos[symbol] = {"amt": 0, "side": d}
                            send_telegram(
                                f"📤 <b>Emirler yerleştirildi</b> — {symbol} {d.upper()}\n"
                                f"Giriş: {o['entry']}  Miktar: {o['qty']}\n"
                                f"SL ve 3 kademe TP borsaya kuruldu.")
                except Exception as e:
                    print(f"[{symbol}] emir hatası: {e}")
                    send_telegram(f"⚠️ {symbol} emir yerleştirilemedi: {e}")

            sym_state[d] = new_pos
            live.append((symbol, d, new_pos, price))
            sent += 1

        if sym_state:
            state[symbol] = sym_state
        if (i + 1) % 50 == 0:
            print(f"{i + 1}/{len(symbols)} tarandı...")

    save_state(state)
    for msg in format_open_positions_digest(live):
        send_telegram(msg)
    print(f"Tarama bitti. {sent} yeni sinyal, {events_sent} olay gönderildi. "
          f"{len(live)} pozisyon takipte.")


if __name__ == "__main__":
    main()
