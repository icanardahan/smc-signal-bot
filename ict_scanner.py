"""
ICT 2022 Trading Model tarayıcısı — scanner.py'daki HTF/LTF Order Block
stratejisinden BAĞIMSIZ ikinci strateji. Michael Huddleston'ın 2022 modelinin
("Complete ICT Trading Strategy – 2022 Trading Model") otomatik uygulamasıdır.

Modelin akışı (dokümandaki sıra):
  1. Daily bias belirlenir (günlük grafik).
  2. NY gece yarısı açılışından (00:00 NY) seans açılışına kadar olan fiyat
     aralığının HIGH/LOW'u işaretlenir:
       - London kurulumu  -> aralık 00:00-03:00 NY
       - New York kurulumu -> aralık 00:00-08:00 NY
  3. Seans açılınca bu aralığın likiditesi süpürülür (Liquidity Sweep /
     Judas swing) — bias'ın TERSİ yönde.
  4. Süpürmeden sonra 5 dakikalık grafikte bias YÖNÜNDE Market Structure Shift
     + Displacement aranır.
  5. Displacement'ın bıraktığı PD Array (FVG) işaretlenir; fiyatın buraya ya da
     OTE (0.618-0.786) bölgesine geri dönmesi giriş tetiğidir.
  6. SL süpürülen ekstremin ötesine, TP aralığın karşı tarafına konur.
     Doküman 1:3 ve üzeri R:R hedefler (ICT_MIN_TP1_RR).

Zaman dilimleri (dokümana göre): Daily -> bias, 5m -> onay ve giriş.
Tüm saatler New York yerel saatiyle hesaplanır (zoneinfo), böylece yaz/kış
saati (EST/EDT) geçişinde pencereler kaymaz.

Bu, otomatikleştirilebilir bir yaklaşımdır — ICT'nin tam metodolojisinin
birebir yerine geçmez, yatırım tavsiyesi değildir.
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from scanner import (
    get_usdt_symbols,
    fetch_klines,
    compute_structure,
    compute_atr,
    is_valid_setup,
    send_telegram,
    PIVOT_LEN_HTF,
    HTF_INTERVAL,
    HTF_LIMIT,
    REQUEST_SLEEP,
)

ICT_STATE_FILE = os.path.join(os.path.dirname(__file__), "ict_state.json")

# ---------------- Anahtar saatler (New York yerel saati, dokümandaki tablo) ----------------
NY_TZ = ZoneInfo("America/New_York")
MIDNIGHT_OPEN_H = 0    # NY Midnight Open  00:00
LONDON_OPEN_H = 3      # London Session Open 03:00
NY_OPEN_H = 8          # New York Session Open 08:00
NY_LUNCH = (12, 14)    # NY Lunch 12:00-14:00 — ranging, işlem aranmaz
LONDON_CLOSE_H = 12    # London Session Close 12:00

# Kill zone pencereleri (NY saati)
LONDON_KZ_NY = (2, 5)
NY_KZ_NY = (7, 10)

# ---------------- Model parametreleri ----------------
ENTRY_INTERVAL = "5m"          # doküman: MSS/displacement/giriş 5m-3m-1m
ENTRY_LIMIT = 1000             # ~3.5 gün
DISPLACEMENT_BODY_MULT = 1.5   # displacement mumunun gövdesi / ortalama gövde
MSS_PIVOT_LEN = 1              # ICT kısa vadeli swing: komşu mumlardan daha uçta olan mum
MSS_SEARCH_BARS = 36           # MSS, süpürmeden sonraki 3 saat (36x5dk) içinde olmalı
SETUP_MAX_AGE_HOURS = 12       # bundan eski kurulumlar bayat sayılır
SL_ATR_MULT = 0.15             # süpürme ekstremine eklenecek tampon
ICT_MIN_TP1_RR = 3.0           # doküman 1:3 ve üzeri hedefler
MIN_TP_DISTANCE_PCT = 0.30     # girişe bundan yakın hedefler elenir
SESSION_DAYS_BACK = 2          # kaç NY gününe kadar geriye bakılsın

# Kullanıcının checklist yapısı korunur: 3 çekirdek + en az 1 onay.
CORE_CRITERIA = ["killzone", "liquidity_sweep", "mss_displacement"]
CONFIRM_CRITERIA = ["fvg_entry", "ote_entry"]
MIN_CONFIRMATIONS = 1

CRITERIA_LABELS = {
    "killzone": "Kill Zone: sweep ve MSS pencere içinde (lunch hariç)",
    "liquidity_sweep": "Liquidity Sweep (NY 00:00 → seans açılışı aralığı)",
    "mss_displacement": "MSS + Displacement (5dk, bias yönünde)",
    "fvg_entry": "PD Array: fiyat FVG'ye geri döndü",
    "ote_entry": "OTE: fiyat 0.618-0.786 bölgesinde",
}


# ---------------- Zaman yardımcıları ----------------
def to_ny(ms):
    """Epoch (ms) -> New York yerel saati. Yaz/kış saati (EST/EDT) otomatik."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(NY_TZ)


