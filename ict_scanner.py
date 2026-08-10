"""
ICT Checklist tarayıcısı — mevcut HTF/LTF Order Block stratejisinden (scanner.py)
BAĞIMSIZ, ikinci bir strateji. Aynı sembolleri tarar ama farklı bir metodoloji
kullanır: "Altın Kural" — 3 çekirdek kriterin HEPSİ + 2 onay kriterinden EN AZ
1'i sağlanırsa (toplam skor >= 4/5) Telegram'a ayrı, kendi formatında bir
uyarı gönderir.

Yapı kırılımı 4H'de tespit edilir; ancak likidite avı, displacement ve giriş
tetiği 4H çözünürlükte ölçülemeyeceği için 15 DAKİKALIK veride, gerçek
saatleriyle aranır. Kill zone saatleri New York yerel saatiyle tanımlıdır,
böylece yaz/kış saati (EST/EDT) geçişinde pencere kaymaz.

Çekirdek (hepsi gerekli):
1. Kill Zone Zamanı   — likidite avı VE displacement, London (02-05 NY) veya
                        New York (07-10 NY) kill zone penceresi İÇİNDE mi
                        gerçekleşti? (Mumun ne zaman açıldığı değil, hareketin
                        ne zaman olduğu önemlidir.)
2. Liquidity Sweep    — Asya seansı (20:00-00:00 NY) tepe/dibi süpürülüp
                        geri dönüldü mü? (Judas swing)
3. MSS / Displacement — sweep'ten SONRA, gövdesi ortalamanın belirgin üstünde
                        yönlü bir kırılım mumu oluştu mu?

Onaylar (en az 1 gerekli):
4. HTF Bias Uyumu     — 4H'deki son yapı kırılımı, günlük trend yönüyle aynı mı?
5. FVG / OTE Teması   — fiyat displacement bacağının FVG'sine veya
                        0.618-0.786 (OTE) bölgesine geri çekildi mi?

Bu basitleştirilmiş, otomatikleştirilebilir yaklaşımlardır — ICT'nin tam
metodolojisinin birebir yerine geçmez, yatırım tavsiyesi değildir.
"""

import json
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from scanner import (
    get_usdt_symbols,
    fetch_klines,
    compute_structure,
    compute_atr,
    collect_pivot_levels,
    pick_tp_levels,
    is_valid_setup,
    send_telegram,
    MIN_TP1_RR,
    PIVOT_LEN_HTF,
    PIVOT_LEN_LTF,
    CONFIRM_WINDOW,
    HTF_INTERVAL,
    LTF_INTERVAL,
    HTF_LIMIT,
    LTF_LIMIT,
    LIQUIDITY_LOOKBACK,
    SL_ATR_MULT,
    REQUEST_SLEEP,
)

ICT_STATE_FILE = os.path.join(os.path.dirname(__file__), "ict_state.json")

# Altın Kural: 3 çekirdek kriterin HEPSİ sağlanmalı, buna ek olarak 2 onay
# kriterinden EN AZ 1'i sağlanmalı (toplam skor en az 4/5).
CORE_CRITERIA = ["killzone", "liquidity_sweep", "mss_displacement"]
CONFIRM_CRITERIA = ["htf_bias", "fvg_ote"]
MIN_CONFIRMATIONS = 1

# Kill zone'lar New York yerel saatiyle tanımlıdır (ICT standardı) — böylece
# yaz/kış saati (EST/EDT) geçişinde pencereler UTC'de kaymaz.
NY_TZ = ZoneInfo("America/New_York")
LONDON_KZ_NY = (2, 5)       # 02:00-05:00 NY saati
NY_KZ_NY = (7, 10)          # 07:00-10:00 NY saati
ASIAN_SESSION_NY = (20, 24)  # 20:00-00:00 NY saati — avlanacak likiditeyi tanımlar

# Likidite avı / displacement / giriş tetiği 4H çözünürlükte ölçülemez;
# bunlar 15 dakikalık veride, gerçek saatleriyle aranır.
LTF_KZ_INTERVAL = "15m"
LTF_KZ_LIMIT = 500           # ~5 gün
KZ_LOOKBACK_CANDLES = 192    # son 48 saat (15dk x 192)

