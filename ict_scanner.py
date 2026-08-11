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
ENTRY_LIMIT = 1000             # ~3.5 gün
BIAS_PIVOT_LEN = 3
DISPLACEMENT_BODY_MULT = 1.5   # displacement gövdesi / önceki 20 mumun ortalaması
MSS_PIVOT_LEN = 1              # ICT kısa vadeli swing: komşularından daha uçta mum
MSS_SEARCH_BARS = 36           # MSS, süpürmeden sonraki 3 saat içinde olmalı
SETUP_MAX_AGE_HOURS = 12       # bundan eski kurulumlar bayat sayılır
SESSION_DAYS_BACK = 2
SL_ATR_MULT = 0.15             # süpürme ekstremine eklenecek tampon
MIN_TP1_RR = 3.0               # doküman 1:3 ve üzeri hedefler
MIN_TP_DISTANCE_PCT = 0.30     # girişe bundan yakın hedefler elenir
REQUEST_SLEEP = 0.15           # rate-limit için istekler arası bekleme

# ---------------- Pozisyon takibi ----------------
FILL_TIMEOUT_HOURS = 12        # bekleyen emir bu sürede dolmazsa iptal
POSITION_TIMEOUT_HOURS = 12    # dolan pozisyon bu sürede SL/TP3 görmezse kapat

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


def pick_targets(entry, direction, range_high, range_low, daily):
    """Dokümandaki likidite tipleri hedef olur: aralığın karşı tarafı
    (birincil hedef), önceki gün ve önceki hafta high/low'u."""
    levels = [range_high if direction == "long" else range_low]
    if daily:
        prev_day = daily[-1]  # en son KAPANMIŞ gün = "önceki gün"
        levels.append(prev_day["high"] if direction == "long" else prev_day["low"])
    if len(daily) >= 8:
        prev_week = daily[-8:-1]  # ondan önceki 7 gün
        levels.append(max(c["high"] for c in prev_week) if direction == "long"
                      else min(c["low"] for c in prev_week))

    d = entry * MIN_TP_DISTANCE_PCT / 100
    if direction == "long":
        cand = sorted({lv for lv in levels if lv is not None and lv > entry + d})
    else:
        cand = sorted({lv for lv in levels if lv is not None and lv < entry - d},
                      reverse=True)

    # Hedefler birbirine de çok yakın olmamalı; aksi halde TP2, TP1'in
    # neredeyse aynısı olup ayrı bir hedef olma değerini kaybediyor.
    picked = []
    for lv in cand:
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


def analyze_session(m5, daily, bias, ny_date, session):
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

    ref = entry if entry is not None else current
    tp1, tp2, tp3 = pick_targets(ref, bias, range_high, range_low, daily)
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
        "sweep_time": sweep_candle["open_time"],
        "mss_time": m5[mss_idx]["open_time"] if mss_idx is not None else None,
    }


def evaluate(daily, m5):
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
            r = analyze_session(m5, daily, bias, d, session)
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
    best["qualifies"] = (core_ok and confirms >= MIN_CONFIRMATIONS
                         and best["entry"] is not None
                         and is_valid_setup(best["entry"], best["sl"],
                                            best["tp1"], best["direction"]))
    return best


def evaluate_symbol(symbol):
    """Canlı tarama: veriyi çeker ve modeli çalıştırır."""
    daily = fetch_klines(symbol, DAILY_INTERVAL, DAILY_LIMIT)
    time.sleep(REQUEST_SLEEP)
    m5 = fetch_klines(symbol, ENTRY_INTERVAL, ENTRY_LIMIT)
    time.sleep(REQUEST_SLEEP)
    return evaluate(daily, m5), m5


# ---------------- Pozisyon takibi ----------------
def pct_move(entry, price, is_long):
    return (price - entry) / entry * 100 if is_long else (entry - price) / entry * 100


EVENT_LABELS = {
    "filled": ("✅", "EMİR DOLDU"),
    "sl_hit": ("🛑", "SL VURULDU"),
    "tp1_hit": ("🎯", "TP1 VURULDU"),
    "tp2_hit": ("🎯🎯", "TP2 VURULDU"),
    "tp3_hit": ("🏁", "TP3 VURULDU (pozisyon kapandı)"),
    "expired": ("⌛", "EMİR İPTAL (dolmadı)"),
    "timeout": ("⏳", "ZAMAN AŞIMI"),
}
EVENT_PRICE_KEY = {"sl_hit": "sl", "tp1_hit": "tp1", "tp2_hit": "tp2", "tp3_hit": "tp3"}
CLOSED_STATES = ("sl_hit", "tp3_hit", "timeout", "expired")
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
        if status == "pending":
            # Limit emir, fiyat giriş seviyesine değdiğinde dolar
            if c["low"] <= entry <= c["high"]:
                status = "open"
                pos["fill_time"] = c["close_time"]
                events.append("filled")
            else:
                continue

        if (c["low"] <= sl) if is_long else (c["high"] >= sl):
            status = "sl_hit"
            events.append("sl_hit")
            break
        if status == "open" and tp1 is not None and \
           ((c["high"] >= tp1) if is_long else (c["low"] <= tp1)):
            status = "tp1_hit"
            events.append("tp1_hit")
        if status == "tp1_hit" and tp2 is not None and \
           ((c["high"] >= tp2) if is_long else (c["low"] <= tp2)):
            status = "tp2_hit"
            events.append("tp2_hit")
        if status == "tp2_hit" and tp3 is not None and \
           ((c["high"] >= tp3) if is_long else (c["low"] <= tp3)):
            status = "tp3_hit"
            events.append("tp3_hit")

    now_ms = new[-1]["close_time"]
    if status == "pending":
        age = (now_ms - pos.get("signal_time", now_ms)) / 3600000
        if age >= FILL_TIMEOUT_HOURS:
            status = "expired"
            events.append("expired")
    elif status in ("open", "tp1_hit", "tp2_hit"):
        age = (now_ms - pos.get("fill_time", now_ms)) / 3600000
        if age >= POSITION_TIMEOUT_HOURS:
            pos["timeout_after_hours"] = round(age, 1)
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
        "<b>Çekirdek (hepsi sağlandı):</b>",
    ]
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


def main():
    state = load_state()
    symbols = get_usdt_symbols()
    print(f"{len(symbols)} sembol taranacak.")

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