def ny_hour(ms):
    dt = to_ny(ms)
    return dt.hour + dt.minute / 60


def in_killzone(ms):
    """Doküman: London 03:00, NY 08:00 açılır; kill zone pencereleri bunları
    kapsar. NY lunch (12:00-14:00) ranging olduğu için hariç tutulur."""
    h = ny_hour(ms)
    if NY_LUNCH[0] <= h < NY_LUNCH[1]:
        return False
    return (LONDON_KZ_NY[0] <= h < LONDON_KZ_NY[1]) or (NY_KZ_NY[0] <= h < NY_KZ_NY[1])


def compute_daily_bias(daily_candles):
    """Günlük grafikteki en son yapı kırılımının yönü = ICT Daily Bias."""
    results = compute_structure(daily_candles, PIVOT_LEN_HTF)
    last_bull = last_bear = None
    for i, r in enumerate(results):
        if r["bull_break"]:
            last_bull = i
        if r["bear_break"]:
            last_bear = i
    if last_bull is None and last_bear is None:
        return None
    if last_bull is None:
        return "short"
    if last_bear is None:
        return "long"
    return "long" if last_bull > last_bear else "short"


# ---------------- Model adımları ----------------
def get_session_range(candles, ny_date, start_h, end_h):
    """NY gece yarısından seans açılışına kadar olan aralığın high/low'u."""
    window = [
        c for c in candles
        if to_ny(c["open_time"]).date() == ny_date
        and start_h <= ny_hour(c["open_time"]) < end_h
    ]
    if not window:
        return None, None
    return max(c["high"] for c in window), min(c["low"] for c in window)


def find_sweep(candles, ny_date, session_open_h, session_end_h, range_high, range_low, bias):
    """Seans açıldıktan sonra aralığın likiditesini süpüren ilk mumu bulur.
    Arama SADECE o seansın penceresinde yapılır — aksi halde London kurulumu,
    NY seansındaki bir süpürmeyi kendi süpürmesi sanar.
    Bias'ın TERSİ yönde süpürme aranır (bullish bias -> range LOW süpürülür)."""
    if range_high is None:
        return None
    for i, c in enumerate(candles):
        if to_ny(c["open_time"]).date() != ny_date:
            continue
        h = ny_hour(c["open_time"])
        if not (session_open_h <= h < session_end_h):
            continue
        if bias == "long" and c["low"] < range_low:
            return i
        if bias == "short" and c["high"] > range_high:
            return i
    return None


def find_mss(candles, sweep_idx, bias, max_bars=MSS_SEARCH_BARS):
    """Süpürmeden sonra bias yönünde Market Structure Shift + Displacement.

    ICT sırası: (1) süpürme ekstremi oluşur, (2) fiyat tepki verip karşı yönde
    kısa vadeli bir swing bırakır, (3) bu swing displacement'lı bir mumla
    kırılır = MSS. Referans, süpürme ÖNCESİNDEKİ değil SONRASINDAKİ swing'dir;
    aksi halde yapı kırılımı yerine tam dönüş şartı aranmış olur.

    (mss_idx, extreme_idx) döner; bulunamazsa (None, extreme_idx)."""
    end = min(len(candles), sweep_idx + 1 + max_bars)
    seg = range(sweep_idx, end)
    if bias == "long":
        ext_idx = min(seg, key=lambda k: candles[k]["low"])
    else:
        ext_idx = max(seg, key=lambda k: candles[k]["high"])

    L = MSS_PIVOT_LEN
    ref = None
    for j in range(ext_idx + 1, end):
        c = candles[j]

        # 1) Kurulmuş bir swing varsa, displacement'lı kırılım MSS'tir.
        #    Kontrol, swing güncellemesinden ÖNCE yapılır; aksi halde kıran mum
        #    swing'i geçersiz kılıp kendi kırılımını gizler.
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

        # 2) ICT kısa vadeli swing: komşularından daha uçta olan mum.
        #    Sadece j'ye kadarki barlara bakılır (ileriye bakış yok).
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
    """Displacement mumunun bıraktığı Fair Value Gap (PD Array).
    (alt, üst) döner; yoksa None."""
    if mss_idx < 1 or mss_idx + 1 >= len(candles):
        return None
    before, after = candles[mss_idx - 1], candles[mss_idx + 1]
    if bias == "long" and after["low"] > before["high"]:
        return before["high"], after["low"]
    if bias == "short" and after["high"] < before["low"]:
        return after["high"], before["low"]
    return None