DISPLACEMENT_BODY_MULT = 1.5     # displacement mumunun gövdesi, ortalamanın kaç katı olmalı

CRITERIA_LABELS = {
    "htf_bias": "HTF Bias Uyumu (günlük trendle aynı yön)",
    "killzone": "Kill Zone: sweep VE displacement KZ içinde (15dk)",
    "liquidity_sweep": "Liquidity Sweep (Asya seansı likiditesi, 15dk)",
    "mss_displacement": "MSS / Displacement (sweep sonrası güçlü mum, 15dk)",
    "fvg_ote": "FVG / OTE Teması (displacement bacağı, 15dk)",
}


def find_last_break(ltf_results, confirm_window):
    """Son confirm_window bar içinde en son oluşan yapı kırılımını (yön, index) döndürür."""
    n = len(ltf_results)
    for offset in range(0, confirm_window + 1):
        idx = n - 1 - offset
        if idx < 0:
            break
        if ltf_results[idx]["bull_break"]:
            return "long", idx
        if ltf_results[idx]["bear_break"]:
            return "short", idx
    return None, None


def compute_htf_bias(htf_candles):
    """Günlük grafikteki en son yapı kırılımının yönünü (long/short) döndürür."""
    results = compute_structure(htf_candles, PIVOT_LEN_HTF)
    last_bull_idx = last_bear_idx = None
    for i, r in enumerate(results):
        if r["bull_break"]:
            last_bull_idx = i
        if r["bear_break"]:
            last_bear_idx = i
    if last_bull_idx is None and last_bear_idx is None:
        return None
    if last_bull_idx is None:
        return "short"
    if last_bear_idx is None:
        return "long"
    return "long" if last_bull_idx > last_bear_idx else "short"


def to_ny(ms):
    """Epoch (ms) -> New York yerel saati. Yaz/kış saati (EST/EDT) otomatik."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(NY_TZ)


def in_killzone(ms):
    """Verilen ANIN London veya NY kill zone penceresinde olup olmadığı.
    Saatler New York yerel saatiyle tanımlıdır (ICT standardı), böylece
    yaz/kış saati geçişinde pencere kaymaz."""
    dt = to_ny(ms)
    h = dt.hour + dt.minute / 60
    return (LONDON_KZ_NY[0] <= h < LONDON_KZ_NY[1]) or (NY_KZ_NY[0] <= h < NY_KZ_NY[1])


def get_ny_midnight_open(m15_candles):
    """En son NY gece yarısı (00:00 NY) mumunun AÇILIŞ fiyatı.
    ICT'de günün premium/discount referansı budur: fiyat bu seviyenin altındaysa
    'ucuz' (long aranır), üstündeyse 'pahalı' (short aranır)."""
    best = None
    for c in m15_candles:
        dt = to_ny(c["open_time"])
        if dt.hour == 0 and dt.minute == 0:
            if best is None or c["open_time"] > best["open_time"]:
                best = c
    return best["open"] if best else None


def get_asian_range(m15_candles):
    """En son tamamlanmış Asya seansının (NY saatiyle 20:00-24:00) tepe/dibi.
    Bu seans, London kill zone'unda avlanacak likiditeyi tanımlar."""
    sessions = {}
    for c in m15_candles:
        dt = to_ny(c["open_time"])
        if ASIAN_SESSION_NY[0] <= dt.hour < ASIAN_SESSION_NY[1]:
            sessions.setdefault(dt.date(), []).append(c)
    if not sessions:
        return None, None
    latest_day = max(sessions)
    session = sessions[latest_day]
    return max(c["high"] for c in session), min(c["low"] for c in session)


def find_kz_setup(m15_candles, direction, asian_high, asian_low):
    """ICT akışını 15 dakikalık veride arar:
      1) Likidite avı (Judas swing): Asya tepesi/dibi süpürülüp geri dönülmesi
      2) Ardından displacement: yönlü, gövdesi belirgin büyük kırılım mumu
    (sweep_candle, displacement_candle) döner; bulunamayan None olur.
    Saat kontrolü yapılmaz — çağıran taraf in_killzone ile denetler."""
    if asian_high is None or asian_low is None:
        return None, None

    recent = m15_candles[-KZ_LOOKBACK_CANDLES:]
    if len(recent) < 25:
        return None, None

    sweep_idx = None
    for i in range(len(recent) - 1, -1, -1):
        c = recent[i]
        if direction == "long" and c["low"] < asian_low and c["close"] > asian_low:
            sweep_idx = i
            break
        if direction == "short" and c["high"] > asian_high and c["close"] < asian_high:
            sweep_idx = i
            break
    if sweep_idx is None:
        return None, None

    for j in range(sweep_idx + 1, len(recent)):
        c = recent[j]
        prior = recent[max(0, j - 20):j]
        bodies = [abs(p["close"] - p["open"]) for p in prior]
        avg_body = sum(bodies) / len(bodies) if bodies else 0
        body = abs(c["close"] - c["open"])
        directional = (c["close"] > c["open"]) if direction == "long" else (c["close"] < c["open"])
        if directional and avg_body > 0 and body >= DISPLACEMENT_BODY_MULT * avg_body:
            return recent[sweep_idx], c

    return recent[sweep_idx], None


def compute_fvg_or_ote_ltf(m15_candles, direction, sweep_candle, disp_candle):
    """Displacement bacağının FVG'si veya OTE (0.618-0.786) bölgesine
    geri çekilme olmuş mu? ICT'de giriş tetiği burada aranır."""
    if disp_candle is None or sweep_candle is None:
        return False
    current = m15_candles[-1]["close"]

    idx = next((i for i, c in enumerate(m15_candles) if c["open_time"] == disp_candle["open_time"]), None)
    if idx is not None and 1 <= idx < len(m15_candles) - 1:
        before, after = m15_candles[idx - 1], m15_candles[idx + 1]
        if direction == "long" and after["low"] > before["high"]:
            if before["high"] <= current <= after["low"]:
                return True
        if direction == "short" and after["high"] < before["low"]:
            if after["high"] <= current <= before["low"]:
                return True

    if direction == "long":
        leg_low, leg_high = sweep_candle["low"], disp_candle["high"]
    else:
        leg_high, leg_low = sweep_candle["high"], disp_candle["low"]
    rng = leg_high - leg_low
    if rng <= 0:
        return False
    if direction == "long":
        return leg_high - 0.786 * rng <= current <= leg_high - 0.618 * rng
    return leg_low + 0.618 * rng <= current <= leg_low + 0.786 * rng


def compute_fvg_or_ote(ltf_candles, direction):
    if len(ltf_candles) >= 3:
        bull_fvg = ltf_candles[-1]["low"] > ltf_candles[-3]["high"]
        bear_fvg = ltf_candles[-1]["high"] < ltf_candles[-3]["low"]
        if direction == "long" and bull_fvg:
            return True
        if direction == "short" and bear_fvg:
            return True

    lookback = ltf_candles[-OTE_SWING_LOOKBACK:]
    if not lookback:
        return False
    swing_low = min(c["low"] for c in lookback)
    swing_high = max(c["high"] for c in lookback)
    rng = swing_high - swing_low
    if rng <= 0:
        return False
    current = ltf_candles[-1]["close"]
    if direction == "long":
        ote_top = swing_high - 0.618 * rng
        ote_bot = swing_high - 0.786 * rng
        return ote_bot <= current <= ote_top
    else:
        ote_bot = swing_low + 0.618 * rng
        ote_top = swing_low + 0.786 * rng
        return ote_bot <= current <= ote_top