def ote_zone(sweep_extreme, mss_extreme, bias):
    """Süpürme ekstreminden displacement ekstremine çizilen bacağın
    0.618-0.786 Optimal Trade Entry bölgesi."""
    rng = abs(mss_extreme - sweep_extreme)
    if rng <= 0:
        return None
    if bias == "long":
        return mss_extreme - 0.786 * rng, mss_extreme - 0.618 * rng
    return mss_extreme + 0.618 * rng, mss_extreme + 0.786 * rng


def pick_targets(entry, direction, range_high, range_low, daily_candles):
    """Dokümandaki likidite tipleri hedef olarak kullanılır:
    aralığın karşı tarafı (birincil hedef), önceki gün ve önceki hafta
    high/low'u. Girişe çok yakın olanlar elenir, mesafeye göre sıralanır."""
    levels = [range_high if direction == "long" else range_low]

    if len(daily_candles) >= 2:
        prev_day = daily_candles[-2]
        levels.append(prev_day["high"] if direction == "long" else prev_day["low"])
    if len(daily_candles) >= 8:
        prev_week = daily_candles[-8:-1]
        levels.append(max(c["high"] for c in prev_week) if direction == "long"
                      else min(c["low"] for c in prev_week))

    min_dist = entry * MIN_TP_DISTANCE_PCT / 100
    if direction == "long":
        valid = sorted({lv for lv in levels if lv is not None and lv > entry + min_dist})
    else:
        valid = sorted({lv for lv in levels if lv is not None and lv < entry - min_dist}, reverse=True)

    valid = list(valid)[:3]
    while len(valid) < 3:
        valid.append(None)
    return valid