def evaluate_symbol_ict(symbol):
    htf_candles = fetch_klines(symbol, HTF_INTERVAL, HTF_LIMIT)
    time.sleep(REQUEST_SLEEP)
    ltf_candles = fetch_klines(symbol, LTF_INTERVAL, LTF_LIMIT)
    time.sleep(REQUEST_SLEEP)

    if len(htf_candles) < PIVOT_LEN_HTF * 2 + 5 or len(ltf_candles) < PIVOT_LEN_LTF * 2 + 5:
        return None

    ltf_results = compute_structure(ltf_candles, PIVOT_LEN_LTF)
    direction, break_idx = find_last_break(ltf_results, CONFIRM_WINDOW)
    if direction is None:
        return None  # skorlanacak bir yapı kırılımı yok

    # Likidite avı, displacement ve giriş tetiği 4H'de ölçülemez —
    # gerçek saatleriyle 15 dakikalık veride aranır.
    m15_candles = fetch_klines(symbol, LTF_KZ_INTERVAL, LTF_KZ_LIMIT)
    time.sleep(REQUEST_SLEEP)

    asian_high, asian_low = get_asian_range(m15_candles)
    sweep_candle, disp_candle = find_kz_setup(m15_candles, direction, asian_high, asian_low)

    # NY Midnight Open (00:00 NY) premium/discount filtresi:
    # long sadece açılışın ALTINDA (discount), short sadece ÜSTÜNDE (premium).
    midnight_open = get_ny_midnight_open(m15_candles)
    current_price = m15_candles[-1]["close"] if m15_candles else None
    if midnight_open is None or current_price is None:
        midnight_ok = False
    elif direction == "long":
        midnight_ok = current_price < midnight_open
    else:
        midnight_ok = current_price > midnight_open

    # Kill zone kriteri: mumun ne zaman AÇILDIĞI değil, likidite avının ve
    # displacement'ın KZ penceresi içinde GERÇEKLEŞMİŞ olması aranır.
    killzone_ok = (
        sweep_candle is not None
        and disp_candle is not None
        and in_killzone(sweep_candle["open_time"])
        and in_killzone(disp_candle["open_time"])
    )

    htf_bias = compute_htf_bias(htf_candles)
    break_candle = ltf_candles[break_idx]
    break_result = ltf_results[break_idx]

    criteria = {
        "htf_bias": htf_bias == direction if htf_bias else False,
        "killzone": killzone_ok,
        "liquidity_sweep": sweep_candle is not None,
        "mss_displacement": disp_candle is not None,
        "fvg_ote": compute_fvg_or_ote_ltf(m15_candles, direction, sweep_candle, disp_candle),
    }
    score = sum(criteria.values())
    core_ok = all(criteria[k] for k in CORE_CRITERIA)
    confirm_count = sum(criteria[k] for k in CONFIRM_CRITERIA)
    checklist_ok = core_ok and confirm_count >= MIN_CONFIRMATIONS

    # SL/TP hesaplaması: scanner.py'daki ana stratejiyle aynı yöntem —
    # kırılımın oluşturduğu order block'un ötesine SL, geçmiş likidite
    # (pivot tepe/dip) seviyelerine TP1/TP2/TP3.
    close = ltf_candles[-1]["close"]
    atr = compute_atr(ltf_candles, 14)
    ltf_highs = collect_pivot_levels(ltf_candles[-(LIQUIDITY_LOOKBACK + PIVOT_LEN_LTF):], PIVOT_LEN_LTF, "high")
    ltf_lows = collect_pivot_levels(ltf_candles[-(LIQUIDITY_LOOKBACK + PIVOT_LEN_LTF):], PIVOT_LEN_LTF, "low")
    htf_highs = collect_pivot_levels(htf_candles, PIVOT_LEN_HTF, "high")
    htf_lows = collect_pivot_levels(htf_candles, PIVOT_LEN_HTF, "low")

    if direction == "long":
        sl = (break_result["ob_bot"] if break_result["ob_dir"] == 1 and break_result["ob_bot"] is not None
              else break_candle["low"]) - atr * SL_ATR_MULT
        tp1, tp2, tp3 = pick_tp_levels(close, ltf_highs, htf_highs, "long")
        rrs = [(tp - close) / (close - sl) if (tp is not None and close > sl) else None for tp in (tp1, tp2, tp3)]
    else:
        sl = (break_result["ob_top"] if break_result["ob_dir"] == -1 and break_result["ob_top"] is not None
              else break_candle["high"]) + atr * SL_ATR_MULT
        tp1, tp2, tp3 = pick_tp_levels(close, ltf_lows, htf_lows, "short")
        rrs = [(close - tp) / (sl - close) if (tp is not None and sl > close) else None for tp in (tp1, tp2, tp3)]

    # Checklist geçse bile şu zorunlu şartlar sağlanmadıkça sinyal gönderilmez:
    #  - geometri tutarlı ve TP1 R:R >= MIN_TP1_RR (is_valid_setup)
    #  - NY Midnight Open premium/discount yönü uyuyor
    qualifies = checklist_ok and midnight_ok and is_valid_setup(close, sl, tp1, direction)

    return {
        "direction": direction,
        "score": score,
        "criteria": criteria,
        "qualifies": qualifies,
        "price": close,
        "sl": sl,
        "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "rr1": rrs[0], "rr2": rrs[1], "rr3": rrs[2],
        "break_close_time": break_candle["close_time"],
        "sweep_time": sweep_candle["open_time"] if sweep_candle else None,
        "disp_time": disp_candle["open_time"] if disp_candle else None,
        "midnight_open": midnight_open,
        "midnight_ok": midnight_ok,
    }


def _fmt_tp(tp, rr):
    if tp is None:
        return "n/a (yeterli likidite seviyesi bulunamadı)"
    rr_text = f"{rr:.2f}" if rr else "n/a"
    return f"{tp:.6g}  (R:R ≈ {rr_text})"


def format_ict_message(symbol, result):
    direction = result["direction"]
    emoji = "🟢" if direction == "long" else "🔴"
    lines = [
        f"🧭 <b>ICT Checklist Sinyali</b> — {emoji} {symbol} {direction.upper()} "
        f"(Skor: {result['score']}/5)",
        f"Giriş: {result['price']:.6g}",
        f"SL: {result['sl']:.6g}",
        f"TP1: {_fmt_tp(result['tp1'], result['rr1'])}",
        f"TP2: {_fmt_tp(result['tp2'], result['rr2'])}",
        f"TP3: {_fmt_tp(result['tp3'], result['rr3'])}",
        "",
        "<b>Çekirdek (hepsi sağlanmalı):</b>",
    ]
    for key in CORE_CRITERIA:
        mark = "✅" if result["criteria"][key] else "❌"
        lines.append(f"{mark} {CRITERIA_LABELS[key]}")
    lines.append("")
    lines.append("<b>Onaylar (en az 1 sağlanmalı):</b>")
    for key in CONFIRM_CRITERIA:
        mark = "✅" if result["criteria"][key] else "❌"
        lines.append(f"{mark} {CRITERIA_LABELS[key]}")
    lines.append("")
    if result.get("midnight_open"):
        bolge = "Discount (ucuz)" if direction == "long" else "Premium (pahalı)"
        lines.append(f"✅ NY Midnight Open: {result['midnight_open']:.6g} — fiyat {bolge} bölgede")
    if result.get("rr1"):
        lines.append(f"✅ TP1 R:R {result['rr1']:.2f} (asgari {MIN_TP1_RR} şartı sağlandı)")
    lines.append("")
    if result.get("sweep_time"):
        lines.append(f"Likidite avı: {to_ny(result['sweep_time']).strftime('%Y-%m-%d %H:%M')} NY")
    if result.get("disp_time"):
        lines.append(f"Displacement: {to_ny(result['disp_time']).strftime('%Y-%m-%d %H:%M')} NY")
    lines.append("")
    lines.append(
        "⚠️ Bu, diğer HTF/LTF Order Block stratejisinden BAĞIMSIZ ayrı bir "
        "yöntemdir (ICT checklist tabanlı). Yatırım tavsiyesi değildir."
    )
    return "\n".join(lines)


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
    print(f"[ICT] {len(symbols)} sembol taranacak.")

    sent = 0
    for i, symbol in enumerate(symbols):
        try:
            result = evaluate_symbol_ict(symbol)
        except Exception as e:
            print(f"[ICT][{symbol}] hata: {e}")
            continue

        if result is None or not result["qualifies"]:
            continue

        sym_state = state.get(symbol, {})
        direction = result["direction"]
        last_alerted = sym_state.get(direction)
        if last_alerted == result["break_close_time"]:
            continue  # bu kırılım için zaten uyarı gönderildi

        print(f"[ICT][{symbol}] {direction} skor={result['score']}/5 sinyali gönderiliyor")
        send_telegram(format_ict_message(symbol, result))
        sym_state[direction] = result["break_close_time"]
        state[symbol] = sym_state
        sent += 1

        if (i + 1) % 50 == 0:
            print(f"[ICT] {i + 1}/{len(symbols)} tarandı...")

    save_state(state)
    print(f"[ICT] Tarama bitti. {sent} yeni sinyal gönderildi.")


if __name__ == "__main__":
    main()