def analyze_session(m5, daily_candles, bias, ny_date, session):
    """Tek bir seans için (london / ny) modelin tüm adımlarını uygular."""
    if session == "london":
        # Aralık: NY 00:00 -> London açılışı. Süpürme London seansında aranır
        # (NY açılışına kadar), böylece NY kurulumuyla karışmaz.
        range_end_h, session_open_h, session_end_h = LONDON_OPEN_H, LONDON_OPEN_H, NY_OPEN_H
    else:
        # Aralık: NY 00:00 -> NY açılışı. Süpürme NY seansında, lunch'a kadar.
        range_end_h, session_open_h, session_end_h = NY_OPEN_H, NY_OPEN_H, NY_LUNCH[0]

    range_high, range_low = get_session_range(m5, ny_date, MIDNIGHT_OPEN_H, range_end_h)
    if range_high is None:
        return None

    sweep_idx = find_sweep(m5, ny_date, session_open_h, session_end_h,
                           range_high, range_low, bias)
    if sweep_idx is None:
        return None

    mss_idx, ext_idx = find_mss(m5, sweep_idx, bias)

    sweep_candle = m5[sweep_idx]
    # SL referansı süpürmenin gerçek ekstremidir (süpürme birkaç muma yayılabilir)
    sweep_extreme = m5[ext_idx]["low"] if bias == "long" else m5[ext_idx]["high"]
    current = m5[-1]["close"]

    # PD Array: fiyatın geri döneceği bölge. Doküman buraya BEKLEYEN (limit)
    # emir koyar — giriş fiyatı o anki fiyat değil, PD array seviyesidir.
    # Bölge, fiyat içinden tamamen geçip geçersiz kılınmadıysa hâlâ kullanılır.
    fvg = find_fvg(m5, mss_idx, bias) if mss_idx is not None else None
    fvg_entry = bool(fvg and (current >= fvg[0] if bias == "long" else current <= fvg[1]))

    # OTE bacağı: dokümandaki "London low'dan NY geri çekilmesi öncesindeki
    # high'a" tarifi — bacak MSS mumunda bitmez, teslimatın ulaştığı en uç
    # noktaya kadar uzar. NY seansındaki geri çekilme bu bölgeye denk gelirse
    # dokümandaki "senaryo I" (devam işlemi) tetiklenmiş olur.
    ote = None
    leg_extreme = None
    if mss_idx is not None:
        leg = [c for c in m5[mss_idx:] if to_ny(c["open_time"]).date() == ny_date]
        if leg:
            leg_extreme = (max(c["high"] for c in leg) if bias == "long"
                           else min(c["low"] for c in leg))
            ote = ote_zone(sweep_extreme, leg_extreme, bias)
    ote_entry = bool(ote and (current >= ote[0] if bias == "long" else current <= ote[1]))

    # Giriş = PD array seviyesi (bekleyen emir). FVG varsa onun ortası,
    # yoksa OTE bölgesinin ortası (~0.70 seviyesi) kullanılır.
    if fvg_entry:
        entry = (fvg[0] + fvg[1]) / 2
        entry_kind = "FVG"
    elif ote_entry:
        entry = (ote[0] + ote[1]) / 2
        entry_kind = "OTE"
    else:
        entry = None
        entry_kind = None

    killzone_ok = (
        mss_idx is not None
        and in_killzone(sweep_candle["open_time"])
        and in_killzone(m5[mss_idx]["open_time"])
    )

    criteria = {
        "killzone": killzone_ok,
        "liquidity_sweep": True,  # buraya gelindiyse süpürme bulunmuş demektir
        "mss_displacement": mss_idx is not None,
        "fvg_entry": fvg_entry,
        "ote_entry": ote_entry,
    }

    # SL: dokümana göre süpürülen ekstremin ötesine (küçük ATR tamponuyla)
    atr = compute_atr(m5, 14)
    sl = sweep_extreme - atr * SL_ATR_MULT if bias == "long" else sweep_extreme + atr * SL_ATR_MULT

    # Hedefler ve R:R, o anki fiyata göre değil GİRİŞ (bekleyen emir)
    # seviyesine göre hesaplanır — dokümandaki 1:3 matematiği buna dayanır.
    ref = entry if entry is not None else current
    tp1, tp2, tp3 = pick_targets(ref, bias, range_high, range_low, daily_candles)
    risk = abs(ref - sl)
    rrs = [abs(tp - ref) / risk if (tp is not None and risk > 0) else None
           for tp in (tp1, tp2, tp3)]

    # Dokümandaki NY "senaryo I": London zaten süpürüp teslimatı yaptıysa ve
    # geri çekilme NY seansında OTE'ye denk geliyorsa, bu bir devam işlemidir.
    continuation = (
        session == "london" and ote_entry
        and NY_OPEN_H <= ny_hour(m5[-1]["open_time"]) < NY_LUNCH[0]
    )

    return {
        "session": session,
        "continuation": continuation,
        "leg_extreme": leg_extreme,
        "ny_date": str(ny_date),
        "direction": bias,
        "criteria": criteria,
        "score": sum(criteria.values()),
        "price": current,          # o anki fiyat (bilgi amaçlı)
        "entry": entry,            # bekleyen emir seviyesi (PD array)
        "entry_kind": entry_kind,
        "sl": sl,
        "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "rr1": rrs[0], "rr2": rrs[1], "rr3": rrs[2],
        "range_high": range_high,
        "range_low": range_low,
        "sweep_time": sweep_candle["open_time"],
        "mss_time": m5[mss_idx]["open_time"] if mss_idx is not None else None,
        "fvg": fvg,
        "ote": ote,
    }


def evaluate_symbol_ict(symbol):
    daily_candles = fetch_klines(symbol, HTF_INTERVAL, HTF_LIMIT)
    time.sleep(REQUEST_SLEEP)
    m5 = fetch_klines(symbol, ENTRY_INTERVAL, ENTRY_LIMIT)
    time.sleep(REQUEST_SLEEP)

    if len(daily_candles) < PIVOT_LEN_HTF * 2 + 5 or len(m5) < 100:
        return None

    bias = compute_daily_bias(daily_candles)
    if bias is None:
        return None  # net bir daily bias yoksa işlem aranmaz

    latest_date = to_ny(m5[-1]["open_time"]).date()
    now_ms = m5[-1]["close_time"]

    best = None
    for day_offset in range(SESSION_DAYS_BACK):
        d = latest_date - timedelta(days=day_offset)
        for session in ("ny", "london"):
            res = analyze_session(m5, daily_candles, bias, d, session)
            if res is None or res["mss_time"] is None:
                continue
            age_h = (now_ms - res["mss_time"]) / 3600000
            if age_h > SETUP_MAX_AGE_HOURS:
                continue
            if best is None or res["mss_time"] > best["mss_time"]:
                best = res
        if best:
            break

    if best is None:
        return None

    core_ok = all(best["criteria"][k] for k in CORE_CRITERIA)
    confirms = sum(best["criteria"][k] for k in CONFIRM_CRITERIA)
    checklist_ok = core_ok and confirms >= MIN_CONFIRMATIONS

    # Zorunlu şartlar: geometri + doküman hedefi olan 1:3 R:R
    best["qualifies"] = checklist_ok and best["entry"] is not None and is_valid_setup(
        best["entry"], best["sl"], best["tp1"], best["direction"], ICT_MIN_TP1_RR
    )
    return best


# ---------------- Mesaj ----------------
def _fmt_tp(tp, rr):
    if tp is None:
        return "n/a"
    rr_text = f"{rr:.2f}" if rr else "n/a"
    return f"{tp:.6g}  (R:R ≈ {rr_text})"


def format_ict_message(symbol, r):
    direction = r["direction"]
    emoji = "🟢" if direction == "long" else "🔴"
    if r.get("continuation"):
        session_name = "London kurulumu → NY devam işlemi (senaryo I)"
    else:
        session_name = "London" if r["session"] == "london" else "New York"
    range_label = "00:00-03:00" if r["session"] == "london" else "00:00-08:00"

    lines = [
        f"🧭 <b>ICT 2022 Modeli</b> — {emoji} {symbol} {direction.upper()} "
        f"(Skor: {r['score']}/5)",
        f"Seans: {session_name} | Daily bias: {direction.upper()}",
        f"Aralık ({range_label} NY): {r['range_low']:.6g} - {r['range_high']:.6g}",
        "",
        f"Giriş (bekleyen emir, {r['entry_kind']}): {r['entry']:.6g}",
        f"Güncel fiyat: {r['price']:.6g}",
        f"SL: {r['sl']:.6g}  (süpürülen ekstremin ötesi)",
        f"TP1: {_fmt_tp(r['tp1'], r['rr1'])}",
        f"TP2: {_fmt_tp(r['tp2'], r['rr2'])}",
        f"TP3: {_fmt_tp(r['tp3'], r['rr3'])}",
        "",
        "<b>Çekirdek (hepsi sağlanmalı):</b>",
    ]
    for key in CORE_CRITERIA:
        lines.append(f"{'✅' if r['criteria'][key] else '❌'} {CRITERIA_LABELS[key]}")
    lines.append("")
    lines.append("<b>Giriş tetiği (en az 1 sağlanmalı):</b>")
    for key in CONFIRM_CRITERIA:
        lines.append(f"{'✅' if r['criteria'][key] else '❌'} {CRITERIA_LABELS[key]}")
    lines.append("")
    lines.append(f"Likidite avı: {to_ny(r['sweep_time']).strftime('%Y-%m-%d %H:%M')} NY")
    if r["mss_time"]:
        lines.append(f"MSS/Displacement: {to_ny(r['mss_time']).strftime('%Y-%m-%d %H:%M')} NY")
    lines.append("")
    lines.append(
        "⚠️ ICT 2022 modeli — scanner.py'daki Order Block stratejisinden bağımsızdır. "
        "Yatırım tavsiyesi değildir."
    )
    return "\n".join(lines)


# ---------------- Durum ve ana akış ----------------
def load_state():
    if os.path.exists(ICT_STATE_FILE):
        with open(ICT_STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(ICT_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def main():
    state = load_state()
    symbols = get_usdt_symbols()
    print(f"[ICT2022] {len(symbols)} sembol taranacak.")

    sent = 0
    for i, symbol in enumerate(symbols):
        try:
            result = evaluate_symbol_ict(symbol)
        except Exception as e:
            print(f"[ICT2022][{symbol}] hata: {e}")
            continue

        if result is None or not result["qualifies"]:
            continue

        sym_state = state.get(symbol, {})
        direction = result["direction"]
        if sym_state.get(direction) == result["mss_time"]:
            continue  # bu MSS için zaten uyarı gönderildi

        print(f"[ICT2022][{symbol}] {direction} {result['session']} "
              f"skor={result['score']}/5 gönderiliyor")
        send_telegram(format_ict_message(symbol, result))
        sym_state[direction] = result["mss_time"]
        state[symbol] = sym_state
        sent += 1

        if (i + 1) % 50 == 0:
            print(f"[ICT2022] {i + 1}/{len(symbols)} tarandı...")

    save_state(state)
    print(f"[ICT2022] Tarama bitti. {sent} yeni sinyal gönderildi.")


if __name__ == "__main__":
    main()
